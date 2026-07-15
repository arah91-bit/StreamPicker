"""TMDB availability checks — used only when a title turns up no good source, to
decide between two outcomes:

  * a digital release is confirmed out but we just couldn't find it  -> acquire
    it via Sonarr/Radarr;
  * no proper release exists yet (still theatrical / not aired)      -> show the
    "not available yet" notice and don't download anything.

Movies use TMDB release_dates (a type-4 Digital or type-5 Physical release, or a
theatrical release old enough that digital must exist by now). Series episodes
use the episode air_date. Everything degrades to None (unknown) on any error so
callers can fall back to the cam/telesync heuristic.
"""

import asyncio
import datetime
import logging
import os
import time

import httpx

from app import content_identity, omdb

logger = logging.getLogger("stream-picker")

TMDB_KEY = os.environ.get("TMDB_API_KEY") or None
# how long after theatrical a digital release is assumed to exist
DIGITAL_ASSUME_DAYS = int(os.environ.get("TMDB_DIGITAL_ASSUME_DAYS", "120"))

_client = httpx.AsyncClient(timeout=8, headers={"User-Agent": "stream-picker"})
_cache: dict[str, tuple[float, object]] = {}
_TTL = 6 * 3600
_NEG_TTL = 120


def _put_cache(key: str, value: object) -> None:
    _cache[key] = (time.monotonic(), value)
    if len(_cache) > 2000:
        _cache.pop(next(iter(_cache)))


def enabled() -> bool:
    return TMDB_KEY is not None


def _today() -> datetime.date:
    return datetime.date.today()


def _parse(d: str | None) -> datetime.date | None:
    if not d:
        return None
    try:
        return datetime.date.fromisoformat(d[:10])
    except ValueError:
        return None


async def _get(path: str, **params):
    params["api_key"] = TMDB_KEY
    r = await _client.get(f"https://api.themoviedb.org/3{path}", params=params)
    r.raise_for_status()
    return r.json()


async def _find(imdb_id: str, kind: str):
    """Return the TMDB id for an imdb id (kind: 'movie_results'|'tv_results')."""
    data = await _get(f"/find/{imdb_id}", external_source="imdb_id")
    results = data.get(kind) or []
    return results[0]["id"] if results else None


async def digital_released(imdb_id: str) -> bool | None:
    """True if a digital/physical release is out, False if not out yet, None if
    unknown. Cached."""
    if not TMDB_KEY:
        return None
    ck = f"movie:{imdb_id}"
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < (_TTL if hit[1] is not None else _NEG_TTL):
        return hit[1]
    result: bool | None = None
    try:
        tmdb_id = await _find(imdb_id, "movie_results")
        if tmdb_id is not None:
            data = await _get(f"/movie/{tmdb_id}/release_dates")
            today = _today()
            digital = False
            theatrical: datetime.date | None = None
            for country in data.get("results", []):
                for rel in country.get("release_dates", []):
                    d = _parse(rel.get("release_date"))
                    if d is None:
                        continue
                    if rel.get("type") in (4, 5) and d <= today:
                        digital = True
                    if rel.get("type") in (2, 3):
                        theatrical = d if theatrical is None else min(theatrical, d)
            if digital:
                result = True
            elif theatrical and (today - theatrical).days >= DIGITAL_ASSUME_DAYS:
                result = True          # old enough that digital must be out
            elif theatrical is not None:
                result = False         # recent/upcoming theatrical, no digital yet
    except Exception as e:
        logger.warning(f"tmdb digital_released {imdb_id}: {type(e).__name__}")
        result = None
    _put_cache(ck, result)
    return result


async def episode_aired(imdb_id: str, season: int, episode: int) -> bool | None:
    """True if the episode has aired, False if it's in the future, None if
    unknown."""
    if not TMDB_KEY:
        return None
    ck = f"tv:{imdb_id}:{season}:{episode}"
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < (_TTL if hit[1] is not None else _NEG_TTL):
        return hit[1]
    result: bool | None = None
    try:
        tmdb_id = await _find(imdb_id, "tv_results")
        if tmdb_id is not None:
            data = await _get(f"/tv/{tmdb_id}/season/{season}/episode/{episode}")
            air = _parse(data.get("air_date"))
            if air is not None:
                result = air <= _today()
    except Exception as e:
        logger.warning(f"tmdb episode_aired {imdb_id}: {type(e).__name__}")
        result = None
    _put_cache(ck, result)
    return result


async def has_release(media: str, media_id: str) -> bool | None:
    """Unified check: is a proper release expected to exist by now? True/False,
    or None if TMDB can't say."""
    parts = media_id.split(":")
    imdb = parts[0]
    if media == "movie":
        return await digital_released(imdb)
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return await episode_aired(imdb, int(parts[1]), int(parts[2]))
    return None


async def tmdb_id(media: str, imdb: str) -> int | None:
    """TMDB id for an imdb id — used to place Jellyseerr requests (TMDB-native)."""
    if not TMDB_KEY:
        return None
    try:
        return await _find(imdb, "movie_results" if media == "movie"
                           else "tv_results")
    except Exception as e:
        logger.warning(f"tmdb id {imdb}: {type(e).__name__}")
        return None


# ── next-episode resolution (season-boundary aware) ─────────────────────────
# TMDB is primary: the show record carries per-season episode counts in one
# call. TVDB is the fallback for the odd series TMDB doesn't know (v4 API:
# login token → imdb remote-id lookup → episode pages).
TVDB_KEY = os.environ.get("TVDB_API_KEY") or None
_TVDB = "https://api4.thetvdb.com/v4"
_tvdb_token: str | None = None
_seasons_cache: dict[str, tuple[float, dict[int, int] | None]] = {}


async def _tvdb_get(path: str, **params):
    """TVDB v4 GET with cached bearer token; re-logins once on a 401."""
    global _tvdb_token
    for attempt in (1, 2):
        if _tvdb_token is None:
            r = await _client.post(f"{_TVDB}/login", json={"apikey": TVDB_KEY})
            r.raise_for_status()
            _tvdb_token = r.json()["data"]["token"]
        r = await _client.get(f"{_TVDB}{path}", params=params,
                              headers={"Authorization": f"Bearer {_tvdb_token}"})
        if r.status_code == 401 and attempt == 1:
            _tvdb_token = None            # token expired — re-login and retry
            continue
        r.raise_for_status()
        return r.json()


async def _tvdb_season_counts(imdb: str) -> dict[int, int] | None:
    """{season_number: episode_count} for aired-order seasons via TVDB."""
    found = await _tvdb_get(f"/search/remoteid/{imdb}")
    series = next((d["series"] for d in (found.get("data") or [])
                   if d.get("series")), None)
    if not series:
        return None
    counts: dict[int, int] = {}
    for page in range(3):                 # 500 episodes/page; 3 covers ~any show
        d = await _tvdb_get(f"/series/{series['id']}/episodes/official",
                            page=page)
        eps = (d.get("data") or {}).get("episodes") or []
        for ep in eps:
            sn = ep.get("seasonNumber")
            if isinstance(sn, int) and sn > 0:
                counts[sn] = counts.get(sn, 0) + 1
        if not (d.get("links") or {}).get("next"):
            break
    return counts or None


async def _season_counts(imdb: str) -> dict[int, int] | None:
    """{season_number: episode_count} for a series, TMDB first then TVDB.
    Cached per imdb id; None when neither API knows the show."""
    cached = _seasons_cache.get(imdb)
    if cached and time.monotonic() - cached[0] < (
            _TTL if cached[1] is not None else _NEG_TTL):
        return cached[1]
    counts = None
    if TMDB_KEY:
        try:
            tid = await _find(imdb, "tv_results")
            if tid is not None:
                d = await _get(f"/tv/{tid}")
                counts = {s["season_number"]: s.get("episode_count") or 0
                          for s in d.get("seasons") or []
                          if s.get("season_number")}      # skip specials (S0)
        except Exception as e:
            logger.warning(f"tmdb seasons {imdb}: {type(e).__name__}")
    if not counts and TVDB_KEY:
        try:
            counts = await _tvdb_season_counts(imdb)
        except Exception as e:
            logger.warning(f"tvdb seasons {imdb}: {type(e).__name__}")
    _seasons_cache[imdb] = (time.monotonic(), counts)
    if len(_seasons_cache) > 1000:
        _seasons_cache.pop(next(iter(_seasons_cache)))
    return counts


async def next_episode(imdb: str, season: int, ep: int) -> tuple[int, int] | None:
    """(season, episode) following S{season}E{ep}, rolling a season finale over
    to the next season's first episode using TMDB/TVDB episode counts. Falls back
    to the naive within-season E+1 when neither API knows the show; returns None
    only when this is provably the last episode of the last season."""
    counts = await _season_counts(imdb)
    if not counts:
        return season, ep + 1
    n = counts.get(season)
    if not n or ep < n:
        return season, ep + 1
    for s2 in sorted(k for k in counts if k > season):
        if counts[s2] > 0:
            return s2, 1
    return None


_title_details_cache: dict[tuple[str, str], tuple[float, tuple[
    str | None, str | None, int | None, str | None]]] = {}
_title_details_inflight: dict[tuple[str, str], asyncio.Task] = {}
_identity_cache: dict[tuple[str, str], tuple[float, content_identity.IdentityProfile]] = {}
_identity_inflight: dict[tuple[str, str], asyncio.Task] = {}

_COUNTRY_REGION = {
    "united states": "us", "united states of america": "us", "usa": "us",
    "united kingdom": "uk", "great britain": "uk", "uk": "uk",
    "australia": "au", "canada": "ca", "japan": "jp",
    "south korea": "kr", "korea": "kr", "france": "fr",
    "germany": "de", "spain": "es", "italy": "it",
}


def _region_tags(countries: tuple[str, ...]) -> frozenset[str]:
    return frozenset(_COUNTRY_REGION[c.strip().lower()]
                     for c in countries
                     if c.strip().lower() in _COUNTRY_REGION)


async def _title_details(media: str, imdb: str
                         ) -> tuple[str | None, str | None, int | None, str | None]:
    """One shared TMDB /find call for title, native title, year and language."""
    base = imdb.split(":")[0]
    key = (media, base)
    cached = _title_details_cache.get(key)
    if cached and time.monotonic() - cached[0] < (
            _TTL if any(cached[1]) else _NEG_TTL):
        return cached[1]
    task = _title_details_inflight.get(key)
    if task is None:
        async def fetch():
            kind = "movie_results" if media == "movie" else "tv_results"
            try:
                data = await _get(f"/find/{base}", external_source="imdb_id")
                rows = data.get(kind) or []
                if not rows:
                    return None, None, None, None
                d = rows[0]
                title = d.get("title") or d.get("name")
                orig = d.get("original_title") or d.get("original_name")
                date = d.get("release_date") or d.get("first_air_date") or ""
                year = int(date[:4]) if date[:4].isdigit() else None
                lang = (d.get("original_language") or "").lower() or None
                result = title, orig, year, lang
                _title_details_cache[key] = (time.monotonic(), result)
                if len(_title_details_cache) > 1000:
                    _title_details_cache.pop(next(iter(_title_details_cache)))
                return result
            except Exception as e:
                logger.warning(f"tmdb title details {base}: {type(e).__name__}")
                return None, None, None, None

        task = asyncio.create_task(fetch())
        _title_details_inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _title_details_inflight.pop(k, None))
    return await asyncio.shield(task)


async def identity_profile(media: str, media_id: str
                           ) -> content_identity.IdentityProfile:
    """One shared authoritative identity profile for a picker request.

    TMDB supplies the original/native title and true original language; OMDb is
    an independent exact-IMDb corroborator for canonical title, year, country,
    and runtime.  For episodes OMDb's exact Season/Episode form supplies the
    episode runtime.  Both providers are cached/singleflight, so this function
    is called once per request identity, never per candidate.
    """
    parts = media_id.split(":")
    base = parts[0].lower()
    key = (media, media_id.lower())
    cached = _identity_cache.get(key)
    if cached and time.monotonic() - cached[0] < _TTL:
        return cached[1]
    task = _identity_inflight.get(key)
    if task is None:
        async def fetch():
            tmdb_result = (None, None, None, None)
            omdb_result = None
            episode_result = None
            jobs: list[tuple[str, asyncio.Future | asyncio.Task | object]] = []
            if TMDB_KEY:
                jobs.append(("tmdb", _title_details(media, base)))
            if omdb.enabled():
                jobs.append(("omdb", omdb.lookup(media, base)))
                if (media != "movie" and len(parts) == 3
                        and parts[1].isdigit() and parts[2].isdigit()):
                    jobs.append(("episode", omdb.lookup_episode(
                        base, int(parts[1]), int(parts[2]))))
            if jobs:
                values = await asyncio.gather(
                    *(job for _, job in jobs), return_exceptions=True)
                for (name, _), value in zip(jobs, values):
                    if isinstance(value, BaseException):
                        continue
                    if name == "tmdb":
                        tmdb_result = value
                    elif name == "omdb":
                        omdb_result = value
                    else:
                        episode_result = value

            title, original, tmdb_year, _lang = tmdb_result
            aliases = tuple(dict.fromkeys(
                value for value in (title, original,
                                    getattr(omdb_result, "title", None))
                if value))
            omdb_year = getattr(omdb_result, "year", None)
            years = frozenset(y for y in (tmdb_year, omdb_year)
                              if isinstance(y, int))
            # A one-year festival/theatrical discrepancy is ordinary. A larger
            # disagreement means metadata cannot by itself authorize #1; exact
            # per-item IMDb evidence can still resolve it in the classifier.
            conflict = bool(tmdb_year and omdb_year
                            and abs(int(tmdb_year) - int(omdb_year)) > 1)
            countries = tuple(getattr(omdb_result, "countries", ()) or ())
            season = episode = None
            if media != "movie" and len(parts) == 3:
                if parts[1].isdigit() and parts[2].isdigit():
                    season, episode = int(parts[1]), int(parts[2])
            # A series-level OMDb runtime is only a typical value. It must not
            # masquerade as exact episode evidence when runtime is being used to
            # disambiguate same-name content.
            runtime = (getattr(omdb_result, "runtime_seconds", None)
                       if media == "movie" or season is None
                       else getattr(episode_result, "runtime_seconds", None))
            profile = content_identity.IdentityProfile(
                media=media, imdb_id=base, aliases=aliases, years=years,
                season=season, episode=episode,
                region_tags=_region_tags(countries),
                metadata_conflict=conflict, runtime_seconds=runtime)
            _identity_cache[key] = (time.monotonic(), profile)
            if len(_identity_cache) > 2000:
                _identity_cache.pop(next(iter(_identity_cache)))
            return profile

        task = asyncio.create_task(fetch())
        _identity_inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _identity_inflight.pop(k, None))
    return await asyncio.shield(task)


async def expected_runtime(media: str, media_id: str) -> float | None:
    """OMDb's exact movie/episode runtime, or None for the existing fallback."""
    try:
        return (await identity_profile(media, media_id)).runtime_seconds
    except Exception:
        return None


async def original_language(media: str, imdb: str) -> str | None:
    """The title's original-audio language as an ISO-639-1 code ('en', 'it', 'ja',
    …) via TMDB, or None if unknown. Cached per imdb id. Used by the picker to
    keep a release whose audio is neither English nor the original language off
    the top of the list (e.g. an English film that only carries an Italian dub)."""
    if not TMDB_KEY:
        return None
    return (await _title_details(media, imdb))[3]


async def title_year(media: str, imdb: str
                     ) -> tuple[str | None, str | None, int | None]:
    """(English title, original/native title, year) for an exact IMDb id —
    used by the acquire fallback to search Sonarr/Radarr by title when the imdb
    id isn't in their metadata. The original name matters because TVDB often
    lists Asian dramas only under their native (Chinese/Korean/…) title. TMDB
    remains primary; quota-cached OMDb fills a missing canonical title/year."""
    if not TMDB_KEY and not omdb.enabled():
        return None, None, None
    tmdb_result = ((await _title_details(media, imdb))
                   if TMDB_KEY else (None, None, None, None))
    omdb_result = await omdb.lookup(media, imdb) if omdb.enabled() else None
    title, orig, year, _ = tmdb_result
    return (title or getattr(omdb_result, "title", None), orig,
            year or getattr(omdb_result, "year", None))


async def shutdown() -> None:
    for task in list(_title_details_inflight.values()) + list(_identity_inflight.values()):
        task.cancel()
    tasks = list(_title_details_inflight.values()) + list(_identity_inflight.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _title_details_inflight.clear()
    _identity_inflight.clear()
    await omdb.shutdown()
    await _client.aclose()
