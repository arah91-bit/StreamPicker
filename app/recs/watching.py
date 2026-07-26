"""Continue Watching and Watch History — the two rows pinned above everything.

These are the only catalogs not produced by the nightly build. A recommendation
row is a slate someone curated for you hours ago and is none the worse for it;
a resume row that still offers an episode you finished at lunchtime is actively
wrong. So they are built on the request path, which makes them correct by
construction and removes any need to push an invalidation when a play happens.

Both rows come from `play_history` — this service's own record of what it
played for whom. There is no Trakt account behind them: a free Trakt account
allows one connected application, and that slot belongs to the client's own
progress sync.

Row semantics:
  * Part-way through a movie or episode  -> resume it
  * Episode finished                     -> the next episode of that season
A title appears at most once, newest play first.
"""

import asyncio
import calendar
import logging
import time

from app.recs import config, db, tmdb
from app.recs.kids import effective_kid_age

logger = logging.getLogger("nuvio-recs")

CONTINUE_ID = "nr-continue-watching"
HISTORY_ID = "nr-watch-history"
CONTINUE_NAME = "Continue Watching"
HISTORY_NAME = "Watch History"

# Whether a series entry addresses the episode (tt1234:3:7) or just the show
# (tt1234). Episode-scoped ids are what make the row a true resume shortcut
# rather than a link to a show page, but support is client-specific, so this
# stays switchable without a redeploy of the row logic.
EPISODE_IDS = config.WATCHING_EPISODE_IDS

_ROW_IDS = (CONTINUE_ID, HISTORY_ID)
# Row -> (display name, the users column that opts into it).
ROWS = {
    CONTINUE_ID: (CONTINUE_NAME, "continue_watching_row"),
    HISTORY_ID: (HISTORY_NAME, "watch_history_row"),
}

# token -> (built_at, {catalog_id: [meta, ...]})
_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}
# One build per user at a time; concurrent home rows share the single result
# rather than each starting their own identical set of Trakt calls.
_locks: dict[str, asyncio.Lock] = {}
# In-flight background rebuilds, so a burst of requests against a stale cache
# schedules one refresh rather than one per row.
_refreshing: dict[str, asyncio.Task] = {}


def catalog_defs(user: dict) -> list[dict]:
    """Manifest descriptors for whichever rows this user opted into.

    An enabled row is advertised even while it is momentarily empty: a catalog
    cannot be added to a manifest mid-session, so a row that appeared only once
    it had content would stay missing until Nuvio next refetched the manifest.
    """
    return [
        {
            "id": row_id,
            "type": config.COMBINED_TYPE,
            "name": name,
            "showInHome": True,
            "extra": [{"name": "skip", "isRequired": False}],
        }
        for row_id, (name, column) in ROWS.items()
        if user.get(column)
    ]


def is_watching_catalog_id(catalog_id: str) -> bool:
    return catalog_id in _ROW_IDS


def enabled_for(user: dict, catalog_id: str | None = None) -> bool:
    """True if the user opted into `catalog_id`, or into either row when no id
    is given."""
    if catalog_id is None:
        return any(user.get(column) for _, column in ROWS.values())
    row = ROWS.get(catalog_id)
    return bool(row and user.get(row[1]))


def invalidate(token: str) -> None:
    _cache.pop(token, None)


def _episode_label(season: int | None, number: int | None) -> str:
    if not season or not number:
        return ""
    return f"S{int(season)}E{int(number)}"


async def _movie_meta(tmdb_id: int, max_age: int | None) -> dict | None:
    return await tmdb.resolve_meta("movie", tmdb_id, max_age,
                                   require_home_release=False)


async def _series_meta(tmdb_id: int, max_age: int | None, *,
                       season: int | None = None, number: int | None = None,
                       prefix: str = "") -> dict | None:
    """A series meta carrying which episode to resume.

    The card's `id` stays the SHOW (`tt26545355`), not the episode
    (`tt26545355:2:1`). A catalog id is what the client fetches meta for, and
    nothing serves meta for an episode id — Cinemeta answers for the series
    only — so an episode-scoped card fails with "could not load details from
    any addon" the moment someone taps it.

    The episode still travels two ways that cost nothing if unsupported:
    `releaseInfo` renders as the card's subtitle, so "S2E1" reads under the
    show name; and `behaviorHints.defaultVideoId` is the protocol's own way of
    saying which video a client should open, which clients that honour it use
    to land straight on the episode.
    """
    meta = await tmdb.resolve_meta("tv", tmdb_id, max_age,
                                   require_home_release=False)
    if not meta:
        return None
    label = _episode_label(season, number)
    if not label:
        return meta
    meta = dict(meta)
    episode_id = f"{meta['id']}:{int(season)}:{int(number)}"
    if EPISODE_IDS:
        # Off by default: proven to break the detail page in Nuvio. Kept as a
        # switch only so the behaviour can be re-tested against a client that
        # resolves episode ids without needing a code change.
        meta["id"] = episode_id
    meta["releaseInfo"] = f"{prefix}{label}" if prefix else label
    hints = dict(meta.get("behaviorHints") or {})
    hints["defaultVideoId"] = episode_id
    meta["behaviorHints"] = hints
    return meta


# ── Continue Watching ───────────────────────────────────────────────────

async def _build(user: dict) -> dict[str, list[dict]]:
    """Both rows from this service's own play history.

    Trakt used to supply three things here: paused playback, a chronological
    log, and per-show next-episode. All three now come from `play_history`,
    which knows them first-hand because it served the bytes.
    """
    from app.recs.profile_streaming import private_namespace_for_user

    viewer_key = private_namespace_for_user(user)
    max_age = effective_kid_age(user)
    plays = await db.play_history(viewer_key,
                                  limit=config.WATCH_HISTORY_ROW_ITEMS * 4)
    resume = await _continue_watching_local(viewer_key, plays, max_age) \
        if enabled_for(user, CONTINUE_ID) else []
    watched = await _watch_history_local(plays, max_age) \
        if enabled_for(user, HISTORY_ID) else []
    logger.info("[%s] watching rows: %d continue, %d history",
                user["token"][:8], len(resume), len(watched))
    return {CONTINUE_ID: resume, HISTORY_ID: watched}


async def _meta_for_row(row: dict, max_age: int | None,
                        label_episode: bool) -> dict | None:
    """One play row -> a card, addressed at the show (never the episode: an
    episode-scoped catalog id has no meta provider and fails to open)."""
    found = await tmdb.find_by_imdb(row["imdb_id"])
    if not found:
        return None
    if row["media_type"] == "movie":
        return await _movie_meta(found["tmdb_id"], max_age)
    return await _series_meta(found["tmdb_id"], max_age,
                              season=row.get("season") if label_episode else None,
                              number=row.get("episode") if label_episode else None)


async def _continue_watching_local(viewer_key: str, plays: list[dict],
                                   max_age: int | None) -> list[dict]:
    """Part-watched plays, plus the next episode of anything finished.

    `position_pct` is the read head, so it is a real resume point — which the
    old bytes-delivered figure never was.
    """
    entries: list[tuple[float, str, dict]] = []
    seen: set[str] = set()
    for row in plays:
        key = f"{row['media_type']}:{row['imdb_id']}"
        if key in seen:
            continue
        pct = row.get("position_pct")
        if pct is None:
            continue
        if pct < config.WATCHING_MIN_THRESHOLD:
            continue          # a false start
        seen.add(key)
        if pct < config.WATCHING_DONE_THRESHOLD:
            meta = await _meta_for_row(row, max_age, label_episode=True)
            if meta:
                entries.append((row["played_at"], key, meta))
            continue
        # Finished. For a series, offer the next episode of the same season;
        # nothing for a movie.
        if row["media_type"] != "series" or row.get("episode") is None:
            continue
        found = await tmdb.find_by_imdb(row["imdb_id"])
        if not found:
            continue
        season, nxt_ep = int(row["season"] or 1), int(row["episode"]) + 1
        # An episode number one higher is not proof one exists: past a season
        # finale, or for something only announced, this would put a card in the
        # row for content nobody can play.
        if not await tmdb.episode_aired(found["tmdb_id"], season, nxt_ep):
            continue
        meta = await _series_meta(found["tmdb_id"], max_age,
                                  season=season, number=nxt_ep)
        if meta:
            entries.append((row["played_at"], key, meta))
    entries.sort(key=lambda e: e[0], reverse=True)
    return [meta for _, _, meta in entries][:config.CONTINUE_WATCHING_ROW_ITEMS]


async def _watch_history_local(plays: list[dict],
                               max_age: int | None) -> list[dict]:
    """Reverse-chronological, one card per title — episodes collapse to their
    show, so a binge is one entry rather than twelve."""
    metas: list[dict] = []
    seen: set[str] = set()
    for row in plays:
        key = f"{row['media_type']}:{row['imdb_id']}"
        if key in seen:
            continue
        seen.add(key)
        meta = await _meta_for_row(row, max_age, label_episode=True)
        if meta:
            metas.append(meta)
        if len(metas) >= config.WATCH_HISTORY_ROW_ITEMS:
            break
    return metas


async def _refresh(user: dict) -> dict[str, list[dict]] | None:
    """Rebuild one viewer's rows under their lock. Never raises."""
    token = user["token"]
    lock = _locks.setdefault(token, asyncio.Lock())
    async with lock:
        try:
            rows = await _build(user)
        except Exception:
            logger.exception("[%s] watching rows build failed", token[:8])
            return None
        _cache[token] = (time.monotonic(), rows)
        return rows


def _schedule_refresh(user: dict) -> None:
    """Kick off a rebuild without making the caller wait for it."""
    token = user["token"]
    task = _refreshing.get(token)
    if task and not task.done():
        return
    _refreshing[token] = asyncio.create_task(_refresh(user))


async def get_metas(user: dict, catalog_id: str) -> list[dict]:
    """Metas for one of the two live rows.

    Stale-while-revalidate. Anything already built is returned immediately and
    refreshed in the background, so this answers in about a millisecond like
    every other row. That matters beyond feeling quick: a client fires all its
    catalog requests at once when the home screen opens, and a row that takes a
    second to answer arrives after everything else — which is enough to change
    where it lands on screen.

    Never raises: a Trakt hiccup should cost the viewer this row, not the whole
    home screen.
    """
    if not enabled_for(user, catalog_id):
        return []
    cached = _cache.get(user["token"])
    if cached:
        if time.monotonic() - cached[0] >= config.WATCHING_CACHE_SECONDS:
            _schedule_refresh(user)
        return cached[1].get(catalog_id, [])
    # Nothing built yet — the very first request has to wait for one.
    rows = await _refresh(user)
    return (rows or {}).get(catalog_id, [])


async def run() -> None:
    """Keep every opted-in viewer's rows warm.

    Without this the first home-screen open of the day pays the full Trakt
    round-trip, which is the one case stale-while-revalidate cannot cover.
    """
    while True:
        try:
            for user in await db.all_users():
                if enabled_for(user):
                    await _refresh(user)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("watching rows: warm pass failed")
        await asyncio.sleep(max(30, config.WATCHING_CACHE_SECONDS))
