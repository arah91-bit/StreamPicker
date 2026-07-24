"""Opt-in private-tracker fallback with progressive torrent playback.

This lane is deliberately separate from ordinary/debrid sources.  Searching is
safe and may be cached, but a torrent is fetched from the private Prowlarr and
added to a torrent client only after a player performs a real GET on an opaque
capability URL. HEAD requests never activate anything.

The compatibility path lets qBittorrent download and serve the release. The
progressive path uses rqbit for the selected file because its HTTP reader
prioritizes the pieces being watched. Once that file is complete, qBittorrent
rechecks the same ordinary files, downloads the remainder of the release, and
becomes the sole long-term seeder.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, Request
from starlette.responses import Response, StreamingResponse

from app import telemetry, usenet

logger = logging.getLogger("stream-picker")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


ENABLED = _bool("PRIVATE_TRACKERS_ENABLED")
PROWLARR_URL = (os.environ.get("PRIVATE_PROWLARR_URL") or "").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PRIVATE_PROWLARR_API_KEY") or ""
QBIT_URL = (os.environ.get("PRIVATE_QBITTORRENT_URL") or "").rstrip("/")
QBIT_USERNAME = os.environ.get("PRIVATE_QBITTORRENT_USERNAME") or "admin"
QBIT_PASSWORD = os.environ.get("PRIVATE_QBITTORRENT_PASSWORD") or ""
STREAM_ENGINE = (os.environ.get("PRIVATE_STREAM_ENGINE")
                 or "qbittorrent").strip().lower()
RQBIT_URL = (os.environ.get("PRIVATE_RQBIT_URL")
             or "http://rqbit:3030").rstrip("/")
RQBIT_USERNAME = os.environ.get("PRIVATE_RQBIT_USERNAME") or ""
RQBIT_PASSWORD = os.environ.get("PRIVATE_RQBIT_PASSWORD") or ""
QBIT_SAVE_PATH = (os.environ.get("PRIVATE_QBITTORRENT_SAVE_PATH")
                  or "/data/nuviodownloads").rstrip("/")
RQBIT_OUTPUT_PATH = (os.environ.get("PRIVATE_RQBIT_OUTPUT_PATH")
                     or QBIT_SAVE_PATH).rstrip("/")
RQBIT_VPN_URL = (os.environ.get("PRIVATE_RQBIT_VPN_URL")
                 or "http://rqbit-vpn:8000").rstrip("/")
RQBIT_VPN_API_KEY = os.environ.get("PRIVATE_RQBIT_VPN_API_KEY") or ""
DOWNLOAD_ROOT = Path(os.environ.get("PRIVATE_TRACKER_DOWNLOAD_ROOT")
                     or "/private-downloads")
CATEGORY = os.environ.get("PRIVATE_QBITTORRENT_CATEGORY") \
    or "stream-picker-private"
PUBLIC_URL = (os.environ.get("ADDON_PUBLIC_URL")
              or "http://localhost:8011").rstrip("/")
MAX_CANDIDATES = max(1, int(os.environ.get(
    "PRIVATE_TRACKER_CANDIDATES", "20")))
_RELEASE_KINDS = ("episode", "season", "series")
RELEASE_ORDER = tuple(dict.fromkeys(
    value.strip().lower()
    for value in os.environ.get(
        "PRIVATE_TRACKER_RELEASE_ORDER",
        "episode,season,series").split(",")
    if value.strip().lower() in _RELEASE_KINDS
)) or _RELEASE_KINDS
RELEASE_RANK = {
    kind: len(RELEASE_ORDER) - index
    for index, kind in enumerate(RELEASE_ORDER)
}
MIN_SEEDERS = max(0, int(os.environ.get("PRIVATE_TRACKER_MIN_SEEDERS", "5")))
MAX_TORRENT_GB = max(0.0, float(os.environ.get(
    "PRIVATE_TRACKER_MAX_TORRENT_GB", "0")))
MAX_ACTIVE_DOWNLOADS = max(0, int(os.environ.get(
    "PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS", "3")))
# Whole torrent (100%) vs single-file. Default ON: download every file and seed
# the complete release (no hit-and-run). When OFF, only the clicked episode is
# downloaded from a season pack (others set to skip); the release stays partial.
WHOLE_TORRENT = _bool("PRIVATE_TRACKER_WHOLE_TORRENT", True)
SEARCH_TIMEOUT = max(2.0, float(os.environ.get(
    "PRIVATE_TRACKER_SEARCH_TIMEOUT", "45")))
START_TIMEOUT = max(5.0, float(os.environ.get(
    "PRIVATE_TRACKER_START_TIMEOUT", "90")))
SEARCH_TTL = max(60.0, float(os.environ.get(
    "PRIVATE_TRACKER_SEARCH_TTL", "10800")))
TOKEN_TTL = max(SEARCH_TTL, 24 * 3600)

_VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts",
              ".webm", ".wmv")
_RES_RE = re.compile(r"\b(2160|1440|1080|720|576|480)p?\b", re.I)
_COMPLETE_SERIES_RE = re.compile(
    r"\b(?:complete[ ._-]*(?:series|collection)|"
    r"(?:series|collection)[ ._-]*complete|all[ ._-]*seasons?)\b", re.I)
_SEASON_RANGE_RES = (
    re.compile(r"(?<![A-Za-z0-9])s0*(\d{1,3})[ ._-]*(?:-|to)[ ._-]*s?0*(\d{1,3})",
               re.I),
    re.compile(r"\bseasons?[ ._-]*0*(\d{1,3})[ ._-]*(?:-|to)[ ._-]*0*(\d{1,3})",
               re.I),
)
_INDEXER_TAG_PREFIX = "stream-picker-indexer="

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=8, read=SEARCH_TIMEOUT, write=15, pool=8),
    follow_redirects=True,
    headers={"User-Agent": "StreamPicker/1.0"},
)
_qbit = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=8, read=15, write=15, pool=8),
    follow_redirects=False,
    headers={"User-Agent": "StreamPicker/1.0"},
)
_rqbit = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=8, read=45, write=45, pool=8),
    follow_redirects=False,
    headers={"User-Agent": "StreamPicker/1.0"},
)
_vpn = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5, read=8, write=8, pool=5),
    follow_redirects=False,
    headers={"User-Agent": "StreamPicker/1.0"},
)
_qbit_auth_lock = asyncio.Lock()
_activation_lock = asyncio.Lock()
_rqbit_vpn_lock = asyncio.Lock()
_qbit_authenticated = False
_rqbit_vpn_checked_at = 0.0

# (media, media_id) -> (monotonic finish, candidates); task registry shares one
# live scrape across repeated slow-picker/background requests.
_search_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_search_tasks: dict[tuple[str, str], asyncio.Task] = {}
_search_outcomes: dict[tuple[str, str], dict] = {}

# Opaque player capabilities. Candidate payloads contain Prowlarr download URLs
# and therefore stay memory-only; the URL exposed to a player is random.
_tokens: dict[str, tuple[float, dict]] = {}
_token_locks: dict[str, asyncio.Lock] = {}
_completion_tasks: dict[str, asyncio.Task] = {}
_rqbit_tasks: dict[str, asyncio.Task] = {}
_rqbit_watch_selection: dict[str, int] = {}
_rqbit_generation: dict[str, int] = {}
_reconcile_task: asyncio.Task | None = None
_private_indexers: tuple[float, dict[int, str]] | None = None


def progressive_enabled() -> bool:
    return STREAM_ENGINE == "rqbit"


def enabled() -> bool:
    base = bool(ENABLED and PROWLARR_URL and PROWLARR_API_KEY and QBIT_URL
                and QBIT_PASSWORD and CATEGORY and QBIT_SAVE_PATH)
    return bool(base and (not progressive_enabled()
                          or (RQBIT_URL and RQBIT_OUTPUT_PATH
                              and RQBIT_VPN_URL and RQBIT_VPN_API_KEY)))


def _nonnegative_int(value) -> int:
    """Untrusted Prowlarr numbers are optional evidence, never fatal input."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _episode_coords(media: str, media_id: str) -> tuple[int, int] | None:
    parts = media_id.split(":")
    if media == "movie" or len(parts) < 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def _season_pattern(season: int) -> re.Pattern:
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:s0*{season}(?!\s*e\d)|season[ ._-]*0*{season})"
        rf"(?!\d)", re.I)


def classify_release(title: str, season: int, episode: int) -> str:
    """Return ``season``, ``episode``, ``series`` or ``""`` (reject).

    Classification is independent of preference. ``RELEASE_ORDER`` decides
    which enabled kind ranks first. The exact-episode parser is shared with the
    direct-Usenet lane so scene/anime punctuation stays consistent.
    """
    text = str(title or "")
    if usenet._episode_match(text, season, episode):
        return "episode"
    if _COMPLETE_SERIES_RE.search(text):
        return "series"
    for pattern in _SEASON_RANGE_RES:
        for left, right in pattern.findall(text):
            low, high = sorted((int(left), int(right)))
            return "series" if low <= season <= high else ""
    seasons = {int(x) for x in re.findall(
        r"(?<![A-Za-z0-9])s0*(\d{1,3})(?!\s*e\d)", text, re.I)}
    seasons |= {int(x) for x in re.findall(
        r"\bseason[ ._-]*0*(\d{1,3})\b", text, re.I)}
    if seasons == {season}:
        return "season"
    if len(seasons) > 1:
        return "series" if season in seasons else ""
    if _season_pattern(season).search(text):
        return "season"
    return ""


def _quality_key(cand: dict) -> tuple:
    text = cand.get("title") or ""
    m = _RES_RE.search(text)
    res = int(m.group(1)) if m else 0
    low = text.lower().replace("-", "").replace(" ", "")
    source = (5 if "remux" in low else 4 if "bluray" in low else
              3 if "webdl" in low else 2 if "webrip" in low else
              1 if "hdtv" in low else 0)
    codec = 2 if re.search(r"\b(?:av1|x265|hevc|h\.?265)\b", text, re.I) else 1
    pack = RELEASE_RANK.get(cand.get("kind"), len(RELEASE_ORDER) + 1)
    # Tracker preference: within a release kind, results from higher-preference
    # trackers rank first. Every tracker defaults to a neutral 50, which makes
    # this term constant and preserves pure quality ordering until a user
    # deliberately reweights a tracker.
    pref = 100 - _indexer_score(cand.get("indexer"))
    return (pack, pref, res, source, codec, int(cand.get("seeders") or 0),
            int(cand.get("size") or 0))


def _indexer_name(value, fallback: str = "Private tracker") -> str:
    """Compact a Prowlarr indexer label for safe player-facing display."""
    clean = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    return (clean or fallback)[:80]


def _indexer_tag(value) -> str:
    """qBittorrent tag used to preserve private-source provenance."""
    name = _indexer_name(value).replace(",", " ")
    return f"{_INDEXER_TAG_PREFIX}{name}"


def _indexer_from_tags(value) -> str:
    for raw in str(value or "").split(","):
        tag = raw.strip()
        if tag.startswith(_INDEXER_TAG_PREFIX):
            return _indexer_name(tag[len(_INDEXER_TAG_PREFIX):])
    return ""


NEUTRAL_SCORE = 50


def _load_indexer_scores() -> dict[str, int]:
    """Case-folded tracker-name -> preference score (1 best … 100 worst).

    Stored as a compact JSON object; only trackers the user reweighted away
    from the neutral default are persisted, so an empty/absent value means
    every tracker is treated equally.
    """
    raw = os.environ.get("PRIVATE_TRACKER_INDEXER_SCORES") or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    scores: dict[str, int] = {}
    for name, value in data.items():
        key = _indexer_name(name, "").casefold()
        if not key:
            continue
        try:
            score = int(value)
        except (TypeError, ValueError):
            continue
        scores[key] = min(100, max(1, score))
    return scores


INDEXER_SCORES = _load_indexer_scores()


def _indexer_score(value) -> int:
    """Preference score for a tracker name; neutral 50 when unscored."""
    return INDEXER_SCORES.get(_indexer_name(value).casefold(), NEUTRAL_SCORE)


async def indexer_preferences() -> list[dict]:
    """Every enabled private torrent indexer with its current preference score,
    ordered most-preferred first for the dashboard editor."""
    indexers = await _private_torrent_indexers()
    rows: dict[str, dict] = {}
    for name in indexers.values():
        rows[name.casefold()] = {"name": name, "score": _indexer_score(name)}
    return sorted(rows.values(), key=lambda r: (r["score"], r["name"].lower()))


async def _private_torrent_indexers() -> dict[int, str]:
    global _private_indexers
    now = time.monotonic()
    if _private_indexers and now - _private_indexers[0] < 600:
        return dict(_private_indexers[1])
    response = await _client.get(
        f"{PROWLARR_URL}/api/v1/indexer",
        headers={"X-Api-Key": PROWLARR_API_KEY}, timeout=15)
    response.raise_for_status()
    indexers = {
        int(row["id"]): _indexer_name(
            row.get("name") or row.get("implementationName"),
            f"Private tracker #{row['id']}")
        for row in response.json()
        if row.get("enable", True)
        and str(row.get("protocol") or "").lower() == "torrent"
        and str(row.get("privacy") or "").lower() == "private"
        and str(row.get("id") or "").isdigit()
    }
    _private_indexers = (now, indexers)
    return dict(indexers)


async def _prowlarr_query(query: str, indexer_ids: set[int]) -> list[dict]:
    params: list[tuple[str, str]] = [
        ("query", query), ("type", "search"), ("limit", "100")]
    params += [("indexerIds", str(i)) for i in sorted(indexer_ids)]
    response = await _client.get(
        f"{PROWLARR_URL}/api/v1/search",
        headers={"X-Api-Key": PROWLARR_API_KEY}, params=params,
        timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _query_strings(media: str, titles: str | list[str], year: int | None,
                   coords: tuple[int, int] | None) -> list[str]:
    names = [titles] if isinstance(titles, str) else list(titles)
    # Metadata order is meaningful (canonical first), but foreign works often
    # need their native-script or regional alias. Bound the fan-out and dedupe
    # case-insensitively so a large alias list cannot hammer Prowlarr.
    clean: list[str] = []
    seen: set[str] = set()
    for raw in names:
        title = str(raw or "").strip()
        folded = title.casefold()
        if title and folded not in seen:
            clean.append(title)
            seen.add(folded)
        if len(clean) >= 4:
            break
    if not coords:
        return list(dict.fromkeys(
            f"{title} {year}" if year else title for title in clean))
    season, episode = coords
    queries: list[str] = []
    for title in clean:
        # Exact episode first, then reusable season/series packs. A bare-title
        # query catches trackers whose search parser discards Sxx tokens.
        queries.extend((f"{title} S{season:02d}E{episode:02d}",
                        f"{title} S{season:02d}", title,
                        f"{title} complete series"))
    return list(dict.fromkeys(queries))


async def _search(media: str, media_id: str) -> list[dict]:
    key = (media, media_id)
    started = time.monotonic()
    try:
        indexers = await _private_torrent_indexers()
        indexer_ids = set(indexers)
        if not indexer_ids:
            raise RuntimeError("no enabled private torrent indexers")
        titles, year = await usenet._expected_info(media, media_id)
        if not titles:
            raise RuntimeError("title metadata unavailable")
        coords = _episode_coords(media, media_id)
        queries = _query_strings(media, titles, year, coords)
        batches = await asyncio.gather(
            *(_prowlarr_query(q, indexer_ids) for q in queries),
            return_exceptions=True)
        if all(isinstance(x, BaseException) for x in batches):
            raise next(x for x in batches if isinstance(x, BaseException))
        rows = [row for batch in batches if isinstance(batch, list)
                for row in batch]
        candidates: list[dict] = []
        for row in rows:
            try:
                indexer_id = int(row.get("indexerId"))
            except (TypeError, ValueError):
                continue
            if indexer_id not in indexer_ids:
                continue
            if str(row.get("protocol") or "").lower() != "torrent":
                continue
            title = str(row.get("title") or "").strip()
            download_url = str(row.get("downloadUrl") or "").strip()
            if not title or not download_url:
                continue
            seeders = _nonnegative_int(row.get("seeders"))
            if seeders < MIN_SEEDERS:
                continue
            if not any(usenet._release_title_match(title, wanted)
                       for wanted in titles if wanted):
                continue
            if not usenet._release_year_match(title, titles, year):
                continue
            kind = "movie"
            if coords:
                kind = classify_release(title, *coords)
                if not kind or kind not in RELEASE_RANK:
                    continue
            size = _nonnegative_int(row.get("size"))
            if MAX_TORRENT_GB and (not size
                                   or size > MAX_TORRENT_GB * 1_000_000_000):
                continue
            candidates.append({
                "media": media, "media_id": media_id, "title": title,
                "kind": kind, "size": size, "seeders": seeders,
                # Search payloads normally include ``indexer``; use the
                # catalog's ID-to-name map when they do not so every result can
                # still identify the exact private tracker it came from.
                "indexer": _indexer_name(
                    row.get("indexer"), indexers[indexer_id]),
                "indexer_id": indexer_id, "download_url": download_url,
                "guid": str(row.get("guid") or "")[:500],
                "season": coords[0] if coords else None,
                "episode": coords[1] if coords else None,
            })
        dedup: dict[str, dict] = {}
        for cand in candidates:
            identity = cand["guid"] or hashlib.sha256(
                f"{cand['title']}\0{cand['size']}".encode()).hexdigest()
            current = dedup.get(identity)
            if current is None or _quality_key(cand) > _quality_key(current):
                dedup[identity] = cand
        ranked = sorted(dedup.values(), key=_quality_key, reverse=True)
        ranked = ranked[:MAX_CANDIDATES]
        _search_outcomes[key] = {
            "state": "ok" if ranked else "empty", "count": len(ranked),
            "seconds": round(time.monotonic() - started, 2), "detail": ""}
        telemetry.record_private_tracker(
            "search", media_id=media_id,
            detail="candidates" if ranked else "empty")
        if ranked:
            telemetry.record_private_tracker(
                "candidates", media_id=media_id, count=len(ranked))
        return ranked
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _search_outcomes[key] = {
            "state": "failed", "count": 0,
            "seconds": round(time.monotonic() - started, 2),
            "detail": type(exc).__name__}
        telemetry.record_private_tracker(
            "search_failed", media_id=media_id, detail=type(exc).__name__)
        logger.warning("private trackers: search failed for %s: %s",
                       media_id, type(exc).__name__)
        return []
    finally:
        _search_tasks.pop(key, None)


async def candidates(media: str, media_id: str,
                     wait: float | None = None) -> list[dict]:
    """Shared/cached private search. It never downloads or adds a torrent."""
    if not enabled():
        return []
    key = (media, media_id)
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return [dict(x) for x in hit[1]]
    task = _search_tasks.get(key)
    if task is None:
        task = asyncio.create_task(_search(media, media_id))
        _search_tasks[key] = task

        def done(fut: asyncio.Task) -> None:
            if fut.cancelled() or fut.exception() is not None:
                return
            # A search that failed outright (every indexer errored or the cold
            # 26-indexer fan-out timed out) returns [] with a "failed" outcome.
            # Caching that would suppress re-searching this title for SEARCH_TTL
            # (hours), so one transient cold-start timeout would hide results
            # that a retry moments later would find. Only cache genuine
            # outcomes ("ok"/"empty"); a real empty result is worth caching.
            if _search_outcomes.get(key, {}).get("state") == "failed":
                return
            _search_cache[key] = (time.monotonic(), list(fut.result()))
            if len(_search_cache) > 500:
                _search_cache.pop(next(iter(_search_cache)), None)

        task.add_done_callback(done)
    if wait is not None and wait <= 0:
        return []
    try:
        result = (await asyncio.wait_for(asyncio.shield(task), timeout=wait)
                  if wait is not None else await asyncio.shield(task))
        return [dict(x) for x in result]
    except asyncio.TimeoutError:
        return []


def search_in_progress(media: str, media_id: str) -> bool:
    task = _search_tasks.get((media, media_id))
    return bool(task and not task.done())


def search_outcome(media: str, media_id: str) -> dict:
    """Last credential-free result for admin/user-facing status decisions."""
    return dict(_search_outcomes.get((media, media_id), {}))


def _prune_tokens() -> None:
    cutoff = time.time() - TOKEN_TTL
    for token, (created, _) in list(_tokens.items()):
        if created < cutoff:
            _tokens.pop(token, None)
            _token_locks.pop(token, None)
    overflow = len(_tokens) - 5000
    if overflow > 0:
        for token in sorted(_tokens, key=lambda t: _tokens[t][0])[:overflow]:
            _tokens.pop(token, None)
            _token_locks.pop(token, None)


def _mint(payload: dict) -> str:
    _prune_tokens()
    token = secrets.token_urlsafe(18)
    _tokens[token] = (time.time(), dict(payload))
    return token


def _gb(size: int) -> str:
    return f"{size / 1e9:.2f} GB" if size else "unknown size"


def _resolution_label(*values) -> str:
    """Resolution text players can see without having to parse a filename."""
    match = _RES_RE.search(" ".join(str(value or "") for value in values))
    return f"{match.group(1)}p" if match else "unknown quality"


def fallback_streams(media: str, media_id: str,
                     found: list[dict]) -> list[dict]:
    """Turn inert search results into explicit click-to-download stream rows."""
    out = []
    for cand in found:
        token = _mint({"mode": "candidate", **cand})
        indexer = _indexer_name(cand.get("indexer"))
        resolution = _resolution_label(cand.get("title"))
        kind = ("single-season pack" if cand.get("kind") == "season"
                else "individual episode" if cand.get("kind") == "episode"
                else "whole-series pack" if cand.get("kind") == "series"
                else "movie")
        detail = (f"{kind} · {_gb(cand.get('size') or 0)} · "
                  f"{cand.get('seeders', 0)} seeders")
        action = ("Progressively streams the selected file through rqbit, "
                  "then hands the same files to qBittorrent for completion "
                  "and permanent seeding."
                  if progressive_enabled() else
                  "The complete torrent downloads through qBittorrent and "
                  "seeds indefinitely.")
        out.append({
            "name": (f"🔒 {indexer} · {resolution} · "
                     "Click to Download & Stream"),
            "title": (f"Private tracker: {indexer}\n{detail}\n{cand['title']}\n"
                      f"Starts only when opened; {action}"),
            "description": (f"Source: private-p2p\nPrivate tracker: {indexer}\n"
                            f"{detail}\n{cand['title']}"),
            "url": f"{PUBLIC_URL}/private/{token}",
            "behaviorHints": {"filename": cand["title"]},
            "_private_action": True,
            "_private_tracker": True,
            "_source_key": "private-p2p",
        })
    if out:
        telemetry.record_private_tracker(
            "shown", media_id=media_id, count=len(out))
    return out


async def choice_streams(media: str, media_id: str,
                         wait: float | None = None) -> list[dict]:
    """Manual alternatives that remain visible beside a local private row."""
    found = await candidates(media, media_id, wait=wait)
    return fallback_streams(media, media_id, found) if found else []


# ── qBittorrent API ─────────────────────────────────────────────────────────

async def _qbit_login(force: bool = False) -> None:
    global _qbit_authenticated
    if _qbit_authenticated and not force:
        return
    async with _qbit_auth_lock:
        if _qbit_authenticated and not force:
            return
        response = await _qbit.post(
            f"{QBIT_URL}/api/v2/auth/login",
            data={"username": QBIT_USERNAME, "password": QBIT_PASSWORD},
            timeout=10)
        # Some qBittorrent 5.x builds return 204 instead of the documented 200.
        if (response.status_code not in (200, 204)
                or (response.status_code == 200
                    and response.text.strip().lower() not in ("", "ok.", "ok"))):
            raise RuntimeError(f"qBittorrent login HTTP {response.status_code}")
        _qbit_authenticated = True


async def _qapi(method: str, path: str, **kwargs) -> httpx.Response:
    await _qbit_login()
    response = await _qbit.request(
        method, f"{QBIT_URL}/api/v2{path}", **kwargs)
    if response.status_code in (401, 403):
        await response.aclose()
        await _qbit_login(force=True)
        response = await _qbit.request(
            method, f"{QBIT_URL}/api/v2{path}", **kwargs)
    response.raise_for_status()
    return response


def _rqbit_auth() -> httpx.BasicAuth | None:
    if not RQBIT_USERNAME:
        return None
    return httpx.BasicAuth(RQBIT_USERNAME, RQBIT_PASSWORD)


async def _ensure_rqbit_vpn(*, force: bool = False) -> None:
    """Fail closed unless Gluetun reports that rqbit's tunnel is running.

    Gluetun's firewall is the actual network kill switch. This independent
    control-server check prevents Stream Picker from even starting or
    refocusing a private download while that kill switch is blocking traffic.
    A short successful-result cache avoids a control request for every player
    range request; failures are never cached.
    """
    global _rqbit_vpn_checked_at
    if not RQBIT_VPN_URL or not RQBIT_VPN_API_KEY:
        raise RuntimeError("rqbit VPN health check is not configured")
    if not force and time.monotonic() - _rqbit_vpn_checked_at < 10:
        return
    async with _rqbit_vpn_lock:
        if not force and time.monotonic() - _rqbit_vpn_checked_at < 10:
            return
        try:
            response = await _vpn.get(
                f"{RQBIT_VPN_URL}/v1/vpn/status",
                headers={"X-API-Key": RQBIT_VPN_API_KEY})
            response.raise_for_status()
            payload = response.json()
            status = (str(payload.get("status") or "").strip().lower()
                      if isinstance(payload, dict) else "")
            if status != "running":
                raise RuntimeError("rqbit VPN is not running")
        except BaseException:
            _rqbit_vpn_checked_at = 0.0
            raise
        _rqbit_vpn_checked_at = time.monotonic()


async def _rapi(method: str, path: str, **kwargs) -> httpx.Response:
    """Call the dedicated rqbit instance without exposing its credentials."""
    kwargs.setdefault("auth", _rqbit_auth())
    response = await _rqbit.request(method, f"{RQBIT_URL}{path}", **kwargs)
    response.raise_for_status()
    return response


async def _rqbit_torrents(*, with_stats: bool = False) -> list[dict]:
    response = await _rapi(
        "GET", "/torrents",
        params={"with_stats": "true"} if with_stats else None)
    payload = response.json()
    rows = payload.get("torrents") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


async def _rqbit_details(info_hash: str) -> dict | None:
    try:
        response = await _rapi("GET", f"/torrents/{info_hash}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _rqbit_file_rows(details: dict, stats: dict | None = None) -> list[dict]:
    """Normalize rqbit's file schema to the rows used by the release picker."""
    progress = (stats or {}).get("file_progress")
    if not isinstance(progress, list):
        progress = []
    rows = []
    for index, raw in enumerate(details.get("files") or []):
        if not isinstance(raw, dict):
            continue
        size = int(raw.get("length") or 0)
        done = int(progress[index] or 0) if index < len(progress) else 0
        rows.append({
            "index": index,
            "name": str(raw.get("name") or ""),
            "size": size,
            "progress": min(1.0, done / size) if size > 0 else 0.0,
            "included": bool(raw.get("included", True)),
        })
    return rows


async def _rqbit_list_metainfo(torrent: bytes) -> dict:
    response = await _rapi(
        "POST", "/torrents",
        params={"list_only": "true", "output_folder": RQBIT_OUTPUT_PATH},
        content=torrent, headers={"Content-Type": "application/octet-stream"},
        timeout=45)
    payload = response.json()
    details = payload.get("details") if isinstance(payload, dict) else None
    if not isinstance(details, dict):
        raise RuntimeError("rqbit returned invalid torrent metadata")
    return details


async def _rqbit_add(torrent: bytes, selected: int) -> dict:
    await _ensure_rqbit_vpn(force=True)
    response = await _rapi(
        "POST", "/torrents",
        params={"overwrite": "true", "only_files": str(selected),
                "output_folder": RQBIT_OUTPUT_PATH},
        content=torrent, headers={"Content-Type": "application/octet-stream"},
        timeout=120)
    payload = response.json()
    details = payload.get("details") if isinstance(payload, dict) else None
    if not isinstance(details, dict):
        raise RuntimeError("rqbit rejected torrent")
    info_hash = str(details.get("info_hash") or "").lower()
    if not info_hash:
        raise RuntimeError("rqbit did not return an info hash")
    # Adding an already-managed torrent leaves its prior file selection intact.
    # Make focus mode explicit and idempotent for repeated episode clicks.
    await _rapi(
        "POST", f"/torrents/{info_hash}/update_only_files",
        json={"only_files": [selected]})
    await _rqbit_start(info_hash)
    return details


async def _rqbit_stats(info_hash: str) -> dict:
    response = await _rapi("GET", f"/torrents/{info_hash}/stats/v1")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def _rqbit_start(info_hash: str) -> None:
    """Start rqbit, treating its already-running HTTP 400 as success.

    A newly added rqbit torrent starts automatically. Calling ``/start`` while
    it is still initializing returns HTTP 400 even though the download is live.
    Confirm observable torrent state before accepting that response so genuine
    start failures remain fatal.
    """
    try:
        await _rapi("POST", f"/torrents/{info_hash}/start")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 400:
            raise
        stats = await _rqbit_stats(info_hash)
        state = str(stats.get("state") or "").strip().lower()
        if state not in ("initializing", "live") and not bool(
                stats.get("finished")):
            raise


async def _rqbit_stop(info_hash: str) -> None:
    try:
        await _rapi("POST", f"/torrents/{info_hash}/pause")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 400:
            raise
        stats = await _rqbit_stats(info_hash)
        state = str(stats.get("state") or "").strip().lower()
        if state != "paused" and not bool(stats.get("finished")):
            raise


async def _rqbit_forget(info_hash: str) -> None:
    await _rapi("POST", f"/torrents/{info_hash}/forget")


async def _ensure_category() -> None:
    response = await _qapi("GET", "/torrents/categories")
    categories = response.json()
    existing = categories.get(CATEGORY)
    if existing is not None:
        # Category already present. Only try to correct its save path when it
        # actually differs: some qBittorrent builds (seen on 5.1.2) return
        # 409 "Unable to edit category" for a no-op editCategory, which would
        # otherwise fail every activation after the first (the create path).
        if str(existing.get("savePath") or "").rstrip("/") == QBIT_SAVE_PATH:
            return
        try:
            await _qapi("POST", "/torrents/editCategory",
                        data={"category": CATEGORY, "savePath": QBIT_SAVE_PATH})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            logger.warning("private trackers: could not edit category %r save "
                           "path (HTTP 409); keeping existing", CATEGORY)
        return
    await _qapi("POST", "/torrents/createCategory",
                data={"category": CATEGORY, "savePath": QBIT_SAVE_PATH})


def _skip_bencode(data: bytes, pos: int, depth: int = 0) -> int:
    if depth > 100 or pos >= len(data):
        raise ValueError("invalid torrent metainfo")
    token = data[pos:pos + 1]
    if token == b"i":
        end = data.find(b"e", pos + 1)
        if end < 0:
            raise ValueError("invalid torrent integer")
        int(data[pos + 1:end])
        return end + 1
    if token in (b"l", b"d"):
        pos += 1
        while pos < len(data) and data[pos:pos + 1] != b"e":
            if token == b"d":
                pos = _skip_bencode(data, pos, depth + 1)
            pos = _skip_bencode(data, pos, depth + 1)
        if pos >= len(data):
            raise ValueError("unterminated torrent collection")
        return pos + 1
    if token.isdigit():
        colon = data.find(b":", pos)
        if colon < 0 or colon - pos > 20:
            raise ValueError("invalid torrent string")
        length = int(data[pos:colon])
        end = colon + 1 + length
        if end > len(data):
            raise ValueError("truncated torrent string")
        return end
    raise ValueError("invalid torrent token")


def torrent_info_hash(data: bytes) -> str:
    """SHA-1 of the raw top-level ``info`` dictionary (BitTorrent v1 hash)."""
    if len(data) > 32 * 1024 * 1024 or not data.startswith(b"d"):
        raise ValueError("invalid or oversized torrent file")
    pos = 1
    while pos < len(data) and data[pos:pos + 1] != b"e":
        key_start = pos
        pos = _skip_bencode(data, pos)
        colon = data.find(b":", key_start, pos)
        if colon < 0:
            raise ValueError("invalid torrent key")
        key = data[colon + 1:pos]
        value_start = pos
        pos = _skip_bencode(data, pos)
        if key == b"info":
            return hashlib.sha1(data[value_start:pos]).hexdigest()
    raise ValueError("torrent has no info dictionary")


def _prowlarr_download_url(raw: str) -> str:
    source = urlsplit(raw)
    base = urlsplit(PROWLARR_URL)
    if not source.path.startswith("/"):
        return urljoin(PROWLARR_URL + "/", raw)
    # Prowlarr commonly returns its browser-facing host (localhost). Preserve
    # only its path/query and route it through the configured internal origin.
    return urlunsplit((base.scheme, base.netloc, source.path,
                       source.query, ""))


async def _torrent_bytes(download_url: str) -> bytes:
    url = _prowlarr_download_url(download_url)
    prowlarr_host = urlsplit(PROWLARR_URL).netloc
    headers = {"X-Api-Key": PROWLARR_API_KEY}
    response = None
    for _ in range(4):
        response = await _client.get(
            url, headers=headers, timeout=30, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        target = urljoin(url, response.headers.get("location") or "")
        await response.aclose()
        if not target:
            raise RuntimeError("Prowlarr returned an empty torrent redirect")
        url = target
        # Never forward the private Prowlarr API key to a tracker/CDN. Its
        # signed/passkey-bearing redirect URL is sufficient for that hop.
        headers = ({"X-Api-Key": PROWLARR_API_KEY}
                   if urlsplit(url).netloc == prowlarr_host else {})
    assert response is not None
    response.raise_for_status()
    data = response.content
    if not data or len(data) > 32 * 1024 * 1024:
        raise RuntimeError("Prowlarr returned an invalid torrent file")
    return data


async def _torrent_info(info_hash: str) -> dict | None:
    response = await _qapi(
        "GET", "/torrents/info", params={"hashes": info_hash})
    rows = response.json()
    return rows[0] if rows else None


async def _active_download_count() -> int:
    response = await _qapi(
        "GET", "/torrents/info", params={"category": CATEGORY})
    rows = response.json()
    return sum(1 for row in rows
               if float(row.get("progress") or 0) < 0.999999)


async def _files(info_hash: str) -> list[dict]:
    response = await _qapi(
        "GET", "/torrents/files", params={"hash": info_hash})
    rows = response.json()
    return rows if isinstance(rows, list) else []


def _pick_file(files: list[dict], media: str,
               season: int | None, episode: int | None) -> dict | None:
    videos = [f for f in files
              if str(f.get("name") or "").lower().endswith(_VIDEO_EXT)]
    if not videos:
        return None
    if media != "movie" and season is not None and episode is not None:
        exact = [f for f in videos if usenet._episode_match(
            str(f.get("name") or ""), season, episode)]
        if not exact:
            return None
        return max(exact, key=lambda f: int(f.get("size") or 0))
    return max(videos, key=lambda f: int(f.get("size") or 0))


async def _start_qbit(info_hash: str) -> None:
    try:
        await _qapi("POST", "/torrents/start", data={"hashes": info_hash})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        await _qapi("POST", "/torrents/resume", data={"hashes": info_hash})


async def _stop_qbit(info_hash: str) -> None:
    try:
        await _qapi("POST", "/torrents/stop", data={"hashes": info_hash})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        await _qapi("POST", "/torrents/pause", data={"hashes": info_hash})


async def _prioritize_all(info_hash: str, files: list[dict], selected: int,
                          *, start: bool = True) -> None:
    # The clicked file always gets maximal priority (7). This is a best-effort
    # compatibility hint on the qBittorrent-only path; progressive mode reaches
    # this function only after rqbit has completed the selected file.
    #
    # WHOLE_TORRENT (default): every other file stays at normal priority (1),
    # so the complete release downloads to 100% and seeds (no hit-and-run).
    # When OFF: every other file is set to 0/skip, so only the clicked episode
    # downloads and the release stays partial.
    all_ids = "|".join(
        str(int(f.get("index", i))) for i, f in enumerate(files))
    if all_ids:
        await _qapi("POST", "/torrents/filePrio",
                    data={"hash": info_hash, "id": all_ids,
                          "priority": "1" if WHOLE_TORRENT else "0"})
    await _qapi("POST", "/torrents/filePrio",
                data={"hash": info_hash, "id": str(selected), "priority": "7"})
    # Enforce the operator's "seed forever" decision per torrent rather than
    # relying on global qBittorrent preferences that could later be changed.
    await _qapi("POST", "/torrents/setShareLimits",
                data={"hashes": info_hash, "ratioLimit": "-1",
                      "seedingTimeLimit": "-1",
                      "inactiveSeedingTimeLimit": "-1"})
    if start:
        await _start_qbit(info_hash)


async def _wait_for_files(info_hash: str, timeout: float = 45) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            files = await _files(info_hash)
        except httpx.HTTPStatusError as exc:
            # qBittorrent accepts /torrents/add before the new hash is always
            # visible to /torrents/files. That brief registration gap is a
            # retryable 404/409, not a failed torrent.
            if exc.response.status_code not in (404, 409):
                raise
            files = []
        if files:
            return files
        await asyncio.sleep(0.5)
    raise RuntimeError("qBittorrent did not load torrent metadata")


async def _wait_for_torrent(info_hash: str, timeout: float = 5) -> dict | None:
    """Allow an asynchronously accepted qBittorrent add to become visible."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        existing = await _torrent_info(info_hash)
        if existing is not None:
            return existing
        await asyncio.sleep(0.25)
    return None


async def _add_qbit_stopped(info_hash: str, torrent: bytes,
                            indexer: str = "") -> dict | None:
    """Register metainfo with qBittorrent without letting it write yet."""
    await _ensure_category()
    existing = await _torrent_info(info_hash)
    if existing is None:
        response = await _qapi(
            "POST", "/torrents/add",
            data={"savepath": QBIT_SAVE_PATH, "category": CATEGORY,
                  "tags": _indexer_tag(indexer),
                  "paused": "true", "stopped": "true",
                  # rqbit writes metainfo file paths directly below its output
                  # folder. Match that layout so qBittorrent can hash the same
                  # bytes in place instead of looking below a release folder.
                  "contentLayout": "NoSubfolder"},
            files={"torrents": ("private.torrent", torrent,
                                 "application/x-bittorrent")},
            timeout=45)
        if response.text.strip().lower() not in ("", "ok.", "ok"):
            existing = await _wait_for_torrent(info_hash)
            if existing is None:
                raise RuntimeError("qBittorrent rejected torrent")
    # Existing hashes may have been added outside this lane. Bring them under
    # the dedicated category as part of the handoff so status/recovery queries
    # continue to find them. This does not start the torrent.
    await _qapi(
        "POST", "/torrents/setCategory",
        data={"hashes": info_hash, "category": CATEGORY})
    await _qapi(
        "POST", "/torrents/addTags",
        data={"hashes": info_hash, "tags": _indexer_tag(indexer)})
    return existing


async def _rqbit_active_download_count() -> int:
    if not progressive_enabled():
        return 0
    rows = await _rqbit_torrents(with_stats=True)
    return sum(
        1 for row in rows
        if isinstance(row.get("stats"), dict)
        and not bool(row["stats"].get("finished")))


def _qbit_was_running(info: dict | None) -> bool:
    state = str((info or {}).get("state") or "").lower()
    return bool(info and not state.startswith(("stopped", "paused")))


async def _activate_rqbit_candidate(payload: dict, torrent: bytes,
                                    info_hash: str) -> dict:
    """Focus rqbit on the selected file and leave qBittorrent stopped."""
    listed = await _rqbit_list_metainfo(torrent)
    files = _rqbit_file_rows(listed)
    pick = _pick_file(files, payload["media"], payload.get("season"),
                      payload.get("episode"))
    if not pick:
        raise RuntimeError("torrent does not contain the requested video")
    selected = int(pick.get("index", files.index(pick)))
    existing: dict | None = None
    resume_qbit_on_error = False
    async with _activation_lock:
        existing = await _torrent_info(info_hash)
        if (existing is None and MAX_ACTIVE_DOWNLOADS
                and (await _active_download_count()
                     + await _rqbit_active_download_count())
                >= MAX_ACTIVE_DOWNLOADS):
            telemetry.record_private_tracker(
                "capacity", media_id=payload.get("media_id", ""),
                detail="active-download-limit")
            raise RuntimeError("private download capacity is currently full")
        # Only one client may write these shared files at a time. Existing
        # qBittorrent releases are stopped while rqbit fills the watched file.
        resume_qbit_on_error = _qbit_was_running(existing)
        if existing is not None:
            await _stop_qbit(info_hash)
        else:
            # Persist the metainfo in the long-term client before rqbit starts.
            # rqbit intentionally has no API for exporting a managed .torrent,
            # so registering qBittorrent stopped here makes the later zero-copy
            # handoff reliable across addon/rqbit restarts. qBittorrent remains
            # stopped and cannot compete for or write any pieces.
            await _add_qbit_stopped(
                info_hash, torrent, payload.get("indexer", ""))
        try:
            added = await _rqbit_add(torrent, selected)
        except BaseException:
            if resume_qbit_on_error:
                await _start_qbit(info_hash)
            raise
        added_hash = str(added.get("info_hash") or "").lower()
        if added_hash != info_hash:
            # Never leave an unexpected private torrent running merely because
            # an upstream client returned inconsistent metadata.
            if added_hash:
                try:
                    await _rqbit_stop(added_hash)
                    await _rqbit_forget(added_hash)
                except Exception:
                    logger.warning("private trackers: could not discard "
                                   "mismatched rqbit torrent %s", added_hash)
            if resume_qbit_on_error:
                await _start_qbit(info_hash)
            raise RuntimeError("rqbit returned a mismatched torrent")
    payload.update({
        "mode": "rqbit", "hash": info_hash, "file_index": selected,
        "file_name": str(pick.get("name") or ""),
        "file_size": int(pick.get("size") or 0),
        "_rqbit_prepared": True,
    })
    payload.pop("_candidate_torrent", None)
    payload.pop("_candidate_hash", None)
    telemetry.record_private_tracker(
        "added", media_id=payload.get("media_id", ""),
        bytes_total=sum(int(f.get("size") or 0) for f in files),
        detail=f"{payload.get('kind', '')}:rqbit")
    _watch_rqbit_completion(
        info_hash, selected, int(pick.get("size") or 0),
        payload.get("media_id", ""), payload.get("indexer", ""))
    _notify_picker(payload.get("media_id", ""))
    return payload


async def _wait_qbit_rechecked(info_hash: str, selected: int,
                               timeout: float = 1800) -> list[dict]:
    """Wait until qBittorrent trusts rqbit's completed selected file."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = await _torrent_info(info_hash)
        if info is None:
            raise RuntimeError("qBittorrent lost torrent during recheck")
        files = await _files(info_hash)
        pick = next((f for f in files
                     if int(f.get("index", -1)) == selected), None)
        checking = "checking" in str(info.get("state") or "").lower()
        if (not checking and pick is not None
                and float(pick.get("progress") or 0) >= 0.999999):
            return files
        await asyncio.sleep(1)
    raise TimeoutError("qBittorrent did not finish checking the streamed file")


def _promote_rqbit_tokens(info_hash: str) -> None:
    """Future range requests use qBittorrent after the zero-copy handoff."""
    for _, payload in _tokens.values():
        if (payload.get("mode") == "rqbit"
                and str(payload.get("hash") or "").lower() == info_hash):
            payload["mode"] = "existing"


async def _handoff_rqbit(info_hash: str, selected: int, media_id: str,
                         indexer: str) -> None:
    """Pause rqbit, recheck the shared files, and start permanent qBit seeding."""
    await _rqbit_stop(info_hash)
    details = await _rqbit_details(info_hash)
    if details is None:
        return
    existing = await _torrent_info(info_hash)
    if existing is None:
        raise RuntimeError(
            "qBittorrent lost the torrent registered for rqbit handoff")
    if existing is not None:
        current_files = await _files(info_hash)
        current_pick = next(
            (f for f in current_files
             if int(f.get("index", -1)) == selected), None)
        if (current_pick is not None
                and float(current_pick.get("progress") or 0) >= 0.999999
                and "checking" not in str(existing.get("state") or "").lower()
                and _qbit_was_running(existing)):
            await _qapi(
                "POST", "/torrents/setCategory",
                data={"hashes": info_hash, "category": CATEGORY})
            await _qapi(
                "POST", "/torrents/addTags",
                data={"hashes": info_hash, "tags": _indexer_tag(indexer)})
            await _prioritize_all(
                info_hash, current_files, selected, start=False)
            _promote_rqbit_tokens(info_hash)
            await asyncio.sleep(2)
            await _rqbit_forget(info_hash)
            return

    await _stop_qbit(info_hash)
    files = await _wait_for_files(info_hash)
    await _prioritize_all(info_hash, files, selected, start=False)
    await _qapi("POST", "/torrents/recheck", data={"hashes": info_hash})
    await _wait_qbit_rechecked(info_hash, selected)
    await _start_qbit(info_hash)
    _promote_rqbit_tokens(info_hash)
    telemetry.record_private_tracker(
        "handoff", media_id=media_id,
        bytes_total=int((details.get("files") or [{}])[selected].get(
            "length") or 0) if selected < len(details.get("files") or []) else 0,
        detail="rqbit-to-qbittorrent")
    _notify_picker(media_id)
    # Give already-issued rqbit HTTP responses time to acquire their file
    # handle. New requests have been promoted to qBittorrent above.
    await asyncio.sleep(2)
    await _rqbit_forget(info_hash)


def _watch_rqbit_completion(info_hash: str, selected: int, size: int,
                            media_id: str, indexer: str = "") -> None:
    prior = _rqbit_tasks.get(info_hash)
    if (prior and not prior.done()
            and _rqbit_watch_selection.get(info_hash) == selected):
        return
    if prior and not prior.done():
        prior.cancel()
    generation = _rqbit_generation.get(info_hash, 0) + 1
    _rqbit_generation[info_hash] = generation
    _rqbit_watch_selection[info_hash] = selected

    async def watch() -> None:
        try:
            while True:
                stats = await _rqbit_stats(info_hash)
                progress = stats.get("file_progress")
                done = (int(progress[selected] or 0)
                        if isinstance(progress, list)
                        and selected < len(progress) else 0)
                if (size > 0 and done >= size) or bool(stats.get("finished")):
                    telemetry.record_private_tracker(
                        "complete", media_id=media_id, bytes_total=size,
                        detail="rqbit-selected-file")
                    await _handoff_rqbit(
                        info_hash, selected, media_id, indexer)
                    return
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                logger.warning("private trackers: rqbit completion watcher "
                               "failed: HTTP %s", exc.response.status_code)
        except Exception:
            logger.warning("private trackers: rqbit completion watcher failed",
                           exc_info=True)
        finally:
            if _rqbit_generation.get(info_hash) == generation:
                _rqbit_tasks.pop(info_hash, None)
                _rqbit_watch_selection.pop(info_hash, None)

    _rqbit_tasks[info_hash] = asyncio.create_task(watch())


async def _activate_candidate(payload: dict) -> dict:
    telemetry.record_private_tracker(
        "clicked", media_id=payload.get("media_id", ""),
        detail=payload.get("kind", ""))
    # Persist the hash into this memory-only capability as soon as the signed
    # Prowlarr handoff succeeds. If qBittorrent needs a moment to register the
    # add, player retries continue from the known hash instead of consuming the
    # same possibly one-shot tracker URL again.
    info_hash = str(payload.get("_candidate_hash") or "").lower()
    torrent = payload.get("_candidate_torrent")
    if not isinstance(torrent, bytes) or not torrent:
        torrent = await _torrent_bytes(payload["download_url"])
        payload["_candidate_torrent"] = torrent
    if not info_hash:
        info_hash = torrent_info_hash(torrent)
        payload["_candidate_hash"] = info_hash
    if progressive_enabled():
        return await _activate_rqbit_candidate(
            payload, torrent, info_hash)
    # Serialize only the admission check and add. Different player clicks can
    # otherwise all observe one free slot and overrun the configured cap.
    async with _activation_lock:
        await _ensure_category()
        existing = await _torrent_info(info_hash)
        if existing is None:
            if (MAX_ACTIVE_DOWNLOADS
                    and await _active_download_count() >= MAX_ACTIVE_DOWNLOADS):
                telemetry.record_private_tracker(
                    "capacity", media_id=payload.get("media_id", ""),
                    detail="active-download-limit")
                raise RuntimeError(
                    "private download capacity is currently full")
            # Add STOPPED so no bytes move until file priorities are set. For a
            # season pack this is what keeps the *clicked* episode leading:
            # otherwise sequential download would start at piece 0 (the first
            # episode of the pack) before _prioritize_all runs. The whole
            # torrent still completes to 100% and seeds — only the ORDER is
            # steered. _prioritize_all starts it once the selected file is 7.
            # ("paused" for qBit 4.x, "stopped" for 5.x — send both.)
            response = await _qapi(
                "POST", "/torrents/add",
                data={"savepath": QBIT_SAVE_PATH, "category": CATEGORY,
                      "tags": _indexer_tag(payload.get("indexer")),
                      "paused": "true", "stopped": "true",
                      "sequentialDownload": "true",
                      "firstLastPiecePrio": "true"},
                files={"torrents": ("private.torrent", torrent,
                                     "application/x-bittorrent")},
                timeout=45)
            if response.text.strip().lower() not in ("", "ok.", "ok"):
                # Some builds report a duplicate/failed add while the accepted
                # torrent is already entering their registry. Trust observable
                # state over the ambiguous response text.
                existing = await _wait_for_torrent(info_hash)
                if existing is None:
                    raise RuntimeError("qBittorrent rejected torrent")
        # Also backfill provenance when the hash was already present (for
        # example another candidate from a different search found the same
        # release). addTags is idempotent and never changes download state.
        await _qapi(
            "POST", "/torrents/addTags",
            data={"hashes": info_hash,
                  "tags": _indexer_tag(payload.get("indexer"))})
    files = await _wait_for_files(info_hash)
    pick = _pick_file(files, payload["media"], payload.get("season"),
                      payload.get("episode"))
    if not pick:
        raise RuntimeError("torrent does not contain the requested video")
    selected = int(pick.get("index", files.index(pick)))
    await _prioritize_all(info_hash, files, selected)
    payload.update({"mode": "existing", "hash": info_hash,
                    "file_index": selected, "file_name": pick.get("name") or "",
                    "file_size": int(pick.get("size") or 0)})
    payload.pop("_candidate_torrent", None)
    payload.pop("_candidate_hash", None)
    telemetry.record_private_tracker(
        "added", media_id=payload.get("media_id", ""),
        bytes_total=sum(int(f.get("size") or 0) for f in files),
        detail=payload.get("kind", ""))
    _watch_completion(info_hash, payload.get("media_id", ""))
    _notify_picker(payload.get("media_id", ""))
    return payload


async def _prepare_existing(payload: dict) -> tuple[dict, list[dict], dict]:
    info_hash = str(payload["hash"]).lower()
    info = await _torrent_info(info_hash)
    if info is None:
        raise RuntimeError("torrent is no longer in qBittorrent")
    files = await _files(info_hash)
    selected = int(payload.get("file_index", -1))
    pick = next((f for f in files if int(f.get("index", -2)) == selected), None)
    if pick is None:
        pick = _pick_file(files, payload["media"], payload.get("season"),
                          payload.get("episode"))
        if not pick:
            raise RuntimeError("requested video is no longer in torrent")
        selected = int(pick.get("index", files.index(pick)))
        payload["file_index"] = selected
    await _prioritize_all(info_hash, files, selected)
    payload["file_name"] = str(pick.get("name") or "")
    payload["file_size"] = int(pick.get("size") or 0)
    _watch_completion(info_hash, payload.get("media_id", ""))
    return info, files, pick


def _safe_local_path(torrent_name: str, file_name: str) -> Path:
    root = DOWNLOAD_ROOT.resolve()
    options = [root / file_name, root / torrent_name / file_name]
    safe: list[Path] = []
    for option in options:
        resolved = option.resolve()
        if resolved == root or root not in resolved.parents:
            continue
        safe.append(resolved)
        if resolved.exists():
            return resolved
    if not safe:
        raise RuntimeError("unsafe torrent path")
    return safe[0]


def _file_offset(files: list[dict], selected: int) -> int:
    offset = 0
    for i, row in enumerate(files):
        index = int(row.get("index", i))
        if index == selected:
            return offset
        offset += int(row.get("size") or 0)
    raise RuntimeError("torrent file index missing")


def _parse_range(value: str, size: int) -> tuple[int, int, int]:
    if not value:
        return 0, max(0, size - 1), 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip(), re.I)
    if not match or "," in value or size <= 0:
        raise HTTPException(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
    elif right:
        length = min(int(right), size)
        start, end = size - length, size - 1
    else:
        raise HTTPException(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
    if start < 0 or start >= size or end < start:
        raise HTTPException(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
    return start, end, 206


async def _pieces_ready(info_hash: str, first: int, last: int) -> bool:
    response = await _qapi(
        "GET", "/torrents/pieceStates", params={"hash": info_hash})
    states = response.json()
    return bool(states and last < len(states)
                and all(int(states[i]) == 2 for i in range(first, last + 1)))


async def _wait_available(info_hash: str, absolute_start: int,
                          absolute_end: int, piece_size: int,
                          deadline: float) -> None:
    first = absolute_start // piece_size
    last = absolute_end // piece_size
    while time.monotonic() < deadline:
        if await _pieces_ready(info_hash, first, last):
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("requested torrent pieces are not available yet")


async def _body(path: Path, info_hash: str, file_offset: int,
                piece_size: int, start: int, end: int, complete: bool):
    pos = start
    while pos <= end:
        final = min(end, pos + 4 * 1024 * 1024 - 1)
        if not complete:
            await _wait_available(
                info_hash, file_offset + pos, file_offset + final, piece_size,
                time.monotonic() + START_TIMEOUT)
        while not path.exists():
            await asyncio.sleep(0.25)

        def read() -> bytes:
            with path.open("rb") as handle:
                handle.seek(pos)
                return handle.read(final - pos + 1)

        chunk = await asyncio.to_thread(read)
        if not chunk:
            await asyncio.sleep(0.25)
            continue
        pos += len(chunk)
        yield chunk


async def _wait_path(path: Path, deadline: float) -> None:
    while time.monotonic() < deadline:
        if path.is_file():
            return
        await asyncio.sleep(0.25)
    raise TimeoutError("torrent file is not visible on the read-only mount")


async def _prepare_rqbit(payload: dict) -> None:
    """Focus an existing rqbit torrent when a local-result row is opened."""
    await _ensure_rqbit_vpn()
    info_hash = str(payload.get("hash") or "").lower()
    selected = int(payload.get("file_index", -1))
    size = int(payload.get("file_size") or 0)
    if not info_hash or selected < 0 or size <= 0:
        raise RuntimeError("rqbit stream metadata is incomplete")
    if not payload.get("_rqbit_prepared"):
        qinfo = await _torrent_info(info_hash)
        resume_qbit_on_error = _qbit_was_running(qinfo)
        if qinfo is not None:
            await _stop_qbit(info_hash)
        try:
            await _rapi(
                "POST", f"/torrents/{info_hash}/update_only_files",
                json={"only_files": [selected]})
            await _rqbit_start(info_hash)
        except BaseException:
            if resume_qbit_on_error:
                await _start_qbit(info_hash)
            raise
        payload["_rqbit_prepared"] = True
    _watch_rqbit_completion(
        info_hash, selected, size, payload.get("media_id", ""),
        payload.get("indexer", ""))


async def _serve_rqbit(payload: dict, request: Request):
    """Reverse-proxy rqbit's range stream through the opaque addon URL."""
    size = int(payload.get("file_size") or 0)
    raw_range = request.headers.get("range", "")
    headers: dict[str, str] = {}
    if raw_range:
        start, end, _ = _parse_range(raw_range, size)
        # rqbit 8.x accepts only an open-ended ``bytes=start-`` range. Supplying
        # an explicit end makes it ignore the header and return the whole file.
        # Normalize suffix and bounded player ranges to rqbit's supported form;
        # the player can close the response once it has enough bytes.
        headers["Range"] = f"bytes={start}-"
    url = (f"{RQBIT_URL}/torrents/{payload['hash']}/stream/"
           f"{int(payload['file_index'])}")
    upstream_request = _rqbit.build_request("GET", url, headers=headers)
    upstream = await _rqbit.send(
        upstream_request, stream=True, auth=_rqbit_auth(),
        follow_redirects=False)
    if upstream.status_code >= 400:
        await upstream.aread()
        status = upstream.status_code
        await upstream.aclose()
        raise RuntimeError(f"rqbit stream HTTP {status}")

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()

    outgoing = {
        "Accept-Ranges": "bytes", "Cache-Control": "no-store",
        "Content-Disposition": ("inline; filename*=UTF-8''" +
                                quote(Path(str(payload.get("file_name")
                                                    or "video.mkv")).name)),
    }
    for key in ("content-length", "content-range", "content-type"):
        if key in upstream.headers:
            outgoing[key.title()] = upstream.headers[key]
    telemetry.record_private_tracker(
        "play", media_id=payload.get("media_id", ""), bytes_total=size,
        detail="rqbit-progressive")
    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=outgoing,
        media_type=None)


async def serve(token: str, request: Request):
    """Activate on GET, then range-stream the requested file as pieces land."""
    if request.method == "HEAD":
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    entry = _tokens.get(token)
    if not entry or time.time() - entry[0] > TOKEN_TTL:
        raise HTTPException(status_code=404)
    payload = entry[1]
    lock = _token_locks.setdefault(token, asyncio.Lock())
    try:
        async with lock:
            if payload.get("mode") == "candidate":
                payload = await _activate_candidate(payload)
                _tokens[token] = (time.time(), payload)
            if payload.get("mode") == "rqbit":
                await _prepare_rqbit(payload)
                return await _serve_rqbit(payload, request)
            info, files, pick = await _prepare_existing(payload)
        size = int(pick.get("size") or payload.get("file_size") or 0)
        if size <= 0:
            raise RuntimeError("torrent file size is unknown")
        start, end, status = _parse_range(request.headers.get("range", ""), size)
        props = await _qapi(
            "GET", "/torrents/properties", params={"hash": payload["hash"]})
        piece_size = int(props.json().get("piece_size") or 0)
        if piece_size <= 0:
            raise RuntimeError("qBittorrent piece size is unavailable")
        file_offset = _file_offset(files, int(payload["file_index"]))
        path = _safe_local_path(str(info.get("name") or ""),
                                str(pick.get("name") or ""))
        complete = float(pick.get("progress") or 0) >= 0.999999
        # Before returning media headers, prove the opening range is actually
        # available. On timeout qBittorrent keeps downloading the full release;
        # a retry of the same link can then start immediately.
        if not complete:
            await _wait_available(
                payload["hash"], file_offset + start,
                file_offset + min(end, start + 1024 * 1024 - 1), piece_size,
                time.monotonic() + START_TIMEOUT)
        await _wait_path(path, time.monotonic() + START_TIMEOUT)
        headers = {
            "Accept-Ranges": "bytes", "Cache-Control": "no-store",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": ("inline; filename*=UTF-8''" +
                                    quote(Path(str(pick.get('name') or
                                                   'video.mkv')).name)),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        media_type = mimetypes.guess_type(str(pick.get("name") or ""))[0] \
            or "application/octet-stream"
        telemetry.record_private_tracker(
            "play", media_id=payload.get("media_id", ""),
            bytes_total=size, detail="complete" if complete else "downloading")
        return StreamingResponse(
            _body(path, payload["hash"], file_offset, piece_size,
                  start, end, complete),
            status_code=status, headers=headers, media_type=media_type)
    except HTTPException:
        raise
    except TimeoutError as exc:
        telemetry.record_private_tracker(
            "start_wait", media_id=payload.get("media_id", ""),
            detail="pieces-unavailable")
        raise HTTPException(
            status_code=503,
            detail="Torrent is still downloading its opening pieces; retry shortly",
            headers={"Retry-After": "15"}) from exc
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTPStatusError HTTP {exc.response.status_code}"
        elif isinstance(exc, RuntimeError):
            detail = f"RuntimeError: {str(exc)[:100]}"
        else:
            detail = type(exc).__name__
        telemetry.record_private_tracker(
            "start_failed", media_id=payload.get("media_id", ""),
            detail=detail)
        logger.warning("private trackers: playback activation failed: %s",
                       detail)
        raise HTTPException(status_code=502,
                            detail="Private torrent could not be started") from exc


def _notify_picker(media_id: str) -> None:
    if not media_id:
        return
    try:
        from app import picker
        picker.private_source_changed(media_id)
    except Exception:
        logger.debug("private trackers: picker invalidation failed", exc_info=True)


def _watch_completion(info_hash: str, media_id: str) -> None:
    task = _completion_tasks.get(info_hash)
    if task and not task.done():
        return

    async def watch() -> None:
        try:
            while True:
                info = await _torrent_info(info_hash)
                if info is None:
                    return
                if float(info.get("progress") or 0) >= 0.999999:
                    telemetry.record_private_tracker(
                        "complete", media_id=media_id,
                        bytes_total=int(info.get("total_size") or 0))
                    _notify_picker(media_id)
                    return
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("private trackers: completion watcher failed",
                         exc_info=True)
        finally:
            _completion_tasks.pop(info_hash, None)

    _completion_tasks[info_hash] = asyncio.create_task(watch())


async def _reconcile_rqbit() -> None:
    """Recover selected-file handoffs after an addon restart or transient error."""
    if not progressive_enabled():
        return
    for row in await _rqbit_torrents(with_stats=True):
        info_hash = str(row.get("info_hash") or "").lower()
        if not info_hash:
            continue
        details = await _rqbit_details(info_hash)
        if details is None:
            continue
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        files = _rqbit_file_rows(details, stats)
        included = [
            f for f in files
            if f.get("included")
            and str(f.get("name") or "").lower().endswith(_VIDEO_EXT)]
        if not included:
            continue
        selected_file = max(included, key=lambda f: int(f.get("size") or 0))
        selected = int(selected_file["index"])
        _watch_rqbit_completion(
            info_hash, selected, int(selected_file.get("size") or 0), "")


async def startup() -> None:
    global _reconcile_task
    if not enabled() or not progressive_enabled():
        return
    if _reconcile_task and not _reconcile_task.done():
        return

    async def loop() -> None:
        while True:
            try:
                await _reconcile_rqbit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("private trackers: rqbit recovery scan failed",
                               exc_info=True)
            await asyncio.sleep(30)

    _reconcile_task = asyncio.create_task(loop())


# ── already-added release reuse ─────────────────────────────────────────────

async def _rqbit_local_streams(
        media: str, media_id: str, titles: list[str],
        coords: tuple[int, int] | None) -> tuple[list[dict], list[dict], set[str]]:
    if not progressive_enabled():
        return [], [], set()
    complete: list[dict] = []
    pending: list[dict] = []
    managed: set[str] = set()
    try:
        torrents = await _rqbit_torrents(with_stats=True)
        for row in torrents[:100]:
            info_hash = str(row.get("info_hash") or "").lower()
            torrent_name = str(row.get("name") or "")
            if not info_hash or not any(
                    usenet._release_title_match(torrent_name, title)
                    for title in titles if title):
                continue
            kind = "movie"
            if coords:
                kind = classify_release(torrent_name, *coords)
                if not kind:
                    continue
            details = await _rqbit_details(info_hash)
            if details is None:
                continue
            stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
            files = _rqbit_file_rows(details, stats)
            pick = _pick_file(
                files, media, coords[0] if coords else None,
                coords[1] if coords else None)
            if not pick:
                continue
            managed.add(info_hash)
            selected = int(pick.get("index", files.index(pick)))
            progress = float(pick.get("progress") or 0)
            is_complete = progress >= 0.999999
            qinfo = await _torrent_info(info_hash)
            indexer = _indexer_from_tags((qinfo or {}).get("tags"))
            source = indexer or "Private tracker"
            label = ("Downloaded" if is_complete else
                     f"Progressive download {progress * 100:.0f}%")
            payload = {
                "mode": "rqbit", "hash": info_hash,
                "file_index": selected,
                "file_name": str(pick.get("name") or ""),
                "file_size": int(pick.get("size") or 0), "media": media,
                "media_id": media_id, "season": coords[0] if coords else None,
                "episode": coords[1] if coords else None, "kind": kind,
                "indexer": indexer,
                "_rqbit_prepared": bool(pick.get("included")),
            }
            token = _mint(payload)
            resolution = _resolution_label(torrent_name, pick.get("name"))
            stream = {
                "name": (f"🔒 {source} Progressive · {resolution} · {label}"),
                "title": (f"Private tracker: {source}\n{torrent_name}\n"
                          f"{pick.get('name') or ''}\n"
                          "rqbit prioritizes the bytes being watched; "
                          "qBittorrent takes over for permanent seeding."),
                "description": (f"Source: private-p2p\n"
                                f"Private tracker: {source}\n"
                                f"Size: {_gb(int(pick.get('size') or 0))}\n"
                                f"{torrent_name}"),
                "url": f"{PUBLIC_URL}/private/{token}",
                "behaviorHints": {
                    "filename": str(pick.get("name") or ""),
                    "videoSize": int(pick.get("size") or 0),
                },
                "_private_action": not is_complete,
                "_private_tracker": True,
                "_source_key": "private-p2p",
            }
            (complete if is_complete else pending).append(stream)
    except Exception as exc:
        logger.info("private trackers: rqbit local reuse unavailable: %s",
                    type(exc).__name__)
    return complete, pending, managed


async def local_streams(media: str, media_id: str) -> tuple[list[dict], list[dict]]:
    """Completed episode/movie files and still-downloading reusable members."""
    if not enabled():
        return [], []
    try:
        titles, _ = await usenet._expected_info(media, media_id)
        coords = _episode_coords(media, media_id)
        complete, pending, rqbit_hashes = await _rqbit_local_streams(
            media, media_id, titles, coords)
        response = await _qapi(
            "GET", "/torrents/info", params={"category": CATEGORY})
        torrents = response.json()
        for info in torrents[:200]:
            if str(info.get("hash") or "").lower() in rqbit_hashes:
                # rqbit is the sole writer until its selected-file handoff.
                continue
            torrent_name = str(info.get("name") or "")
            if not any(usenet._release_title_match(torrent_name, title)
                       for title in titles if title):
                continue
            kind = "movie"
            if coords:
                kind = classify_release(torrent_name, *coords)
                # A season pack's name matches every episode; an individual
                # torrent only matches the episode it was acquired for.
                if not kind:
                    continue
            info_hash = str(info.get("hash") or "").lower()
            files = await _files(info_hash)
            pick = _pick_file(files, media,
                              coords[0] if coords else None,
                              coords[1] if coords else None)
            if not pick:
                continue
            index = int(pick.get("index", files.index(pick)))
            payload = {
                "mode": "existing", "hash": info_hash,
                "file_index": index, "file_name": str(pick.get("name") or ""),
                "file_size": int(pick.get("size") or 0), "media": media,
                "media_id": media_id, "season": coords[0] if coords else None,
                "episode": coords[1] if coords else None, "kind": kind,
            }
            token = _mint(payload)
            progress = float(pick.get("progress") or 0)
            is_complete = progress >= 0.999999
            label = ("Downloaded" if is_complete else
                     f"Downloading {progress * 100:.0f}%")
            resolution = _resolution_label(torrent_name, pick.get("name"))
            indexer = _indexer_from_tags(info.get("tags"))
            source = indexer or "Unknown private tracker"
            stream = {
                "name": (f"🔒 {source} Local · {resolution} · "
                         f"{label}"),
                "title": (f"Private tracker: {source}\n{torrent_name}\n"
                          f"{pick.get('name') or ''}\n"
                          "The complete torrent remains selected and seeds indefinitely."),
                "description": (f"Source: private-p2p\nPrivate tracker: {source}\nSize: "
                                f"{_gb(int(pick.get('size') or 0))}\n"
                                f"{torrent_name}"),
                "url": f"{PUBLIC_URL}/private/{token}",
                "behaviorHints": {
                    "filename": str(pick.get("name") or ""),
                    "videoSize": int(pick.get("size") or 0),
                },
                "_private_action": not is_complete,
                "_private_tracker": True,
                "_source_key": "private-p2p",
            }
            (complete if is_complete else pending).append(stream)
        return complete, pending
    except Exception as exc:
        logger.info("private trackers: local reuse unavailable: %s",
                    type(exc).__name__)
        return [], []


async def status() -> dict:
    """Admin-safe connection/download status; never returns credentials."""
    base = {
        "enabled": enabled(), "configured": bool(
            PROWLARR_URL and PROWLARR_API_KEY and QBIT_URL and QBIT_PASSWORD
            and (not progressive_enabled()
                 or (RQBIT_URL and RQBIT_VPN_URL
                     and RQBIT_VPN_API_KEY))),
        "stream_engine": STREAM_ENGINE,
        "storage": (DOWNLOAD_ROOT.is_dir()
                    and os.access(DOWNLOAD_ROOT, os.R_OK)),
        "save_path": QBIT_SAVE_PATH, "download_root": str(DOWNLOAD_ROOT),
        "rqbit_output_path": RQBIT_OUTPUT_PATH,
        "category": CATEGORY, "searches_active": len(_search_tasks),
        "minimum_seeders": MIN_SEEDERS,
        "maximum_torrent_gb": MAX_TORRENT_GB,
        "maximum_active_downloads": MAX_ACTIVE_DOWNLOADS,
        "last_searches": [
            {"media_id": key[1], **value}
            for key, value in list(_search_outcomes.items())[-8:]],
    }
    if not enabled():
        return {**base, "prowlarr": False, "qbittorrent": False,
                "rqbit": False, "vpn": not progressive_enabled(),
                "downloads": []}
    try:
        indexers = await _private_torrent_indexers()
        base["prowlarr"] = True
        base["private_torrent_indexers"] = len(indexers)
    except Exception:
        base["prowlarr"] = False
        base["private_torrent_indexers"] = 0
    try:
        response = await _qapi(
            "GET", "/torrents/info", params={"category": CATEGORY})
        rows = response.json()
        base["qbittorrent"] = True
        qbit_downloads = [{
            "engine": "qBittorrent",
            "hash": str(row.get("hash") or "").lower(),
            "name": str(row.get("name") or "")[:160],
            "progress": round(float(row.get("progress") or 0) * 100, 1),
            "state": str(row.get("state") or "")[:40],
            "size": int(row.get("total_size") or 0),
            "download_speed": int(row.get("dlspeed") or 0),
            "upload_speed": int(row.get("upspeed") or 0),
            "ratio": round(float(row.get("ratio") or 0), 2),
        } for row in rows[:100]]
    except Exception:
        base["qbittorrent"] = False
        qbit_downloads = []
    rqbit_downloads: list[dict] = []
    rqbit_hashes: set[str] = set()
    if progressive_enabled():
        try:
            await _ensure_rqbit_vpn(force=True)
            base["vpn"] = True
        except Exception:
            base["vpn"] = False
        try:
            rows = await _rqbit_torrents(with_stats=True)
            base["rqbit"] = True
            for row in rows[:100]:
                stats = row.get("stats") \
                    if isinstance(row.get("stats"), dict) else {}
                total = int(stats.get("total_bytes") or 0)
                done = int(stats.get("progress_bytes") or 0)
                live = stats.get("live") \
                    if isinstance(stats.get("live"), dict) else {}
                down = live.get("download_speed") \
                    if isinstance(live.get("download_speed"), dict) else {}
                up = live.get("upload_speed") \
                    if isinstance(live.get("upload_speed"), dict) else {}
                info_hash = str(row.get("info_hash") or "").lower()
                rqbit_hashes.add(info_hash)
                rqbit_downloads.append({
                    "engine": "rqbit", "hash": info_hash,
                    "name": str(row.get("name") or "")[:160],
                    "progress": round(done / total * 100, 1) if total else 0.0,
                    "state": str(stats.get("state") or "")[:40],
                    "size": total,
                    "download_speed": int(float(down.get("mbps") or 0)
                                          * 1024 * 1024),
                    "upload_speed": int(float(up.get("mbps") or 0)
                                        * 1024 * 1024),
                    "ratio": round(int(stats.get("uploaded_bytes") or 0)
                                   / max(1, done), 2),
                })
        except Exception:
            base["rqbit"] = False
    else:
        base["rqbit"] = True
        base["vpn"] = True
    base["downloads"] = rqbit_downloads + [
        row for row in qbit_downloads if row["hash"] not in rqbit_hashes]
    return base


async def test_connections(values: dict) -> dict:
    """Test pending UI values without ever echoing a submitted credential."""
    purl = str(values.get("PRIVATE_PROWLARR_URL") or PROWLARR_URL).rstrip("/")
    pkey = str(values.get("PRIVATE_PROWLARR_API_KEY") or PROWLARR_API_KEY)
    qurl = str(values.get("PRIVATE_QBITTORRENT_URL") or QBIT_URL).rstrip("/")
    quser = str(values.get("PRIVATE_QBITTORRENT_USERNAME") or QBIT_USERNAME)
    qpass = str(values.get("PRIVATE_QBITTORRENT_PASSWORD") or QBIT_PASSWORD)
    engine = str(values.get("PRIVATE_STREAM_ENGINE")
                 or STREAM_ENGINE).strip().lower()
    rqurl = str(values.get("PRIVATE_RQBIT_URL") or RQBIT_URL).rstrip("/")
    rquser = str(values.get("PRIVATE_RQBIT_USERNAME") or RQBIT_USERNAME)
    rqpass = str(values.get("PRIVATE_RQBIT_PASSWORD") or RQBIT_PASSWORD)
    vpnurl = str(values.get("PRIVATE_RQBIT_VPN_URL")
                 or RQBIT_VPN_URL).rstrip("/")
    vpnapikey = str(values.get("PRIVATE_RQBIT_VPN_API_KEY")
                    or RQBIT_VPN_API_KEY)
    download_root = Path(str(values.get("PRIVATE_TRACKER_DOWNLOAD_ROOT")
                             or DOWNLOAD_ROOT))
    storage_ok = (download_root.is_absolute() and download_root.is_dir()
                  and os.access(download_root, os.R_OK))
    result = {"prowlarr": {"ok": False, "detail": "not configured"},
              "qbittorrent": {"ok": False, "detail": "not configured"},
              "rqbit": {"ok": engine != "rqbit",
                        "detail": ("not selected" if engine != "rqbit"
                                   else "not configured")},
              "vpn": {"ok": engine != "rqbit",
                      "detail": ("not selected" if engine != "rqbit"
                                 else "not configured")},
              "storage": {"ok": storage_ok,
                          "detail": (f"{download_root} is readable"
                                     if storage_ok else
                                     f"{download_root} is not mounted/readable")}}
    if purl and pkey:
        try:
            response = await _client.get(
                f"{purl}/api/v1/indexer", headers={"X-Api-Key": pkey},
                timeout=10)
            response.raise_for_status()
            rows = response.json()
            count = sum(1 for row in rows if row.get("enable", True)
                        and str(row.get("protocol") or "").lower() == "torrent"
                        and str(row.get("privacy") or "").lower() == "private")
            result["prowlarr"] = {
                "ok": count > 0,
                "detail": f"{count} enabled private torrent indexers"}
        except Exception as exc:
            result["prowlarr"] = {"ok": False,
                                  "detail": type(exc).__name__}
    if qurl and qpass:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                login = await client.post(
                    f"{qurl}/api/v2/auth/login",
                    data={"username": quser, "password": qpass})
                if (login.status_code not in (200, 204)
                        or (login.status_code == 200
                            and login.text.strip().lower()
                            not in ("", "ok.", "ok"))):
                    raise RuntimeError(f"login HTTP {login.status_code}")
                prefs = await client.get(f"{qurl}/api/v2/app/preferences")
                prefs.raise_for_status()
            result["qbittorrent"] = {"ok": True,
                                      "detail": "authenticated"}
        except Exception as exc:
            result["qbittorrent"] = {"ok": False,
                                      "detail": type(exc).__name__}
    if engine == "rqbit" and vpnurl and vpnapikey:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{vpnurl}/v1/vpn/status",
                    headers={"X-API-Key": vpnapikey})
                response.raise_for_status()
                payload = response.json()
                status = (str(payload.get("status") or "").strip().lower()
                          if isinstance(payload, dict) else "")
                if status != "running":
                    raise RuntimeError("VPN is not running")
            result["vpn"] = {"ok": True,
                             "detail": "Gluetun tunnel running"}
        except Exception as exc:
            result["vpn"] = {"ok": False,
                             "detail": type(exc).__name__}
    if engine == "rqbit" and rqurl:
        try:
            auth = httpx.BasicAuth(rquser, rqpass) if rquser else None
            async with httpx.AsyncClient(timeout=10, auth=auth) as client:
                response = await client.get(f"{rqurl}/")
                response.raise_for_status()
                payload = response.json()
                if (not isinstance(payload, dict)
                        or payload.get("server") != "rqbit"):
                    raise RuntimeError("unexpected server")
            result["rqbit"] = {
                "ok": True,
                "detail": f"rqbit {payload.get('version') or 'authenticated'}"}
        except Exception as exc:
            result["rqbit"] = {"ok": False,
                               "detail": type(exc).__name__}
    result["ok"] = all(v["ok"] for v in result.values())
    return result


async def shutdown() -> None:
    tasks = (list(_search_tasks.values()) + list(_completion_tasks.values())
             + list(_rqbit_tasks.values()))
    if _reconcile_task is not None:
        tasks.append(_reconcile_task)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(
        _client.aclose(), _qbit.aclose(), _rqbit.aclose(), _vpn.aclose(),
        return_exceptions=True)
