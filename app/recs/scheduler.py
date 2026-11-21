"""Daily refresh loop.

Every profile somebody used since its last build is rebuilt nightly. A profile
nobody opened or watched on is left alone: rebuilding it costs a full TMDB
fan-out for a slate no one will see. When its viewer does come back, opening
the app rebuilds it in the background (main._refresh_if_stale), so the slate
is fresh within minutes of their return rather than the following night. The
very first page they load after a long absence is the stored one.
"""

import asyncio
import datetime
import logging
import time

from app.recs import config, db
from app.recs import history as local_history
from app.recs.catalogs import generate_for_user
from app.recs.profile_streaming import private_namespace_for_user
logger = logging.getLogger("nuvio-recs")

async def _refresh_reason(user: dict) -> str | None:
    """Return why this user should rebuild, or None to leave them be.

    Kept as a small policy function for the admin status/tests. A profile is
    rebuilt nightly only if somebody used it since its last build: opened the
    app (``last_served_at``) or watched something on it. Otherwise the work is
    wasted. When such
    a viewer comes back, opening the app rebuilds their slate in the
    background (see main._refresh_if_stale).
    """
    if not user["last_generated_at"] or user["last_error"]:
        return "missing catalogs" if not user["last_generated_at"] else "retry after error"
    built = user["last_generated_at"]
    if (user.get("last_served_at") or 0) >= built:
        return "opened since last build"
    try:
        last_play = await local_history.last_play_at(
            private_namespace_for_user(user))
    except Exception:
        last_play = 0
    if last_play >= built:
        return "watched since last build"
    return None


async def _needs_refresh(user: dict) -> bool:
    """Compatibility wrapper used by startup catch-up and focused tests."""
    return await _refresh_reason(user) is not None


def _seconds_until_next_run() -> float:
    now = datetime.datetime.now()
    target = now.replace(hour=config.REFRESH_HOUR, minute=config.REFRESH_MINUTE,
                         second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


async def _refresh_all(reason: str) -> None:
    users = await db.all_users()
    logger.info(f"refresh ({reason}): {len(users)} user(s)")
    ran = False
    for user in users:
        refresh_reason = await _refresh_reason(user)
        if refresh_reason is None:
            logger.info(f"[{user['token'][:8]}] idle since last build — skipped")
            continue
        logger.info(f"[{user['token'][:8]}] rebuilding: {refresh_reason}")
        if ran:
            await asyncio.sleep(config.STAGGER_SECONDS)
        ran = True
        err = await generate_for_user(user, trigger="nightly")
        if err:
            logger.warning(f"[{user['token'][:8]}] refresh error: {err}")


async def _catch_up() -> None:
    """On startup, regenerate every profile whose slate is missing or stale."""
    cutoff = time.time() - config.STALE_HOURS * 3600
    stale = [u for u in await db.all_users()
             if not u["last_generated_at"] or u["last_generated_at"] < cutoff]
    ran = False
    for user in stale:
        if ran:
            await asyncio.sleep(config.STAGGER_SECONDS)
        ran = True
        await generate_for_user(user, trigger="startup-catch-up")


async def run() -> None:
    try:
        await _catch_up()
    except Exception:
        logger.exception("startup catch-up failed")
    while True:
        delay = _seconds_until_next_run()
        logger.info(f"next nightly refresh in {delay / 3600:.1f}h")
        await asyncio.sleep(delay)
        try:
            await _refresh_all("nightly")
        except Exception:
            logger.exception("nightly refresh failed")
        # The bootstrapper's deal log is a row per card per viewer and is
        # worthless the moment it falls out of its window, so it is swept with
        # the same pass rather than left to grow for the life of the install.
        try:
            dropped = await db.prune_dealt(config.TASTE_DEALT_HOURS)
            if dropped:
                logger.info(f"pruned {dropped} expired taste deal record(s)")
        except Exception:
            logger.exception("pruning taste deal records failed")
