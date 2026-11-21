"""Want to Watch — the row of titles somebody saved for later.

Built on the request path rather than by the nightly job, for the same reason
Continue Watching is: somebody taps "want to watch" in the bootstrapper and
then goes to look for it. A row that only learned about the save at the next
nightly build would be wrong for up to a day, at the one moment the viewer is
actually checking whether the button did anything.

It is the only row on the surface assembled from an explicit request rather
than from an inference, which makes it the cheapest possible correction when
the recommendations are wrong: whatever else the slate offers, this is the
shelf the viewer stocked themselves.

**Saved titles leave the row once they are watched**, but stay in the table.
Those are two different questions. The row answers "what have I still not got
round to", so a film watched last night does not belong in it. The table
answers "have they already ruled on this", which the bootstrapper needs in
order not to deal the same card again — and that stays true forever.
"""

import asyncio
import logging
import time

from app.recs import config, db, tmdb
from app.recs.kids import effective_kid_age

logger = logging.getLogger("nuvio-recs")

WATCHLIST_ID = "nr-watchlist"
WATCHLIST_NAME = "Want to Watch"
WATCHLIST_FLAG = "watchlist_row"

# token -> (built_at, [meta, ...])
_cache: dict[str, tuple[float, list[dict]]] = {}
# One build per viewer at a time, so a home screen firing every row at once
# does not start one identical build per row.
_locks: dict[str, asyncio.Lock] = {}
_refreshing: dict[str, asyncio.Task] = {}


def catalog_defs(user: dict) -> list[dict]:
    """Manifest descriptor, when this viewer has the row switched on.

    Advertised even while the watchlist is empty. A catalog cannot be added to
    a manifest mid-session, so a row that appeared only once it had something
    in it would stay missing until the client next refetched the manifest —
    which is to say, it would be missing for exactly the first save.
    """
    if not enabled_for(user):
        return []
    return [{
        "id": WATCHLIST_ID,
        "type": config.COMBINED_TYPE,
        "name": WATCHLIST_NAME,
        "showInHome": True,
        "extra": [{"name": "skip", "isRequired": False}],
    }]


def is_watchlist_catalog_id(catalog_id: str) -> bool:
    return catalog_id == WATCHLIST_ID


def enabled_for(user: dict) -> bool:
    return bool(user.get(WATCHLIST_FLAG))


def invalidate(token: str) -> None:
    """Drop the cached row. Called the moment something is saved, so the tap
    and the row cannot disagree."""
    _cache.pop(token, None)


async def _build(user: dict) -> list[dict]:
    """Saved titles, newest first, minus anything already watched."""
    from app.recs.profile_streaming import private_namespace_for_user

    token = user["token"]
    saved = await db.watchlist_for(token, limit=config.WATCHLIST_ROW_ITEMS * 2)
    if not saved:
        return []
    watched: set[str] = set()
    try:
        viewer_key = private_namespace_for_user(user)
        watched = {row["imdb_id"]
                   for row in await db.played_titles(viewer_key)}
    except Exception:
        # A history read that fails costs a few stale cards, not the row.
        logger.debug("[%s] watchlist: play history unreadable", token[:8],
                     exc_info=True)
    max_age = effective_kid_age(user)
    metas: list[dict] = []
    for row in saved:
        if row["imdb_id"] in watched:
            continue
        found = await tmdb.find_by_imdb(row["imdb_id"])
        if not found:
            continue
        media = "movie" if row["media_type"] == "movie" else "tv"
        # `require_home_release=False`: somebody may perfectly well save a film
        # that is still in cinemas, and dropping it out of their own list until
        # it is streamable would look like the save had failed.
        meta = await tmdb.resolve_meta(media, found["tmdb_id"], max_age,
                                       require_home_release=False)
        if meta:
            metas.append(meta)
        if len(metas) >= config.WATCHLIST_ROW_ITEMS:
            break
    logger.info("[%s] watchlist row: %d saved, %d shown",
                token[:8], len(saved), len(metas))
    return metas


async def _refresh(user: dict) -> list[dict] | None:
    """Rebuild one viewer's row under their lock. Never raises."""
    token = user["token"]
    lock = _locks.setdefault(token, asyncio.Lock())
    async with lock:
        try:
            metas = await _build(user)
        except Exception:
            logger.exception("[%s] watchlist row build failed", token[:8])
            return None
        _cache[token] = (time.monotonic(), metas)
        return metas


def _schedule_refresh(user: dict) -> None:
    token = user["token"]
    task = _refreshing.get(token)
    if task and not task.done():
        return
    _refreshing[token] = asyncio.create_task(_refresh(user))


async def get_metas(user: dict) -> list[dict]:
    """The row. Stale-while-revalidate, so it answers in about a millisecond
    like every other row — a client fires all its catalog requests at once when
    the home screen opens, and a row that takes a second to answer arrives
    after everything else, which is enough to move it on screen."""
    if not enabled_for(user):
        return []
    cached = _cache.get(user["token"])
    if cached:
        if time.monotonic() - cached[0] >= config.WATCHING_CACHE_SECONDS:
            _schedule_refresh(user)
        return cached[1]
    metas = await _refresh(user)
    return metas or []
