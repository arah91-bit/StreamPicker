"""Search-only Stremio catalog backed by local interpretation and TMDB evidence."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import datetime
import json
import logging
from pathlib import Path
import re
import time
import uuid

import httpx

from app.recs import config, tmdb
from app.recs.search_intent import interpret
from app.recs.search_retrieval import Retriever
from app.recs.search_ranking import assess, eligible_candidates, constraint_reason

logger = logging.getLogger("nuvio-recs")
AI_SEARCH_ID = "nr-ai-search"
AI_SEARCH_NAME = "AI Search"
PIPELINE_VERSION = "v2"
_MAX_CACHE_ENTRIES = 200
_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
_active_tasks: dict[str, asyncio.Task] = {}
_client = httpx.AsyncClient(timeout=config.AI_SEARCH_TIMEOUT)

_SYSTEM_PROMPT = """Interpret a movie/TV search request. Return JSON only with fields: intent (title_lookup|remembered_title|browse|similar_titles), media (movie|tv|both), genres (array of genre slugs), topics (array of up to four short concepts), entities (array of title or character clues), search_queries (up to three title or alias searches), preference (recognition|quality|recency|obscurity|similarity). Preserve explicit user constraints. Do not invent titles. The catalog will verify every suggestion."""


def is_ai_search_catalog_id(catalog_id: str) -> bool:
    return catalog_id == AI_SEARCH_ID


def catalog_defs(user: dict) -> list[dict]:
    if not config.AI_SEARCH_ENABLED:
        return []
    return [{"id": AI_SEARCH_ID, "type": config.COMBINED_TYPE,
             "name": AI_SEARCH_NAME, "showInHome": False,
             "extra": [{"name": "search", "isRequired": True}]}]


def active_model() -> str:
    override_path = Path(config.DB_PATH).parent / "ai_search_model"
    try:
        override = override_path.read_text(encoding="utf-8").splitlines()[0].strip()
        if override:
            return override
    except (OSError, IndexError):
        pass
    return config.AI_SEARCH_MODEL


def clear_cache() -> None:
    _cache.clear()


def _normalize_query(query: str) -> str:
    return " ".join((query or "").split())[:500]


def _cache_key(query: str, kid_age: int | None) -> tuple:
    relative = bool(re.search(r"\b(today|yesterday|this year|last year|recent|new)\b", query, re.I))
    return (PIPELINE_VERSION, query.casefold(), kid_age, active_model(),
            datetime.date.today().isoformat() if relative else "")


async def _query_ollama(query: str, model: str, kid_age: int | None) -> dict:
    system = f"{_SYSTEM_PROMPT}\nToday's date is {datetime.date.today().isoformat()}."
    if kid_age is not None:
        system += f"\nViewer age is {kid_age}; final eligibility is checked separately."
    payload = {
        "model": model, "stream": False, "keep_alive": config.AI_SEARCH_KEEP_ALIVE,
        "format": "json", "options": {"temperature": 0, "num_predict": 400},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"Query: {query}"}],
    }
    if "qwen" in model.lower():
        payload["think"] = False
    resp = await _client.post(f"{config.AI_SEARCH_OLLAMA_URL}/api/chat", json=payload,
                              timeout=min(8.0, config.AI_SEARCH_TIMEOUT))
    resp.raise_for_status()
    raw = ((resp.json().get("message") or {}).get("content")) or ""
    if len(raw) > 12000:
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


async def warm_up() -> None:
    if not config.AI_SEARCH_ENABLED:
        return
    model = active_model()
    try:
        await _client.post(f"{config.AI_SEARCH_OLLAMA_URL}/api/chat",
                           json={"model": model, "keep_alive": config.AI_SEARCH_KEEP_ALIVE,
                                 "messages": []}, timeout=60.0)
        logger.info("ai_search: pre-warmed model %s", model)
    except Exception as exc:
        logger.debug("ai_search: warm-up skipped (%r)", exc)


async def _resolve_safe(media_type: str, tmdb_id: int, kid_age: int | None) -> dict | None:
    return await tmdb.resolve_meta(media_type, tmdb_id, max_age=kid_age,
                                   require_home_release=True)


def _needs_planner(query: str, initial) -> bool:
    if len(query) <= 3:
        return False
    descriptive = bool(re.search(r"\b(where|about|featuring|who|with)\b", query, re.I))
    if initial.kind == "browse":
        return not (initial.genres or initial.topics or initial.date or initial.setting_period)
    if initial.kind == "remembered_title":
        return descriptive and not (initial.topics or initial.date)
    return descriptive and initial.kind == "title_lookup"


async def _run_search(query: str, kid_age: int | None) -> list[dict]:
    model = active_model()
    trace = uuid.uuid4().hex[:8]
    began = time.monotonic()
    initial = interpret(query)
    raw = None
    if _needs_planner(query, initial):
        try:
            raw = await asyncio.wait_for(_query_ollama(query, model, kid_age),
                                         timeout=min(8.0, config.AI_SEARCH_TIMEOUT))
        except Exception:
            raw = None
    intent = interpret(query, raw)
    planned_at = time.monotonic()
    retrieval = Retriever(intent, deadline=began + config.AI_SEARCH_TIMEOUT - 8)
    candidates = await retrieval.retrieve()
    retrieved_at = time.monotonic()
    if not candidates:
        return []
    keyword_grounded = bool(intent.topics) and all(any(":keywords:" in p for p in c.provenance) for c in candidates)
    topic_needs_assessment = intent.topics and not keyword_grounded and not all(c.identity >= 4 for c in candidates)
    if intent.setting_period or topic_needs_assessment or (intent.kind != "browse" and len(candidates) > 1):
        await assess(query, intent, candidates, _client, model, config.AI_SEARCH_OLLAMA_URL,
                     config.AI_SEARCH_KEEP_ALIVE,
                     min(8.0, max(0.1, config.AI_SEARCH_TIMEOUT - (time.monotonic() - began))))
    ranked_at = time.monotonic()
    ordered = eligible_candidates(candidates, intent)
    recovery = "none"
    if intent.kind == "browse" and len(ordered) < 12 and retrieval.calls < 16:
        await retrieval.browse_more()
        recovery = "page3"
        candidates = list(retrieval.items.values())
        ordered = eligible_candidates(candidates, intent)
    metas: list[dict] = []
    target = min(config.AI_SEARCH_RESULTS, 4) if intent.kind in ("title_lookup", "remembered_title") else config.AI_SEARCH_RESULTS
    seen: set[str] = set()
    processed: set[str] = set()
    resolution_count = 0
    pending = ordered
    while pending and resolution_count < 24:
        if len(metas) >= target or time.monotonic() - began > config.AI_SEARCH_TIMEOUT - 2:
            break
        batch = pending[:min(6, 24 - resolution_count)]
        pending = pending[len(batch):]
        processed.update(c.ref for c in batch)
        remaining = max(0.1, config.AI_SEARCH_TIMEOUT - (time.monotonic() - began) - 1)
        resolved = await asyncio.gather(*(asyncio.wait_for(
            _resolve_safe(c.media, c.tmdb_id, kid_age), timeout=remaining) for c in batch),
                                        return_exceptions=True)
        resolution_count += len(batch)
        for candidate, meta in zip(batch, resolved):
            if not isinstance(meta, dict) or not meta.get("id") or meta["id"] in seen:
                continue
            if intent.date and intent.date.required and meta.get("releaseInfo"):
                year = str(meta["releaseInfo"])[:4]
                if not year.isdigit() or not (intent.date.start.year <= int(year) <= intent.date.end.year):
                    continue
            if constraint_reason(candidate, intent):
                continue
            seen.add(meta["id"])
            metas.append(meta)
            if len(metas) >= target:
                break
        if (intent.kind == "browse" and recovery == "none" and resolution_count >= 12
                and len(metas) < 12 and retrieval.calls < 16):
            await retrieval.browse_more()
            recovery = "page3"
            candidates = list(retrieval.items.values())
            ordered = eligible_candidates(candidates, intent)
            pending = [c for c in ordered if c.ref not in processed]
    logger.info("ai_search: trace=%s intent=%s model=%s media=%s date=%s genres=%s topics=%s candidates=%d accepted=%d rejected=%d tmdb_calls=%d tmdb_errors=%d recovery=%s resolved=%d elapsed=%.2fs",
                trace, intent.kind, model, intent.media, intent.date, intent.genres, intent.topics,
                len(candidates), len(metas), len(candidates)-len(ordered), retrieval.calls,
                retrieval.errors, recovery, resolution_count, time.monotonic()-began)
    logger.debug("ai_search: trace=%s plan=%.2fs retrieve=%.2fs rank=%.2fs resolve=%.2fs",
                 trace, planned_at-began, retrieved_at-planned_at,
                 ranked_at-retrieved_at, time.monotonic()-ranked_at)
    return metas


async def search(token: str, query: str, kid_age: int | None = None) -> list[dict]:
    if not config.AI_SEARCH_ENABLED:
        return []
    cleaned = _normalize_query(query)
    if len(cleaned) < 2:
        return []
    prev = _active_tasks.get(token)
    if prev is not None and not prev.done():
        prev.cancel()
    key = _cache_key(cleaned, kid_age)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None:
        ts, metas = cached
        if now - ts < config.AI_SEARCH_CACHE_TTL:
            _cache.move_to_end(key)
            logger.debug("ai_search: cache=hit model=%s age=%s count=%d", key[3], kid_age, len(metas))
            return metas
        _cache.pop(key, None)
    task = asyncio.current_task()
    if task is not None:
        _active_tasks[token] = task
    try:
        metas = await asyncio.wait_for(_run_search(cleaned, kid_age),
                                       timeout=config.AI_SEARCH_TIMEOUT)
    except asyncio.CancelledError:
        return []
    except Exception as exc:
        logger.warning("ai_search: failed for %r: %s", cleaned, exc)
        return []
    finally:
        if task is not None and _active_tasks.get(token) is task:
            _active_tasks.pop(token, None)
    if metas:
        _cache[key] = (time.monotonic(), metas)
        _cache.move_to_end(key)
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return metas
