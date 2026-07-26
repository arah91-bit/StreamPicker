"""TMDB client + Stremio meta building.

Metas resolve to IMDb tt-ids so the household's stream addons (Auto Stream,
AIOStreams — all idPrefixes ["tt"]) can actually play them; TMDB-only titles
are dropped. The tmdb->meta resolution cache lives in a JSON file so nightly
rebuilds only pay for titles they haven't seen before.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.recs.dramas import config

logger = logging.getLogger("asian-dramas")

IMG = "https://image.tmdb.org/t/p"
CACHE_PATH = Path(config.DATA_DIR) / "meta_cache.json"

_client = httpx.AsyncClient(
    base_url="https://api.themoviedb.org/3",
    timeout=30,
    params={"api_key": config.TMDB_API_KEY},
)
_sem = asyncio.Semaphore(8)

# tv:{tmdb_id} -> meta dict, or None for "no IMDb id / not found"
_meta_cache: dict[str, dict | None] = {}


def load_cache() -> None:
    global _meta_cache
    try:
        _meta_cache = json.loads(CACHE_PATH.read_text())
        logger.info(f"meta cache loaded: {len(_meta_cache)} entries")
    except (FileNotFoundError, json.JSONDecodeError):
        _meta_cache = {}


def save_cache() -> None:
    CACHE_PATH.write_text(json.dumps(_meta_cache))


async def _get(path: str, params: dict | None = None) -> Any:
    async with _sem:
        r = await _client.get(path, params=params or {})
    r.raise_for_status()
    return r.json()


async def discover_tv(params: dict, pages: int = 2) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        data = await _get("/discover/tv", {**params, "page": page})
        out.extend(data.get("results", []))
        if page >= data.get("total_pages", 1):
            break
    return out


async def search_keywords(query: str, limit: int = 2) -> list[int]:
    data = await _get("/search/keyword", {"query": query})
    return [k["id"] for k in data.get("results", [])[:limit]]


async def search_person(name: str) -> dict | None:
    data = await _get("/search/person", {"query": name})
    results = data.get("results") or []
    return results[0] if results else None


async def aggregate_credits(tv_id: int) -> list[dict]:
    """Top-billed cast for a show across all seasons."""
    try:
        data = await _get(f"/tv/{tv_id}/aggregate_credits")
    except httpx.HTTPStatusError:
        return []
    return data.get("cast") or []


async def person_tv_credits(person_id: int) -> list[dict]:
    try:
        data = await _get(f"/person/{person_id}/tv_credits")
    except httpx.HTTPStatusError:
        return []
    return data.get("cast") or []


def _build_meta(imdb_id: str, detail: dict) -> dict:
    meta = {
        "id": imdb_id,
        "type": "series",
        "name": detail.get("name"),
        "description": detail.get("overview") or "",
    }
    if detail.get("poster_path"):
        meta["poster"] = f"{IMG}/w500{detail['poster_path']}"
    if detail.get("backdrop_path"):
        meta["background"] = f"{IMG}/original{detail['backdrop_path']}"
    date = detail.get("first_air_date") or ""
    if date[:4]:
        meta["releaseInfo"] = date[:4]
    if detail.get("vote_average"):
        meta["imdbRating"] = str(round(detail["vote_average"], 1))
    if detail.get("genres"):
        meta["genres"] = [g["name"] for g in detail["genres"]]
    return meta


async def resolve_tv_meta(tmdb_id: int) -> dict | None:
    key = f"tv:{tmdb_id}"
    if key in _meta_cache:
        return _meta_cache[key]
    try:
        detail = await _get(f"/tv/{tmdb_id}", {"append_to_response": "external_ids"})
    except httpx.HTTPStatusError:
        _meta_cache[key] = None
        return None
    imdb_id = (detail.get("external_ids") or {}).get("imdb_id")
    if not imdb_id or not imdb_id.startswith("tt") or detail.get("adult"):
        _meta_cache[key] = None
        return None
    meta = _build_meta(imdb_id, detail)
    _meta_cache[key] = meta
    return meta


async def resolve_many(tmdb_ids: list[int], limit: int) -> list[dict]:
    """Resolve TMDB tv ids to metas, keeping order, dropping IMDb-less ones."""
    seen: set[int] = set()
    ids = [i for i in tmdb_ids if not (i in seen or seen.add(i))]
    metas: list[dict] = []
    seen_imdb: set[str] = set()
    for start in range(0, len(ids), 12):
        batch = ids[start:start + 12]
        results = await asyncio.gather(*(resolve_tv_meta(i) for i in batch))
        for meta in results:
            if meta and meta["id"] not in seen_imdb:
                seen_imdb.add(meta["id"])
                metas.append(meta)
        if len(metas) >= limit:
            break
    return metas[:limit]
