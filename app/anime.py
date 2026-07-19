"""Anime-aware episode identity: reconcile absolute, seasonal and per-cour numbering.

Anime is the hard case for "is this file the episode I asked for?".  The same
episode carries different numbers depending on who is counting:

  * **Seasonal / aired order** — what Stremio shows via Cinemeta/TheTVDB:
    ``S03E13``.  This is the coordinate a picker request arrives in.
  * **Absolute** — what most fansub/scene releases use: ``Attack on Titan - 50``.
    A continuous count across the whole franchise.
  * **Per-cour** — MyAnimeList/Kitsu/AniDB treat each broadcast cour as its own
    entry numbered from 1, so "The Final Season Part 2 - 01" is also episode 50.

One TVDB season can be several MAL/Kitsu cours (AoT S3 = a 12-ep cour + a 10-ep
cour), and there is no published offset table, so the offsets are *computed*
from each cour's episode count.  This module builds that map and answers, for a
requested TVDB ``(season, episode)``, which release numbers legitimately refer
to it — so an absolute-numbered release can be positively confirmed instead of
left unverifiable, and a genuinely different episode can be contradicted.

Data sources, merged conservatively and all cached/optional so a pick never
blocks on them:
  * **Fribb/anime-lists** (``anime-list-full.json``) — the id + TVDB-season
    backbone: imdb/tvdb ↔ kitsu/mal/anidb, and which TVDB season each cour is.
  * **Kitsu** — reliable episode counts and per-episode titles (primary).
  * **Jikan/MyAnimeList** — the same, as corroboration; frequently rate-limited
    or down (503/504), so strictly best-effort and never required.

The layer degrades to the picker's ordinary filename identity when a title is
not anime, not mapped, or the sources are unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("ANIME_ENABLED", "1") not in ("0", "false", "")
JIKAN_ENABLED = os.environ.get("ANIME_JIKAN", "1") not in ("0", "false", "")
LISTS_URL = os.environ.get(
    "ANIME_LISTS_URL",
    "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json")
KITSU_BASE = os.environ.get("ANIME_KITSU_BASE", "https://kitsu.io/api/edge").rstrip("/")
JIKAN_BASE = os.environ.get("ANIME_JIKAN_BASE", "https://api.jikan.moe/v4").rstrip("/")
LISTS_TTL = float(os.environ.get("ANIME_LISTS_TTL_HOURS", "24")) * 3600
SHOW_TTL = float(os.environ.get("ANIME_META_TTL", "86400"))
NEG_TTL = float(os.environ.get("ANIME_NEG_TTL", "3600"))
TIMEOUT = float(os.environ.get("ANIME_TIMEOUT", "6"))

CONFIRM, CONTRADICT, NEUTRAL = "confirm", "contradict", "neutral"

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(connect=TIMEOUT, read=TIMEOUT, write=TIMEOUT, pool=TIMEOUT),
    headers={"User-Agent": "StreamPicker/1.0 (+anime-identity)"},
)

_IMDB_RE = re.compile(r"tt\d{5,12}")


# ── the Fribb id / TVDB-season backbone ──────────────────────────────────────

@dataclass(slots=True)
class _Entry:
    tvdb_id: int | None
    imdb_ids: tuple[str, ...]
    kitsu_id: int | None
    mal_id: int | None
    anidb_id: int | None
    tvdb_season: int | None
    kind: str


class _Lists:
    """Lazily downloaded, indexed and daily-refreshed anime-lists backbone."""

    def __init__(self) -> None:
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self.by_imdb: dict[str, list[_Entry]] = {}
        self.by_tvdb: dict[int, list[_Entry]] = {}
        self.by_kitsu: dict[int, _Entry] = {}
        self.by_mal: dict[int, _Entry] = {}

    def _ingest(self, raw: list[dict]) -> None:
        by_imdb: dict[str, list[_Entry]] = {}
        by_tvdb: dict[int, list[_Entry]] = {}
        by_kitsu: dict[int, _Entry] = {}
        by_mal: dict[int, _Entry] = {}
        for row in raw:
            imdb = row.get("imdb_id")
            imdb_ids = tuple(x for x in (
                imdb if isinstance(imdb, list) else [imdb])
                if isinstance(x, str) and _IMDB_RE.fullmatch(x))
            season = row.get("season")
            tvdb_season = season.get("tvdb") if isinstance(season, dict) else season
            entry = _Entry(
                tvdb_id=_as_int(row.get("tvdb_id")),
                imdb_ids=imdb_ids,
                kitsu_id=_as_int(row.get("kitsu_id")),
                mal_id=_as_int(row.get("mal_id")),
                anidb_id=_as_int(row.get("anidb_id")),
                tvdb_season=_as_int(tvdb_season),
                kind=str(row.get("type") or "").upper(),
            )
            for tt in imdb_ids:
                by_imdb.setdefault(tt, []).append(entry)
            if entry.tvdb_id is not None:
                by_tvdb.setdefault(entry.tvdb_id, []).append(entry)
            if entry.kitsu_id is not None:
                by_kitsu[entry.kitsu_id] = entry
            if entry.mal_id is not None:
                by_mal[entry.mal_id] = entry
        self.by_imdb, self.by_tvdb = by_imdb, by_tvdb
        self.by_kitsu, self.by_mal = by_kitsu, by_mal
        self._loaded_at = time.monotonic()
        logger.info("anime: indexed %d anime-lists entries (%d imdb, %d kitsu)",
                    len(raw), len(by_imdb), len(by_kitsu))

    async def ready(self) -> bool:
        if self._loaded_at and time.monotonic() - self._loaded_at < LISTS_TTL:
            return True
        async with self._lock:
            if self._loaded_at and time.monotonic() - self._loaded_at < LISTS_TTL:
                return True
            try:
                response = await _client.get(LISTS_URL, timeout=max(TIMEOUT, 30))
                response.raise_for_status()
                raw = response.json()
                if not isinstance(raw, list):
                    raise ValueError("anime-lists payload is not a list")
                await asyncio.to_thread(self._ingest, raw)
                return True
            except Exception as exc:
                if self._loaded_at:                 # keep serving the stale index
                    logger.warning("anime: anime-lists refresh failed (%s); "
                                   "using cached index", type(exc).__name__)
                    return True
                logger.warning("anime: anime-lists unavailable (%s)",
                               type(exc).__name__)
                return False


_lists = _Lists()


def _as_int(value) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ── a resolved show and one episode's expectation ────────────────────────────

@dataclass(slots=True)
class _Cour:
    kitsu_id: int | None
    mal_id: int | None
    tvdb_season: int
    count: int
    start: str = ""
    show_titles: tuple[str, ...] = ()
    titles: dict[int, tuple[str, ...]] = field(default_factory=dict)
    abs_start: int = 0        # absolute number of (this cour's ep 0)
    season_start: int = 0     # ep count of earlier cours sharing this TVDB season


@dataclass(slots=True)
class AnimeShow:
    tvdb_id: int | None
    imdb_id: str
    cours: list[_Cour]
    titles: tuple[str, ...]           # every cour's titles, for release matching

    def expectation(self, season: int, episode: int) -> "Expectation | None":
        for cour in self.cours:
            if (cour.tvdb_season == season
                    and cour.season_start < episode <= cour.season_start + cour.count):
                rel = episode - cour.season_start
                return Expectation(
                    season=season, episode=episode,
                    absolute=cour.abs_start + rel, relative=rel,
                    max_cour=max((c.count for c in self.cours), default=0),
                    total=sum(c.count for c in self.cours),
                    split_season=sum(1 for c in self.cours
                                     if c.tvdb_season == season) > 1,
                    titles=cour.titles.get(rel, ()),
                    show_titles=self.titles)
        return None


@dataclass(slots=True)
class Expectation:
    season: int
    episode: int
    absolute: int
    relative: int          # episode number within its own cour
    max_cour: int
    total: int
    split_season: bool     # more than one cour shares this TVDB season
    titles: tuple[str, ...]            # the requested episode's own titles
    show_titles: tuple[str, ...] = ()  # show titles (incl. romaji) for matching


# ── Kitsu / Jikan enrichment (episode counts and titles) ─────────────────────

async def _kitsu_json(path: str) -> dict:
    response = await _client.get(
        f"{KITSU_BASE}{path}",
        headers={"Accept": "application/vnd.api+json"}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


async def _kitsu_by_external(site: str, external_id: int) -> int | None:
    """Kitsu id for a myanimelist/anidb/thetvdb external id, or None."""
    try:
        payload = await _kitsu_json(
            f"/mappings?filter[externalSite]={site}"
            f"&filter[externalId]={external_id}&include=item")
    except Exception:
        return None
    for item in payload.get("included") or []:
        if str(item.get("type")) == "anime":
            return _as_int(item.get("id"))
    return None


async def _kitsu_anime(kitsu_id: int) -> tuple[int | None, str, tuple[str, ...]]:
    """(episode count, start date, titles) for one Kitsu anime entry."""
    try:
        payload = await _kitsu_json(f"/anime/{kitsu_id}")
    except Exception:
        return None, "", ()
    attrs = (payload.get("data") or {}).get("attributes") or {}
    names = [attrs.get("canonicalTitle")]
    for value in (attrs.get("titles") or {}).values():
        names.append(value)
    for value in attrs.get("abbreviatedTitles") or []:
        names.append(value)
    titles = tuple(dict.fromkeys(n for n in names if isinstance(n, str) and n))
    return _as_int(attrs.get("episodeCount")), str(attrs.get("startDate") or ""), titles


async def _kitsu_titles(kitsu_id: int) -> dict[int, tuple[str, ...]]:
    """Per-episode canonical/native titles keyed by the cour-relative number."""
    titles: dict[int, tuple[str, ...]] = {}
    offset = 0
    for _ in range(20):                              # ≤ 400 episodes, defensive
        try:
            payload = await _kitsu_json(
                f"/anime/{kitsu_id}/episodes"
                f"?page[limit]=20&page[offset]={offset}")
        except Exception:
            break
        rows = payload.get("data") or []
        for row in rows:
            attrs = row.get("attributes") or {}
            number = _as_int(attrs.get("relativeNumber")) or _as_int(attrs.get("number"))
            if number is None:
                continue
            names = [attrs.get("canonicalTitle")]
            for value in (attrs.get("titles") or {}).values():
                names.append(value)
            clean = tuple(dict.fromkeys(n for n in names if isinstance(n, str) and n))
            if clean:
                titles[number] = clean
        if len(rows) < 20:
            break
        offset += 20
    return titles


async def _jikan_count(mal_id: int) -> int | None:
    if not JIKAN_ENABLED:
        return None
    try:
        response = await _client.get(f"{JIKAN_BASE}/anime/{mal_id}", timeout=TIMEOUT)
        response.raise_for_status()
        return _as_int((response.json().get("data") or {}).get("episodes"))
    except Exception:
        return None


# ── show resolution ──────────────────────────────────────────────────────────

_show_cache: dict[str, tuple[float, AnimeShow | None]] = {}
_show_locks: dict[str, asyncio.Lock] = {}

# TV-like Fribb entry kinds that carry aired-order episodes (exclude MOVIE/OVA
# specials which live in TVDB season 0).
_SERIES_KINDS = {"TV", "ONA", "TV_SHORT", "TV SHORT", "UNKNOWN", ""}


def _title_set(*values: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v.strip() for v in values if v and v.strip()))


async def _cour_from_entry(entry: _Entry) -> _Cour | None:
    """Resolve one Fribb entry into a cour with a known episode count."""
    count: int | None = None
    start = ""
    titles: tuple[str, ...] = ()
    if entry.kitsu_id is not None:
        count, start, titles = await _kitsu_anime(entry.kitsu_id)
    if count is None and entry.mal_id is not None:
        count = await _jikan_count(entry.mal_id)
    if not count or count <= 0 or entry.tvdb_season is None:
        return None
    return _Cour(kitsu_id=entry.kitsu_id, mal_id=entry.mal_id,
                 tvdb_season=int(entry.tvdb_season), count=int(count),
                 start=start, show_titles=titles)


async def _build_show(entries: list[_Entry], imdb_id: str,
                      want_season: int | None) -> AnimeShow | None:
    tvdb_id = next((e.tvdb_id for e in entries if e.tvdb_id is not None), None)
    series = [e for e in entries
              if (e.tvdb_season or 0) >= 1 and e.kind in _SERIES_KINDS
              and (e.kitsu_id is not None or e.mal_id is not None)]
    if not series:
        return None
    cours = [c for c in await asyncio.gather(*(_cour_from_entry(e) for e in series))
             if c is not None]
    if not cours:
        return None
    # Chronological within (and across) TVDB seasons: air date, then id.
    cours.sort(key=lambda c: (c.tvdb_season, c.start or "9999",
                              c.kitsu_id or c.mal_id or 0))
    running_abs = 0
    per_season: dict[int, int] = {}
    for cour in cours:
        cour.abs_start = running_abs
        cour.season_start = per_season.get(cour.tvdb_season, 0)
        running_abs += cour.count
        per_season[cour.tvdb_season] = cour.season_start + cour.count
    # Episode titles only for the cour we actually need (bounded network work).
    if want_season is not None:
        for cour in cours:
            if cour.tvdb_season == want_season and cour.kitsu_id is not None:
                cour.titles = await _kitsu_titles(cour.kitsu_id)
    show_titles = tuple(dict.fromkeys(
        t for cour in cours for t in cour.show_titles))
    return AnimeShow(tvdb_id=tvdb_id, imdb_id=imdb_id, cours=cours,
                     titles=show_titles)


async def _resolve_entries(media_id: str) -> tuple[list[_Entry], str]:
    """The anime-lists entries and canonical IMDb id for a request id."""
    if not await _lists.ready():
        return [], ""
    base = media_id.split(":", 1)[0].strip().lower()
    if base.startswith("kitsu") or base.startswith("mal"):
        # A native anime id: kitsu:<id>[:ep] or mal:<id>[:ep].
        parts = media_id.split(":")
        key = _as_int(parts[1]) if len(parts) > 1 else None
        entry = (_lists.by_kitsu.get(key) if base.startswith("kitsu")
                 else _lists.by_mal.get(key)) if key is not None else None
        if entry is None:
            return [], ""
        imdb = entry.imdb_ids[0] if entry.imdb_ids else ""
        if entry.tvdb_id is not None:
            return list(_lists.by_tvdb.get(entry.tvdb_id, [entry])), imdb
        return [entry], imdb
    if _IMDB_RE.fullmatch(base):
        entries = _lists.by_imdb.get(base)
        if not entries:
            return [], base
        tvdb_id = next((e.tvdb_id for e in entries if e.tvdb_id is not None), None)
        if tvdb_id is not None:
            return list(_lists.by_tvdb.get(tvdb_id, entries)), base
        return entries, base
    return [], ""


async def resolve(media: str, media_id: str,
                  season: int | None = None) -> AnimeShow | None:
    """Return the anime mapping for a request, or None when it is not mapped
    anime.  Cached per show; failures are negatively cached briefly."""
    if not ENABLED or media == "movie":
        return None
    cache_key = f"{media_id.split(':', 1)[0].lower()}#{season}"
    hit = _show_cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < (SHOW_TTL if hit[1] else NEG_TTL):
        return hit[1]
    lock = _show_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        hit = _show_cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < (SHOW_TTL if hit[1] else NEG_TTL):
            return hit[1]
        try:
            entries, imdb = await _resolve_entries(media_id)
            show = await _build_show(entries, imdb, season) if entries else None
        except Exception as exc:
            logger.warning("anime: resolve failed for %s (%s)",
                           media_id, type(exc).__name__)
            show = None
        _show_cache[cache_key] = (time.monotonic(), show)
        if len(_show_cache) > 500:
            _show_cache.pop(next(iter(_show_cache)))
        _show_locks.pop(cache_key, None)
        return show


# ── release assessment ───────────────────────────────────────────────────────

_SXXEXX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:S0*(\d{1,3})[^A-Za-z0-9]*E0*(\d{1,4})|"
    r"0*(\d{1,3})x0*(\d{1,4}))(?!\d)", re.I)
# Anime absolute/episode number: a 1–4 digit run set off by a real episode
# marker ("- 099", "Ep 099", "E099", "#099") — never a bare space, so a number
# inside the title ("Kaiju No. 8", "Mob Psycho 100") is not read as the episode.
_ABS_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:-\s*|ep(?:isode)?[\s._]*|e|#)0*(\d{1,4})(?:v\d)?(?!\d)"
    r"(?=[\s._\-\[\](){}]|$)", re.I)
_STOP_ABS = re.compile(r"^(?:19|20)\d{2}$|^(?:480|540|576|720|1080|1440|2160|4320)$")


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _pairs(text: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for m in _SXXEXX_RE.finditer(text):
        out.add((int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))))
    return out


def _abs_numbers(text: str) -> set[int]:
    pruned = _SXXEXX_RE.sub(" ", text)          # never read S/E digits as absolute
    out: set[int] = set()
    for m in _ABS_RE.finditer(pruned):
        token = m.group(1)
        if _STOP_ABS.fullmatch(token):
            continue
        out.add(int(token))
    return out


def _title_hit(expected: tuple[str, ...], text: str) -> bool:
    folded = _fold(text)
    for title in expected:
        needle = _fold(title)
        # Require a reasonably specific title, matched whole, to avoid a common
        # word ("the", "two") triggering a false confirm.
        if len(needle) >= 8 and needle in folded:
            return True
    return False


def _compact(value: str) -> str:
    """Alphanumeric-only fold, so punctuation/spacing variation is harmless
    ('Shingeki no Kyojin' ↔ 'Shingeki.no.Kyojin', 'Re:Zero' ↔ 'ReZero')."""
    return "".join(re.findall(r"[^\W_]+", _fold(value), flags=re.UNICODE))


def title_present(titles: tuple[str, ...], text: str) -> bool:
    """Whether a show title appears anywhere in a release string, tolerant of a
    leading ``[group]`` prefix and punctuation.  Anchored to a specific show's
    titles (English + romaji + native), so it distinguishes shows rather than
    merely detecting "some anime"."""
    haystack = _compact(text)
    if not haystack:
        return False
    for title in titles:
        needle = _compact(title)
        if len(needle) >= 6 and needle in haystack:
            return True
    return False


def assess(exp: Expectation, text: str) -> str:
    """Classify one release string against an episode expectation.

    Generous on confirmation (any legitimate numbering that lands on the
    requested episode), conservative on contradiction (only an unambiguous
    different episode), neutral otherwise — the caller keeps its filename verdict
    for the neutral case, so the anime layer never *loses* existing information.
    """
    text = str(text or "")
    if not text.strip():
        return NEUTRAL
    pairs = _pairs(text)
    numbers = _abs_numbers(text)

    if ((exp.season, exp.episode) in pairs
            or exp.absolute in numbers
            or exp.relative in numbers
            or _title_hit(exp.titles, text)):
        return CONFIRM

    if pairs and (exp.season, exp.episode) not in pairs:
        # A SxxExx naming a *different* TVDB season is a different episode.
        if all(s != exp.season for s, _ in pairs):
            return CONTRADICT
        # Right season, wrong episode contradicts too — unless the season is
        # split across cours, where per-cour labels legitimately disagree with
        # TVDB numbering and a hard reject would drop correct files.
        if all(s == exp.season for s, _ in pairs) and not exp.split_season:
            return CONTRADICT

    # An unambiguous absolute index (larger than any single cour, so it cannot be
    # a cour-relative number) that is not the requested absolute is a different
    # episode.
    decisive = {n for n in numbers if n > exp.max_cour}
    if decisive and exp.absolute not in decisive:
        return CONTRADICT

    return NEUTRAL


async def shutdown() -> None:
    await _client.aclose()
