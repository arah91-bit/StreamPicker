"""Wires the vendored Daily Picks package (app/recs) into stream-picker.

The two halves stay separate packages on purpose — this module is the whole
seam between them. It does three things:

  * copies app/recs' routes onto stream-picker's FastAPI app, skipping the
    handful whose paths the host app already owns,
  * runs app/recs' startup/shutdown work from the host lifespan, since only
    the host app's lifespan actually executes,
  * builds the merged per-viewer manifest, which is the point of the whole
    exercise: one addon advertising both `catalog` and `stream`, so a stream
    request arrives with a viewer token attached and a play can be attributed
    to a person.

Route ordering matters. stream-picker registers `/{secret}/manifest.json`
before this runs, and `/{token}/manifest.json` compiles to the identical
pattern, so the host's handler would swallow every per-viewer manifest. The
host therefore keeps ONE handler for that shape and dispatches on whether the
path segment is the shared addon secret or a known viewer token; the recs
version is skipped here rather than being allowed to lose the race silently.
"""

import logging

from starlette.routing import Mount

logger = logging.getLogger("stream-picker")

# Paths app/recs defines that stream-picker already owns, or that it must own
# because the host dispatches them (see module docstring).
_SKIP = {
    "/",                       # stream-picker's admin dashboard
    "/health",                 # host readiness contract, used by the healthcheck
    "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect",
    "/{token}/manifest.json",  # dispatched by the host
}


def attach(app) -> int:
    """Copy app/recs' routes onto `app`. Returns how many were added.

    Order is the whole subtlety. stream-picker owns `/{secret}/manifest.json`,
    whose leading path parameter matches ANY single segment — including the
    literal ones app/recs needs, so `/kids/manifest.json` would bind `secret`
    to "kids" and 404. Routes with a literal first segment therefore go in
    FRONT of the host's parameterised ones; routes that themselves start with a
    parameter are appended, where they cannot shadow anything.
    """
    from app.recs.main import app as recs_app

    existing = {(r.path, frozenset(getattr(r, "methods", ()) or ()))
                for r in app.routes if hasattr(r, "path")}
    literal, parameterised = [], []
    for route in recs_app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        if isinstance(route, Mount):
            # Static asset dirs (/kids/assets, /catalogs/assets, …). Mounts have
            # no methods; match on path alone.
            if any(p == path for p, _ in existing):
                continue
            literal.append(route)
            continue
        if path in _SKIP:
            continue
        key = (path, frozenset(getattr(route, "methods", ()) or ()))
        if key in existing:
            logger.warning("recs route %s already served by stream-picker; "
                           "leaving the host's handler in place", path)
            continue
        (parameterised if path.startswith("/{") else literal).append(route)

    app.router.routes[:0] = literal
    app.router.routes.extend(parameterised)
    return len(literal) + len(parameterised)


async def startup() -> None:
    """app/recs' own startup work: open SQLite, then start its background jobs.

    Runs from stream-picker's lifespan because a sub-application's lifespan
    never fires when only its routes are copied across.
    """
    import asyncio

    from app.recs import (db, dramas, kids_catalogs, playhistory,
                          profile_streaming, scheduler, watching)

    await db.init()
    # Register the telemetry hook before anything can play.
    playhistory.install()
    # Everything app/recs would have started from its own @app.on_event
    # handler. Those never fire here — only its routes were copied across — so
    # this list has to stay in step with app/recs/main.py's startup.
    return [
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(kids_catalogs.run()),
        asyncio.create_task(profile_streaming.run()),
        asyncio.create_task(dramas.run()),
        asyncio.create_task(watching.run()),
        asyncio.create_task(playhistory.run()),
    ]


async def shutdown(tasks) -> None:
    import asyncio
    import contextlib

    from app.recs import db

    for task in tasks or ():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await db.close()


def viewer_key(user: dict | None) -> str:
    """Opaque, stable per-viewer id — a hash of the addon token, never the
    token itself. Reuses the namespace already minted for private collections
    so there is one such identifier rather than two."""
    if not user:
        return ""
    try:
        from app.recs.profile_streaming import private_namespace_for_user
        return private_namespace_for_user(user)
    except Exception:
        return ""


async def viewer_for(ident: str, addon_secret: str) -> dict | None:
    """The Daily Picks viewer whose personal addon this path belongs to.

    Returns None for the legacy shared stream-only addon secret, and for any
    unknown segment — callers fall back to the host's own `_check`, so an
    unrecognised token still 404s exactly as it did before.
    """
    import secrets as _secrets

    if _secrets.compare_digest(ident, addon_secret):
        return None
    try:
        from app.recs import db
        return await db.get_user(ident)
    except Exception:
        logger.exception("viewer lookup failed for a manifest/stream request")
        return None


# The stream lanes a viewer can install, keyed by URL prefix. A Stremio
# manifest declares exactly one stream endpoint, and the slow picker waits for
# every source to finish before answering — so fast and slow cannot share one
# addon without making every request as slow as the slow lane. They are
# separate installs, exactly as the shared Auto Stream addon already does it.
#
# What matters for tracking is that each of these URLs still carries the
# viewer's token: two per-viewer addons attribute fine. It was the *shared*
# addon that could not, because its URL named nobody.
# Mobile lanes are deliberately absent: MAX_BITRATE_MBPS in Settings caps
# quality for anyone who needs it, and two more installs per viewer was not
# worth the shelf space. The shared Auto Stream (Mobile) addon still exists for
# anyone already on it.
STREAM_LANES = {
    "": ("", "full", False),
    "slow": (" (Best Quality)", "full", True),
}


async def personal_manifest(token: str, lane: str = "") -> dict:
    """The per-viewer manifest for one stream lane.

    The primary lane carries the catalogs as well as streams. The extra lanes
    are streams only — repeating 300-odd catalog rows on each would duplicate
    every row on the home screen.
    """
    from app.recs import main as recs_main

    suffix, _profile, _slow = STREAM_LANES.get(lane, STREAM_LANES[""])
    manifest = dict(await recs_main.manifest(token))
    manifest["idPrefixes"] = ["tt"]
    if not lane:
        # Catalogs come from app/recs; streams come from the picker in this
        # same process. Advertising both is what lets a stream request carry a
        # viewer.
        manifest["resources"] = ["catalog", "stream"]
        return manifest
    # A distinct id per lane, or Stremio treats them as one addon and only the
    # first install sticks.
    manifest["id"] = f"{manifest['id']}.{lane.replace('/', '.')}"
    manifest["name"] = f"{manifest['name']}{suffix}"
    manifest["resources"] = ["stream"]
    manifest["catalogs"] = []
    return manifest
