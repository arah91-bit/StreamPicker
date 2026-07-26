"""One-time import of each viewer's Trakt history into `play_history`.

Trakt allows a free account one connected application, and that slot is moving
to the client's own progress sync. The moment it does, every viewer's accumulated
history becomes unreachable — so it is copied into our own table first, while
the OAuth grants still work.

Imported rows are marked `picker='trakt-import'`, which keeps them
distinguishable from plays this service actually served. That matters: an
imported row has no byte offsets, so it can seed taste and "already watched"
but can never provide a resume point.

`/sync/history` is the chronological event log and is the primary source. The
`/sync/watched` rollups are a fallback for titles whose individual events have
aged out of the history window — those get a synthetic timestamp from
`last_watched_at`, which is the best fidelity Trakt still holds for them.
"""

import logging

from app.recs import db, trakt

logger = logging.getLogger("nuvio-recs")

IMPORT_PICKER = "trakt-import"
# Trakt's history endpoint pages; this is how deep we go per viewer.
HISTORY_LIMIT = 10_000


def _epoch(value: str | None) -> int:
    import calendar
    import time as _time

    if not value:
        return 0
    try:
        return calendar.timegm(_time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _imdb(ids: dict) -> str:
    value = str((ids or {}).get("imdb") or "").strip()
    return value if value.startswith("tt") else ""


def _history_events(items: list[dict]) -> list[dict]:
    """Chronological watch events -> play_history rows."""
    out = []
    for item in items:
        watched_at = _epoch(item.get("watched_at"))
        if not watched_at:
            continue
        kind = item.get("type")
        if kind == "movie":
            imdb_id = _imdb((item.get("movie") or {}).get("ids"))
            if not imdb_id:
                continue
            out.append({"imdb_id": imdb_id, "content_id": imdb_id,
                        "media_type": "movie", "season": None, "episode": None,
                        "played_at": watched_at})
        elif kind == "episode":
            show = item.get("show") or {}
            episode = item.get("episode") or {}
            imdb_id = _imdb(show.get("ids"))
            season, number = episode.get("season"), episode.get("number")
            if not imdb_id or season is None or number is None:
                continue
            out.append({"imdb_id": imdb_id,
                        "content_id": f"{imdb_id}:{int(season)}:{int(number)}",
                        "media_type": "series", "season": int(season),
                        "episode": int(number), "played_at": watched_at})
    return out


def _watched_rollup(movies: list[dict], shows: list[dict]) -> list[dict]:
    """Per-title rollups, for anything the history window no longer covers."""
    out = []
    for item in movies or []:
        imdb_id = _imdb((item.get("movie") or {}).get("ids"))
        watched_at = _epoch(item.get("last_watched_at"))
        if imdb_id and watched_at:
            out.append({"imdb_id": imdb_id, "content_id": imdb_id,
                        "media_type": "movie", "season": None, "episode": None,
                        "played_at": watched_at})
    for item in shows or []:
        imdb_id = _imdb((item.get("show") or {}).get("ids"))
        if not imdb_id:
            continue
        for season in item.get("seasons") or []:
            s_no = season.get("number")
            for episode in season.get("episodes") or []:
                e_no = episode.get("number")
                watched_at = _epoch(episode.get("last_watched_at"))
                if s_no is None or e_no is None or not watched_at:
                    continue
                out.append({
                    "imdb_id": imdb_id,
                    "content_id": f"{imdb_id}:{int(s_no)}:{int(e_no)}",
                    "media_type": "series", "season": int(s_no),
                    "episode": int(e_no), "played_at": watched_at})
    return out


async def import_user(user: dict, viewer_key: str) -> dict:
    """Import one viewer. Returns a small report; never raises."""
    name = user.get("name") or "viewer"
    report = {"name": name, "history": 0, "rollup": 0, "stored": 0, "error": ""}
    try:
        access = await trakt.ensure_fresh_token(user)
    except Exception as exc:
        report["error"] = f"no usable Trakt token: {exc}"
        return report

    events: list[dict] = []
    try:
        history = await trakt.history(access, limit=HISTORY_LIMIT)
        found = _history_events(history)
        report["history"] = len(found)
        events += found
    except Exception:
        logger.exception("[%s] trakt import: history fetch failed", name)
    try:
        movies = await trakt.watched_movies(access)
        shows = await trakt.watched_shows(access)
        found = _watched_rollup(movies, shows)
        report["rollup"] = len(found)
        events += found
    except Exception:
        logger.exception("[%s] trakt import: watched fetch failed", name)

    # De-duplicate within the run. The insert is idempotent on
    # (viewer, content, timestamp) anyway, so a rollup entry that duplicates a
    # history event collapses rather than double-counting.
    seen: set[tuple] = set()
    for event in events:
        key = (event["content_id"], event["played_at"])
        if key in seen:
            continue
        seen.add(key)
        try:
            await db.record_play({
                **event,
                "viewer_key": viewer_key,
                "seconds": 0.0,
                "megabytes": 0.0,
                "watched_pct": None,
                "position_bytes": None,
                "total_bytes": None,
                "position_pct": None,
                "picker": IMPORT_PICKER,
            })
            report["stored"] += 1
        except Exception:
            logger.exception("[%s] trakt import: could not store a play", name)
    logger.info("[%s] trakt import: %d history + %d rollup -> %d stored",
                name, report["history"], report["rollup"], report["stored"])
    return report


async def import_all(viewer_key_for) -> list[dict]:
    """Import every user. `viewer_key_for(user)` maps a user to their key."""
    reports = []
    for user in await db.all_users():
        key = viewer_key_for(user)
        if not key:
            reports.append({"name": user.get("name"), "error": "no viewer key"})
            continue
        reports.append(await import_user(user, key))
    return reports
