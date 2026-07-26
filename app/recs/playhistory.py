"""Watch history built from what this service actually played.

The successor to Trakt as a history source. Trakt limits a free account to one
connected application, and that slot is wanted for the client's own progress
sync — so the recommendation engine has to learn what people watch from what it
serves them, which it is uniquely well placed to observe.

Shape of the pipeline:

    proxy finishes a playback
        -> telemetry.record_play writes its JSONL record
        -> this module's sink drops an event on a queue      (sync, non-blocking)
        -> a background task drains the queue into SQLite     (async)

The queue matters. `record_play` is synchronous and sits on the playback path;
it must never wait on a database write, and a database problem must never
propagate into streaming. Losing a queued play is acceptable — this is a taste
signal, not an accounting ledger.
"""

import asyncio
import logging
import queue
import re
import time

from app.recs import db

logger = logging.getLogger("nuvio-recs")

# Bounded so a stalled drain cannot grow without limit. Dropping the oldest
# pending play is strictly better than exhausting memory on a media server.
_QUEUE_MAX = 10_000
_events: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_dropped = 0

# `tt1234567` or `tt1234567:2:5` — Stremio addresses episodes by suffixing the
# show's IMDb id with season and episode.
_ID_RE = re.compile(r"^(tt\d+)(?::(\d+):(\d+))?$")


def parse_content_id(content_id: str) -> tuple[str, str, int | None, int | None] | None:
    """(imdb_id, media_type, season, episode), or None if unrecognised."""
    match = _ID_RE.match(str(content_id or "").strip())
    if not match:
        return None
    imdb_id, season, episode = match.groups()
    if season is None:
        return imdb_id, "movie", None, None
    return imdb_id, "series", int(season), int(episode)


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sink(record: dict, entry: dict) -> None:
    """Telemetry hook.

    Sync, non-blocking, and must never raise: it runs on the playback path, so
    any exception here would surface as a streaming failure. Every field is
    coerced defensively rather than trusted.
    """
    global _dropped
    try:
        viewer_key = (entry or {}).get("viewer_key") or ""
        parsed = parse_content_id((record or {}).get("id"))
        if not viewer_key or not parsed:
            # No viewer means the shared stream-only addon, which cannot know
            # who asked; an unparseable id is not IMDb-addressed.
            return
        imdb_id, media_type, season, episode = parsed
        event = {
            "viewer_key": viewer_key,
            "content_id": str(record.get("id")),
            "imdb_id": imdb_id,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "played_at": int(_number(record.get("ts"), time.time())),
            "seconds": _number(record.get("secs")),
            "megabytes": _number(record.get("mb")),
            "watched_pct": (None if record.get("watched") is None
                            else _number(record.get("watched"))),
            "picker": str(record.get("picker") or ""),
        }
        pos = _number(record.get("pos"))
        total = _number(record.get("total"))
        event["position_bytes"] = int(pos) if pos > 0 else None
        event["total_bytes"] = int(total) if total > 0 else None
        event["position_pct"] = (round(100.0 * pos / total, 2)
                                 if pos > 0 and total > 0 else None)
    except Exception:
        logger.debug("play history: unusable play record", exc_info=True)
        return
    try:
        _events.put_nowait(event)
    except queue.Full:
        _dropped += 1
        if _dropped % 100 == 1:
            logger.warning("play history: queue full, dropped %d plays", _dropped)


async def drain_once() -> int:
    """Write everything currently queued. Returns how many rows were stored."""
    stored = 0
    while True:
        try:
            event = _events.get_nowait()
        except queue.Empty:
            return stored
        try:
            await db.record_play(event)
            stored += 1
        except Exception:
            logger.exception("play history: could not store a play")
    return stored


async def run(interval: float = 5.0) -> None:
    """Drain the queue on a short cycle.

    Batched rather than per-play so a binge does not turn into one SQLite
    commit per episode on the same connection the catalogs are served from.
    """
    while True:
        try:
            stored = await drain_once()
            if stored:
                logger.info("play history: stored %d play(s)", stored)
        except asyncio.CancelledError:
            await drain_once()  # do not lose what is already queued
            raise
        except Exception:
            logger.exception("play history: drain cycle failed")
        await asyncio.sleep(interval)


def install() -> None:
    """Register the telemetry hook. Idempotent."""
    from app import telemetry

    telemetry.add_play_sink(sink)
