"""Native Jellyfin library source and credential-free playback proxy.

Titles already owned by the user are resolved directly from Jellyfin's native
API.  An exact, cached IMDb index handles movies and series; episodes are then
matched strictly by season/episode coordinates.  No Jellio plugin is involved.

Jellyfin user tokens never leave this process.  Stream rows contain an opaque,
HMAC-signed URL on StreamPicker; :func:`serve` validates that capability and
range-proxies the exact item/media-source while injecting ``X-Emby-Token`` on
the internal upstream request.  This preserves direct-play seeking without
putting a Jellyfin bearer token in player URLs, logs, or proxy sessions.

Files whose video codec a player can decode (H.264/HEVC/VP9/AV1) are served
byte-for-byte as above.  A codec it cannot (MPEG-2, XviD/DivX, VC-1, WMV) would
otherwise play audio-only, so it is instead routed through a Jellyfin transcode
and served back as HLS: the master and variant playlists are fetched
server-side, the token is stripped from every URI, and each is rewritten to a
signed sub-resource URL that we proxy — keeping the token server-side there too.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import (parse_qsl, quote, urlencode, urljoin, urlsplit,
                          urlunsplit)

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse


logger = logging.getLogger("stream-picker")

JELLYFIN_URL = (os.environ.get("JELLYFIN_URL") or "").rstrip("/")
JELLYFIN_USERNAME = os.environ.get("JELLYFIN_USERNAME") or ""
JELLYFIN_PASSWORD = os.environ.get("JELLYFIN_PASSWORD") or ""
PUBLIC_URL = os.environ.get("ADDON_PUBLIC_URL", "http://localhost:8011").rstrip("/")
TIMEOUT = float(os.environ.get("JELLYFIN_TIMEOUT", "8"))
INDEX_TTL = float(os.environ.get("JELLYFIN_INDEX_TTL", "300"))
NEG_TTL = float(os.environ.get("JELLYFIN_NEG_TTL", "60"))
STREAM_TOKEN_TTL = float(os.environ.get("JELLYFIN_STREAM_TOKEN_TTL", "604800"))
TRANSCODE = os.environ.get("JELLYFIN_TRANSCODE", "1") not in ("0", "false", "")

# Video codecs a player can hand to its own decoder unchanged. Anything else
# (mpeg2video, mpeg4/xvid/divx, vc1, wmv3, msmpeg4*, …) plays audio-only on a
# player that lacks that decoder, so it is routed through a Jellyfin transcode.
_DIRECT_PLAY_VIDEO = {"h264", "avc", "hevc", "h265", "vp8", "vp9", "av1"}

# Sent to PlaybackInfo so Jellyfin computes a transcode plan (its TranscodingUrl)
# targeting a universally decodable H.264/AAC HLS ladder for the exotic sources.
_PLAY_PROFILE = {
    "MaxStreamingBitrate": 120_000_000,
    "MaxStaticBitrate": 100_000_000,
    "DirectPlayProfiles": [{
        "Container": "mp4,m4v,mkv,webm,mov,ts", "Type": "Video",
        "VideoCodec": "h264,hevc,vp8,vp9,av1",
        "AudioCodec": "aac,mp3,ac3,eac3,opus,flac,vorbis,mp2",
    }],
    "TranscodingProfiles": [{
        "Container": "ts", "Type": "Video", "Protocol": "hls",
        "VideoCodec": "h264", "AudioCodec": "aac,mp3,ac3",
        "Context": "Streaming", "MinSegments": 1, "BreakOnNonKeyFrames": True,
    }],
    "CodecProfiles": [],
    "SubtitleProfiles": [{"Format": "vtt", "Method": "Hls"}],
}

_ADDON_SECRET = (os.environ.get("ADDON_SECRET") or "").encode("utf-8")
_DEVICE_ID = hashlib.sha256(
    b"stream-picker/jellyfin/device/" + _ADDON_SECRET).hexdigest()[:32]
_AUTHORIZATION = (
    f'MediaBrowser Client="StreamPicker", Device="StreamPicker Server", '
    f'DeviceId="{_DEVICE_ID}", Version="1.0"')

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(connect=TIMEOUT, read=None, write=TIMEOUT, pool=TIMEOUT),
    headers={"User-Agent": "StreamPicker/1.0"},
)
_auth_lock = asyncio.Lock()
_access_token = ""
_user_id = ""

# monotonic timestamp + exact lowercase IMDb -> compact Movie/Series records
_index: tuple[float, dict[str, list[dict]]] | None = None
_index_lock = asyncio.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}

_IDENTITY_TRUST_KEY = "_library_identity_trust"
_IDENTITY_TRUST_SENTINEL = object()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SAFE_CONTAINER = re.compile(r"^[A-Za-z0-9]{1,12}$")


def enabled() -> bool:
    return bool(JELLYFIN_URL and JELLYFIN_USERNAME and JELLYFIN_PASSWORD)


def _provider_imdb(item: dict | None) -> str:
    for key, value in ((item or {}).get("ProviderIds") or {}).items():
        if str(key).lower() == "imdb":
            value = str(value or "").strip().lower()
            return value if re.fullmatch(r"tt\d{5,12}", value) else ""
    return ""


async def _authenticate(*, force: bool = False,
                        stale_token: str = "") -> tuple[str, str]:
    """Return (access token, user id), coalescing concurrent logins."""
    global _access_token, _user_id
    if _access_token and _user_id and not force:
        return _access_token, _user_id
    if not enabled():
        raise RuntimeError("native Jellyfin credentials are incomplete")
    async with _auth_lock:
        # Another request may already have refreshed the exact token that this
        # caller saw fail. Reuse its new session instead of serially creating a
        # second one after waiting for the lock.
        if (force and stale_token and _access_token and _user_id
                and _access_token != stale_token):
            return _access_token, _user_id
        if _access_token and _user_id and not force:
            return _access_token, _user_id
        response = await _client.post(
            f"{JELLYFIN_URL}/Users/AuthenticateByName",
            headers={"Authorization": _AUTHORIZATION},
            json={"Username": JELLYFIN_USERNAME, "Pw": JELLYFIN_PASSWORD},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("AccessToken") or "")
        user_id = str((payload.get("User") or {}).get("Id") or "")
        if not token or not _SAFE_ID.fullmatch(user_id):
            raise RuntimeError("Jellyfin authentication returned an incomplete session")
        _access_token, _user_id = token, user_id
        logger.info("jellyfin: authenticated native library session")
        return token, user_id


async def _api(method: str, path: str, **kwargs) -> httpx.Response:
    """Authenticated API request with one forced re-login after a 401."""
    failed_token = ""
    for attempt in range(2):
        token, _ = await _authenticate(
            force=attempt == 1, stale_token=failed_token)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Emby-Token"] = token
        response = await _client.request(
            method, f"{JELLYFIN_URL}{path}", headers=headers,
            timeout=kwargs.pop("timeout", TIMEOUT), **kwargs)
        if response.status_code != 401 or attempt:
            response.raise_for_status()
            return response
        failed_token = token
        await response.aclose()
    raise RuntimeError("Jellyfin authentication retry failed")


async def _build_index() -> dict[str, list[dict]]:
    """Fetch the user-visible Movie/Series inventory and build an exact map."""
    _, user_id = await _authenticate()
    found: dict[str, list[dict]] = {}
    start, page_size = 0, 1000
    while True:
        response = await _api(
            "GET", f"/Users/{quote(user_id, safe='')}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "ProviderIds",
                "EnableImages": "false",
                "EnableUserData": "false",
                "EnableTotalRecordCount": "true",
                "StartIndex": start,
                "Limit": page_size,
            },
        )
        payload = response.json()
        items = payload.get("Items") or []
        for raw in items:
            imdb = _provider_imdb(raw)
            item_id = str(raw.get("Id") or "")
            typ = str(raw.get("Type") or "")
            if not imdb or typ not in ("Movie", "Series") or not _SAFE_ID.fullmatch(item_id):
                continue
            found.setdefault(imdb, []).append({
                "Id": item_id,
                "Type": typ,
                "ProviderIds": {"Imdb": imdb},
                "Name": str(raw.get("Name") or ""),
                "ProductionYear": raw.get("ProductionYear"),
            })
        start += len(items)
        total = payload.get("TotalRecordCount")
        if not items or len(items) < page_size or (
                isinstance(total, int) and start >= total):
            break
        if start > 100_000:
            raise RuntimeError("Jellyfin library pagination exceeded safety limit")
    logger.info("jellyfin: indexed %d exact IMDb ids", len(found))
    return found


async def _library_index(*, force: bool = False) -> dict[str, list[dict]]:
    global _index
    now = time.monotonic()
    if _index and not force and now - _index[0] < INDEX_TTL:
        return _index[1]
    async with _index_lock:
        now = time.monotonic()
        if _index and not force and now - _index[0] < INDEX_TTL:
            return _index[1]
        try:
            fresh = await _build_index()
        except Exception:
            if _index:
                logger.warning("jellyfin: index refresh failed; serving stale index")
                return _index[1]
            raise
        _index = (time.monotonic(), fresh)
        return fresh


async def _episode_items(series: dict, season: int, episode: int) -> list[dict]:
    _, user_id = await _authenticate()
    response = await _api(
        "GET", f"/Shows/{quote(series['Id'], safe='')}/Episodes",
        params={
            "UserId": user_id,
            "Season": season,
            "Fields": "ProviderIds",
            "EnableImages": "false",
            "EnableUserData": "false",
        },
    )
    out = []
    for item in response.json().get("Items") or []:
        item_id = str(item.get("Id") or "")
        try:
            coords = (int(item.get("ParentIndexNumber")), int(item.get("IndexNumber")))
        except (TypeError, ValueError):
            continue
        if (str(item.get("Type") or "") == "Episode"
                and coords == (season, episode)
                and _SAFE_ID.fullmatch(item_id)):
            out.append(item)
    return out


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _mint(item_id: str, source_id: str, container: str,
          play_session_id: str = "", mode: str = "d") -> str:
    payload = {
        "e": int(time.time() + max(STREAM_TOKEN_TTL, 60)),
        "i": item_id,
        "m": source_id,
        "c": container,
    }
    if mode == "h":
        payload["k"] = "h"          # HLS transcode rather than direct play
    if play_session_id and _SAFE_ID.fullmatch(play_session_id):
        payload["p"] = play_session_id
    body = _b64(json.dumps(payload, separators=(",", ":"),
                           sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(
        _ADDON_SECRET, b"jellyfin-playback/v1/" + body.encode("ascii"),
        hashlib.sha256).digest())
    return f"{PUBLIC_URL}/library/{body}.{signature}"


def _decode_capability(token: str) -> dict:
    try:
        body, supplied = token.split(".", 1)
        if len(body) > 1024 or len(supplied) > 128:
            raise ValueError
        expected = _b64(hmac.new(
            _ADDON_SECRET, b"jellyfin-playback/v1/" + body.encode("ascii"),
            hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        payload = json.loads(_unb64(body))
        item_id = str(payload.get("i") or "")
        source_id = str(payload.get("m") or "")
        container = str(payload.get("c") or "")
        expires = int(payload.get("e") or 0)
        play_session = str(payload.get("p") or "")
        mode = str(payload.get("k") or "d")
        if (not _SAFE_ID.fullmatch(item_id)
                or not _SAFE_ID.fullmatch(source_id)
                or not _SAFE_CONTAINER.fullmatch(container)
                or mode not in ("d", "h")
                or expires < int(time.time())
                or expires > int(time.time() + max(STREAM_TOKEN_TTL, 60) + 300)
                or (play_session and not _SAFE_ID.fullmatch(play_session))):
            raise ValueError
        return {"item": item_id, "source": source_id,
                "container": container.lower(), "session": play_session,
                "mode": mode}
    except Exception as exc:
        raise ValueError("invalid or expired library playback capability") from exc


def _resolution(source: dict) -> str:
    video = next((x for x in source.get("MediaStreams") or []
                  if str(x.get("Type") or "").lower() == "video"), None)
    height = int((video or {}).get("Height") or 0)
    if height >= 2000:
        return "4K"
    if height >= 1000:
        return "1080P"
    if height >= 700:
        return "720P"
    if height:
        return f"{height}P"
    return ""


async def _item_streams(item: dict) -> list[dict]:
    _, user_id = await _authenticate()
    response = await _api(
        "POST", f"/Items/{quote(str(item['Id']), safe='')}/PlaybackInfo",
        params={"UserId": user_id},
        json={"UserId": user_id},
    )
    payload = response.json()
    play_session = str(payload.get("PlaySessionId") or "")
    out = []
    for source in payload.get("MediaSources") or []:
        source_id = str(source.get("Id") or "")
        container = str(source.get("Container") or "").lower().lstrip(".")
        if not _SAFE_ID.fullmatch(source_id) or not _SAFE_CONTAINER.fullmatch(container):
            continue
        filename = os.path.basename(str(source.get("Path") or ""))
        if not filename:
            filename = str(source.get("Name") or item.get("Name") or "Library file")
        video = next((x for x in source.get("MediaStreams") or []
                      if str(x.get("Type") or "").lower() == "video"), None)
        vcodec = str((video or {}).get("Codec") or "").lower()
        direct = (not vcodec) or vcodec in _DIRECT_PLAY_VIDEO
        if not direct and not TRANSCODE:
            # A codec the player can't decode, with transcoding disabled: drop
            # it rather than hand over a file that plays audio-only.
            logger.info("jellyfin: %s skipped (codec %r not direct-play, "
                        "transcode disabled)", filename, vcodec)
            continue
        mode = "d" if direct else "h"
        quality = _resolution(source)
        stream = {
            "url": _mint(str(item["Id"]), source_id, container,
                         play_session, mode),
            "name": f"📚 Library{(' ' + quality) if quality else ''}",
            "title": filename,
            "behaviorHints": {"filename": filename},
            "_source_key": "library",
            "_library_identity_confidence": "strong",
            "_library_identity_evidence": "jellyfin-imdb",
            _IDENTITY_TRUST_KEY: _IDENTITY_TRUST_SENTINEL,
        }
        if mode == "h":
            logger.info("jellyfin: %s → transcode lane (video codec %r)",
                        filename, vcodec or "unknown")
        if source.get("Size"):
            try:
                stream["behaviorHints"]["videoSize"] = int(source["Size"])
            except (TypeError, ValueError):
                pass
        if video and video.get("BitRate"):
            try:
                stream["_vbitrate"] = int(video["BitRate"])
            except (TypeError, ValueError):
                pass
        out.append(stream)
    return out


async def streams(media: str, media_id: str) -> list[dict]:
    """Return exact native-Jellyfin streams, or an empty list when absent."""
    if not enabled() or media not in ("movie", "series"):
        return []
    key = f"{media}:{media_id.lower()}"
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < (INDEX_TTL if cached[1] else NEG_TTL):
        return cached[1]
    stale = cached[1] if cached else []
    try:
        base = media_id.split(":", 1)[0].lower()
        if not re.fullmatch(r"tt\d{5,12}", base):
            return []
        index = await _library_index()
        matches = index.get(base) or []
        items: list[dict] = []
        if media == "movie":
            if ":" in media_id:
                return []
            items = [x for x in matches if x.get("Type") == "Movie"]
        else:
            parts = media_id.split(":")
            if (len(parts) != 3 or not parts[1].isdigit()
                    or not parts[2].isdigit()):
                return []
            season, episode = int(parts[1]), int(parts[2])
            if season < 0 or episode < 0:
                return []
            series = [x for x in matches if x.get("Type") == "Series"]
            episode_lists = await asyncio.gather(
                *(_episode_items(x, season, episode) for x in series))
            items = [item for group in episode_lists for item in group]
        groups = await asyncio.gather(*(_item_streams(item) for item in items))
        out = [stream for group in groups for stream in group]
        _cache[key] = (time.monotonic(), out)
        if len(_cache) > 500:
            _cache.pop(next(iter(_cache)))
        if out:
            logger.info("jellyfin %s: %d native library stream(s)", key, len(out))
        return out
    except Exception as exc:
        logger.warning("jellyfin %s: %s", key, type(exc).__name__)
        return stale


def identity_trusted(stream: dict) -> bool:
    """True only for a stream minted from our exact native IMDb lookup."""
    return stream.get(_IDENTITY_TRUST_KEY) is _IDENTITY_TRUST_SENTINEL


_REQUEST_HEADERS = ("range", "if-range", "if-none-match", "if-modified-since")
_RESPONSE_HEADERS = (
    "accept-ranges", "cache-control", "content-disposition", "content-length",
    "content-range", "content-type", "etag", "last-modified",
)


async def _open_upstream(method: str, url: str,
                         request_headers: dict[str, str],
                         params: dict | None = None) -> httpx.Response:
    """Streamed upstream request with the user token injected and one forced
    re-login after a 401. The caller owns closing the returned response."""
    failed_token = ""
    for attempt in range(2):
        token, _ = await _authenticate(
            force=attempt == 1, stale_token=failed_token)
        headers = {"X-Emby-Token": token, **request_headers}
        upstream = _client.build_request(
            method, url, params=params, headers=headers)
        response = await _client.send(upstream, stream=True)
        if response.status_code != 401 or attempt:
            return response
        failed_token = token
        await response.aclose()
    raise RuntimeError("Jellyfin playback authentication retry failed")


async def _open_playback(method: str, capability: dict,
                         request_headers: dict[str, str]) -> httpx.Response:
    item = quote(capability["item"], safe="")
    container = quote(capability["container"], safe="")
    params = {
        "static": "true",
        "MediaSourceId": capability["source"],
        "DeviceId": _DEVICE_ID,
    }
    if capability.get("session"):
        params["PlaySessionId"] = capability["session"]
    return await _open_upstream(
        method, f"{JELLYFIN_URL}/Videos/{item}/stream.{container}",
        request_headers, params)


# ── HLS transcode: token-safe playlist rewriting ─────────────────────────────
#
# For a codec the player can't decode, Jellyfin transcodes to an H.264/AAC HLS
# ladder. Its playlists reference variants/segments with the access token baked
# into every URL. We fetch the master server-side, strip the token, and rewrite
# each URI to a signed, host-relative /library/{token}?u=…&s=… proxy URL. The
# player only ever talks to us; we re-inject the token via X-Emby-Token when we
# fetch upstream. The signature (ADDON_SECRET over the capability token + the
# Jellyfin-relative path) means only URLs this process minted are fetchable, so
# the endpoint can never be used as an open proxy.

_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')


def _strip_api_key(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in ("api_key", "x-emby-token")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def _to_rel(base_abs: str, uri: str) -> str:
    """Resolve a playlist URI against its playlist URL and return it as a
    token-free path+query relative to the Jellyfin host."""
    parts = urlsplit(_strip_api_key(urljoin(base_abs, uri)))
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _res_sign(token: str, rel: str) -> str:
    return _b64(hmac.new(
        _ADDON_SECRET,
        b"jellyfin-hls-res/v1/" + token.encode("utf-8") + b"|"
        + rel.encode("utf-8"), hashlib.sha256).digest())


def _res_url(token: str, rel: str) -> str:
    return (f"{PUBLIC_URL}/library/{token}"
            f"?u={_b64(rel.encode('utf-8'))}&s={_res_sign(token, rel)}")


def _rewrite_playlist(text: str, base_abs: str, token: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            if 'URI="' in stripped:
                stripped = _URI_ATTR_RE.sub(
                    lambda m: 'URI="%s"' % _res_url(
                        token, _to_rel(base_abs, m.group(1))), stripped)
            out.append(stripped)
            continue
        out.append(_res_url(token, _to_rel(base_abs, stripped)))
    return "\n".join(out) + "\n"


async def _transcode_master_path(capability: dict) -> str:
    """Ask Jellyfin for a transcode plan and return its master.m3u8 path
    (token stripped), or '' if the item can't be transcoded."""
    _, user_id = await _authenticate()
    response = await _api(
        "POST", f"/Items/{quote(capability['item'], safe='')}/PlaybackInfo",
        params={"UserId": user_id},
        json={"UserId": user_id, "DeviceProfile": _PLAY_PROFILE})
    for source in response.json().get("MediaSources") or []:
        if str(source.get("Id") or "") == capability["source"]:
            transcoding_url = str(source.get("TranscodingUrl") or "")
            return _strip_api_key(transcoding_url) if transcoding_url else ""
    return ""


def _playlist_response(text: str, base_abs: str, token: str) -> Response:
    return Response(_rewrite_playlist(text, base_abs, token),
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


async def _serve_hls_master(token: str, capability: dict) -> Response:
    if not TRANSCODE:
        return Response(status_code=404)
    try:
        master_path = await _transcode_master_path(capability)
    except Exception as exc:
        logger.warning("jellyfin transcode plan failed: %s", type(exc).__name__)
        return Response(status_code=502)
    if not master_path:
        logger.warning("jellyfin: transcode unavailable for item — check the "
                       "library user's 'allow video transcoding' permission")
        return Response(status_code=502)
    try:
        response = await _api("GET", master_path)
    except Exception as exc:
        logger.warning("jellyfin master fetch failed: %s", type(exc).__name__)
        return Response(status_code=502)
    return _playlist_response(response.text, f"{JELLYFIN_URL}{master_path}",
                              token)


async def _serve_resource(token: str, request: Request) -> Response:
    """A signed sub-resource of a transcode session: variant playlists come
    back rewritten; segments/keys/subtitles stream through with Range."""
    u = request.query_params.get("u", "")
    supplied = request.query_params.get("s", "")
    try:
        rel = _unb64(u).decode("utf-8")
    except Exception:
        return Response(status_code=404)
    if (len(rel) > 4096 or not rel.startswith("/") or rel.startswith("//")
            or "://" in rel
            or not hmac.compare_digest(_res_sign(token, rel), supplied)):
        return Response(status_code=404)
    forwarded = {name: request.headers[name] for name in _REQUEST_HEADERS
                 if name in request.headers}
    if ".m3u8" in rel.split("?", 1)[0].lower():
        try:
            response = await _api("GET", rel)
        except Exception as exc:
            logger.warning("jellyfin variant fetch failed: %s",
                           type(exc).__name__)
            return Response(status_code=502)
        return _playlist_response(response.text, f"{JELLYFIN_URL}{rel}", token)
    try:
        upstream = await _open_upstream(
            request.method, f"{JELLYFIN_URL}{rel}", forwarded)
    except Exception as exc:
        logger.warning("jellyfin segment open failed: %s", type(exc).__name__)
        return Response(status_code=502)
    headers = {name: value for name, value in upstream.headers.items()
               if name.lower() in _RESPONSE_HEADERS}
    if upstream.status_code >= 400 or request.method == "HEAD":
        status = upstream.status_code
        await upstream.aclose()
        return Response(status_code=status, headers=headers)

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code,
                             headers=headers)


async def serve(token: str, request: Request) -> Response:
    """Serve one signed native-Jellyfin capability: a direct-play byte range,
    an HLS transcode master, or a signed transcode sub-resource."""
    try:
        capability = _decode_capability(token)
    except ValueError:
        return Response(status_code=404)
    if request.query_params.get("u"):
        return await _serve_resource(token, request)
    if capability.get("mode") == "h":
        if request.method == "HEAD":
            return Response(status_code=200,
                            media_type="application/vnd.apple.mpegurl")
        return await _serve_hls_master(token, capability)
    forwarded = {name: request.headers[name] for name in _REQUEST_HEADERS
                 if name in request.headers}
    try:
        upstream = await _open_playback(request.method, capability, forwarded)
    except Exception as exc:
        logger.warning("jellyfin playback open failed: %s", type(exc).__name__)
        return Response(status_code=502)
    headers = {name: value for name, value in upstream.headers.items()
               if name.lower() in _RESPONSE_HEADERS}
    if upstream.status_code >= 400 or request.method == "HEAD":
        status = upstream.status_code
        await upstream.aclose()
        return Response(status_code=status, headers=headers)

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code,
                             headers=headers)


async def shutdown() -> None:
    global _access_token, _user_id
    _access_token = _user_id = ""
    await _client.aclose()
