"""Acquisition fallback — slow picker only.

When the slow picker can't find a source *anywhere* (no online streams and not
in the library), hand the title to Radarr (movies) or Sonarr (series) so it
gets downloaded. Once it lands in the library, Jellio serves it and the next
open plays it locally. The picker meanwhile shows a "being added" notice.

Deliberately conservative: only ever called from pick_slow's no-source branch
(never the fast picker), and deduped per title so repeated opens don't spam adds
or searches. If the title is already in Radarr/Sonarr it just re-triggers a
search rather than adding a duplicate.
"""

import asyncio
import logging
import os
import time

import httpx

from app import meta

logger = logging.getLogger("stream-picker")


def _cfg(prefix: str) -> tuple[str | None, str | None]:
    return ((os.environ.get(f"{prefix}_URL") or "").rstrip("/") or None,
            os.environ.get(f"{prefix}_API_KEY") or None)


RADARR_URL, RADARR_KEY = _cfg("RADARR")
SONARR_URL, SONARR_KEY = _cfg("SONARR")
RADARR_ROOT = os.environ.get("RADARR_ROOT") or None
SONARR_ROOT = os.environ.get("SONARR_ROOT") or None
RADARR_QP = os.environ.get("RADARR_QUALITY_PROFILE", "HD-1080p")
SONARR_QP = os.environ.get("SONARR_QUALITY_PROFILE", "HD-1080p")
# Jellyseerr (optional, preferred): TMDB-native request manager that handles the
# imdb→TMDB→Sonarr/Radarr mapping, quality profiles and auto-approval itself, and
# keeps a visible request history. Falls back to direct Sonarr/Radarr if unset or
# if a request fails.
JELLYSEERR_URL = (os.environ.get("JELLYSEERR_URL") or "").rstrip("/") or None
JELLYSEERR_KEY = os.environ.get("JELLYSEERR_API_KEY") or None
ENABLED = (os.environ.get("ACQUIRE_ENABLED", "true").lower()
           in ("1", "true", "yes", "on"))
REQUEST_TTL = float(os.environ.get("ACQUIRE_DEDUP_TTL", str(30 * 60)))

_client = httpx.AsyncClient(timeout=30, headers={"User-Agent": "stream-picker"})
_requested: dict[str, float] = {}
_inflight: dict[str, asyncio.Task] = {}
_qp_cache: dict[str, int] = {}
_rf_cache: dict[str, str] = {}


def _direct_configured(media: str) -> bool:
    return bool((RADARR_URL and RADARR_KEY) if media == "movie"
                else (SONARR_URL and SONARR_KEY))


def enabled_for(media: str) -> bool:
    if not ENABLED:
        return False
    return bool(JELLYSEERR_URL and JELLYSEERR_KEY) or _direct_configured(media)


async def _resolve_qp(base: str, key: str, want: str) -> int:
    ck = f"{base}:{want}"
    if ck in _qp_cache:
        return _qp_cache[ck]
    profs = (await _client.get(f"{base}/api/v3/qualityprofile",
                               headers={"X-Api-Key": key})).json()
    pid = next((p["id"] for p in profs
                if p.get("name", "").lower() == want.lower()), None)
    if pid is None:
        raise ValueError(f"quality profile {want!r} does not exist at {base}")
    _qp_cache[ck] = pid
    return pid


async def _resolve_rf(base: str, key: str, configured: str | None) -> str:
    if configured:
        return configured
    if base in _rf_cache:
        return _rf_cache[base]
    rfs = (await _client.get(f"{base}/api/v3/rootfolder",
                             headers={"X-Api-Key": key})).json()
    if not rfs:
        raise ValueError(f"no root folder is configured at {base}")
    path = rfs[0]["path"]
    _rf_cache[base] = path
    return path


async def _lookup(base: str, key: str, kind: str, imdb: str, media: str) -> dict | None:
    """Look a title up in Sonarr/Radarr by imdb id, falling back to a title+year
    search when the imdb id isn't in their (TVDB/TMDB) metadata — common for
    Asian dramas and for imdb-id mismatches. `kind` is 'series' or 'movie'.
    Returns the lookup object to add, or None (logged) if nothing matches."""
    h = {"X-Api-Key": key}
    hits = (await _client.get(f"{base}/api/v3/{kind}/lookup",
                              params={"term": f"imdb:{imdb}"}, headers=h)).json()
    if hits:
        return hits[0]
    title, orig, year = await meta.title_year(media, imdb)
    if not title and not orig:
        logger.info(f"{kind}: no imdb match and no TMDB title for {imdb}")
        return None
    # Try the English title, then the native title — TVDB often only carries the
    # original (e.g. Chinese/Korean) name. Match within ±1 year to avoid grabbing
    # an unrelated same-named show.
    seen: set[str] = set()
    for term in (title, orig):
        if not term or term in seen:
            continue
        seen.add(term)
        hits = (await _client.get(f"{base}/api/v3/{kind}/lookup",
                                  params={"term": term}, headers=h)).json()
        for s in hits:
            if year and s.get("year") and abs(s["year"] - year) <= 1:
                logger.info(f"{kind}: matched {imdb} to '{s.get('title')}'"
                            f" ({s.get('year')}) via {term!r}")
                return s
    logger.info(f"{kind}: no match for {imdb} ({title!r}/{orig!r}, {year}) —"
                f" not in {'TVDB' if kind == 'series' else 'TMDB'}")
    return None


async def _radarr(imdb: str) -> bool:
    h = {"X-Api-Key": RADARR_KEY}
    movie = await _lookup(RADARR_URL, RADARR_KEY, "movie", imdb, "movie")
    if not movie:
        return False
    tmdb = movie.get("tmdbId")
    existing = (await _client.get(
        f"{RADARR_URL}/api/v3/movie?tmdbId={tmdb}", headers=h)).json()
    if existing:
        mid = existing[0]["id"]
        command = await _client.post(
            f"{RADARR_URL}/api/v3/command", headers=h,
            json={"name": "MoviesSearch", "movieIds": [mid]})
        if not 200 <= command.status_code < 300:
            logger.warning(f"radarr search command failed: HTTP {command.status_code}")
            return False
        logger.info(f"radarr: '{movie.get('title')}' already in library,"
                    f" triggered search")
        return True
    body = {**movie,
            "qualityProfileId": await _resolve_qp(RADARR_URL, RADARR_KEY, RADARR_QP),
            "rootFolderPath": await _resolve_rf(RADARR_URL, RADARR_KEY, RADARR_ROOT),
            "monitored": True, "minimumAvailability": "released",
            "addOptions": {"searchForMovie": True}}
    r = await _client.post(f"{RADARR_URL}/api/v3/movie", headers=h, json=body)
    ok = r.status_code in (200, 201)
    logger.info(f"radarr add '{movie.get('title')}' ({imdb}): HTTP "
                f"{r.status_code}{'' if ok else ' ' + r.text[:150]}")
    return ok


async def _sonarr(media_id: str) -> bool:
    parts = media_id.split(":")
    imdb = parts[0]
    season = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    h = {"X-Api-Key": SONARR_KEY}
    series = await _lookup(SONARR_URL, SONARR_KEY, "series", imdb, "series")
    if not series:
        return False
    tvdb = series.get("tvdbId")
    allseries = (await _client.get(
        f"{SONARR_URL}/api/v3/series", headers=h)).json()
    existing = [s for s in allseries if s.get("tvdbId") == tvdb]
    if existing:
        sid = existing[0]["id"]
        cmd = ({"name": "SeasonSearch", "seriesId": sid, "seasonNumber": season}
               if season is not None else
               {"name": "SeriesSearch", "seriesId": sid})
        command = await _client.post(
            f"{SONARR_URL}/api/v3/command", headers=h, json=cmd)
        if not 200 <= command.status_code < 300:
            logger.warning(f"sonarr search command failed: HTTP {command.status_code}")
            return False
        logger.info(f"sonarr: '{series.get('title')}' already added,"
                    f" search season={season}")
        return True
    body = {**series,
            "qualityProfileId": await _resolve_qp(SONARR_URL, SONARR_KEY, SONARR_QP),
            "rootFolderPath": await _resolve_rf(SONARR_URL, SONARR_KEY, SONARR_ROOT),
            "monitored": True,
            "addOptions": {"monitor": "all", "searchForMissingEpisodes": True,
                           "searchForCutoffUnmetEpisodes": False}}
    r = await _client.post(f"{SONARR_URL}/api/v3/series", headers=h, json=body)
    ok = r.status_code in (200, 201)
    logger.info(f"sonarr add '{series.get('title')}' ({imdb}): HTTP "
                f"{r.status_code}{'' if ok else ' ' + r.text[:150]}")
    return ok


async def _jellyseerr(media: str, media_id: str) -> bool:
    """Place a request through Jellyseerr (TMDB-native; auto-approves and forwards
    to Sonarr/Radarr). Returns False if it can't (no TMDB id, or Jellyseerr has no
    TVDB mapping for a series) so the caller can fall back to direct."""
    parts = media_id.split(":")
    imdb = parts[0]
    season = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    tid = await meta.tmdb_id(media, imdb)
    if not tid:
        logger.info(f"jellyseerr: no tmdb id for {imdb}")
        return False
    body: dict = {"mediaType": "movie" if media == "movie" else "tv",
                  "mediaId": tid}
    if media != "movie":
        body["seasons"] = [season] if season else "all"
    r = await _client.post(f"{JELLYSEERR_URL}/api/v1/request",
                           headers={"X-Api-Key": JELLYSEERR_KEY}, json=body)
    # Any 2xx is accepted (Jellyseerr returns 201/202); 409 = already
    # requested/available — still "in place", so treat that as success too.
    ok = 200 <= r.status_code < 300 or r.status_code == 409
    logger.info(f"jellyseerr request {media} {imdb} tmdb={tid} season={season}:"
                f" HTTP {r.status_code}{'' if ok else ' ' + r.text[:150]}")
    return ok


async def _perform(media: str, media_id: str, dedup_key: str) -> bool:
    try:
        ok = False
        if JELLYSEERR_URL and JELLYSEERR_KEY:
            ok = await _jellyseerr(media, media_id)
        if not ok and _direct_configured(media):
            ok = await (_radarr(media_id.split(":")[0]) if media == "movie"
                        else _sonarr(media_id))
    except Exception:
        logger.exception(f"acquire {media_id} failed")
        ok = False
    if ok:
        _requested[dedup_key] = time.monotonic()
        if len(_requested) > 1000:
            oldest = min(_requested, key=_requested.get)
            _requested.pop(oldest, None)
    return ok


async def request(media: str, media_id: str) -> bool:
    """Request the title. Prefers Jellyseerr when configured, falling back to a
    direct Sonarr/Radarr add. Deduped per title (per season for series). Returns
    True if a request is in place."""
    if not enabled_for(media):
        return False
    # series dedup includes season so a later season re-triggers; movies by imdb
    dedup_key = (f"series:{media_id}" if media == "series"
                 else f"movie:{media_id.split(':')[0]}")
    now = time.monotonic()
    if dedup_key in _requested and now - _requested[dedup_key] < REQUEST_TTL:
        return True
    task = _inflight.get(dedup_key)
    if task is None:
        task = asyncio.create_task(_perform(media, media_id, dedup_key))
        _inflight[dedup_key] = task
        task.add_done_callback(lambda done, key=dedup_key:
                               _inflight.pop(key, None)
                               if _inflight.get(key) is done else None)
    # One caller timing out/cancelling must not cancel the shared acquisition.
    return bool(await asyncio.shield(task))


async def shutdown() -> None:
    tasks = list(_inflight.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _inflight.clear()
    await _client.aclose()
