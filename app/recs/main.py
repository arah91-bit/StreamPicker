import asyncio
import contextlib
import datetime
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.recs import (ai_search, bootstrap, config, db, dramas,
                      kids_catalogs, movienight, playhistory,
                      profile_streaming, trailers, watching, watchlist)
from app.recs.catalogs import TOP_PICKS_ID, generate_for_user
from app.recs.kids import birthdate_from_age, clamp_age, effective_kid_age
from app.recs import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("nuvio-recs")

app = FastAPI(title="Daily Picks", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.mount("/kids/assets", StaticFiles(directory=str(config.CATALOG_ASSETS_DIR),
                                      check_dir=False), name="kids-assets")
app.mount("/catalogs/assets", StaticFiles(directory=str(config.CATALOG_ASSETS_DIR),
                                          check_dir=False), name="catalog-assets")
app.mount("/catalogs/kids-thumbnails",
          StaticFiles(directory=str(config.CATALOGS_DIR / "Kids Thumbnails"),
                      check_dir=False),
          name="kids-thumbnails")
# Country flags and focus GIFs for the Asian Dramas collection. They came over
# with the standalone service but its hostname did not, so nothing served them
# between the fold-in and now.
app.mount(dramas.STATIC_ROUTE,
          StaticFiles(directory=str(dramas.config.STATIC_DIR), check_dir=False),
          name="dramas-static")

_scheduler_task: asyncio.Task | None = None
_kids_task: asyncio.Task | None = None
_profile_streaming_task: asyncio.Task | None = None
_dramas_task: asyncio.Task | None = None
_watching_task: asyncio.Task | None = None
_playhistory_task: asyncio.Task | None = None
# Rebuilds started from request handlers. asyncio holds only a weak reference to
# a running task, so one nobody keeps can be collected mid-build, and its
# exception is otherwise reported at garbage collection, if at all.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_reaped)
    return task


def _reaped(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("recs: background task failed",
                     exc_info=task.exception())
CONFIGURE_HTML = (Path(__file__).parent / "templates" / "configure.html").read_text()
# Trailer playback is identical on both card pages and is the one thing on them
# that must not drift, so it lives once and is folded into each. See the
# fragment's own header for why these pages stay single documents.
_TRAILER_HTML = (Path(__file__).parent / "templates" / "_trailer.html").read_text()
BOOTSTRAP_HTML = (Path(__file__).parent / "templates" / "bootstrap.html"
                  ).read_text().replace("__TRAILER__", _TRAILER_HTML)
MOVIE_NIGHT_HTML = (Path(__file__).parent / "templates" / "movienight.html"
                    ).read_text().replace("__TRAILER__", _TRAILER_HTML)
LANDING_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Picks</title><style>
:root{color-scheme:dark}body{margin:0;background:#0d1117;color:#e6edf3;
font:16px/1.55 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}
main{max-width:620px;padding:32px}h1{margin:0 0 8px}p{color:#a8b0ba}
.ok{color:#3fb950}code{background:#161b22;padding:2px 6px;border-radius:5px}
</style></head><body><main><h1>Daily Picks</h1>
<p>Personal viewing catalogs built from what each viewer actually watches, refreshed
daily. Daily Picks is installed in Nuvio through a private per-profile addon URL.</p>
<p class="ok">Service online</p></main></body></html>"""


@app.on_event("startup")
async def startup() -> None:
    global _scheduler_task, _kids_task, _profile_streaming_task, _dramas_task
    global _watching_task, _playhistory_task
    await db.init()
    _scheduler_task = asyncio.create_task(scheduler.run())
    _kids_task = asyncio.create_task(kids_catalogs.run())
    _profile_streaming_task = asyncio.create_task(profile_streaming.run())
    _dramas_task = asyncio.create_task(dramas.run())
    _watching_task = asyncio.create_task(watching.run())
    playhistory.install()
    _playhistory_task = asyncio.create_task(playhistory.run())
    if config.AI_SEARCH_ENABLED:
        _spawn(ai_search.warm_up())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _scheduler_task:
        _scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _scheduler_task
    if _kids_task:
        _kids_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _kids_task
    if _profile_streaming_task:
        _profile_streaming_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _profile_streaming_task
    if _dramas_task:
        _dramas_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _dramas_task
    if _watching_task:
        _watching_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _watching_task
    if _playhistory_task:
        _playhistory_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _playhistory_task
    await db.close()


# ── configure page + viewer management (gated behind SETUP_SECRET) ──────

def _check_setup_secret(secret: str) -> None:
    # require(), not a constant defaulting to "": an install that never set
    # SETUP_SECRET must not end up comparing the caller's secret against the
    # empty string, which an empty guess would match.
    if not secrets.compare_digest(secret, config.require("SETUP_SECRET")):
        raise HTTPException(404)


def _collection_export(user: dict) -> list[dict]:
    """Every Nuvio collection this viewer can import, in one file.

    Streaming and dramas are separate opt-ins but they land in the same
    download: they are all backed by the one private Daily Picks addon, so
    splitting them would only mean importing two files that name the same
    manifest id.
    """
    return (profile_streaming.collection_export_for_user(user)
            + dramas.collection_export_for_user(
                user, profile_streaming.private_addon_id_for_user(user)))


def _collection_url(user: dict) -> str | None:
    if not _collection_export(user):
        return None
    return f"{config.HOST_NAME}/{user['token']}/collections.json"


def _lane_urls(token: str) -> list[dict]:
    """Every add-on this viewer can install, in install order.

    A Stremio manifest declares one stream endpoint, and the slow picker waits
    for every source before answering — so fast and slow have to be separate
    installs. Each URL still carries this viewer's token, so all of them
    attribute plays to the same person.
    """
    base = _manifest_url(token)
    lanes = [{"lane": "", "label": "Daily Picks — catalogs + fast streams",
              "url": base}]
    for lane, label in (("slow", "Best quality (slower to answer)"),):
        lanes.append({
            "lane": lane,
            "label": label,
            "url": base.replace(f"/{token}/manifest.json",
                                f"/{token}/{lane}/manifest.json"),
        })
    return lanes


def _manifest_url(token: str) -> str:
    # Nuvio caches manifests by install URL for several hours. A version query
    # makes this identity-changing fold-in a clean install while remaining a
    # normal Stremio resource URL; Nuvio carries the query onto catalog calls.
    return (
        f"{config.HOST_NAME}/{token}/manifest.json"
        f"?v={profile_streaming.PRIVATE_MANIFEST_VERSION}"
    )


def _queue_profile_streaming_build(user: dict) -> bool:
    profile_id = profile_streaming.profile_id_for_user(user)
    if not profile_id:
        return False
    _spawn(profile_streaming.build(profile_id))
    return True


@app.get("/", response_class=HTMLResponse)
async def root():
    return LANDING_HTML


@app.get("/setup/{secret}", response_class=HTMLResponse)
async def configure(secret: str):
    _check_setup_secret(secret)
    return CONFIGURE_HTML


class NewViewerBody(BaseModel):
    name: str
    is_kid: bool = False
    kid_age: int | None = None
    preferred_media: str = "balanced"
    adventurousness: int = 30


@app.post("/setup/{secret}/api/users")
async def create_viewer(secret: str, body: NewViewerBody):
    """Add a viewer. A name is all it takes.

    This used to be a Trakt OAuth device flow, which existed to borrow an
    account's watch history. Taste is built from what this service actually
    plays for someone, so there is no account to connect — and a new viewer
    starts with an empty profile that fills in as they watch, rather than
    with someone else's idea of what they like.
    """
    _check_setup_secret(secret)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "a viewer needs a name")

    token = secrets.token_urlsafe(16)
    kid_age = clamp_age(body.kid_age) if body.is_kid else None
    await db.create_user(
        token, name, is_kid=body.is_kid, kid_age=kid_age,
        kid_birthdate=birthdate_from_age(kid_age) if kid_age else None)
    preferred_media = body.preferred_media \
        if body.preferred_media in {"balanced", "movies", "series"} else "balanced"
    await db.update_preferences(
        token, preferred_media, max(0, min(100, body.adventurousness)))
    user = await db.get_user(token)
    _spawn(generate_for_user(user, trigger="signup"))
    _queue_profile_streaming_build(user)
    logger.info(f"new viewer '{name}' -> token {token[:8]}…")
    return {
        "status": "ok",
        "token": token,
        "manifest_url": _manifest_url(token),
        "collection_url": _collection_url(user),
        "collection_filename": collection_filename(user),
        "streaming_profile_configured": bool(
            profile_streaming.profile_id_for_user(user)),
    }


@app.get("/setup/{secret}/api/users")
async def list_users(secret: str):
    _check_setup_secret(secret)
    out = []
    for u in await db.all_users():
        defs = await db.get_catalog_defs(u["token"])
        streaming_defs = profile_streaming.catalog_defs_for_user(u)
        streaming_configured = bool(profile_streaming.profile_id_for_user(u))
        measurement = await db.get_recommendation_summary(
            u["token"], window_days=30)
        verdicts = await db.feedback_counts(u["token"])
        out.append({
            "name": u["name"],
            "is_kid": bool(u["is_kid"]),
            # Always the live age computed from the internal anchor. The
            # anchor date itself is deliberately not exposed: nobody is asked
            # for a birthday, and showing one back would imply we hold it.
            "kid_age": effective_kid_age(u),
            "preferred_media": u.get("preferred_media") or "balanced",
            "adventurousness": u.get("adventurousness", 30),
            "continue_watching_row": bool(u.get("continue_watching_row")),
            "watch_history_row": bool(u.get("watch_history_row")),
            "watchlist_row": bool(u.get(watchlist.WATCHLIST_FLAG)),
            "streaming_catalogs_row": bool(u.get("streaming_catalogs_row")),
            "asian_dramas_row": bool(u.get("asian_dramas_row")),
            # Every service the catalogue knows, each marked on or off for this
            # viewer, so the builder can draw the switches without duplicating
            # the provider list client-side.
            "streaming_providers": [
                {"id": p["id"], "title": p["title"],
                 "on": p["id"] in {q["id"]
                                   for q in profile_streaming.providers_for_user(u)}}
                for p in kids_catalogs.PROVIDERS
            ],
            "catalogs": len(defs),
            "last_generated_at": u["last_generated_at"],
            "last_served_at": u.get("last_served_at"),
            "refresh_queued": bool(
                (u.get("last_served_at") or 0) > (u["last_generated_at"] or 0)),
            "last_error": u["last_error"],
            "measurement": measurement,
            "token": u["token"],
            "manifest_url": _manifest_url(u["token"]),
            "streaming_catalogs": len(streaming_defs),
            "streaming_profile_configured": streaming_configured,
            "collection_url": _collection_url(u),
            "collection_filename": collection_filename(u),
            "lane_urls": _lane_urls(u["token"]),
            "taste_url": f"{config.HOST_NAME}/{u['token']}/taste",
            "taste_rated": verdicts.get("liked", 0) + verdicts.get("disliked", 0),
            "taste_saved": await db.watchlist_count(u["token"]),
        })
    return {"users": out}


class KidBody(BaseModel):
    is_kid: bool
    kid_age: int | None = None  # omit to keep the existing birthdate anchor


class PreferenceBody(BaseModel):
    preferred_media: str = "balanced"
    adventurousness: int = 30


class WatchingBody(BaseModel):
    row: str
    enabled: bool


# Every opt-in row family the catalog builder can switch, mapped to its column.
# The two live rows come from app.recs.watching; the other two are whole row
# families that used to be separate addons or an env list.
ROW_FAMILIES = {
    **{row_id: column for row_id, (_name, column) in watching.ROWS.items()},
    watchlist.WATCHLIST_ID: watchlist.WATCHLIST_FLAG,
    "streaming-catalogs": "streaming_catalogs_row",
    "asian-dramas": "asian_dramas_row",
}


@app.post("/setup/{secret}/api/watching/{token}")
async def admin_update_watching(secret: str, token: str, body: WatchingBody):
    _check_setup_secret(secret)
    await _require_user(token)
    column = ROW_FAMILIES.get(body.row)
    if not column:
        raise HTTPException(422, f"unknown row: {body.row}")
    await db.update_watching_row(token, column, body.enabled)
    # Adding or removing a manifest catalog only takes effect once the client
    # refetches the manifest, so drop any built rows rather than leaving a
    # disabled user's data cached.
    watching.invalidate(token)
    watchlist.invalidate(token)
    user = await db.get_user(token)
    if column == "streaming_catalogs_row":
        # This one has prebuilt per-viewer rows behind it, so a fresh tick has
        # nothing to serve until its profile is built.
        await profile_streaming.refresh_targets()
        if body.enabled:
            _queue_profile_streaming_build(user)
    return {"status": "ok", "row": body.row, "enabled": body.enabled,
            "manifest_url": _manifest_url(user["token"])}


class StreamingProviderBody(BaseModel):
    provider: str
    enabled: bool


@app.post("/setup/{secret}/api/streaming-provider/{token}")
async def admin_update_streaming_provider(secret: str, token: str,
                                          body: StreamingProviderBody):
    _check_setup_secret(secret)
    user = await _require_user(token)
    known = {p["id"] for p in kids_catalogs.PROVIDERS}
    if body.provider not in known:
        raise HTTPException(422, f"unknown streaming provider: {body.provider}")
    current = {p["id"] for p in profile_streaming.providers_for_user(user)}
    current.add(body.provider) if body.enabled else current.discard(body.provider)
    # Store the full list even when it happens to be everything: a viewer who
    # has touched the switches has a preference, and should not silently gain a
    # service that gets added to the catalogue later.
    await db.update_streaming_providers(
        token, [p["id"] for p in kids_catalogs.PROVIDERS if p["id"] in current])
    user = await db.get_user(token)
    await profile_streaming.refresh_targets()
    # Newly ticked services have no rows built for them yet; already-built ones
    # are left alone, so unticking is instant and costs nothing.
    if body.enabled:
        _queue_profile_streaming_build(user)
    return {"status": "ok", "provider": body.provider, "enabled": body.enabled}


@app.post("/setup/{secret}/api/kid/{token}")
async def admin_update_kid(secret: str, token: str, body: KidBody):
    _check_setup_secret(secret)
    user = await _require_user(token)
    if body.kid_age is not None:
        age = clamp_age(body.kid_age)
        await db.update_kid(token, body.is_kid, age, birthdate_from_age(age))
    elif body.is_kid and not (user.get("kid_birthdate") or user.get("kid_age")):
        # An omitted age means "keep the existing anchor", and there isn't
        # one. Kid mode without an age filters nothing at all, so a profile
        # marked as a child would be the one surface with no ceiling on it.
        age = clamp_age(None)
        await db.update_kid(token, True, age, birthdate_from_age(age))
    else:
        await db.update_kid(token, body.is_kid, None, None)
    user = await db.get_user(token)
    # filtering changed, so rebuild this user's catalogs right away
    _spawn(generate_for_user(user, trigger="kid-settings"))
    _queue_profile_streaming_build(user)
    return {"status": "ok", "is_kid": bool(user["is_kid"]),
            "kid_age": effective_kid_age(user)}


@app.post("/setup/{secret}/api/preferences/{token}")
async def admin_update_preferences(secret: str, token: str, body: PreferenceBody):
    _check_setup_secret(secret)
    user = await _require_user(token)
    try:
        await db.update_preferences(
            token, body.preferred_media, body.adventurousness)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    user = await db.get_user(token)
    _spawn(generate_for_user(user, trigger="taste-settings"))
    return {
        "status": "ok",
        "preferred_media": user["preferred_media"],
        "adventurousness": user["adventurousness"],
    }


@app.post("/setup/{secret}/api/refresh/{token}")
async def admin_refresh(secret: str, token: str):
    _check_setup_secret(secret)
    user = await _require_user(token)
    _spawn(generate_for_user(user, trigger="admin-refresh"))
    _queue_profile_streaming_build(user)
    return {"status": "refreshing"}


@app.post("/setup/{secret}/api/delete/{token}")
async def admin_delete(secret: str, token: str):
    _check_setup_secret(secret)
    await _require_user(token)
    await db.delete_user(token)
    try:
        await profile_streaming.write_import_files()
    except Exception:
        # Account deletion is authoritative even if the convenience export
        # directory is temporarily unavailable; startup will reconcile it.
        logger.exception("failed to reconcile streaming imports after user deletion")
    logger.info(f"deleted user {token[:8]}…")
    return {"status": "deleted"}


@app.get("/api/status/{token}")
async def status(token: str):
    user = await db.get_user(token)
    if not user:
        raise HTTPException(404)
    defs = await db.get_catalog_defs(token)
    streaming_defs = profile_streaming.catalog_defs_for_user(user)
    streaming_configured = bool(profile_streaming.profile_id_for_user(user))
    return {
        "ready": bool(defs),
        "catalogs": len(defs),
        "last_generated_at": user["last_generated_at"],
        "last_served_at": user.get("last_served_at"),
        "refresh_queued": bool(
            (user.get("last_served_at") or 0) > (user["last_generated_at"] or 0)),
        "last_error": user["last_error"],
        "streaming_catalogs": len(streaming_defs),
        "streaming_profile_configured": streaming_configured,
        "collection_url": _collection_url(user),
        "collection_filename": collection_filename(user),
    }


# ── Public kids streaming collection pack ────────────────────────────────

@app.get("/kids/manifest.json")
async def kids_manifest():
    return {
        "id": config.KIDS_ADDON_ID,
        "version": "1.0.0",
        "name": config.KIDS_ADDON_NAME,
        "description": "US kids and teen streaming rows for Netflix, Prime "
                       "Video, Disney+, Paramount+, Apple TV+, and HBO Max. "
                       "Refreshed daily from TMDB.",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": kids_catalogs.catalog_defs(),
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }


@app.get("/kids/catalog/{ctype}/{catalog_id}.json")
async def kids_catalog(ctype: str, catalog_id: str):
    metas = kids_catalogs.get_metas(catalog_id)
    return {"metas": metas or []}


@app.get("/kids/catalog/{ctype}/{catalog_id}/{extra}.json")
async def kids_catalog_extra(ctype: str, catalog_id: str, extra: str):
    return await kids_catalog(ctype, catalog_id)


def _collection_download() -> Response:
    payload = kids_catalogs.collection_export()
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 'attachment; filename="nuvio-kids-streaming-collection.json"'},
    )


# If the collection URL is pasted into Addons by mistake, Nuvio appends
# /manifest.json. Let that install the backing addon instead of returning 404.
@app.get("/kids/collection.json/manifest.json")
async def kids_collection_manifest_alias():
    return await kids_manifest()


@app.get("/kids/collection.json")
async def kids_collection():
    return _collection_download()


@app.post("/kids/refresh")
@app.get("/kids/refresh")
async def kids_refresh():
    _spawn(kids_catalogs.build())
    return {"status": "rebuilding"}


# ── Retired two-addon streaming helper routes ──────────────────────────

_RETIRED_STREAMING_HELPER_DETAIL = (
    "The shared streaming helper has been folded into each private Daily Picks "
    "addon. Install the private addon URL and import its matching Collection."
)


def _retired_streaming_helper() -> None:
    raise HTTPException(410, _RETIRED_STREAMING_HELPER_DETAIL)

@app.get("/streaming-profiles/manifest.json")
async def profile_streaming_manifest():
    _retired_streaming_helper()


@app.get("/streaming-profiles/catalog/{ctype}/{catalog_id}.json")
async def profile_streaming_catalog(ctype: str, catalog_id: str):
    _retired_streaming_helper()


@app.get("/streaming-profiles/catalog/{ctype}/{catalog_id}/{extra}.json")
async def profile_streaming_catalog_extra(ctype: str, catalog_id: str, extra: str):
    _retired_streaming_helper()


@app.get("/streaming-profiles/collection/{profile_id}.json/manifest.json")
async def profile_streaming_collection_manifest_alias(profile_id: str):
    _retired_streaming_helper()


@app.get("/streaming-profiles/collection/{profile_id}.json")
async def profile_streaming_collection(profile_id: str):
    _retired_streaming_helper()


# ── Stremio/Nuvio addon endpoints (all keyed by per-user token) ──────────

async def _require_user(token: str) -> dict:
    user = await db.get_user(token)
    if not user:
        raise HTTPException(404, "Unknown addon token")
    return user


def collection_filename(user: dict) -> str:
    """`Phil collections.json` — named per viewer.

    Every viewer's collection used to download under one shared filename, so
    setting up a household meant six files called
    `daily-picks-streaming-collection(3).json` with no way to tell whose was
    whose. The display name is what the admin panel shows, so it is the name
    that makes the file identifiable on disk.
    """
    raw = (user.get("name") or "daily-picks").strip()
    # Anything that would break a Content-Disposition header, a shell, or a
    # filesystem path. Spaces are kept — this is a human-facing filename.
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", raw).strip() or "daily-picks"
    return f"{safe} collections.json"


def _attachment(payload: dict, filename: str) -> Response:
    # filename* carries the exact UTF-8 name for modern clients; the plain
    # filename= stays ASCII so older ones still get something sensible.
    ascii_name = filename.encode("ascii", "ignore").decode() or "collections.json"
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{ascii_name}"; '
                 f"filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/{token}/collections.json")
@app.get("/{token}/streaming-collection.json")   # pre-dramas name, still linked
async def private_collections(token: str):
    user = await _require_user(token)
    payload = _collection_export(user)
    if not payload:
        raise HTTPException(404, "No collections are ready for this user")
    return _attachment(payload, collection_filename(user))


@app.get("/{token}/manifest.json")
async def manifest(token: str):
    user = await _require_user(token)
    home_catalogs = await db.get_catalog_defs(token)
    # Advertise standard Stremio/Nuvio horizontal pagination.  Rows currently
    # return one configured page, and can grow deeper without changing URLs.
    catalogs = [
        {
            **catalog,
            "showInHome": True,
            "extra": [{"name": "skip", "isRequired": False}],
        }
        for catalog in home_catalogs
    ]
    # Continue Watching, then Watch History, then Want to Watch, then the
    # nightly slate. These are pinned in the serving layer rather than the
    # builder because they are rebuilt per request, so they can never be part
    # of a stored generation.
    catalogs = (watching.catalog_defs(user) + watchlist.catalog_defs(user)
                + catalogs)
    # Nuvio resolves Collection sources through descriptors on the installed
    # addon's manifest. TV honors showInHome=false; Mobile uses the descriptor's
    # required one-value genre extra. The private definitions carry both guards.
    catalogs.extend(profile_streaming.catalog_defs_for_user(user))
    catalogs.extend(dramas.catalog_defs_for_user(user))
    catalogs.extend(ai_search.catalog_defs(user))
    # The combined type leads because it is this addon's primary content type:
    # the opening rows are all mixed movie+series. Note this does NOT control
    # where rows land on the home screen — that was tested and disproved, since
    # Top Picks is also `all` and renders at the top while other `all` rows do
    # not. Row position is decided client-side.
    types = [config.COMBINED_TYPE] + [t for t in ("movie", "series")
                                      if t != config.COMBINED_TYPE]
    return {
        # Collection resolution is by manifest ID rather than install URL.
        # Give every private picker an opaque identity so another household
        # profile's Daily Picks install can never satisfy this collection.
        "id": profile_streaming.private_addon_id_for_user(user),
        "version": profile_streaming.PRIVATE_MANIFEST_VERSION,
        "name": config.ADDON_NAME,
        "description": "Personal daily recommendations plus private, "
                       "collection-only streaming catalogs from your taste profile.",
        "resources": ["catalog"],
        "types": types,
        "idPrefixes": ["tt"],
        "catalogs": catalogs,
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }


# Profiles whose slate is being rebuilt because their viewer opened the app.
_open_refreshing: set[str] = set()


def _refresh_if_stale(user: dict) -> None:
    """Rebuild a stale slate in the background when its viewer shows up.

    The nightly job skips profiles nobody opened (see scheduler), so this is
    how an occasional viewer's picks catch up: the request that noticed is
    answered from the slate already stored, and the rebuild lands for the
    next visit. One rebuild per profile at a time.
    """
    token = user["token"]
    generated = user.get("last_generated_at") or 0
    if token in _open_refreshing:
        return
    if time.time() - generated < config.OPEN_REFRESH_AFTER_HOURS * 3600:
        return
    _open_refreshing.add(token)

    async def rebuild():
        try:
            await generate_for_user(user, trigger="open-stale")
        finally:
            _open_refreshing.discard(token)

    _spawn(rebuild())


async def _note_top_picks_seen(token: str, page: list[dict]) -> None:
    """Count today as a viewing day for the cards at the front of the page.

    Measurement: it must never make serving fail.
    """
    seen = [m.get("id") for m in page[:config.TOP_PICKS_SEEN_POSITIONS]
            if m.get("id")]
    try:
        rested = await db.record_top_picks_seen(
            token, seen, day=datetime.date.today().isoformat(),
            turn_days=config.TOP_PICKS_TURN_DAYS,
            rest_days=config.TOP_PICKS_REST_DAYS)
    except Exception as exc:
        logger.warning("top picks turn ledger failed: %s", exc)
        return
    if rested:
        logger.info("[%s] top picks: %d title(s) finished their turn and rest "
                    "for %d days", token[:8], len(rested),
                    config.TOP_PICKS_REST_DAYS)


async def _serve_catalog(token: str, ctype: str, catalog_id: str,
                         skip: int = 0, genre: str | None = None,
                         search: str | None = None):
    user = await _require_user(token)
    if ai_search.is_ai_search_catalog_id(catalog_id):
        if not search:
            return {"metas": []}
        metas = await ai_search.search(
            token, search, kid_age=effective_kid_age(user))
        return {"metas": metas[skip:skip + max(1, config.CATALOG_PAGE_SIZE)]}
    _refresh_if_stale(user)
    page_size = max(1, config.CATALOG_PAGE_SIZE)
    if watching.is_watching_catalog_id(catalog_id):
        # Built live, so it is never in the stored catalogs table. Deliberately
        # outside the outcome ledger too: these rows are the viewer's own
        # backlog, and counting a resume as an assisted discovery would make
        # the recommendation measurements flatter.
        metas = await watching.get_metas(user, catalog_id)
        return {"metas": metas[skip:skip + page_size]}

    if watchlist.is_watchlist_catalog_id(catalog_id):
        # Live for the same reason, and outside the outcome ledger for the same
        # reason: a viewer opening the shelf they stocked themselves is not an
        # assisted discovery, and counting it as one would flatter every
        # recommendation measurement.
        metas = await watchlist.get_metas(user)
        return {"metas": metas[skip:skip + page_size]}

    if dramas.is_drama_catalog_id(catalog_id):
        # One nightly build shared by every opted-in viewer, so this is a
        # lookup rather than a per-user render. Kept out of the outcome ledger
        # for the same reason the collection rows are: browsing an imported
        # collection is not a home-row exposure.
        return {"metas": dramas.get_metas(user, catalog_id,
                                          genre)[skip:skip + page_size]}

    if profile_streaming.is_private_catalog_id(catalog_id):
        metas = profile_streaming.get_metas_for_user(user, ctype, catalog_id)
        if metas is None:
            return {"metas": []}
        # Collection browsing is deliberately separate from the Daily Picks
        # outcome ledger: Nuvio may prefetch collection tabs, which is not a
        # home-row exposure or an assisted-pick session.
        return {"metas": metas[skip:skip + page_size]}

    metas = await db.get_catalog_metas(token, ctype, catalog_id)
    if metas is None:
        return {"metas": []}
    if catalog_id == TOP_PICKS_ID:
        # Served whole: the row was made longer so that more is there, and a
        # second page only arrives if the player asks for one.
        page_size = max(page_size, len(metas))
    page = metas[skip:skip + page_size]
    # Record row delivery without claiming that Nuvio's prefetch means the user
    # actually saw every card. An empty pagination probe is not a delivery and
    # must not inflate sessions/exposures. Measurement must never make serving
    # fail when a real page is returned.
    if page:
        try:
            await db.record_catalog_delivery(token, ctype, catalog_id)
        except Exception as exc:
            logger.warning("delivery ledger failed for %s/%s: %s",
                           ctype, catalog_id, exc)
            await db.mark_served(token)
        if catalog_id == TOP_PICKS_ID and skip == 0:
            await _note_top_picks_seen(token, page)
    return {"metas": page}


@app.get("/{token}/catalog/{ctype}/{catalog_id}.json")
async def catalog(token: str, ctype: str, catalog_id: str):
    return await _serve_catalog(token, ctype, catalog_id)


@app.get("/{token}/catalog/{ctype}/{catalog_id}/{extra}.json")
async def catalog_extra(token: str, ctype: str, catalog_id: str, extra: str):
    params = parse_qs(unquote(extra), keep_blank_values=True)
    try:
        skip = max(0, int((params.get("skip") or ["0"])[0]))
    except (TypeError, ValueError):
        skip = 0
    # Drama country rows are addressed by a required one-value `genre` extra,
    # which is how they stay hidden from the home screen while remaining
    # reachable from an imported collection.
    genre = (params.get("genre") or [None])[0]
    search = (params.get("search") or [None])[0]
    return await _serve_catalog(
        token, ctype, catalog_id, skip=skip, genre=genre, search=search)


@app.post("/{token}/refresh")
@app.get("/{token}/refresh")
async def refresh(token: str):
    user = await _require_user(token)
    _spawn(generate_for_user(user, trigger="manual-refresh"))
    _queue_profile_streaming_build(user)
    return {"status": "refreshing"}


# ── taste bootstrapper ───────────────────────────────────────────────────
#
# Reached by opening a per-viewer link on a phone. The token in the path is
# the same one that addresses that viewer's addon, so this grants exactly the
# access they already hand to their player — no new secret, and no new thing
# to leak. Anyone holding the link can shape that viewer's recommendations,
# which is why the page says whose profile it is.

@app.get("/{token}/taste", response_class=HTMLResponse)
async def taste_bootstrap(token: str):
    user = await _require_user(token)
    return BOOTSTRAP_HTML.replace("__VIEWER__", html.escape(user["name"]))


@app.get("/{token}/taste/api/state")
async def taste_state(token: str):
    user = await _require_user(token)
    genres, countries = db.bootstrap_taste(user)
    return {
        "name": user["name"],
        "genres": [{"slug": s, "label": l} for s, l in bootstrap.PICKABLE_GENRES],
        "countries": [{"code": c, "label": l}
                      for c, l in bootstrap.PICKABLE_COUNTRIES],
        "picked_genres": genres,
        "picked_countries": countries,
        "progress": await bootstrap.progress(token),
    }


class TastePicks(BaseModel):
    genres: list[str] = []
    countries: list[str] = []


@app.post("/{token}/taste/api/picks")
async def taste_picks(token: str, body: TastePicks):
    await _require_user(token)
    known = {slug for slug, _ in bootstrap.PICKABLE_GENRES}
    codes = {code for code, _ in bootstrap.PICKABLE_COUNTRIES}
    await db.set_bootstrap_taste(
        token,
        [g for g in body.genres if g in known],
        [c for c in body.countries if c in codes])
    return {"status": "saved"}


class DeckRequest(BaseModel):
    # Ids the phone is already holding. They exist nowhere on the server — a
    # dealt-but-unanswered card is not a verdict — so without them the next
    # deck happily deals the same titles again.
    have: list[str] = []


@app.post("/{token}/taste/api/deck")
async def taste_deck(token: str, body: DeckRequest):
    user = await _require_user(token)
    from app.recs.profile_streaming import private_namespace_for_user
    seen = await bootstrap.already_known(token,
                                         private_namespace_for_user(user))
    seen.update(i for i in body.have if i)
    cards = await bootstrap.deck(user, seen)
    # Recorded as they go out, not as they are answered. Most of a deck is
    # never answered — people close the tab — and those are exactly the cards
    # that were being dealt again on the next visit.
    try:
        await db.record_dealt(token, [c["id"] for c in cards])
    except Exception:
        logger.exception("could not record dealt cards for %s", token[:8])
    return {"cards": cards, "progress": await bootstrap.progress(token)}


@app.get("/{token}/taste/api/trailer/{media}/{tmdb_id}")
async def taste_trailer(token: str, media: str, tmdb_id: int):
    """Whether this title has a trailer, and where to play it from.

    Resolved on tap rather than dealt with the card: a deck is 24 cards and
    this is a TMDB round trip each, so doing it up front would cost every
    session 24 requests to answer a question about the two or three posters
    anybody actually presses.
    """
    await _require_user(token)
    key = await trailers.key_for(media, tmdb_id)
    if not key:
        return {"trailer": None}
    return {"trailer": f"/{token}/taste/api/trailer-video/{key}.mp4"}


async def _trailer_response(key: str, request: Request) -> StreamingResponse:
    """The trailer itself, as a plain MP4 this service serves.

    Not a redirect: the extracted URL is locked to *this machine's* address, so
    handing it to a phone is a 403 as soon as the phone is on mobile data. See
    `app/recs/trailers.py` for why it is not an embed either.

    Shared by the taste page and movie night, which authenticate differently
    and must go on doing so — a movie-night seat is deliberately not a viewer
    token. Only the byte-shifting is common.
    """
    if not trailers.KEY_RE.match(key):
        raise HTTPException(400, "not a video id")
    upstream, client = await trailers.open_stream(key, dict(request.headers))
    if not upstream:
        raise HTTPException(502, "trailer unavailable")

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code,
        headers=trailers.response_headers(upstream),
        media_type=upstream.headers.get("content-type", "video/mp4"))


# A path of its own rather than a child of the route above. Sharing a prefix
# made `/trailer/<key>/video.mp4` match `/trailer/{media}/{tmdb_id}` first, so
# every playback request was a 422 from trying to read "video.mp4" as an
# integer. Route order would fix it and would go on being one edit away from
# breaking again.
@app.get("/{token}/taste/api/trailer-video/{key}.mp4")
async def taste_trailer_video(token: str, key: str, request: Request):
    await _require_user(token)
    return await _trailer_response(key, request)


class Verdict(BaseModel):
    id: str
    type: str = "movie"
    verdict: str


@app.post("/{token}/taste/api/verdict")
async def taste_verdict(token: str, body: Verdict):
    await _require_user(token)
    if body.verdict not in db.VERDICTS and body.verdict != bootstrap.SAVE_VERDICT:
        raise HTTPException(400, "unknown verdict")
    media_type = "movie" if body.type == "movie" else "series"
    if body.verdict == bootstrap.SAVE_VERDICT:
        # Not a verdict, and deliberately not written as one: `taste_feedback`
        # replaces on conflict and every reader of it treats a row as "already
        # seen, keep it out of the rows". A save has to survive being rated
        # later, and has to stay recommendable. See `taste_watchlist`.
        await db.add_to_watchlist(token, body.id, media_type)
        watchlist.invalidate(token)
    else:
        await db.record_feedback(token, body.id, media_type, body.verdict)
    state = await bootstrap.progress(token)
    # Nobody presses "done" — they close the tab. So the rebuild has to happen
    # on its own, at intervals, or a session's work would sit unused until the
    # nightly run.
    if bootstrap.should_rebuild(state["rated"]):
        user = await db.get_user(token)
        if user:
            _spawn(
                generate_for_user(user, trigger="taste-bootstrap"))
            state["rebuilding"] = True
    return {"progress": state}


@app.post("/{token}/taste/api/done")
async def taste_done(token: str):
    """Best-effort end-of-session ping, sent by `navigator.sendBeacon` when the
    page is hidden or closed. Never relied upon: a beacon can be dropped, and
    the interval rebuild above is what actually guarantees the work lands."""
    user = await _require_user(token)
    state = await bootstrap.progress(token)
    if state["rated"]:
        _spawn(generate_for_user(user, trigger="taste-bootstrap"))
    return {"status": "ok", "progress": state}


# ── movie night ──────────────────────────────────────────────────────────
#
# Created from the admin page, then one link per person. Seat links carry
# their own short-lived key rather than a viewer token: a guest gets no access
# to anybody's profile, and a known viewer's seat cannot be used to read or
# change their recommendations either.

class MatchSeat(BaseModel):
    label: str = ""
    user_token: str | None = None


class NewMatch(BaseModel):
    seats: list[MatchSeat]


@app.post("/setup/{secret}/api/movie-night")
async def create_movie_night(secret: str, body: NewMatch):
    _check_setup_secret(secret)
    seats = []
    youngest: int | None = None
    for seat in body.seats:
        token = (seat.user_token or "").strip() or None
        label = seat.label.strip()
        if token:
            user = await db.get_user(token)
            if not user:
                raise HTTPException(400, "unknown viewer on a seat")
            label = label or user["name"]
            age = effective_kid_age(user)
            if age is not None:
                youngest = age if youngest is None else min(youngest, age)
        seats.append({"label": label, "user_token": token})
    try:
        # A room containing a child is filtered to the youngest person in it.
        created = await movienight.create(seats, kid_age=youngest)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return {
        "session_id": created["session_id"],
        "playlist_size": created["playlist_size"],
        "seats": [{
            "seat": s["seat"], "label": s["label"],
            "known": bool(s["user_token"]),
            "url": f"{config.HOST_NAME}/night/{s['seat_key']}",
        } for s in created["seats"]],
    }


async def _require_seat(seat_key: str) -> dict:
    seat = await db.match_by_seat_key(seat_key)
    if not seat:
        raise HTTPException(404, "This movie night has finished or expired")
    return seat


@app.get("/night/{seat_key}", response_class=HTMLResponse)
async def movie_night_page(seat_key: str):
    seat = await _require_seat(seat_key)
    return MOVIE_NIGHT_HTML.replace("__WHO__", html.escape(seat["label"]))


@app.get("/night/{seat_key}/state")
async def movie_night_state(seat_key: str):
    return await movienight.state(await _require_seat(seat_key))


class MatchVote(BaseModel):
    id: str
    want: bool


@app.post("/night/{seat_key}/vote")
async def movie_night_vote(seat_key: str, body: MatchVote):
    seat = await _require_seat(seat_key)
    return await movienight.vote(seat, body.id, body.want)


# Trailers on the seat's own authority. A seat key is deliberately not a viewer
# token — a guest gets no access to anybody's profile — so these cannot reuse
# the taste page's routes, and holding a seat must not become a way in.
@app.get("/night/{seat_key}/trailer/{media}/{tmdb_id}")
async def movie_night_trailer(seat_key: str, media: str, tmdb_id: int):
    await _require_seat(seat_key)
    key = await trailers.key_for(media, tmdb_id)
    if not key:
        return {"trailer": None}
    return {"trailer": f"/night/{seat_key}/trailer-video/{key}.mp4"}


@app.get("/night/{seat_key}/trailer-video/{key}.mp4")
async def movie_night_trailer_video(seat_key: str, key: str, request: Request):
    await _require_seat(seat_key)
    return await _trailer_response(key, request)


@app.get("/health")
async def health():
    return {"status": "ok"}
