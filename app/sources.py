"""Shared upstream search layer.

Both addons served by this process — the fast picker and the slow "best
quality" picker — go through here to reach their shared upstream sources.
This module is the single choke point that guarantees a given title is
searched *at most once* per TTL, no matter how many addons, retries, or
household viewers ask for it at the same moment. That is the whole reason
the slow picker can exist without doubling API calls and tripping upstream
rate limits: it does not run its own searches in parallel with the fast
picker; it joins the fast picker's search and waits for it to finish.

Mechanics: each (source, media, media_id) has one registry-owned asyncio
task. Callers never hold or cancel that task — they *join* it (shielded)
for as long as they're willing to wait. When a caller gives up early (the
fast picker bailing after its fast-lane hit, or a deadline), the underlying
search keeps running and caches its raw result, so a later joiner or the
slow picker gets it for free.
"""

import asyncio
import json
import logging
import os
import re
import time

import httpx

from app import usenet

logger = logging.getLogger("stream-picker")

FAST, STREMTHRU, MEDIAFUSION = "fast", "stremthru", "mediafusion"
# Direct usenet lane (app.usenet): our own Newznab searches + nzbdav mounts,
# not an HTTP addon — _run special-cases it, but it shares the same registry so
# a title is searched/mounted at most once per TTL across both pickers.
NZB = "nzb"

_BASES = {
    FAST: (os.environ.get("FAST_BASE_URL") or "").rstrip("/") or None,
    # StremThru Torz — the long-tail online source (replaced AIOStreams). Its
    # crowdsourced public hash index + TorBox cache-check answers fast (~2s) with
    # deep coverage, so it races alongside Comet in the fast lane, not just slow.
    STREMTHRU: (os.environ.get("STREMTHRU_BASE_URL") or "").rstrip("/") or None,
    # Broad debrid-safe source: on-demand scrape of the public-only Prowlarr.
    # First hit for a title live-scrapes (~20s) then caches, so it feeds the
    # slow/quality build and later fast requests, not the first fast answer.
    MEDIAFUSION: (os.environ.get("MEDIAFUSION_BASE_URL") or "").rstrip("/") or None,
    NZB: "internal" if usenet.enabled() else None,
}

# httpx request timeout per source — how long the *underlying* search may run,
# independent of how long any one caller chooses to wait for it.
_REQ_TIMEOUT = {
    FAST: float(os.environ.get("FAST_TIMEOUT", "8")),
    STREMTHRU: float(os.environ.get("STREMTHRU_TIMEOUT", "20")),
    # generous: the first hit for a title live-scrapes prowlarr-public + FlareSolverr
    MEDIAFUSION: float(os.environ.get("MEDIAFUSION_TIMEOUT", "60")),
    # search all indexers + fetch/PUT/mount top NZBs; normally ~8-15s
    NZB: float(os.environ.get("NZB_TIMEOUT", "45")),
}

# ── user-added player addons ─────────────────────────────────────────────────
# Any addon that serves /stream/{type}/{id}.json — AIOStreams, a usenet addon,
# a debrid catalog — can be plugged in from the dashboard. Each becomes a
# first-class online source keyed `x:<slug>`: it joins the same shared search,
# and its streams flow through the identical filter + playback-probe pipeline,
# so only *verified* results reach the player. Stored as a JSON list of
# {name, url} in EXTRA_ADDONS (managed by the settings dashboard).
EXTRA_TIMEOUT = float(os.environ.get("EXTRA_ADDON_TIMEOUT", "30"))
BUILTIN_ONLINE = [FAST, STREMTHRU, MEDIAFUSION]
EXTRAS: list[str] = []          # extra source keys, in configured order
EXTRA_META: list[dict] = []     # [{key, name, url}] for the dashboard


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "addon"


def _base_of(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/manifest.json"):
        url = url[:-len("/manifest.json")].rstrip("/")
    return url


def _load_extras() -> None:
    raw = (os.environ.get("EXTRA_ADDONS") or "").strip()
    if not raw:
        return
    try:
        items = json.loads(raw)
    except ValueError:
        logger.warning("EXTRA_ADDONS: invalid JSON — ignoring custom addons")
        return
    used: set[str] = set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        base = _base_of(str(it.get("url", "")))
        if not base.startswith(("http://", "https://")):
            continue
        name = str(it.get("name", "")).strip() or base
        slug, key, i = _slug(name), "", 2
        key = f"x:{slug}"
        while key in used:
            key = f"x:{slug}-{i}"
            i += 1
        used.add(key)
        _BASES[key] = base
        _REQ_TIMEOUT[key] = EXTRA_TIMEOUT
        EXTRAS.append(key)
        EXTRA_META.append({"key": key, "name": name, "url": base})
    if EXTRAS:
        logger.info(f"custom addons enabled: {', '.join(m['name'] for m in EXTRA_META)}")


_load_extras()


def search_all() -> list[str]:
    """Every source to search for a title, built-ins + user addons, that is
    actually configured. NZB stays last (it's the slow direct lane)."""
    return [k for k in [*BUILTIN_ONLINE, *EXTRAS, NZB] if has(k)]


# Successful (non-empty) searches are reused for RAW_TTL. Empty results —
# usually a transient upstream hiccup rather than "this title truly has no
# sources" — are only briefly reused, enough to dedupe two addons opening the
# same title together, then re-searched.
RAW_TTL = float(os.environ.get("RAW_CACHE_TTL", str(6 * 3600)))
NEG_TTL = float(os.environ.get("RAW_NEG_TTL", "90"))

_client = httpx.AsyncClient(timeout=None, headers={"User-Agent": "Stremio"})

# (source, media, media_id) -> (finished_at, streams)
_raw: dict[tuple, tuple[float, list[dict]]] = {}
# (source, media, media_id) -> in-flight task (present only while running)
_inflight: dict[tuple, asyncio.Task] = {}


def has(source: str) -> bool:
    return _BASES.get(source) is not None


def _fresh(hit: tuple[float, list[dict]]) -> bool:
    ts, streams = hit
    ttl = RAW_TTL if streams else NEG_TTL
    return (time.monotonic() - ts) < ttl


async def _run(key: tuple, base: str, media: str, media_id: str,
               timeout: float) -> list[dict]:
    source = key[0]
    t0 = time.monotonic()
    try:
        if source == NZB:      # internal lane, not an HTTP addon
            streams = await asyncio.wait_for(
                usenet.streams(media, media_id), timeout)
        else:
            url = f"{base}/stream/{media}/{media_id}.json"
            r = await _client.get(url, timeout=timeout)
            r.raise_for_status()
            streams = r.json().get("streams") or []
    except asyncio.CancelledError:
        raise
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        detail = f" HTTP {status}" if status else ""
        logger.warning(f"upstream {source} failed: {type(e).__name__}{detail}")
        streams = []
    # cache + deregister atomically (no await between) so a concurrent joiner
    # either sees the running task or the fresh cache, never a gap.
    _raw[key] = (time.monotonic(), streams)
    _inflight.pop(key, None)
    if len(_raw) > 800:
        _raw.pop(next(iter(_raw)))
    logger.info(f"shared {source} {media}/{media_id}: {len(streams)} streams "
                f"in {time.monotonic() - t0:.1f}s")
    return streams


def _task(source: str, media: str, media_id: str) -> asyncio.Task | None:
    """Ensure a shared search task exists for this title/source and return it,
    or None if a fresh cached result already answers it. Starts a search only
    when neither a fresh cache entry nor an in-flight task exists — this is
    where double-searching is prevented."""
    base = _BASES.get(source)
    if not base:
        return None
    key = (source, media, media_id)
    hit = _raw.get(key)
    if hit and _fresh(hit):
        return None
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(
            _run(key, base, media, media_id, _REQ_TIMEOUT[source]))
        _inflight[key] = task
    return task


def start(source: str, media: str, media_id: str) -> None:
    """Kick off (register) the shared search now without waiting. Lets slow
    sources like Usenet begin at t=0 while the caller works on something else.
    No-op if disabled or already fresh/running."""
    _task(source, media, media_id)


def peek(source: str, media: str, media_id: str) -> list[dict] | None:
    """Non-blocking: the raw streams if the search has finished, else None.
    Never starts or waits on a search."""
    hit = _raw.get((source, media, media_id))
    return hit[1] if hit else None


async def get(source: str, media: str, media_id: str,
              wait: float) -> list[dict]:
    """Join the shared search and wait up to `wait` seconds for it. Returns
    whatever is available now (possibly []). Shielded, so a timeout here never
    cancels the underlying search — it keeps running and caches its result for
    the next joiner."""
    base = _BASES.get(source)
    if not base:
        return []
    key = (source, media, media_id)
    hit = _raw.get(key)
    if hit and _fresh(hit):
        return hit[1]
    task = _task(source, media, media_id)
    if task is None:                      # became fresh between the checks
        return peek(source, media, media_id) or []
    try:
        return await asyncio.wait_for(asyncio.shield(task), max(wait, 0.1))
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return peek(source, media, media_id) or []
