import asyncio
import logging
import os
import pathlib
import re
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)

from app import config

# Settings saved on the /settings dashboard overlay the env file. This must
# run BEFORE the imports below bake env vars into module constants — which is
# also why edits there only land on restart. See app/config.py.
_CONFIG_APPLIED = config.apply_env()

from app import (adminui, connections, dashboard, envref,  # noqa: E402
                 overview, picker, proxy, reputation, settings_ui,
                 telemetry, usenet_health)

NOTICE_FILE = pathlib.Path(__file__).parent / "static" / "notice.mp4"
NOTICE_THEATRICAL_FILE = (pathlib.Path(__file__).parent / "static"
                          / "notice_theatrical.mp4")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
# httpx logs every request URL at INFO — those include debrid API keys
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("stream-picker")

SECRET = os.environ["ADDON_SECRET"]
ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

app = FastAPI()


@app.middleware("http")
async def _log_request(request: Request, call_next):
    # Logs the real client (Caddy forwards X-Forwarded-For) and the User-Agent
    # so we can tell which app is actually hitting which endpoint — e.g. whether
    # Nuvio is querying /slow/ at all, vs. bots hitting the bare domain.
    resp = await call_next(request)
    client = request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else "-")
    ua = (request.headers.get("user-agent") or "-")[:70]
    # The addon secret is a path segment — mask it so logs can't leak an
    # installable addon URL.  Proxy tokens are short-lived playback capabilities
    # and must be masked for the same reason.
    path = request.url.path.replace(SECRET, "<secret>")
    path = re.sub(r"^/proxy/[^/]+", "/proxy/<token>", path)
    logger.info(f'req {client} "{request.method} {path}" '
                f'{resp.status_code} ua="{ua}"')
    return resp

SLOW_NAME = os.environ.get("SLOW_ADDON_NAME", f"{ADDON_NAME} (Best Quality)")

MANIFEST = {
    "id": "org.streampicker.auto",
    "version": "1.0.0",
    "name": ADDON_NAME,
    "description": "Races debrid and direct usenet sources, verifies playback, "
                   "and puts a high-quality English/original-language stream "
                   "first.",
    "resources": [{"name": "stream", "types": ["movie", "series"],
                   "idPrefixes": ["tt"]}],
    "types": ["movie", "series"],
    "catalogs": [],
    "behaviorHints": {"configurable": False, "configurationRequired": False},
}

# The slow / best-quality sibling addon. Same process, same searches — it just
# waits for every source to finish and ranks harder (TRaSH-guides scoring)
# before answering, so install it alongside the fast one, not instead of it.
SLOW_MANIFEST = {
    **MANIFEST,
    "id": "org.streampicker.auto.slow",
    "name": SLOW_NAME,
    "description": "Waits for every source to finish, digs through all of "
                   "them, and returns the best quality that actually plays "
                   "(TRaSH-guides ranked). Slower to first answer; reuses the "
                   "fast picker's search so it won't double API calls.",
}


def _check(secret: str) -> None:
    if not secrets.compare_digest(secret, SECRET):
        raise HTTPException(status_code=404)


def _admin(request: Request) -> None:
    """Gate the local admin dashboard. On by default: only loopback/LAN/Docker
    clients may reach it, never the public reverse proxy. DASHBOARD_LOCAL_ONLY=0
    lifts it (do that only behind your own auth)."""
    local_only = os.environ.get("DASHBOARD_LOCAL_ONLY", "1") not in (
        "0", "false", "no", "off", "")
    if local_only and not adminui.is_local(request):
        raise HTTPException(status_code=404)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/notice.mp4")
async def notice():
    # The "being added" placeholder video the slow picker points at when it has
    # to send a title to Sonarr/Radarr. Ungated on purpose — it's not sensitive.
    return FileResponse(NOTICE_FILE, media_type="video/mp4",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/notice_theatrical.mp4")
async def notice_theatrical():
    # The "not out yet" placeholder shown when no proper digital release exists
    # (cam/theatrical only, or not aired) and nothing was sent to Sonarr/Radarr.
    return FileResponse(NOTICE_THEATRICAL_FILE, media_type="video/mp4",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/{secret}/manifest.json")
async def manifest(secret: str):
    _check(secret)
    return MANIFEST


@app.get("/{secret}/mobile/manifest.json")
async def manifest_mobile(secret: str):
    _check(secret)
    return {**MANIFEST, "id": MANIFEST["id"] + ".mobile",
            "name": f"{ADDON_NAME} (Mobile)",
            "description": "Bandwidth-capped variant for phones/tablets: "
                           "1080p max, modest file bitrates."}


@app.get("/{secret}/slow/manifest.json")
async def manifest_slow(secret: str):
    _check(secret)
    return SLOW_MANIFEST


@app.get("/{secret}/slow/mobile/manifest.json")
async def manifest_slow_mobile(secret: str):
    _check(secret)
    return {**SLOW_MANIFEST, "id": SLOW_MANIFEST["id"] + ".mobile",
            "name": f"{SLOW_NAME} (Mobile)",
            "description": "Best-quality picker, bandwidth-capped for "
                           "phones/tablets: 1080p max, modest file bitrates."}


async def _streams(media: str, media_id: str, profile: str, slow: bool = False):
    if media not in ("movie", "series"):
        return JSONResponse({"streams": []})
    # Tag every probe this request spawns with what's being watched + which
    # picker, so the telemetry log can attribute a slow source to a title.
    telemetry.request_ctx.set(
        {"media": media, "media_id": media_id,
         "picker": ("slow" if slow else "fast") + ("/mob" if profile == "mobile" else "")})
    fn = picker.pick_slow if slow else picker.pick
    try:
        streams = await fn(media, media_id, profile)
    except Exception:
        logger.exception(f"pick failed for {media}/{media_id}")
        streams = []
    if streams:
        telemetry.record_served(streams[0])   # log real host/source before rewrite
    picker_label = ("slow" if slow else "fast") + ("/mob" if profile == "mobile" else "")
    streams = proxy.wrap(streams, media, media_id, picker_label)  # → /proxy URLs
    return JSONResponse({"streams": picker.clean_output(streams)})


@app.get("/{secret}/stream/{media}/{media_id}.json")
async def stream(secret: str, media: str, media_id: str):
    _check(secret)
    return await _streams(media, media_id, "full")


@app.get("/{secret}/mobile/stream/{media}/{media_id}.json")
async def stream_mobile(secret: str, media: str, media_id: str):
    _check(secret)
    return await _streams(media, media_id, "mobile")


@app.get("/{secret}/slow/stream/{media}/{media_id}.json")
async def stream_slow(secret: str, media: str, media_id: str):
    _check(secret)
    return await _streams(media, media_id, "full", slow=True)


@app.get("/{secret}/slow/mobile/stream/{media}/{media_id}.json")
async def stream_slow_mobile(secret: str, media: str, media_id: str):
    _check(secret)
    return await _streams(media, media_id, "mobile", slow=True)


# ── local admin dashboard (clean paths, no secret; see _admin guard) ─────────
@app.get("/")
async def dash_home(request: Request):
    _admin(request)
    return HTMLResponse(overview.render(telemetry.load()))


@app.get("/settings")
async def settings_page(request: Request):
    _admin(request)
    return HTMLResponse(settings_ui.render())


@app.get("/stats")
async def stats(request: Request, min_n: int = 3):
    _admin(request)
    blocks = reputation.listing() + usenet_health.blocked_listing()
    return HTMLResponse(dashboard.render(telemetry.load(), blocks, min_n=min_n))


@app.get("/api/stats.json")
async def stats_json(request: Request, min_n: int = 3):
    _admin(request)
    recs = telemetry.load()
    return {
        "records": len(recs),
        "by_source": telemetry.aggregate(recs, "src", min_n=min_n),
        "by_debrid": telemetry.aggregate(recs, "debrid", min_n=min_n),
        "by_group": telemetry.aggregate(recs, "grp", min_n=min_n),
        "nzb_indexers": usenet_health.indexer_listing(),
        "nzb_blocked": usenet_health.blocked_listing(),
        "nzb_failure_samples": telemetry.aggregate_usenet_failures(recs),
    }


@app.get("/api/unblock")
async def unblock(request: Request, sig: str):
    _admin(request)
    if sig.startswith("nzb:"):
        usenet_health.unblock(sig)
    else:
        reputation.unblock(sig)
    return RedirectResponse(url="/stats", status_code=303)


@app.get("/api/settings/status.json")
async def settings_status(request: Request):
    _admin(request)
    return {"playing": proxy.active_streams(),
            "restart_pending": config.restart_pending()}


@app.post("/api/settings/save")
async def settings_save(request: Request):
    _admin(request)
    body = await request.json()
    try:
        return config.save(dict(body.get("values") or {}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/settings/test/{service}")
async def settings_test(service: str, request: Request):
    _admin(request)
    body = await request.json()
    try:
        return await connections.test(service, dict(body.get("values") or {}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/settings/export.env")
async def settings_export(request: Request):
    # The current effective config as a ready-to-edit .env (secrets redacted).
    _admin(request)
    return PlainTextResponse(
        envref.current_dotenv(),
        headers={"Content-Disposition":
                 "attachment; filename=stream-picker.env"})


@app.post("/api/settings/restart")
async def settings_restart(request: Request):
    # Clean exit; the container's restart policy brings the process back up
    # with the saved config applied. The delay lets this response flush.
    _admin(request)
    logger.info("settings: restart requested — exiting to apply saved config")
    asyncio.get_running_loop().call_later(0.6, os._exit, 0)
    return {"ok": True}


@app.on_event("startup")
async def _startup():
    if _CONFIG_APPLIED:
        logger.info(f"config: {_CONFIG_APPLIED} setting(s) overlaid from "
                    "config.json onto the environment")
    proxy.load()


@app.api_route("/proxy/{token}", methods=["GET", "HEAD"])
async def proxy_stream(token: str, request: Request):
    # No secret gate: the token itself is an unguessable capability, and the
    # player must fetch it without our addon secret.
    return await proxy.serve(token, request)


@app.api_route("/proxy/{token}/hls", methods=["GET", "HEAD"])
async def proxy_hls(token: str, request: Request):
    # HLS sub-resources (variants, segments, keys) rewritten into the playlist
    # by app.hlsproxy; each URL carries an HMAC binding it to this token, so
    # this cannot be used as an open proxy.
    return await proxy.serve_hls(token, request)
