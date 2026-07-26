"""Daily refresh loop.

Daily Picks is the household's viewing surface, so freshness matters even for
someone returning after several quiet days.  Every profile is rebuilt nightly;
otherwise the first session after an absence would be served an old slate and
would not become fresh until the *following* night.
"""

import asyncio
import datetime
import logging
import time

from app.recs import config, db
from app.recs.catalogs import generate_for_user
logger = logging.getLogger("nuvio-recs")

async def _refresh_reason(user: dict) -> str | None:
    """Return why this user should rebuild.

    Kept as a small policy function for the admin status/tests.  At the nightly
    boundary every healthy profile is eligible because release, popularity,
    theme, and rotation signals change even when Trakt history did not.
    """
    if not user["last_generated_at"] or user["last_error"]:
        return "missing catalogs" if not user["last_generated_at"] else "retry after error"
    return "daily freshness"


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
