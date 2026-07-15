"""Quota-safe OMDb metadata lookups by exact IMDb id.

OMDb's free key is deliberately treated as a scarce corroboration source, not
as a search engine.  This module therefore has a narrow contract:

* works are identified only by ``i=<exact IMDb id>``; episodes add exact
  ``Season``/``Episode`` coordinates (never a title/search query);
* successful, normalized records persist across container rebuilds;
* concurrent fast/slow picker lookups share one request;
* an atomic UTC-day ledger stops network calls before the configured budget;
* expired positive records remain usable when refreshes fail.

Only identity fields useful to the picker are retained.  Raw OMDb responses,
request URLs, errors, and the API key never enter SQLite or logs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Callable

import httpx

logger = logging.getLogger("stream-picker")

API_URL = "https://www.omdbapi.com/"
API_KEY = (os.environ.get("OMDB_API_KEY") or "").strip()
DB_PATH = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"),
                       "omdb-cache.sqlite3")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


DAILY_BUDGET = _env_int("OMDB_DAILY_BUDGET", 750)
POSITIVE_TTL = 365 * 86400
NEGATIVE_TTL = 300
TIMEOUT = 8

_IMDB_RE = re.compile(r"^tt\d{5,12}$", re.I)
_YEAR_RE = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
_RUNTIME_RE = re.compile(r"(\d{1,4})\s*min\b", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True, slots=True)
class TitleRecord:
    """The small, trusted subset of one successful OMDb response."""

    imdb_id: str
    item_type: str                 # "movie" or "series"
    title: str
    year: int | None
    runtime_minutes: int | None
    countries: tuple[str, ...]
    languages: tuple[str, ...]

    @property
    def runtime_seconds(self) -> float | None:
        return self.runtime_minutes * 60.0 if self.runtime_minutes else None


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    """Validated metadata for one exact series/season/episode query."""

    series_imdb_id: str
    episode_imdb_id: str
    season: int
    episode: int
    title: str
    year: int | None
    runtime_minutes: int | None
    countries: tuple[str, ...]
    languages: tuple[str, ...]

    @property
    def runtime_seconds(self) -> float | None:
        return self.runtime_minutes * 60.0 if self.runtime_minutes else None


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    record: TitleRecord
    fetched_at: float


@dataclass(frozen=True, slots=True)
class _EpisodeCacheEntry:
    record: EpisodeRecord
    fetched_at: float


def _expected_type(media: str) -> str | None:
    value = (media or "").strip().lower()
    if value == "movie":
        return "movie"
    if value in ("series", "tv"):
        return "series"
    return None


def _base_imdb(imdb_id: str) -> str:
    # Stremio episode ids are series-imdb:season:episode.  OMDb still receives
    # only the exact base IMDb id; no title query is ever synthesized.
    return (imdb_id or "").split(":", 1)[0].strip().lower()


def _safe_text(value: object, limit: int) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text.upper() == "N/A" else text[:limit]


def _tuple_field(value: object) -> tuple[str, ...]:
    parts = []
    for raw in str(value or "").split(","):
        item = _safe_text(raw, 80)
        if item and item not in parts:
            parts.append(item)
        if len(parts) >= 16:
            break
    return tuple(parts)


def _parse_year(value: object) -> int | None:
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(1)) if match else None


def _parse_runtime(value: object) -> int | None:
    match = _RUNTIME_RE.search(str(value or ""))
    if not match:
        return None
    minutes = int(match.group(1))
    return minutes if 1 <= minutes <= 1440 else None


def _error_kind(value: object) -> str:
    """Classify an OMDb error without retaining its free-form text."""
    low = str(value or "").lower()
    if "limit" in low or "too many" in low:
        return "quota"
    if "api key" in low or "apikey" in low:
        return "auth"
    if "not found" in low or "incorrect imdb" in low:
        return "not-found"
    return "api-error"


def _parse_payload(payload: object, imdb_id: str,
                   expected_type: str) -> tuple[TitleRecord | None, str]:
    """Return ``(record, outcome)`` using allowlisted outcome names only."""
    if not isinstance(payload, dict):
        return None, "invalid-json-shape"
    if str(payload.get("Response") or "").lower() != "true":
        return None, _error_kind(payload.get("Error"))

    returned_id = _base_imdb(str(payload.get("imdbID") or ""))
    item_type = str(payload.get("Type") or "").strip().lower()
    if returned_id != imdb_id:
        return None, "imdb-mismatch"
    if item_type != expected_type:
        return None, "type-mismatch"
    title = _safe_text(payload.get("Title"), 300)
    if not title:
        return None, "missing-title"

    return TitleRecord(
        imdb_id=imdb_id,
        item_type=item_type,
        title=title,
        year=_parse_year(payload.get("Year")),
        runtime_minutes=_parse_runtime(payload.get("Runtime")),
        countries=_tuple_field(payload.get("Country")),
        languages=_tuple_field(payload.get("Language")),
    ), "ok"


def _parse_episode_payload(
        payload: object, series_imdb_id: str, season: int, episode: int,
        ) -> tuple[EpisodeRecord | None, str]:
    if not isinstance(payload, dict):
        return None, "invalid-json-shape"
    if str(payload.get("Response") or "").lower() != "true":
        return None, _error_kind(payload.get("Error"))

    returned_series = _base_imdb(str(payload.get("seriesID") or ""))
    returned_episode = _base_imdb(str(payload.get("imdbID") or ""))
    item_type = str(payload.get("Type") or "").strip().lower()
    try:
        returned_season = int(str(payload.get("Season") or ""))
        returned_number = int(str(payload.get("Episode") or ""))
    except (TypeError, ValueError):
        return None, "episode-number-mismatch"
    if returned_series != series_imdb_id:
        return None, "series-imdb-mismatch"
    if not _IMDB_RE.fullmatch(returned_episode):
        return None, "episode-imdb-mismatch"
    if item_type != "episode":
        return None, "type-mismatch"
    if returned_season != season or returned_number != episode:
        return None, "episode-number-mismatch"
    title = _safe_text(payload.get("Title"), 300)
    if not title:
        return None, "missing-title"

    return EpisodeRecord(
        series_imdb_id=series_imdb_id,
        episode_imdb_id=returned_episode,
        season=season,
        episode=episode,
        title=title,
        year=_parse_year(payload.get("Released") or payload.get("Year")),
        runtime_minutes=_parse_runtime(payload.get("Runtime")),
        countries=_tuple_field(payload.get("Country")),
        languages=_tuple_field(payload.get("Language")),
    ), "ok"


class OMDbProvider:
    """Persistent exact-ID provider with fail-closed quota accounting."""

    def __init__(
        self,
        api_key: str,
        *,
        path: str = DB_PATH,
        daily_budget: int = DAILY_BUDGET,
        positive_ttl: float = POSITIVE_TTL,
        negative_ttl: float = NEGATIVE_TTL,
        timeout: float = TIMEOUT,
        client=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.path = path
        self.daily_budget = max(0, int(daily_budget))
        self.positive_ttl = max(0.0, float(positive_ttl))
        self.negative_ttl = max(0.0, float(negative_ttl))
        self.timeout = max(1.0, float(timeout))
        self.clock = clock
        self._client = client
        self._owns_client = client is None
        self._db_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._negative: dict[tuple[str, str], float] = {}
        self._blocked_until = 0.0
        self._open()

    def _now(self) -> float:
        return float(self.clock())

    def _day(self, now: float | None = None) -> str:
        instant = self._now() if now is None else now
        return dt.datetime.fromtimestamp(instant, dt.timezone.utc).date().isoformat()

    def _open(self) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=2,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS title_cache (
                    imdb_id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    runtime_minutes INTEGER,
                    countries TEXT NOT NULL,
                    languages TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS title_cache_fetched
                    ON title_cache(fetched_at);
                CREATE TABLE IF NOT EXISTS episode_cache (
                    series_imdb_id TEXT NOT NULL,
                    season INTEGER NOT NULL,
                    episode INTEGER NOT NULL,
                    episode_imdb_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    runtime_minutes INTEGER,
                    countries TEXT NOT NULL,
                    languages TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (series_imdb_id,season,episode)
                );
                CREATE INDEX IF NOT EXISTS episode_cache_fetched
                    ON episode_cache(fetched_at);
                CREATE TABLE IF NOT EXISTS daily_quota (
                    utc_day TEXT PRIMARY KEY,
                    used INTEGER NOT NULL
                );
                """
            )
            conn.commit()
            self._conn = conn
            self._secure_files()
        except Exception:
            # No durable ledger means no safe way to promise the daily ceiling.
            # Cache/network access therefore fail closed, while the rest of the
            # picker can continue through TMDB/Cinemeta.
            logger.warning("omdb cache unavailable: %s",
                           "database-open-failed")
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            self._conn = None

    def _secure_files(self) -> None:
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                os.chmod(candidate, 0o600)
            except (FileNotFoundError, OSError):
                pass

    def _cached(self, imdb_id: str) -> _CacheEntry | None:
        with self._db_lock:
            try:
                if self._conn is None:
                    return None
                row = self._conn.execute(
                    "SELECT * FROM title_cache WHERE imdb_id=?", (imdb_id,)
                ).fetchone()
                if row is None:
                    return None
                record = TitleRecord(
                    imdb_id=str(row["imdb_id"]),
                    item_type=str(row["item_type"]),
                    title=str(row["title"]),
                    year=int(row["year"]) if row["year"] is not None else None,
                    runtime_minutes=(int(row["runtime_minutes"])
                                     if row["runtime_minutes"] is not None
                                     else None),
                    countries=tuple(json.loads(row["countries"])),
                    languages=tuple(json.loads(row["languages"])),
                )
                return _CacheEntry(record, float(row["fetched_at"]))
            except Exception:
                logger.warning("omdb cache read failed")
                return None

    def _cached_episode(self, series_imdb_id: str, season: int,
                        episode: int) -> _EpisodeCacheEntry | None:
        with self._db_lock:
            try:
                if self._conn is None:
                    return None
                row = self._conn.execute(
                    """SELECT * FROM episode_cache
                       WHERE series_imdb_id=? AND season=? AND episode=?""",
                    (series_imdb_id, season, episode),
                ).fetchone()
                if row is None:
                    return None
                record = EpisodeRecord(
                    series_imdb_id=str(row["series_imdb_id"]),
                    episode_imdb_id=str(row["episode_imdb_id"]),
                    season=int(row["season"]),
                    episode=int(row["episode"]),
                    title=str(row["title"]),
                    year=int(row["year"]) if row["year"] is not None else None,
                    runtime_minutes=(int(row["runtime_minutes"])
                                     if row["runtime_minutes"] is not None
                                     else None),
                    countries=tuple(json.loads(row["countries"])),
                    languages=tuple(json.loads(row["languages"])),
                )
                return _EpisodeCacheEntry(record, float(row["fetched_at"]))
            except Exception:
                logger.warning("omdb episode cache read failed")
                return None

    def _store(self, record: TitleRecord, fetched_at: float) -> None:
        with self._db_lock:
            try:
                if self._conn is None:
                    return
                self._conn.execute(
                    """INSERT INTO title_cache
                       (imdb_id,item_type,title,year,runtime_minutes,countries,
                        languages,fetched_at) VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(imdb_id) DO UPDATE SET
                         item_type=excluded.item_type,title=excluded.title,
                         year=excluded.year,runtime_minutes=excluded.runtime_minutes,
                         countries=excluded.countries,languages=excluded.languages,
                         fetched_at=excluded.fetched_at""",
                    (record.imdb_id, record.item_type, record.title, record.year,
                     record.runtime_minutes,
                     json.dumps(record.countries, separators=(",", ":")),
                     json.dumps(record.languages, separators=(",", ":")),
                     fetched_at),
                )
                self._conn.commit()
                self._secure_files()
            except Exception:
                if self._conn is not None:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                logger.warning("omdb cache write failed")

    def _store_episode(self, record: EpisodeRecord, fetched_at: float) -> None:
        with self._db_lock:
            try:
                if self._conn is None:
                    return
                self._conn.execute(
                    """INSERT INTO episode_cache
                       (series_imdb_id,season,episode,episode_imdb_id,title,year,
                        runtime_minutes,countries,languages,fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(series_imdb_id,season,episode) DO UPDATE SET
                         episode_imdb_id=excluded.episode_imdb_id,
                         title=excluded.title,year=excluded.year,
                         runtime_minutes=excluded.runtime_minutes,
                         countries=excluded.countries,languages=excluded.languages,
                         fetched_at=excluded.fetched_at""",
                    (record.series_imdb_id, record.season, record.episode,
                     record.episode_imdb_id, record.title, record.year,
                     record.runtime_minutes,
                     json.dumps(record.countries, separators=(",", ":")),
                     json.dumps(record.languages, separators=(",", ":")),
                     fetched_at),
                )
                self._conn.commit()
                self._secure_files()
            except Exception:
                if self._conn is not None:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                logger.warning("omdb episode cache write failed")

    def _reserve(self, now: float) -> bool:
        """Atomically reserve one request before it can reach OMDb."""
        with self._db_lock:
            conn = self._conn
            if conn is None or self.daily_budget <= 0:
                return False
            day = self._day(now)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT used FROM daily_quota WHERE utc_day=?", (day,)
                ).fetchone()
                used = int(row["used"]) if row is not None else 0
                if used >= self.daily_budget:
                    conn.rollback()
                    return False
                conn.execute(
                    """INSERT INTO daily_quota(utc_day,used) VALUES (?,1)
                       ON CONFLICT(utc_day) DO UPDATE SET used=used+1""", (day,))
                conn.commit()
                self._secure_files()
                return True
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                logger.warning("omdb quota ledger unavailable")
                return False

    def quota_status(self) -> dict:
        now = self._now()
        day = self._day(now)
        used = 0
        with self._db_lock:
            try:
                if self._conn is not None:
                    row = self._conn.execute(
                        "SELECT used FROM daily_quota WHERE utc_day=?", (day,)
                    ).fetchone()
                    used = int(row["used"]) if row is not None else 0
            except Exception:
                pass
        return {"utc_day": day, "used": used, "limit": self.daily_budget,
                "remaining": max(0, self.daily_budget - used)}

    def _client_for_request(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": "stream-picker/1.0"})
        return self._client

    def _reflects_api_key(self, record: TitleRecord | EpisodeRecord) -> bool:
        """Reject the unlikely case where an upstream field reflects the key."""
        if not self.api_key:
            return False
        selected = [record.title, *record.countries, *record.languages]
        return any(self.api_key in value for value in selected)

    async def lookup(self, media: str, imdb_id: str) -> TitleRecord | None:
        expected = _expected_type(media)
        base = _base_imdb(imdb_id)
        if expected is None or not _IMDB_RE.fullmatch(base):
            return None

        cached = self._cached(base)
        if cached and cached.record.item_type != expected:
            # A cached exact-ID record is already decisive type evidence.
            return None
        now = self._now()
        if cached and now - cached.fetched_at <= self.positive_ttl:
            return cached.record
        key = (expected, base)
        if self._negative.get(key, 0) > now:
            return cached.record if cached else None

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._refresh(expected, base, cached, now))
            self._inflight[key] = task
            task.add_done_callback(
                lambda finished, k=key: self._inflight.pop(k, None)
                if self._inflight.get(k) is finished else None)
        return await asyncio.shield(task)

    async def lookup_episode(self, series_imdb_id: str, season: int,
                             episode: int) -> EpisodeRecord | None:
        """Lookup one exact episode via ``i`` + ``Season`` + ``Episode``.

        This is intentionally separate from :meth:`lookup`: callers normally
        want the long-lived base-show identity and only request an episode row
        when its exact runtime/identity is useful corroborating evidence.
        """
        base = _base_imdb(series_imdb_id)
        try:
            season, episode = int(season), int(episode)
        except (TypeError, ValueError):
            return None
        if (not _IMDB_RE.fullmatch(base) or season < 0 or season > 999
                or episode < 1 or episode > 9999):
            return None

        cached = self._cached_episode(base, season, episode)
        now = self._now()
        if cached and now - cached.fetched_at <= self.positive_ttl:
            return cached.record
        token = f"{base}:{season}:{episode}"
        key = ("episode", token)
        if self._negative.get(key, 0) > now:
            return cached.record if cached else None
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._refresh_episode(base, season, episode, cached, now))
            self._inflight[key] = task
            task.add_done_callback(
                lambda finished, k=key: self._inflight.pop(k, None)
                if self._inflight.get(k) is finished else None)
        return await asyncio.shield(task)

    async def _refresh(self, expected: str, base: str,
                       cached: _CacheEntry | None,
                       now: float) -> TitleRecord | None:
        stale = cached.record if cached else None
        if not self.api_key or now < self._blocked_until:
            return stale
        if not self._reserve(now):
            return stale

        try:
            response = await self._client_for_request().get(
                API_URL,
                # The provider contract intentionally permits only exact ID.
                params={"apikey": self.api_key, "i": base, "r": "json"},
                timeout=self.timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # httpx exception text/repr can contain the full key-bearing URL.
            logger.warning("omdb lookup %s failed: %s", base,
                           type(exc).__name__)
            return stale

        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            logger.warning("omdb lookup %s failed: HTTP %d", base, status)
            if status in (401, 403):
                self._blocked_until = max(self._blocked_until, now + 3600)
            elif status == 429:
                self._blocked_until = max(
                    self._blocked_until, self._next_utc_day(now))
            return stale
        try:
            payload = response.json()
        except Exception:
            logger.warning("omdb lookup %s failed: invalid-json", base)
            return stale

        record, outcome = _parse_payload(payload, base, expected)
        if record is not None and self._reflects_api_key(record):
            record, outcome = None, "secret-reflection"
        if record is not None:
            self._store(record, now)
            self._negative.pop((expected, base), None)
            return record
        if outcome == "not-found":
            self._negative[(expected, base)] = now + self.negative_ttl
        elif outcome == "quota":
            self._blocked_until = max(
                self._blocked_until, self._next_utc_day(now))
        elif outcome == "auth":
            self._blocked_until = max(self._blocked_until, now + 3600)
        # The allowlisted label is safe; OMDb's free-form Error is discarded.
        logger.warning("omdb lookup %s rejected: %s", base, outcome)
        return stale

    async def _refresh_episode(
            self, base: str, season: int, episode: int,
            cached: _EpisodeCacheEntry | None, now: float,
            ) -> EpisodeRecord | None:
        stale = cached.record if cached else None
        if not self.api_key or now < self._blocked_until:
            return stale
        if not self._reserve(now):
            return stale
        token = f"{base}:{season}:{episode}"
        try:
            response = await self._client_for_request().get(
                API_URL,
                params={"apikey": self.api_key, "i": base,
                        "Season": str(season), "Episode": str(episode),
                        "r": "json"},
                timeout=self.timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("omdb episode %s failed: %s", token,
                           type(exc).__name__)
            return stale

        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            logger.warning("omdb episode %s failed: HTTP %d", token, status)
            if status in (401, 403):
                self._blocked_until = max(self._blocked_until, now + 3600)
            elif status == 429:
                self._blocked_until = max(
                    self._blocked_until, self._next_utc_day(now))
            return stale
        try:
            payload = response.json()
        except Exception:
            logger.warning("omdb episode %s failed: invalid-json", token)
            return stale

        record, outcome = _parse_episode_payload(
            payload, base, season, episode)
        if record is not None and self._reflects_api_key(record):
            record, outcome = None, "secret-reflection"
        key = ("episode", token)
        if record is not None:
            self._store_episode(record, now)
            self._negative.pop(key, None)
            return record
        if outcome == "not-found":
            self._negative[key] = now + self.negative_ttl
        elif outcome == "quota":
            self._blocked_until = max(
                self._blocked_until, self._next_utc_day(now))
        elif outcome == "auth":
            self._blocked_until = max(self._blocked_until, now + 3600)
        logger.warning("omdb episode %s rejected: %s", token, outcome)
        return stale

    @staticmethod
    def _next_utc_day(now: float) -> float:
        instant = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        tomorrow = instant.date() + dt.timedelta(days=1)
        return dt.datetime.combine(tomorrow, dt.time(),
                                   tzinfo=dt.timezone.utc).timestamp()

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None
        with self._db_lock:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()


_provider = OMDbProvider(API_KEY)


def enabled() -> bool:
    return bool(API_KEY)


async def lookup(media: str, imdb_id: str) -> TitleRecord | None:
    """Return cached/corroborated OMDb metadata for one exact IMDb id."""
    return await _provider.lookup(media, imdb_id)


async def lookup_episode(series_imdb_id: str, season: int,
                         episode: int) -> EpisodeRecord | None:
    """Return cached/corroborated metadata for one exact episode."""
    return await _provider.lookup_episode(series_imdb_id, season, episode)


def quota_status() -> dict:
    return _provider.quota_status()


async def shutdown() -> None:
    await _provider.close()
