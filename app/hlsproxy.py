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

from app import cfsolver, decode_health, reputation, telemetry

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("PROXY_HLS", "1") not in ("0", "false", "")
PUBLIC = os.environ.get("ADDON_PUBLIC_URL", "http://localhost:8011").rstrip("/")
CACHE_BYTES = int(float(os.environ.get("HLS_SEG_CACHE_MB", "64")) * 1e6)
CACHE_TTL = float(os.environ.get("HLS_SEG_CACHE_TTL_SECONDS", "300"))
REJECT_COOLDOWN = float(os.environ.get("PLAYER_REJECT_COOLDOWN_HOURS",
                                       "24")) * 3600
_PREFETCH = 2                      # segments fetched ahead of the player
_OPEN_BUDGET = 15.0                # total seconds an HLS open may spend trying candidates
_MAX_SEG_BUF = 32 * 1024 * 1024    # buffer/cache segments up to this size
_REJECT_SILENCE = 15.0             # playlist(s) fetched, no segment, quiet
_FLUSH_IDLE = 300.0                # idle seconds before a play record flushes
_PLAYLIST_MAX = 2 * 1024 * 1024
_BUFFER_CONCURRENCY = max(1, int(os.environ.get("HLS_BUFFER_CONCURRENCY", "4")))

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

def _sign(token: str, url: str, candidate_key: str = "") -> str:
    return hmac.new(_KEY, f"{token}|{candidate_key}|{url}".encode(), hashlib.sha256) \
               .hexdigest()[:32]


def _res_url(token: str, url: str, candidate_key: str = "") -> str:
    u = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    c = f"&c={candidate_key}" if candidate_key else ""
    return (f"{PUBLIC}/proxy/{token}/hls?u={u}{c}"
            f"&s={_sign(token, url, candidate_key)}")


def _decode_u(u: str) -> str:
    return base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode()


# ── playlist rewriting ───────────────────────────────────────────────────────

_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')


def is_playlist(body: bytes) -> bool:
    return body.lstrip()[:7] == b"#EXTM3U"


def rewrite(text: str, base_url: str, token: str,
            candidate_key: str = "") -> str:
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
                        token, urljoin(base_url, m.group(1)), candidate_key), s)
            out.append(s)
            continue
        out.append(_res_url(token, urljoin(base_url, s), candidate_key))
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

_seg_cache: OrderedDict[str, tuple[bytes, str, float]] = OrderedDict()
_seg_cache_bytes = 0
_prefetching: set[str] = set()
_bg: set = set()
_buffer_slots = asyncio.Semaphore(_BUFFER_CONCURRENCY)


def _cache_key(url: str, rh: dict | None = None,
               candidate_key: str = "") -> str:
    if not rh and not candidate_key:             # keeps the small test/debug API legible
        return url
    headers = "\n".join(f"{str(k).lower()}:{v}" for k, v in
                         sorted((rh or {}).items(), key=lambda kv: str(kv[0]).lower()))
    return hashlib.sha256(f"{candidate_key}|{url}|{headers}".encode()).hexdigest()


def _cache_get(url: str, rh: dict | None = None,
               candidate_key: str = "") -> tuple[bytes, str] | None:
    global _seg_cache_bytes
    key = _cache_key(url, rh, candidate_key)
    hit = _seg_cache.get(key)
    if hit is not None:
        body, ct, expires = hit
        if expires <= time.monotonic():
            _seg_cache.pop(key, None)
            _seg_cache_bytes -= len(body)
            return None
        _seg_cache.move_to_end(key)
        return body, ct
    return None


def _cache_put(url: str, body: bytes, ct: str, rh: dict | None = None,
               candidate_key: str = "") -> None:
    global _seg_cache_bytes
    key = _cache_key(url, rh, candidate_key)
    if len(body) > _MAX_SEG_BUF or key in _seg_cache:
        return
    _seg_cache[key] = (body, ct, time.monotonic() + CACHE_TTL)
    _seg_cache_bytes += len(body)
    while _seg_cache_bytes > CACHE_BYTES and _seg_cache:
        _, (old, _ct, _expires) = _seg_cache.popitem(last=False)
        _seg_cache_bytes -= len(old)


def _reap_task(t) -> None:
    _bg.discard(t)
    if not t.cancelled() and t.exception() is not None:
        logger.warning(f"hls: background task failed: {t.exception()!r}")


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_reap_task)


async def _prefetch_one(url: str, rh: dict, candidate_key: str) -> None:
    try:
        async with _buffer_slots:
            req = _client.build_request(
                "GET", url, headers=cfsolver.merge_headers(url, rh))
            r = await _client.send(req, stream=True)
            try:
                length = r.headers.get("content-length") or ""
                if (r.status_code != 200
                        or (length.isdigit() and int(length) > _MAX_SEG_BUF)):
                    return
                chunks, got = [], 0
                async for chunk in r.aiter_bytes():
                    got += len(chunk)
                    if got > _MAX_SEG_BUF:
                        return
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not is_playlist(body[:16]):
                    _cache_put(url, body,
                               r.headers.get("content-type", "video/mp2t"),
                               rh, candidate_key)
            finally:
                await r.aclose()
    except Exception:
        pass
    finally:
        _prefetching.discard(_cache_key(url, rh, candidate_key))


def _prefetch_next(entry: dict, url: str, rh: dict,
                   candidate_key: str = "") -> None:
    # Per-variant sequences: an ABR player that just switched bitrates must
    # get read-ahead on the variant it is *on*, not the one it left.
    for seq_key, seq in (entry.get("_hlsseqs") or {}).items():
        if candidate_key and not str(seq_key).startswith(candidate_key + "|"):
            continue
        try:
            i = seq.index(url)
        except ValueError:
            continue
        for nxt in seq[i + 1:i + 1 + _PREFETCH]:
            key = _cache_key(nxt, rh, candidate_key)
            if key in _seg_cache or key in _prefetching:
                continue
            _prefetching.add(key)
            _spawn(_prefetch_one(nxt, rh, candidate_key))
        return


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
    # flush_idle removes quiet sessions from the registry but intentionally
    # leaves their counters on the token. A later resume must register again.
    _active[token] = entry
    return st


def _mark_rejected(token: str, entry: dict, st: dict) -> None:
    cand = st.get("cand") or {}
    sig, lbl = cand.get("sig") or "", cand.get("lbl", "")
    st["rejected"] = True
    if cand:
        rejected = entry.setdefault("_hls_rejected", [])
        key = _cand_key(cand)
        if key not in rejected:
            rejected.append(key)
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
    try:
        from app import picker      # lazy: avoids an import cycle
        picker.invalidate(entry.get("id", ""))
    except Exception:
        pass


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
        # A series episode is really playing over HLS: prep the next episode
        # exactly like the byte-range path does at its first serve.  Lazy
        # import — proxy imports this module at load time.
        from app import proxy as _pxy
        if (_pxy.PREFETCH_NEXT and ":" in (entry.get("id") or "")
                and not entry.get("nextfetched")):
            entry["nextfetched"] = True
            task = asyncio.create_task(_pxy._fire_prefetch(
                entry["id"], entry.get("picker", "")))
            _pxy._bg_tasks.add(task)
            task.add_done_callback(_pxy._bg_tasks.discard)
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


def _note_seg_failure(token: str, entry: dict, st: dict,
                      reason: str = "segment-fetch-failed",
                      cand_override: dict | None = None) -> None:
    """Segments dying under a playing session = the host is failing mid-stream.
    Strike the release once per session so a repeat offender cools and the next
    open serves a different one — the HLS analog of 'mid-stream-dead'."""
    cand = cand_override or st.get("cand") or {}
    sig, lbl = cand.get("sig") or "", cand.get("lbl", "")
    telemetry.record_buffer("segment_error", sig=sig,
                            picker=entry.get("picker", ""),
                            media_id=entry.get("id", ""), source=lbl,
                            reason=reason)
    current = st.get("cand") or {}
    if cand_override and _cand_key(cand_override) != _cand_key(current):
        return                      # a late old-variant request cannot strike the new pick
    st["seg_fails"] += 1
    if st["struck"] or st["seg_fails"] < 3:
        return
    st["struck"] = True
    logger.info(f"hls: segments failing for {lbl!r} "
                f"({st['seg_fails']} fetch failures) — cooling release")
    if sig:
        reputation.observe(sig, token, "hls-segments-dead", lbl)
        reputation.cooldown(sig)
    telemetry.record_buffer("failed", sig=sig, picker=entry.get("picker", ""),
                            media_id=entry.get("id", ""), source=lbl,
                            reason=f"{st['seg_fails']} segment fetch failures; {reason}")


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


async def shutdown() -> None:
    """Stop HLS workers, flush completed session accounting, and close HTTP."""
    global _seg_cache_bytes
    tasks = list(_bg)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _bg.clear()
    # Lifespan shutdown happens after request draining. Make completed plays
    # eligible for the same idempotent ledger flush used by maintenance.
    for entry in _active.values():
        st = entry.get("_hls") or {}
        if st.get("active", 0) <= 0:
            st["last"] = time.monotonic() - _FLUSH_IDLE - 1
    await flush_idle()
    _active.clear()
    _prefetching.clear()
    _seg_cache.clear()
    _seg_cache_bytes = 0
    await _client.aclose()


# ── serving ──────────────────────────────────────────────────────────────────

_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$", re.I)
_SUFFIX_RE = re.compile(r"^bytes=-(\d+)$", re.I)
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.I)


def _range_ok(resp, requested: str | None) -> bool:
    if not requested:
        if resp.status_code == 200:
            return True
        match = _CONTENT_RANGE_RE.fullmatch(
            (resp.headers.get("content-range") or "").strip()) \
            if resp.status_code == 206 else None
        return bool(match and int(match.group(1)) == 0)
    explicit = _RANGE_RE.fullmatch(requested.strip())
    suffix = _SUFFIX_RE.fullmatch(requested.strip())
    if not explicit and not suffix:
        return False
    if resp.status_code != 206:
        return False
    match = _CONTENT_RANGE_RE.fullmatch(
        (resp.headers.get("content-range") or "").strip())
    if not match:
        return False
    start, end = int(match.group(1)), int(match.group(2))
    total = int(match.group(3)) if match.group(3).isdigit() else None
    if end < start or (total is not None and end >= total):
        return False
    if suffix:
        wanted = int(suffix.group(1))
        return bool(wanted > 0 and total is not None and end == total - 1
                    and end - start + 1 == min(wanted, total))
    wanted_start = int(explicit.group(1))
    wanted_end = int(explicit.group(2)) if explicit.group(2) else None
    return start == wanted_start and (wanted_end is None or end <= wanted_end)


def _range_request_valid(requested: str) -> bool:
    explicit = _RANGE_RE.fullmatch(requested.strip())
    if explicit:
        return not explicit.group(2) or int(explicit.group(2)) >= int(explicit.group(1))
    suffix = _SUFFIX_RE.fullmatch(requested.strip())
    return bool(suffix and int(suffix.group(1)) > 0)


def _safe_raw(url: str) -> bool:
    """True when the upstream URL may be handed to the player directly (public
    FQDN, no credentials) — the escape hatch when a '.m3u8' URL turns out not
    to serve a playlist at all."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return "." in host and "@" not in (parts.netloc or "")


def _cand_key(cand: dict) -> str:
    return cand.get("key") or hashlib.sha256((cand.get("u") or "").encode()).hexdigest()


def _is_media_segment(entry: dict, url: str, candidate_key: str) -> bool:
    prefix = candidate_key + "|"
    return any(url in seq for key, seq in (entry.get("_hlsseqs") or {}).items()
               if not candidate_key or str(key).startswith(prefix))


def _playlist_response(token: str, entry: dict, st: dict, text: str,
                       base_url: str, cand: dict | None = None) -> Response:
    candidate_key = _cand_key(cand or st.get("cand") or {})
    if "#EXTINF" in text:                    # media playlist: remember order
        seqs = entry.setdefault("_hlsseqs", {})
        seqs[f"{candidate_key}|{base_url}"] = segment_urls(text, base_url)
        while len(seqs) > 4:                 # a few variants, not a catalog
            seqs.pop(next(iter(seqs)))
    else:                                    # master: learn declared codecs
        ac, vc = declared_codecs(text)
        if ac or vc:
            entry["_hlscodecs"] = (ac, vc)
    st["pl"] += 1
    st["last"] = time.monotonic()
    _arm_reject_timer(token, entry, st)
    return Response(rewrite(text, base_url, token, candidate_key),
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
    rejected = entry.get("_hls_rejected") or set()
    cands = [c for c in cands
             if not reputation.blocked(c.get("sig") or "")
             and _cand_key(c) not in rejected]
    if not cands:
        return Response(status_code=502)
    skip_cooled = not all(reputation.cooled(c.get("sig") or "") for c in cands)
    # Bounded open: several dead mirrors must not turn the player's open into
    # a minute of spinner — spend at most _OPEN_BUDGET across all candidates.
    deadline = time.monotonic() + _OPEN_BUDGET
    last_upstream = ""

    def bad(c: dict, reason: str) -> None:
        sig, label = c.get("sig") or "", c.get("lbl") or ""
        if sig:
            reputation.observe(sig, token, reason, label)
            reputation.cooldown(sig)
        telemetry.record_buffer("failed", sig=sig,
                                picker=entry.get("picker", ""),
                                media_id=entry.get("id", ""), source=label,
                                reason=reason)

    for idx, c in enumerate(cands):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            logger.info("hls: open budget exhausted, giving up on this open")
            break
        if skip_cooled and reputation.cooled(c.get("sig") or ""):
            continue
        rh = c.get("rh") or {}
        try:
            r = await _client.get(c["u"], headers=cfsolver.merge_headers(c["u"], rh),
                                  timeout=min(10.0, remaining))
        except Exception as e:
            logger.info(f"hls: cand {idx} playlist fetch failed "
                        f"({type(e).__name__})")
            bad(c, f"playlist-{type(e).__name__.lower()}")
            continue
        if r.status_code != 200 or len(r.content) > _PLAYLIST_MAX:
            if cfsolver.looks_challenged(r.status_code, r.headers, r.content):
                cfsolver.note_challenge(c["u"])
            bad(c, (f"playlist-http-{r.status_code}" if r.status_code != 200
                    else "playlist-too-large"))
            continue
        if not is_playlist(r.content):
            last_upstream = str(r.url)
            bad(c, "playlist-not-hls")
            continue
        if idx and st.get("cand") is not c:
            logger.info(f"hls: serving candidate {idx} ({c.get('lbl', '')!r})")
        st["cand"], st["cand_idx"] = c, idx
        return _playlist_response(token, entry, st,
                                  r.content.decode("utf-8", errors="replace"),
                                  str(r.url), c)
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
    candidate_key = request.query_params.get("c", "")
    if not hmac.compare_digest(_sign(token, url, candidate_key), s):
        return Response(status_code=403)
    cands = entry.get("cands") or []
    bound = next((c for c in cands if _cand_key(c) == candidate_key), None) \
        if candidate_key else None
    if candidate_key and bound is None:
        return Response(status_code=403)
    st = _st(token, entry)
    cand = bound or st.get("cand") or (cands[0] if cands else {})
    rh = dict(cand.get("rh") or {})
    rng = request.headers.get("range")
    if rng and not _range_request_valid(rng):
        return Response(status_code=416, headers={"Accept-Ranges": "bytes"})
    media_segment = _is_media_segment(entry, url, candidate_key)
    accountable_segment = bool(
        media_segment and (not candidate_key
                           or candidate_key == _cand_key(st.get("cand") or {})))

    if not rng:
        hit = _cache_get(url, rh, candidate_key)
        if hit is not None:
            body, ct = hit
            if accountable_segment:
                _note_segment(token, entry, st, len(body))
                _prefetch_next(entry, url, rh, candidate_key)
            return Response(body, media_type=ct)

    if rng:
        rh["Range"] = rng
    st["active"] += 1
    handed_off = False
    slot_held = False
    try:
        r = None
        last_reason = "segment-fetch-failed"
        for attempt in (1, 2):
            try:
                req = _client.build_request(
                    "GET", url, headers=cfsolver.merge_headers(url, rh))
                r = await _client.send(req, stream=True)
                if _range_ok(r, rng):
                    break
                if cfsolver.looks_challenged(r.status_code, r.headers):
                    cfsolver.note_challenge(url)
                last_reason = (f"http-{r.status_code}"
                               if r.status_code not in (200, 206)
                               else "bad-content-range")
                await r.aclose()
                r = None
            except Exception as e:
                logger.info(f"hls: segment fetch attempt {attempt} failed "
                            f"({type(e).__name__})")
                last_reason = type(e).__name__
                r = None
        if r is None:
            _note_seg_failure(token, entry, st, last_reason, cand)
            return Response(status_code=502)
        ct = r.headers.get("content-type", "video/mp2t")
        headers = {"Accept-Ranges": "bytes"}
        if r.status_code == 206 and r.headers.get("content-range"):
            headers["Content-Range"] = r.headers["content-range"]
        if r.headers.get("content-length"):
            headers["Content-Length"] = r.headers["content-length"]
        # Buffer small resources (playlist sniff + cache + prefetch); a large
        # body — a playlist URI pointing at a whole file is not unheard of —
        # must stream through, never sit in RAM.
        chunks: list[bytes] = []
        got = 0
        length = r.headers.get("content-length") or ""
        big = length.isdigit() and int(length) > _MAX_SEG_BUF
        if not big:
            await _buffer_slots.acquire()
            slot_held = True
        try:
            if not big:
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    got += len(chunk)
                    if got > _MAX_SEG_BUF:
                        big = True
                        break
        except Exception as exc:
            await r.aclose()
            _note_seg_failure(token, entry, st, type(exc).__name__, cand)
            return Response(status_code=502)
        if big:
            handed_off = True

            async def gen(pre=chunks, resp=r, held=slot_held):
                total = sum(len(c) for c in pre)
                try:
                    for c in pre:
                        yield c
                    async for c in resp.aiter_bytes():
                        total += len(c)
                        yield c
                    if accountable_segment:
                        _note_segment(token, entry, st, total)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _note_seg_failure(token, entry, st, type(exc).__name__, cand)
                    raise
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    st["active"] = max(0, st["active"] - 1)
                    st["last"] = time.monotonic()
                    if held:
                        _buffer_slots.release()
            return StreamingResponse(gen(), status_code=r.status_code,
                                     media_type=ct, headers=headers)
        await r.aclose()
        body = b"".join(chunks)
        if is_playlist(body):
            return _playlist_response(
                token, entry, st, body.decode("utf-8", errors="replace"),
                str(r.url), cand)
        if accountable_segment:
            _note_segment(token, entry, st, len(body))
        if not rng:
            _cache_put(url, body, ct, rh, candidate_key)
            if accountable_segment:
                _prefetch_next(entry, url, rh, candidate_key)
        return Response(body, status_code=r.status_code, media_type=ct,
                        headers=headers)
    finally:
        if not handed_off:
            st["active"] = max(0, st["active"] - 1)
            st["last"] = time.monotonic()
            if slot_held:
                _buffer_slots.release()
