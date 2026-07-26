"""Which viewers' taste orders the drama actor rows.

This used to borrow Trakt access tokens out of Daily Picks' SQLite. There is no
Trakt account behind any of it now — a free account allows one connected
application, and that slot belongs to the client's own progress sync — so the
signal comes from `play_history`, this service's own record of what it played.

The rows are shared, so this is a household taste signal built from everyone who
ticked Asian dramas on, not a per-viewer one.
"""

import logging

from app.recs import db, history
from app.recs.profile_streaming import private_namespace_for_user

logger = logging.getLogger("nuvio-recs")


async def watched_shows_by_viewer() -> dict[str, list[dict]]:
    """{display name: Trakt-shaped watched-show entries} for opted-in viewers.

    Trakt-shaped because builder.py was written against that structure; only
    the source changed.
    """
    out: dict[str, list[dict]] = {}
    try:
        users = await db.users_with_row("asian_dramas_row")
    except Exception:
        logger.exception("dramas: could not read opted-in viewers")
        return out
    for user in users:
        name = str(user.get("name") or "").strip() or "viewer"
        try:
            _movies, shows, _rm, _rs = await history.watched_lists(
                private_namespace_for_user(user))
            out[name] = shows
        except Exception:
            logger.warning("dramas: no local history for %s", name)
    return out
