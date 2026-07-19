import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import pathlib
import re
import secrets
import signal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)

from app import config

# Settings saved on the /settings dashboard overlay the env file. This must
# run BEFORE the imports below bake env vars into module constants — which is
# also why edits there only land on restart. See app/config.py.
_CONFIG_APPLIED = config.apply_env()
# Validate before importing modules that parse numbers into constants. A bad
# environment now fails with one actionable message instead of a traceback in
# an arbitrary picker/proxy module. Invalid saved files are quarantined by
# config.apply_env() so the service can recover on environment/defaults.
try:
    config.validate_pending()
except ValueError as exc:
    raise RuntimeError(f"invalid stream-picker configuration: {exc}") from exc

from app import (acquire, admin_auth, adminui, connections, dashboard, envref,  # noqa: E402
                 library, meta, overview, picker, probe, proxy, reputation,
                 settings_ui, sources, tbcache, telemetry, usenet,
                 usenet_health, vprobe, wizard)

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

_READY = False
_RESTART_TASK: asyncio.Task | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _READY
    if _CONFIG_APPLIED:
        logger.info("config: %d setting(s) overlaid from config.json onto "
                    "the environment", _CONFIG_APPLIED)
    data_dir = config.ensure_storage()
    if await adminui.migrate_legacy():
        logger.info("admin: migrated explicit dashboard credentials to scrypt")
    proxy.load()
    _READY = True
    logger.info("startup complete; persistent data writable at %s", data_dir)
    try:
        yield
    finally:
        _READY = False
        logger.info("shutdown: draining background work and upstream clients")
        hooks = []
        names = []
        for name, module in (
            ("proxy", proxy), ("picker", picker), ("probe", probe),
            ("metadata", meta), ("library", library), ("acquire", acquire),
            ("video probe", vprobe), ("sources", sources), ("usenet", usenet),
            ("tbcache", tbcache),
        ):
            shutdown = getattr(module, "shutdown", None)
            if shutdown is not None:
                names.append(name)
                hooks.append(shutdown())
        client = getattr(connections, "_client", None)
        if client is not None:
            names.append("settings client")
            hooks.append(client.aclose())
        results = await asyncio.gather(*hooks, return_exceptions=True)
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                logger.error("shutdown: %s cleanup failed: %s", name, result,
                             exc_info=(type(result), result,
                                       result.__traceback__))


app = FastAPI(lifespan=_lifespan)


@app.middleware("http")
async def _log_request(request: Request, call_next):
    # Logs the real client (Caddy forwards X-Forwarded-For) and the User-Agent
    # so we can tell which app is actually hitting which endpoint — e.g. whether
    # Nuvio is querying /slow/ at all, vs. bots hitting the bare domain.
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if (request.url.path in ("/", "/settings", "/stats")
            or request.url.path.startswith("/api/")):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; media-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
    client = adminui.client_ip(request) or "untrusted-forwarder"
    ua = (request.headers.get("user-agent") or "-")[:70]
    # The addon secret is a path segment — mask it so logs can't leak an
    # installable addon URL.  Proxy tokens are short-lived playback capabilities
    # and must be masked for the same reason.
    path = request.url.path.replace(SECRET, "<secret>")
    path = re.sub(r"^/proxy/[^/]+", "/proxy/<token>", path)
    path = re.sub(r"^/library/[^/]+", "/library/<token>", path)
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


def _setup_local(request: Request) -> None:
    """First-run enrollment is always local, even if the dashboard is public."""
    if not adminui.is_local(request):
        raise HTTPException(status_code=404)


async def _admin(request: Request, *, mutation: bool = False) -> None:
    """Apply network, authentication, and (for writes) CSRF boundaries."""
    if adminui.setup_required():
        _setup_local(request)
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=428,
                                detail="administrator setup required")
        raise HTTPException(status_code=307, headers={"Location": "/"})
    local_only = os.environ.get("DASHBOARD_LOCAL_ONLY", "1") not in (
        "0", "false", "no", "off", "")
    if local_only and not adminui.is_local(request):
        raise HTTPException(status_code=404)
    await adminui.require_auth(request)
    if mutation:
        adminui.require_csrf(request)


def _readiness() -> tuple[bool, str]:
    if not _READY:
        return False, "startup not complete"
    try:
        config.validate_pending()
    except ValueError as exc:
        return False, f"configuration invalid: {exc}"
    if not config.storage_ready():
        return False, "persistent data directory is not writable"
    return True, "ready"


@app.get("/health/live")
async def health_live():
    return {"ok": True, "status": "alive"}


@app.get("/health/ready")
async def health_ready():
    ok, detail = _readiness()
    return JSONResponse({"ok": ok, "status": detail},
                        status_code=200 if ok else 503)


@app.get("/health")
async def health():
    """Compatibility/readiness endpoint used by the container healthcheck."""
    return await health_ready()


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


@app.api_route("/library/{token}", methods=["GET", "HEAD"])
async def library_playback(token: str, request: Request):
    """Opaque native-Jellyfin playback URL; credentials stay server-side."""
    return await library.serve(token, request)


# ── local admin dashboard (clean paths, no secret; see _admin guard) ─────────
def _addon_links(request: Request) -> list[tuple[str, str]]:
    """Manifest URLs for every picker variant, ready to paste into Stremio.
    Prefer the configured public URL; a plain-LAN install falls back to the
    address the dashboard itself was reached on."""
    base = (os.environ.get("ADDON_PUBLIC_URL") or "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return [
        (ADDON_NAME, f"{base}/{SECRET}/manifest.json"),
        (SLOW_NAME, f"{base}/{SECRET}/slow/manifest.json"),
        (f"{ADDON_NAME} (Mobile)", f"{base}/{SECRET}/mobile/manifest.json"),
        (f"{SLOW_NAME} (Mobile)",
         f"{base}/{SECRET}/slow/mobile/manifest.json"),
    ]


@app.get("/")
async def dash_home(request: Request):
    if adminui.setup_required():
        _setup_local(request)
        return HTMLResponse(adminui.setup_page(ADDON_NAME))
    await _admin(request)
    if wizard.needed():
        # Fresh install with no stream source yet: the overview would be an
        # empty ledger, so the home tab walks through setup instead.
        return HTMLResponse(wizard.render())
    return HTMLResponse(overview.render(telemetry.load(),
                                        addons=_addon_links(request)))


@app.get("/setup")
async def setup_wizard(request: Request):
    """The guided first-run setup; revisitable any time by URL."""
    await _admin(request)
    return HTMLResponse(wizard.render())


@app.post("/api/setup/apply")
async def setup_apply(request: Request):
    """Mint source URLs from the wizard's picks, live-test them, save what
    passed. Same mutation gate as every settings write."""
    await _admin(request, mutation=True)
    body = await _json_body(request)
    try:
        return await wizard.apply(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/setup", status_code=201)
async def admin_setup(request: Request):
    """Create the one and only administrator account from a local browser."""
    _setup_local(request)
    if not adminui.setup_required():
        raise HTTPException(status_code=409,
                            detail="administrator account is already initialized")
    adminui.require_csrf(request)
    body = await _json_body(request)
    username = body.get("username")
    password = body.get("password")
    confirmation = body.get("confirmation")
    if not all(isinstance(v, str)
               for v in (username, password, confirmation)):
        raise HTTPException(status_code=400, detail="all account fields are required")
    if password != confirmation:
        raise HTTPException(status_code=400, detail="passwords do not match")
    try:
        username = await adminui.create_account(username, password)
    except admin_auth.AccountExistsError:
        raise HTTPException(status_code=409,
                            detail="administrator account is already initialized")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("admin: first-run administrator account initialized")
    return {"ok": True, "username": username}


@app.get("/settings")
async def settings_page(request: Request):
    await _admin(request)
    return HTMLResponse(settings_ui.render())


@app.get("/stats")
async def stats(request: Request, min_n: int = 3):
    await _admin(request)
    blocks = reputation.listing() + usenet_health.blocked_listing()
    return HTMLResponse(dashboard.render(telemetry.load(), blocks, min_n=min_n))


@app.get("/api/stats.json")
async def stats_json(request: Request, min_n: int = 3):
    await _admin(request)
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


@app.post("/api/unblock")
async def unblock(request: Request, sig: str):
    await _admin(request, mutation=True)
    if sig.startswith("nzb:"):
        usenet_health.unblock(sig)
    else:
        reputation.unblock(sig)
    return RedirectResponse(url="/stats", status_code=303)


@app.post("/api/decode/clear")
async def decode_clear(request: Request, key: str):
    await _admin(request, mutation=True)
    from app import decode_health
    decode_health.clear(key)
    return RedirectResponse(url="/stats", status_code=303)


@app.post("/api/nzb-indexer/clear")
async def nzb_indexer_clear(request: Request, name: str):
    await _admin(request, mutation=True)
    usenet_health.clear_fetch_health(name)
    return RedirectResponse(url="/stats", status_code=303)


@app.get("/api/settings/status.json")
async def settings_status(request: Request):
    await _admin(request)
    return {"playing": proxy.active_streams(),
            "restart_pending": config.restart_pending()}


@app.get("/api/admin/csrf")
async def admin_csrf(request: Request):
    """Issue the process-local mutation token to an authenticated operator."""
    await _admin(request)
    return {"csrf_token": adminui.csrf_token()}


@app.post("/api/settings/save")
async def settings_save(request: Request):
    await _admin(request, mutation=True)
    body = await _json_body(request)
    try:
        return config.save(dict(body.get("values") or {}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/settings/test/{service}")
async def settings_test(service: str, request: Request):
    await _admin(request, mutation=True)
    body = await _json_body(request)
    try:
        return await connections.test(service, dict(body.get("values") or {}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/settings/export.env")
async def settings_export(request: Request):
    # The current effective config as a ready-to-edit .env (secrets redacted).
    await _admin(request)
    return PlainTextResponse(
        envref.current_dotenv(),
        headers={"Content-Disposition":
                 "attachment; filename=stream-picker.env"})


@app.post("/api/settings/restart")
async def settings_restart(request: Request):
    # SIGTERM lets Uvicorn stop accepting work and execute the lifespan cleanup;
    # the container's restart policy then brings the process back with new config.
    await _admin(request, mutation=True)
    try:
        config.validate_pending()
    except ValueError as exc:
        raise HTTPException(status_code=409,
                            detail=f"configuration cannot restart: {exc}")
    global _RESTART_TASK
    if _RESTART_TASK is None or _RESTART_TASK.done():
        _RESTART_TASK = asyncio.create_task(_signal_restart())
    logger.info("settings: graceful restart requested")
    return {"ok": True}


async def _json_body(request: Request, limit: int = 256 * 1024) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json required")
    declared = request.headers.get("content-length", "")
    if declared:
        try:
            if int(declared) > limit:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="request body too large")
    try:
        value = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return value


async def _signal_restart() -> None:
    await asyncio.sleep(0.6)  # allow the JSON response to reach the browser
    os.kill(os.getpid(), signal.SIGTERM)


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
