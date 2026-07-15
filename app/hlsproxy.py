"""HLS playlist-rewriting proxy — brings HLS streams inside the tent.

File streams flow through /proxy/{token} and get failover, telemetry,
player-rejected detection, and read-ahead. HLS used to bypass all of it:
served raw when public (the player fetched flaky hosts directly, from its own
IP, without the addon's declared request headers) and dropped entirely when
credentialed/internal. This module rewrites playlists instead — every URI
(variants, segments, AES keys, init maps, alternate renditions) becomes a
signed /proxy/{token}/hls?u=… URL — so the player only ever talks to us and we
talk upstream with the right headers from one consistent IP. That fixes
referer-gated and IP-locked hosts (common on free scraper addons and the
Asian-content CDNs behind them), makes credentialed playlists servable, and
lets the usual machinery see HLS playback:

  * per-segment retries + a small read-ahead cache smooth flaky hosts;
  * playback stats flow into the ledger (flushed when a session goes idle);
  * player-rejected detection: playlists fetched but no segments ever pulled,
    then silence, cools the release — the next open serves the next candidate;
  * decode_health learns from the master playlist's declared CODECS.

Security: resource URLs carry an HMAC over (token, upstream URL) derived from
ADDON_SECRET, so only URLs this process minted are fetchable — the endpoint
cannot be used as an open proxy. Fallback: PROXY_HLS=0 restores the old
raw-or-drop behavior; a URL that turns out not to be a playlist redirects the
player to the upstream when that is safe to hand out.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlsplit

import httpx
from starlette.responses import (RedirectResponse, Response,
                                 StreamingResponse)

from app import decode_health, reputation, telemetry

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("PROXY_HLS", "1") not in ("0", "false", "")
PUBLIC = os.environ.get("ADDON_PUBLIC_URL", "http://localhost:8011").rstrip("/")
CACHE_BYTES = int(float(os.environ.get("HLS_SEG_CACHE_MB", "64")) * 1e6)
REJECT_COOLDOWN = float(os.environ.get("PLAYER_REJECT_COOLDOWN_HOURS",
                                       "24")) * 3600
_PREFETCH = 2                      # segments fetched ahead of the player
_MAX_SEG_BUF = 32 * 1024 * 1024    # buffer/cache segments up to this size
_REJECT_SILENCE = 15.0             # playlist(s) fetched, no segment, quiet
_FLUSH_IDLE = 300.0                # idle seconds before a play record flushes
_PLAYLIST_MAX = 2 * 1024 * 1024

_KEY = hashlib.sha256(
    ("hls:" + os.environ.get("ADDON_SECRET", "")).encode()).digest()

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
    headers={"User-Agent": "Stremio"})

# Upstream request headers a stream may declare (behaviorHints.proxyHeaders.
# request). Allowlisted: these carry host requirements (referers, tokens) —
# anything else stays out of our upstream requests.
_ALLOWED_HDRS = {"user-agent", "referer", "origin", "cookie", "authorization",
                 "accept", "accept-language"}


def request_headers(s: dict) -> dict:
    ph = (s.get("behaviorHints") or {}).get("proxyHeaders") or {}
    req = ph.get("request") if isinstance(ph, dict) else None
    if not isinstance(req, dict):
        return {}
    return {k: str(v) for k, v in req.items()
            if isinstance(k, str) and k.lower() in _ALLOWED_HDRS}


# ── signed resource URLs ─────────────────────────────────────────────────────

def _sign(token: str, url: str) -> str:
    return hmac.new(_KEY, f"{token}|{url}".encode(), hashlib.sha256) \
               .hexdigest()[:32]


def _res_url(token: str, url: str) -> str:
    u = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"{PUBLIC}/proxy/{token}/hls?u={u}&s={_sign(token, url)}"


def _decode_u(u: str) -> str:
    return base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode()


# ── playlist rewriting ───────────────────────────────────────────────────────

_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')


def is_playlist(body: bytes) -> bool:
    return body.lstrip()[:7] == b"#EXTM3U"


def rewrite(text: str, base_url: str, token: str) -> str:
    """Every URI in the playlist — plain URI lines (variants/segments) and
    URI="…" attributes (EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, …) — resolved
    against the playlist's own URL and replaced with a signed proxy URL."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                s = _URI_ATTR_RE.sub(
                    lambda m: 'URI="%s"' % _res_url(
                        token, urljoin(base_url, m.group(1))), s)
            out.append(s)
            continue
        out.append(_res_url(token, urljoin(base_url, s)))
    return "\n".join(out) + "\n"


def segment_urls(text: str, base_url: str) -> list[str]:
    """Ordered absolute segment URLs of a media playlist ('' for masters)."""
    if "#EXTINF" not in text:
        return []
    return [urljoin(base_url, ln.strip()) for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")][:4096]


# ── declared codecs (decode-compatibility learning for HLS) ──────────────────

_CODECS_RE = re.compile(r'CODECS="([^"]*)"')
_AUDIO_PREFIX = {"mp4a": "aac", "ac-3": "ac3", "ec-3": "eac3", "flac": "flac",
                 "opus": "opus", "dtsc": "dts", "dtse": "dts", "dtsx": "dts"}
_VIDEO_PREFIX = {"avc1": "h264", "avc3": "h264", "hvc1": "hevc",
                 "hev1": "hevc", "av01": "av1", "vp09": "vp9"}


def declared_codecs(master_text: str) -> tuple[list[str], str]:
    """(audio codecs, video codec) declared by the first CODECS attribute,
    normalized to ffprobe-style names so decode_health keys line up."""
    m = _CODECS_RE.search(master_text)
    if not m:
        return [], ""
    audio, video = [], ""
    for c in m.group(1).split(","):
        p = c.strip().split(".")[0].lower()
        if p in _AUDIO_PREFIX and _AUDIO_PREFIX[p] not in audio:
            audio.append(_AUDIO_PREFIX[p])
        elif p in _VIDEO_PREFIX and not video:
            video = _VIDEO_PREFIX[p]
    return audio, video


# ── segment cache (read-ahead) ───────────────────────────────────────────────

_seg_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_seg_cache_bytes = 0
_prefetching: set[str] = set()
_bg: set = set()


def _cache_get(url: str) -> tuple[bytes, str] | None:
    hit = _seg_cache.get(url)
    if hit is not None:
        _seg_cache.move_to_end(url)
    return hit


def _cache_put(url: str, body: bytes, ct: str) -> None:
    global _seg_cache_bytes
    if len(body) > _MAX_SEG_BUF or url in _seg_cache:
        return
    _seg_cache[url] = (body, ct)
    _seg_cache_bytes += len(body)
    while _seg_cache_bytes > CACHE_BYTES and _seg_cache:
        _, (old, _ct) = _seg_cache.popitem(last=False)
        _seg_cache_bytes -= len(old)


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _prefetch_one(url: str, rh: dict) -> None:
    try:
        r = await _client.get(url, headers=rh)
        if r.status_code == 200 and not is_playlist(r.content[:16]):
            _cache_put(url, r.content,
                       r.headers.get("content-type", "video/mp2t"))
    except Exception:
        pass
    finally:
        _prefetching.discard(url)


def _prefetch_next(entry: dict, url: str, rh: dict) -> None:
    seq = entry.get("_hlsseq") or []
    try:
        i = seq.index(url)
    except ValueError:
        return
    for nxt in seq[i + 1:i + 1 + _PREFETCH]:
        if nxt in _seg_cache or nxt in _prefetching:
            continue
        _prefetching.add(nxt)
        _spawn(_prefetch_one(nxt, rh))


# ── per-session state, rejection detection, stats ────────────────────────────

_active: dict[str, dict] = {}      # token -> session entry, for the idle flush


def _st(token: str, entry: dict) -> dict:
    st = entry.get("_hls")
    if st is None:
        st = entry["_hls"] = {"pl": 0, "segs": 0, "bytes": 0, "t0": 0.0,
                              "last": time.monotonic(), "active": 0,
                              "cand": None, "cand_idx": 0, "rejected": False,
                              "timer": False, "credited": False,
                              "seg_fails": 0, "struck": False}
        _active[token] = entry
    return st


def _mark_rejected(token: str, entry: dict, st: dict) -> None:
    cand = st.get("cand") or {}
    sig, lbl = cand.get("sig") or "", cand.get("lbl", "")
    st["rejected"] = True
    logger.info(f"hls: player rejected {lbl!r} — playlists fetched, no "
                f"segment ever pulled, then silence; cooling release")
    if sig:
        reputation.observe(sig, token, "player-rejected", lbl)
        reputation.cooldown(sig, REJECT_COOLDOWN)
    telemetry.record_buffer("player_rejected", sig=sig,
                            picker=entry.get("picker", ""),
                            media_id=entry.get("id", ""), source=lbl,
                            reason=f"{st['pl']} playlist fetches, 0 segments")
    ac, vc = entry.get("_hlscodecs") or ([], "")
    if ac or vc:
        decode_health.record_reject(ac, vc, label=lbl)
    entry["rejected_at"] = time.time()     # recovery_ok pairs with this


def _arm_reject_timer(token: str, entry: dict, st: dict) -> None:
    if st["rejected"] or st["segs"] or st["timer"]:
        return
    st["timer"] = True

    async def _fire():
        try:
            await asyncio.sleep(_REJECT_SILENCE)
            if (st["rejected"] or st["segs"] or st["active"] > 0
                    or time.monotonic() - st["last"] < _REJECT_SILENCE - 1):
                return
            _mark_rejected(token, entry, st)
        finally:
            st["timer"] = False

    _spawn(_fire())


def _note_segment(token: str, entry: dict, st: dict, size: int) -> None:
    now = time.monotonic()
    if st.get("flushed"):                  # resumed after a flush: a new play
        st.update({"segs": 0, "bytes": 0, "t0": now, "flushed": False,
                   "credited": False})
    if not st["t0"]:
        st["t0"] = now
    st["segs"] += 1
    st["bytes"] += size
    st["last"] = now
    if st["segs"] == 3 and not st["credited"]:
        st["credited"] = True
        ac, vc = entry.get("_hlscodecs") or ([], "")
        if ac or vc:
            decode_health.record_play(ac, vc)
        if entry.pop("rejected_at", None) is not None:
            cand = st.get("cand") or {}
            logger.info(f"hls: auto-recovery — {cand.get('lbl', '')!r} plays "
                        f"after a rejected release on the same token")
            telemetry.record_buffer(
                "recovery_ok", sig=cand.get("sig", ""),
                picker=entry.get("picker", ""), media_id=entry.get("id", ""),
                source=cand.get("lbl", ""),
                reason="played after player-rejected swap")


def _note_seg_failure(token: str, entry: dict, st: dict) -> None:
    """Segments dying under a playing session = the host is failing mid-stream.
    Strike the release once per session so a repeat offender cools and the next
    open serves a different one — the HLS analog of 'mid-stream-dead'."""
    st["seg_fails"] += 1
    if st["struck"] or st["seg_fails"] < 3:
        return
    st["struck"] = True
    cand = st.get("cand") or {}
    sig, lbl = cand.get("sig") or "", cand.get("lbl", "")
    logger.info(f"hls: segments failing for {lbl!r} "
                f"({st['seg_fails']} fetch failures) — cooling release")
    if sig:
        reputation.observe(sig, token, "hls-segments-dead", lbl)
        reputation.cooldown(sig)
    telemetry.record_buffer("failed", sig=sig, picker=entry.get("picker", ""),
                            media_id=entry.get("id", ""), source=lbl,
                            reason=f"{st['seg_fails']} segment fetch failures")


async def flush_idle() -> None:
    """Emit one play record per HLS session once it has gone quiet — called
    from the proxy's reaper loop. HLS playback is hundreds of tiny requests,
    so 'the session' rather than 'the connection' is the unit of accounting."""
    now = time.monotonic()
    for token, entry in list(_active.items()):
        st = entry.get("_hls") or {}
        if not st.get("segs") or st.get("flushed"):
            # Nothing to account for — but a session that never pulled a
            # segment (rejected/abandoned) must still leave the registry.
            if st.get("flushed") or now - (st.get("last") or now) > _FLUSH_IDLE:
                _active.pop(token, None)
            continue
        if now - st["last"] < _FLUSH_IDLE:
            continue
        st["flushed"] = True
        _active.pop(token, None)
        try:
            telemetry.record_play(
                {"picker": entry.get("picker", ""), "id": entry.get("id", "")},
                st.get("cand") or {}, st.get("cand_idx", 0),
                served=st["bytes"], dur=max(st["last"] - st["t0"], 1.0),
                ttfb=0.0, reconnects=0, reason="hls", session=token)
        except Exception:
            logger.debug("hls play-record flush failed", exc_info=True)


# ── serving ──────────────────────────────────────────────────────────────────

def _safe_raw(url: str) -> bool:
    """True when the upstream URL may be handed to the player directly (public
    FQDN, no credentials) — the escape hatch when a '.m3u8' URL turns out not
    to serve a playlist at all."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return "." in host and "@" not in (parts.netloc or "")


def _playlist_response(token: str, entry: dict, st: dict, text: str,
                       base_url: str) -> Response:
    if "#EXTINF" in text:                    # media playlist: remember order
        entry["_hlsseq"] = segment_urls(text, base_url)
    else:                                    # master: learn declared codecs
        ac, vc = declared_codecs(text)
        if ac or vc:
            entry["_hlscodecs"] = (ac, vc)
    st["pl"] += 1
    st["last"] = time.monotonic()
    _arm_reject_timer(token, entry, st)
    return Response(rewrite(text, base_url, token),
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


async def serve_master(token: str, entry: dict, request) -> Response:
    """Entry point for /proxy/{token} when the token wraps an HLS stream: pick
    the best non-cooled candidate, fetch its playlist upstream (with the
    stream's declared headers), and serve it rewritten."""
    st = _st(token, entry)
    cands = entry.get("cands") or []
    if not cands:
        return Response(status_code=404)
    skip_cooled = not all(reputation.cooled(c.get("sig") or "") for c in cands)
    last_upstream = ""
    for idx, c in enumerate(cands):
        if skip_cooled and reputation.cooled(c.get("sig") or ""):
            continue
        rh = c.get("rh") or {}
        try:
            r = await _client.get(c["u"], headers=rh)
        except Exception as e:
            logger.info(f"hls: cand {idx} playlist fetch failed "
                        f"({type(e).__name__})")
            continue
        if r.status_code != 200 or len(r.content) > _PLAYLIST_MAX:
            continue
        if not is_playlist(r.content):
            last_upstream = str(r.url)
            continue
        if idx and st.get("cand") is not c:
            logger.info(f"hls: serving candidate {idx} ({c.get('lbl', '')!r})")
        st["cand"], st["cand_idx"] = c, idx
        return _playlist_response(token, entry, st,
                                  r.content.decode("utf-8", errors="replace"),
                                  str(r.url))
    # Nothing served a playlist. If one answered with something else and its
    # URL is safe to hand out, let the player try it directly (old behavior).
    if last_upstream and _safe_raw(last_upstream):
        return RedirectResponse(last_upstream, status_code=302)
    return Response(status_code=502)


async def serve_resource(token: str, entry: dict, request) -> Response:
    """Signed sub-resource fetch: variant playlists come back rewritten,
    segments/keys/init maps stream through (with retries and read-ahead)."""
    u = request.query_params.get("u", "")
    s = request.query_params.get("s", "")
    try:
        url = _decode_u(u)
    except Exception:
        return Response(status_code=403)
    if not hmac.compare_digest(_sign(token, url), s):
        return Response(status_code=403)
    st = _st(token, entry)
    rh = dict((st.get("cand") or (entry.get("cands") or [{}])[0])
              .get("rh") or {})
    rng = request.headers.get("range")

    if not rng:
        hit = _cache_get(url)
        if hit is not None:
            body, ct = hit
            _note_segment(token, entry, st, len(body))
            _prefetch_next(entry, url, rh)
            return Response(body, media_type=ct)

    if rng:
        rh["Range"] = rng
    st["active"] += 1
    try:
        r = None
        for attempt in (1, 2):
            try:
                req = _client.build_request("GET", url, headers=rh)
                r = await _client.send(req, stream=True)
                if r.status_code in (200, 206):
                    break
                await r.aclose()
                r = None
            except Exception as e:
                logger.info(f"hls: segment fetch attempt {attempt} failed "
                            f"({type(e).__name__})")
                r = None
        if r is None:
            _note_seg_failure(token, entry, st)
            return Response(status_code=502)
        ct = r.headers.get("content-type", "video/mp2t")
        headers = {}
        if r.status_code == 206 and r.headers.get("content-range"):
            headers["Content-Range"] = r.headers["content-range"]
        # Buffer small resources (playlist sniff + cache + prefetch); a large
        # body — a playlist URI pointing at a whole file is not unheard of —
        # must stream through, never sit in RAM.
        chunks: list[bytes] = []
        got = 0
        big = False
        try:
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                got += len(chunk)
                if got > _MAX_SEG_BUF:
                    big = True
                    break
        except Exception:
            await r.aclose()
            _note_seg_failure(token, entry, st)
            return Response(status_code=502)
        if big:
            async def gen(pre=chunks, resp=r):
                try:
                    for c in pre:
                        yield c
                    async for c in resp.aiter_bytes():
                        yield c
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
            _note_segment(token, entry, st, got)
            return StreamingResponse(gen(), status_code=r.status_code,
                                     media_type=ct, headers=headers)
        await r.aclose()
        body = b"".join(chunks)
        if is_playlist(body):
            return _playlist_response(
                token, entry, st, body.decode("utf-8", errors="replace"),
                str(r.url))
        _note_segment(token, entry, st, len(body))
        if not rng:
            _cache_put(url, body, ct)
            _prefetch_next(entry, url, rh)
        return Response(body, status_code=r.status_code, media_type=ct,
                        headers=headers)
    finally:
        st["active"] -= 1
        st["last"] = time.monotonic()
