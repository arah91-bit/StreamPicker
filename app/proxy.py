"""Playback proxy + start-of-play auto-switcher.

The picker rewrites each online stream URL it returns into a `/proxy/<token>`
URL on this server. When the player opens it we stream the real bytes through,
which does two things the direct URL can't:

  1. Auto-switch on a bad source. The token maps to the *ranked list* from that
     position, not one URL. On the opening request we try the first candidate;
     if it's dead (4xx/5xx), never sends a byte, or delivers below the file's
     bitrate, we transparently fall through to the next and *pin* the winner —
     the player's URL never changes. This is the "drop to next priority on a
     failed/slow stream" behaviour, done in the first moment before any picture
     has shown, so it's seamless.
  2. Measure real delivery. We record actual throughput to the device, stalls,
     mid-stream drops and watched fraction per source — the ground truth the
     server-side probe can only approximate.

Hard constraint we respect: different releases are different files, so once the
picture is playing we can only ever *reconnect the same source* (byte-identical)
on a drop — never swap to a different release mid-stream (that would feed the
player a file layout it never parsed). So cross-source failover happens only on
the initial request; seeks and mid-stream retries are locked to the pinned
source. A source that dies mid-movie ends as a playback error (logged), which is
the signal to pre-demote it next time.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from urllib.parse import urlsplit

import httpx
from starlette.responses import Response, StreamingResponse

from app import (decode_health, hlsproxy, reputation, telemetry,
                 usenet_health, vprobe)

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("PROXY_PLAYBACK", "1") not in ("0", "false", "")
PUBLIC = os.environ.get("ADDON_PUBLIC_URL", "http://localhost:8011").rstrip("/")

WRAP_MAX = int(os.environ.get("PROXY_WRAP_MAX", "8"))     # streams to proxy per list
MAXFAIL = int(os.environ.get("PROXY_MAX_FAILOVER", "4"))  # candidates per token
SESS_TTL = float(os.environ.get("PROXY_SESSION_TTL", str(24 * 3600)))
SESS_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"), "sessions.jsonl")
# Compact the session file once it outgrows this (it appends ~8-16 records per
# stream-list request and previously only compacted at startup — weeks of uptime
# let it grow into the hundreds of MB).
SESS_MAX_BYTES = int(os.environ.get("PROXY_SESSION_MAX_BYTES", str(20 * 1024 * 1024)))

# start-of-play evaluation
EVAL_BYTES = 2 * 1024 * 1024
EVAL_DEADLINE = 8.0
START_TTFB = 8.0        # play-time links may cold-start; more lenient than probe
RATE_MARGIN = 0.85
RECONNECT_MAX = 3
# Transport-stream containers (.ts / .m2ts Blu-ray BDAV) are valid video, but
# some players — including the household's Nuvio player — can't demux them and
# error with ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED on an otherwise-fine remux.
# So the start-selector prefers a file container (MKV/MP4/AVI/…) and serves a
# transport stream only when nothing else works. Env kill-switch → 0.
PREFER_FILE_CONTAINERS = os.environ.get("PREFER_FILE_CONTAINERS", "1") not in ("0", "false", "")
_DEFER_CONTAINERS = {"ts", "m2ts"}

# ── mid-stream twin-splice ───────────────────────────────────────────────────
# A byte-identical *twin* is the same release (same telemetry.signature) served
# by a *different* debrid (TorBox<->Real-Debrid) — i.e. the same file cached on
# genuinely separate infrastructure, so a different delivery node. Because the
# bytes are identical, we can splice to it mid-stream at the current offset with
# no gap and nothing for the player to re-parse. This rescues the one failure
# same-source reconnect can't: a node that is itself dead or congested — where
# reconnecting to the same link just lands on the same bad node. Only ever fires
# when a twin exists (dual-cached release) and its total byte-length matches, so
# it stays dormant and zero-cost otherwise.
TWIN_SPLICE = os.environ.get("TWIN_SPLICE", "1") not in ("0", "false", "")
# Proactively splice on sustained mid-stream buffering (not just on a hard drop).
TWIN_PROACTIVE = os.environ.get("TWIN_PROACTIVE", "1") not in ("0", "false", "")
SPLICE_MAX = int(os.environ.get("TWIN_SPLICE_MAX", "3"))
# Proactive trigger: source-limited delivery below need*margin sustained across
# this many seconds of time we actually spent *waiting on the source* (not wall
# time — a satisfied client throttling its reads never counts, see _body).
SPLICE_WINDOW = float(os.environ.get("TWIN_SPLICE_WINDOW", "8"))
SPLICE_MARGIN = float(os.environ.get("TWIN_SPLICE_MARGIN", "0.9"))

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
    headers={"User-Agent": "Stremio"},
)

# token -> (created_ts, entry). entry = {"cands":[...], "id":.., "picker":.., "pin":int|None}
_sessions: dict[str, tuple[float, dict]] = {}
_sess_compact_after = SESS_MAX_BYTES

# Only cryptographic release identities are safe for byte-cache reuse.  Older
# sessions may contain truncated filenames or metadata fallbacks such as
# ``gr0s0``; preserve their playback URLs across a restart, but strip those
# unsafe identities so they cannot alias a new cache entry.
_STRONG_SIG_RE = re.compile(r"^(?:file|nzb):[0-9a-f]{64}$")


def _scrub_legacy_sigs(entry: dict) -> int:
    scrubbed = 0
    seen: set[int] = set()
    for key in ("cands", "pool"):
        cands = entry.get(key) or []
        if not isinstance(cands, list):
            continue
        for cand in cands:
            if not isinstance(cand, dict) or id(cand) in seen:
                continue
            seen.add(id(cand))
            sig = cand.get("sig") or ""
            if sig and not _STRONG_SIG_RE.fullmatch(sig):
                cand["sig"] = ""
                scrubbed += 1
    # Buffer entries are intentionally wiped at startup, so a persisted mapping
    # is stale even when its signature is otherwise valid.
    entry.pop("bufsig", None)
    return scrubbed


# ── session store (disk-persisted so a restart mid-movie doesn't break) ──────
def _persist(token: str, entry: dict) -> None:
    global _sess_compact_after
    try:
        os.makedirs(os.path.dirname(SESS_FILE), exist_ok=True)
        try:
            if os.path.getsize(SESS_FILE) > _sess_compact_after:
                _compact()
        except FileNotFoundError:
            pass
        with open(SESS_FILE, "a") as f:
            f.write(json.dumps({"t": token, "ts": time.time(), "e": entry},
                               separators=(",", ":")) + "\n")
        os.chmod(SESS_FILE, 0o600)
    except Exception:
        logger.debug("session persist failed", exc_info=True)


def _compact() -> None:
    """Rewrite the session file from the live in-memory set (the source of
    truth), dropping expired sessions — the same shrink load() does at startup,
    but size-triggered so weeks of uptime can't grow the file without bound."""
    global _sess_compact_after
    cutoff = time.time() - SESS_TTL
    live = [(t, ts, e) for t, (ts, e) in _sessions.items() if ts >= cutoff]
    tmp = SESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        for t, ts, e in live:
            f.write(json.dumps({"t": t, "ts": ts, "e": e},
                               separators=(",", ":")) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESS_FILE)
    # If the live set itself is larger than SESS_MAX_BYTES, compacting again on
    # the very next append cannot help.  Wait for another full budget of append
    # growth instead of synchronously rewriting the same large file per token.
    size = os.path.getsize(SESS_FILE)
    _sess_compact_after = max(SESS_MAX_BYTES, size + SESS_MAX_BYTES)
    logger.info(f"proxy: compacted session file to {len(live)} live sessions")


def load() -> None:
    """Rebuild live sessions from disk at startup, dropping expired ones and
    compacting the file so it can't grow without bound."""
    global _sess_compact_after
    now = time.time()
    scrubbed = 0
    try:
        os.chmod(SESS_FILE, 0o600)
        with open(SESS_FILE) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if now - d["ts"] < SESS_TTL:
                        scrubbed += _scrub_legacy_sigs(d["e"])
                        _sessions[d["t"]] = (d["ts"], d["e"])
                except Exception:
                    pass
    except FileNotFoundError:
        return
    try:
        with open(SESS_FILE, "w") as f:
            for t, (ts, e) in _sessions.items():
                f.write(json.dumps({"t": t, "ts": ts, "e": e},
                                   separators=(",", ":")) + "\n")
        os.chmod(SESS_FILE, 0o600)
        size = os.path.getsize(SESS_FILE)
        _sess_compact_after = max(SESS_MAX_BYTES, size + SESS_MAX_BYTES)
    except Exception:
        pass
    logger.info(f"proxy: loaded {len(_sessions)} live playback sessions"
                f" (scrubbed {scrubbed} legacy release identities)")
    _bufcache_startup()


def _lookup(token: str) -> dict | None:
    hit = _sessions.get(token)
    if not hit:
        return None
    ts, entry = hit
    if time.time() - ts > SESS_TTL:
        _sessions.pop(token, None)
        return None
    return entry


def _mint(cands: list[dict], pool: list[dict], media: str, media_id: str,
          picker: str, hls: bool = False) -> str:
    token = secrets.token_urlsafe(9)
    # pool = every proxyable candidate for this title (the whole ranked list), so
    # a twin of whatever ends up playing can be found even if it sits outside this
    # token's short failover window. total = the file's byte-length, learned on
    # the opening request and used to prove a twin is byte-identical before we
    # splice to it.
    entry = {"cands": cands, "pool": pool, "id": media_id, "picker": picker,
             "pin": None, "total": None}
    if hls:
        entry["hls"] = True
    _sessions[token] = (time.time(), entry)
    _persist(token, entry)
    if len(_sessions) > 5000:                     # opportunistic prune
        cutoff = time.time() - SESS_TTL
        for t in [t for t, (ts, _) in _sessions.items() if ts < cutoff]:
            _sessions.pop(t, None)
    return token


# ── URL rewriting (called by the picker before it answers) ───────────────────
def _proxyable(s: dict) -> bool:
    if not s.get("url"):
        return False
    name = s.get("name") or ""
    return not any(e in name for e in ("📚", "⏳", "🎬"))   # skip library / notices


def _internal_url(url: str) -> bool:
    """True if the URL points at an internal Docker service name — a bare
    hostname with no dot, e.g. `http://stremthru:8080/…` or `mediafusion`. The
    player can't resolve those (only containers on the compose network can), so
    such a URL MUST be proxied through us or it fails with UnknownHostException.
    Public FQDNs (addon.example.com, comet.example.net) and IPs contain a dot."""
    host = urlsplit(url).hostname or ""
    return bool(host) and "." not in host


def _must_wrap(url: str) -> bool:
    """URLs that may never be handed to a player raw: internal Docker hostnames
    (unresolvable outside) and URLs carrying credentials in their userinfo (the
    direct-nzb lane embeds the WebDAV login — the proxy uses it upstream, but it
    must never appear in a response)."""
    return _internal_url(url) or "@" in (urlsplit(url).netloc or "")


def _is_hls(url: str) -> bool:
    """HLS playlists (custom HTTP addons serve these) must reach the player
    untouched: the byte-range buffer can't cache a playlist meaningfully, and
    serving one from /proxy/ breaks its relative segment URIs — the player
    would resolve them against our host and 404 every segment."""
    path = (urlsplit(url or "").path or "").lower()
    return path.endswith((".m3u8", ".m3u"))


def _cand(s: dict) -> dict:
    ident = telemetry.identity(s)
    lbl = f"{ident['res']}p {ident['src']} {ident['grp']}".strip()
    return {"u": s["url"], "need": (s.get("_qbps") or 0) / 8,   # bytes/s the file needs
            "src": ident["src"], "dbr": ident["debrid"], "grp": ident["grp"],
            "res": ident["res"], "size": ident["size"],
            "codec": ident["codec"], "hdr": ident["hdr"],
            "sig": telemetry.signature(s), "lbl": lbl,
            "rh": hlsproxy.request_headers(s),   # upstream request headers
            "nzb_indexers": list(s.get("_nzb_indexers") or [])}


def wrap(streams: list[dict], media: str, media_id: str, picker: str) -> list[dict]:
    """Return a copy of the list with online stream URLs replaced by proxy URLs
    whose token carries the failover tail from each position. Library and notice
    entries are left untouched. Never mutates the input dicts (they're cached).

    The top WRAP_MAX proxyable streams are wrapped for the auto-switch/telemetry
    win; past that, public-host URLs are served raw (they still play). But an
    internal-host URL (Docker service name) is UNPLAYABLE unwrapped, so it's
    always wrapped regardless of the cap — the fix for streams past #8 leaking a
    bare `stremthru:8080` the player couldn't resolve."""
    if not ENABLED:
        # No proxy to reach internal hosts through — drop them rather than serve
        # a URL the player can't resolve (or one carrying credentials); public
        # URLs pass through untouched.
        return [s for s in streams
                if not (_proxyable(s) and _must_wrap(s["url"]))]
    tail = [s for s in streams if _proxyable(s) and not _is_hls(s["url"])]
    pool = [_cand(x) for x in tail]          # every proxyable candidate, once
    hls_tail = [s for s in streams if _proxyable(s) and _is_hls(s["url"])]
    hls_pool = [_cand(x) for x in hls_tail]
    made = 0
    out = []
    for s in streams:
        if _proxyable(s) and _is_hls(s["url"]):
            if hlsproxy.ENABLED:
                # Playlist-rewriting proxy (app.hlsproxy): the player fetches
                # everything from us, we fetch upstream with the declared
                # headers from one IP — fixes referer-gated/IP-locked hosts
                # and makes credentialed playlists servable. Failover
                # candidates are the other HLS releases for this title.
                pos = hls_tail.index(s)
                token = _mint(hls_pool[pos:pos + MAXFAIL], hls_pool,
                              media, media_id, picker, hls=True)
                ns = dict(s)
                ns["url"] = f"{PUBLIC}/proxy/{token}"
                bh = dict(ns.get("behaviorHints") or {})
                # The upstream headers are ours to send now; never hand the
                # player a copy (they can carry tokens/cookies).
                bh.pop("proxyHeaders", None)
                ns["behaviorHints"] = bh
                out.append(ns)
                continue
            # Proxying disabled: playlists pass raw — unless raw is unsafe
            # (credentials/internal host), in which case there is no way to
            # serve them at all: wrapped they 404 every relative segment
            # without rewriting, raw they leak.
            if not _must_wrap(s["url"]):
                out.append(s)
            continue
        if _proxyable(s) and (made < WRAP_MAX or _must_wrap(s["url"])):
            pos = tail.index(s)
            cands = pool[pos:pos + MAXFAIL]
            token = _mint(cands, pool, media, media_id, picker)
            ns = dict(s)
            ns["url"] = f"{PUBLIC}/proxy/{token}"
            out.append(ns)
            made += 1
        else:
            out.append(s)
    return out


# ── proxying ─────────────────────────────────────────────────────────────────
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$", re.I)
_SUFFIX_RANGE_RE = re.compile(r"^bytes=-(\d+)$", re.I)
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.I)


def _container(head: bytes) -> str:
    """Best-guess A/V container from the opening bytes, or '' if it isn't media
    at all. A debrid link that's expired or points at an uncached torrent (or the
    wrong file index) returns 200 with an HTML/JSON error page, a plain-text
    error, or an archive — which the player can't parse (it shows
    ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED / "error page instead of video"), so
    '' means fail over. Real releases are one of the standard containers below.
    Transport streams (ts/m2ts) ARE valid video but are singled out so the
    start-selector can prefer a file container (see _select_start). Sniffed only
    on the byte-0 opening request, where the container header is present."""
    if len(head) < 12:
        return ""                                               # too short to be video
    b = head
    if b[:4] == b"\x1aE\xdf\xa3":                               # Matroska / WebM (EBML)
        return "mkv"
    if b[4:8] in (b"ftyp", b"styp", b"moov", b"free", b"skip",
                  b"mdat", b"wide", b"pnot"):                   # ISO-BMFF: MP4/MOV/M4V
        return "mp4"
    if b[:4] == b"RIFF" and b[8:12] in (b"AVI ", b"AVIX"):      # AVI
        return "avi"
    if b[:3] == b"FLV":                                         # Flash Video
        return "flv"
    if b[:4] == b"OggS":                                        # Ogg
        return "ogg"
    if b[:4] == b"\x30\x26\xb2\x75":                            # ASF / WMV
        return "asf"
    if b[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):     # MPEG PS / video ES
        return "mpegps"
    # Transport streams: 0x47 sync byte every 188 (TS), 192 (M2TS/BDAV — 4-byte
    # timestamp prefix, so sync sits at offset 4), or 204 (TS+FEC). Require the
    # sync to repeat 4x so a 'G'-prefixed (0x47) text error isn't mistaken for TS.
    for pkt in (188, 192, 204):
        for off in (0, 4):
            if len(b) >= off + 3 * pkt + 1 and all(
                    b[off + k * pkt] == 0x47 for k in range(4)):
                return "m2ts" if off == 4 or pkt == 192 else "ts"
    return ""


def _parse_range(h: str | None) -> tuple[int, int | None, bool]:
    if not h:
        return 0, None, False
    value = h.strip()
    m = _RANGE_RE.fullmatch(value)
    if m:
        return int(m.group(1)), (int(m.group(2)) if m.group(2) else None), True
    # A suffix range has no absolute offset until the upstream reports its total.
    # Mark it as a real range; serve() bypasses the sequential byte-zero cache and
    # forwards it directly with strict Content-Range validation.
    if _SUFFIX_RANGE_RE.fullmatch(value):
        return 0, None, True
    return 0, None, False


def _suffix_length(h: str | None) -> int | None:
    if not h:
        return None
    m = _SUFFIX_RANGE_RE.fullmatch(h.strip())
    return int(m.group(1)) if m else None


def _content_range(resp) -> tuple[int, int, int | None] | None:
    m = _CONTENT_RANGE_RE.fullmatch((resp.headers.get("content-range") or "").strip())
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    total = int(m.group(3)) if m.group(3).isdigit() else None
    if end < start or (total is not None and end >= total):
        return None
    return start, end, total


def _range_response_ok(resp, range_header: str | None,
                       expected_total: int | None = None) -> bool:
    """Validate that an upstream honored exactly the byte range we requested.

    A 200 is safe only for an unbounded byte-zero request.  At a nonzero offset,
    accepting a full-file 200 would append byte zero at the seek/reconnect point.
    """
    if not range_header:
        return resp.status_code in (200, 206)
    value = range_header.strip()
    suffix = _SUFFIX_RANGE_RE.fullmatch(value)
    explicit = _RANGE_RE.fullmatch(value)
    if not suffix and not explicit:
        return False
    if explicit and int(explicit.group(1)) == 0 and not explicit.group(2):
        if resp.status_code == 200:
            if expected_total is None:
                return True
            length = resp.headers.get("content-length") or ""
            return length.isdigit() and int(length) == expected_total
    if resp.status_code != 206:
        return False
    parsed = _content_range(resp)
    if parsed is None:
        return False
    start, end, total = parsed
    if expected_total is not None and total != expected_total:
        return False
    if suffix:
        wanted = int(suffix.group(1))
        if wanted <= 0 or total is None or end != total - 1:
            return False
        return end - start + 1 == min(wanted, total)
    wanted_start = int(explicit.group(1))
    wanted_end = int(explicit.group(2)) if explicit.group(2) else None
    return start == wanted_start and (wanted_end is None or end <= wanted_end)


async def _send(url: str, range_header: str | None):
    headers = {}
    if range_header:
        headers["Range"] = range_header
    req = _client.build_request("GET", url, headers=headers)
    return await _client.send(req, stream=True, follow_redirects=True)


def _fwd_headers(resp) -> dict:
    out = {"Accept-Ranges": "bytes"}
    for k, name in (("content-type", "Content-Type"),
                    ("content-length", "Content-Length"),
                    ("content-range", "Content-Range")):
        v = resp.headers.get(k)
        if v:
            out[name] = v
    return out


async def _head(entry: dict) -> Response:
    for c in entry["cands"]:
        try:
            resp = await _send(c["u"], "bytes=0-0")
            if resp.status_code in (200, 206):
                cr = resp.headers.get("content-range", "")
                total = cr.split("/")[-1] if "/" in cr else None
                ct = resp.headers.get("content-type", "application/octet-stream")
                await resp.aclose()
                h = {"Accept-Ranges": "bytes", "Content-Type": ct}
                if total and total != "*":
                    h["Content-Length"] = total
                return Response(status_code=200, headers=h)
            await resp.aclose()
        except Exception:
            continue
    return Response(status_code=502)


async def _select_start(cands: list[dict], range_header: str | None,
                        offset: int, end: int | None, token: str):
    """Try candidates in order; return the first that connects, starts promptly,
    and (for a full/large opening range) streams fast enough. Returns
    (idx, cand, resp, iterator, prebuffer, ttfb, net) or None if all failed.
    Each rejection is recorded against the release for *this* session — a release
    only gets blocked once several separate sessions go bad (see app.reputation)."""
    eval_full = end is None or (end - offset) >= EVAL_BYTES
    # Skip releases in short-term cooldown (one just delivered badly) so 'hit
    # play again' lands on the next source — unless they're ALL cooled, in which
    # case serve the best of them rather than nothing.
    skip_cooled = not all(reputation.cooled(c["sig"]) for c in cands)

    def _bad(c, reason, node="", extreme=False):
        reputation.observe(c["sig"], token, reason, c["lbl"], node=node, extreme=extreme)
        reputation.cooldown(c["sig"])
        if c["sig"].startswith("nzb:"):
            usenet_health.record_failure(
                c["sig"], c["lbl"], c.get("nzb_indexers") or [], reason,
                f"play:{token}:{c['sig']}")

    def _good(c):
        if c["sig"].startswith("nzb:"):
            usenet_health.record_success(
                c["sig"], c["lbl"], c.get("nzb_indexers") or [],
                f"play-ok:{token}:{c['sig']}")

    async def _attempt(idx, c):
        """Connect, read the opening bytes, and validate the source is prompt,
        fast enough, and actually media. Returns (idx, c, resp, it, prebuf, ttfb,
        net, container) with resp left OPEN on success; else None (resp closed,
        failure recorded)."""
        t0 = time.monotonic()
        try:
            resp = await _send(c["u"], range_header)
        except Exception as e:
            logger.info(f"proxy start: cand {idx} connect fail ({type(e).__name__})")
            _bad(c, "connect-fail")
            return None
        node = telemetry.netinfo(resp).get("node", "")
        if not _range_response_ok(resp, range_header):
            logger.info(f"proxy start: cand {idx} invalid range response "
                        f"(HTTP {resp.status_code}, "
                        f"Content-Range={resp.headers.get('content-range', '')!r})")
            await resp.aclose()
            reason = (f"http-{resp.status_code}" if resp.status_code not in (200, 206)
                      else "bad-content-range")
            _bad(c, reason, node=node)
            return None
        it = resp.aiter_raw()
        prebuf, got, t_first = [], 0, None
        try:
            async for chunk in it:
                now = time.monotonic()
                if t_first is None:
                    t_first = now
                prebuf.append(chunk)
                got += len(chunk)
                if not eval_full or got >= EVAL_BYTES or now - t0 > EVAL_DEADLINE:
                    break
        except Exception as e:
            logger.info(f"proxy start: cand {idx} read fail ({type(e).__name__})")
            await resp.aclose()
            _bad(c, "read-fail", node=node)
            return None
        # A 200/206 that isn't actually a media container = an error page / wrong
        # file from an expired or uncached debrid link. Serving it gives the player
        # a PARSING_CONTAINER_UNSUPPORTED error; reject and fail over instead.
        container = _container(b"".join(prebuf[:4])[:2048])
        if not container:
            ct = resp.headers.get("content-type", "")
            logger.info(f"proxy start: cand {idx} not a media container "
                        f"(ct={ct!r}, first={b''.join(prebuf[:1])[:12]!r}), trying next")
            await resp.aclose()
            _bad(c, "not-video", node=node)
            return None
        ttfb = (t_first - t0) if t_first else 999.0
        speed = got / max(time.monotonic() - (t_first or t0), 0.05)
        ok = ttfb <= START_TTFB
        extreme = False
        if ok and eval_full and got >= 512 * 1024 and c.get("need"):
            if speed < c["need"] * RATE_MARGIN:
                ok = False
                extreme = speed < c["need"] * 0.5      # can't sustain even half
        if not ok:
            logger.info(f"proxy start: cand {idx} too slow "
                        f"(ttfb {ttfb:.1f}s, {speed/1e6:.1f} MB/s), trying next")
            await resp.aclose()
            _bad(c, "extreme-slow" if extreme else "slow", node=node, extreme=extreme)
            return None
        return idx, c, resp, it, prebuf, ttfb, telemetry.netinfo(resp), container

    # Pass 1: prefer a file container (MKV/MP4/…). Transport-stream sources that
    # otherwise pass are held back — real video, but our players can't demux them.
    deferred: list[tuple[int, dict]] = []
    for idx, c in enumerate(cands):
        if skip_cooled and reputation.cooled(c["sig"]):
            logger.info(f"proxy start: cand {idx} in cooldown, skipping")
            continue
        res = await _attempt(idx, c)
        if res is None:
            continue
        if PREFER_FILE_CONTAINERS and res[-1] in _DEFER_CONTAINERS:
            await res[2].aclose()
            deferred.append((idx, c))
            logger.info(f"proxy start: cand {idx} is {res[-1]} (transport stream), "
                        f"deferring in favor of a file container")
            continue
        if idx:
            logger.info(f"proxy: failed over to candidate {idx} ({res[5]:.1f}s ttfb)")
        _good(c)
        return res[:-1]
    # Pass 2: no file container worked — serve the best transport stream we held.
    for idx, c in deferred:
        res = await _attempt(idx, c)
        if res is not None:
            logger.info(f"proxy start: no file-container source; serving "
                        f"transport-stream cand {idx} ({res[-1]}) as last resort")
            _good(c)
            return res[:-1]
    return None


def _total_of(resp) -> int | None:
    """The file's full byte-length from a 206's Content-Range, else None."""
    cr = resp.headers.get("content-range", "")
    if "/" in cr:
        t = cr.rsplit("/", 1)[-1]
        if t.isdigit():
            return int(t)
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit() and resp.status_code == 200:
        return int(cl)
    return None


def _twin_cands(entry: dict, cand: dict) -> list[dict]:
    """Byte-identical twins of `cand`: same release signature, different debrid
    (so a different node). Drawn from the whole ranked pool, not just this
    token's failover window."""
    sig = cand.get("sig")
    if not sig:
        return []
    pool = entry.get("pool") or entry.get("cands") or []
    return [c for c in pool
            if c.get("sig") == sig and c.get("dbr") != cand.get("dbr")
            and c.get("u") != cand.get("u")]


async def _open_twin(entry, cand, cur, end, tried):
    """Find and open a byte-identical twin at byte `cur`. Verifies the twin's
    total length matches this session's file before committing — a mismatch
    means it is NOT the same file, so we refuse to splice (feeding the player a
    different layout mid-stream would corrupt playback). Returns
    (twin_cand, resp, it, net) or None."""
    if not TWIN_SPLICE:
        return None
    want_total = entry.get("total")
    if cur > 0 and not want_total:
        return None                         # cannot prove byte identity before splicing
    for tw in _twin_cands(entry, cand):
        if tw["u"] in tried:
            continue
        tried.add(tw["u"])
        rh = f"bytes={cur}-" + (str(end) if end is not None else "")
        try:
            resp = await _send(tw["u"], rh)
        except Exception:
            continue
        if not _range_response_ok(resp, rh, expected_total=want_total):
            await resp.aclose()
            continue
        got_total = _total_of(resp)
        if want_total and got_total != want_total:
            logger.info(f"twin {tw['lbl']!r} size {got_total} != {want_total}, "
                        f"not byte-identical — refusing splice")
            await resp.aclose()
            continue
        return tw, resp, resp.aiter_raw(), telemetry.netinfo(resp)
    return None


# ── player-rejected detection ────────────────────────────────────────────────
# A stream the server verified (real video bytes, plenty of speed) can still be
# undecodable for the household's player — e.g. an MKV whose audio codec the
# player lacks (seen live: a 5×FLAC multi-audio fansub remux; H.264+DTS from
# the same player played fine). The player reports nothing: it just pulls the
# file header a few times at cache speed, gives up silently, and the viewer
# stares at a spinner. That consumption shape is unmistakable — several
# short-lived connections with header-sized reads and never a sustained play.
# After PLAYER_REJECT_STARTS such false starts the release is cooled (the very
# next open serves the next candidate) and a reputation session is recorded, so
# a repeat offender is eventually blocked for good. Server-side and
# player-agnostic: no codec guessing, pure observed behavior.
PLAYER_REJECT_STARTS = int(os.environ.get("PLAYER_REJECT_STARTS", "2"))
# Rejection is deterministic for a given player+file (unlike a flaky node), so
# the cooldown is much longer than the generic 15-minute one — long enough that
# the same cached pick can't re-serve the file tomorrow night either.
REJECT_COOLDOWN = float(os.environ.get("PLAYER_REJECT_COOLDOWN_HOURS", "24")) * 3600
_REAL_PLAY_SECS = 20.0                   # a connection this long = actual playback
_REAL_PLAY_BYTES = 256 * 1024 * 1024     # pulling this much = playing/buffering
# What counts as one false start. Live data: a decode-rejecting player dies in
# well under a second and always re-tries from byte 0, while normal playback is
# full of short connections too — but those are seeks and chunked mid-file
# range reads at NONZERO offsets (one healthy session had 931 of them). Offset
# zero + sub-_FALSE_START_SECS is the shape only a rejecting player produces.
_FALSE_START_SECS = 5.0
# Strikes must also cluster: a viewer sampling a title a few times over an
# evening is not a decode failure. Old strikes age out of the window.
_STRIKE_WINDOW = 180.0


def _note_consumer_close(state: dict, *, sig: str, label: str, node: str,
                         token: str, picker: str, media_id: str,
                         served: int, dur: float, offset: int = 0,
                         immediate: bool = False) -> None:
    """Feed one closed player connection into the player-rejected detector.
    `state` is a mutable per-release dict living on the cache entry (buffered
    path) or the playback session (legacy path); real playback latches it open
    so seek storms, header probes, and chunk-readers never count. On the
    buffered path rejection additionally requires the silence confirmation
    (_arm_reject_timer) — a player mid-startup-burst is never quiet, a player
    that gave up is. `immediate=True` (legacy path, which has no entry to hang
    a timer on) rejects on the strike count alone."""
    if not PLAYER_REJECT_STARTS or not sig:
        return
    if dur >= _REAL_PLAY_SECS or served >= _REAL_PLAY_BYTES:
        state["real_play"] = True
        return
    if state.get("real_play") or state.get("rejected"):
        return
    if offset != 0 or dur >= _FALSE_START_SECS:
        return                            # seek/chunk read, or a sampled watch
    now = time.monotonic()
    strikes = [t for t in state.get("strikes", []) if now - t < _STRIKE_WINDOW]
    strikes.append(now)
    state["strikes"] = strikes
    state["false_starts"] = len(strikes)
    if immediate and len(strikes) >= PLAYER_REJECT_STARTS + 1:
        _mark_rejected(state, sig=sig, label=label, node=node, token=token,
                       picker=picker, media_id=media_id,
                       detail=f"{len(strikes)} false starts, no sustained play")


def _mark_rejected(state: dict, *, sig: str, label: str, node: str, token: str,
                   picker: str, media_id: str, detail: str) -> None:
    state["rejected"] = True
    logger.info(f"proxy: player rejected {label!r} — {detail} (codec/container "
                f"the player can't open?); cooling release, next open serves "
                f"the next source")
    reputation.observe(sig, token, "player-rejected", label, node=node)
    reputation.cooldown(sig, REJECT_COOLDOWN)
    telemetry.record_buffer("player_rejected", sig=sig, picker=picker,
                            media_id=media_id, source=label, node=node,
                            reason=detail)
    sess = _lookup(token)
    if sess is not None:     # lets the first real play log a recovery_ok event
        sess["rejected_at"] = time.time()


# Players that reject a file usually go quiet, then re-request the same URL on
# their own every ~30s. After PLAYER_REJECT_STARTS false starts, silence IS the
# confirmation: no new connection and no active reader for _REJECT_SILENCE_SECS
# means the player has given up on this file — reject then, so its very next
# self-retry is served the next candidate. Recovery without the viewer touching
# anything. The silence requirement is also the false-positive guard: a player
# mid-startup-burst or chunk-reading through a working stream is never quiet.
_REJECT_SILENCE_SECS = 15.0


def _arm_reject_timer(e: "_Entry", token: str) -> None:
    state = e.playfail
    if (not PLAYER_REJECT_STARTS or state.get("rejected")
            or state.get("real_play")
            or state.get("false_starts", 0) < PLAYER_REJECT_STARTS
            or state.get("timer_armed")):
        return
    state["timer_armed"] = True

    async def _fire():
        try:
            await asyncio.sleep(_REJECT_SILENCE_SECS)
            if (state.get("rejected") or state.get("real_play")
                    or e.consumers > 0):
                return                 # reattached or resolved meanwhile
            _mark_rejected(state, sig=e.sig,
                           label=(e.source or {}).get("lbl", ""), node=e.node,
                           token=token, picker=e.picker, media_id=e.media_id,
                           detail=f"{state.get('false_starts', 0)} false starts"
                                  f" then {_REJECT_SILENCE_SECS:.0f}s silence")
            _spawn_learn(e, played=False)
        finally:
            state["timer_armed"] = False

    t = asyncio.create_task(_fire())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _spawn_learn(e: "_Entry", played: bool) -> None:
    """Learn the *class* of file from this entry, off the serving path."""
    if not vprobe.enabled():
        return
    t = asyncio.create_task(_learn_codecs(e, played))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def _learn_codecs(e: "_Entry", played: bool) -> None:
    """ffprobe the entry's cached bytes and feed decode_health: a rejection
    strikes the file's codec attributes, real playback credits them. This is
    what turns one FLAC spinner into 'FLAC stops ranking first' instead of
    re-learning the same lesson release by release."""
    try:
        ac, vc = await vprobe.codecs_of(e.path)
    except Exception:
        return
    if not ac and not vc:
        return
    lbl = (e.source or {}).get("lbl", "")
    if played:
        decode_health.record_play(ac, vc)
    else:
        logger.info(f"decode-health: rejected file {lbl!r} carries "
                    f"audio={list(ac)} video={vc!r}")
        decode_health.record_reject(ac, vc, label=lbl)


def _note_recovery(e: "_Entry", token: str) -> None:
    """A session that had a player-rejected release just reached real playback
    on another one — the automatic swap worked. One durable event, so the
    dashboard can count auto-recoveries instead of inferring them from logs."""
    sess = _lookup(token)
    if sess is None or sess.pop("rejected_at", None) is None:
        return
    logger.info(f"proxy: auto-recovery — {(e.source or {}).get('lbl', '')!r} "
                f"plays after a rejected release on the same token")
    telemetry.record_buffer("recovery_ok", sig=e.sig, picker=e.picker,
                            media_id=e.media_id,
                            source=(e.source or {}).get("lbl", ""), node=e.node,
                            reason="played after player-rejected swap")


async def _body(entry, idx, cand, resp, it, prebuf, offset, end, ttfb, token, net):
    t0 = time.monotonic()
    served = sum(len(c) for c in prebuf)
    cur = offset + served
    reconnects, splices, reason = 0, 0, "eof"
    tried = {cand["u"]}                      # source URLs already used this session
    # Time spent *waiting on the source* vs bytes it delivered in that time. This
    # isolates a slow SOURCE from a satisfied client throttling its reads: when
    # the client's buffer is full it stops pulling, so we stop awaiting upstream
    # and this rate stays clean. up_rate < needed bitrate over a real sample = the
    # mid-stream buffering the start-eval can't catch.
    up_bytes, up_time = 0, 0.0
    win: deque = deque()                     # (up_time, up_bytes) for buffering detect

    async def _swap_to(target) -> None:
        """Splice to an opened byte-identical twin: close the current source and
        switch reads to the twin at the same offset. The player's byte stream is
        uninterrupted (identical bytes), so it never sees the change. Resets the
        source-rate window so we judge the new node on its own. No signature
        cooldown here — signature keys the *release*, which both twins share, so
        cooling it would wrongly demote the good twin we're switching to."""
        nonlocal resp, it, cand, net, up_bytes, up_time
        tw, tresp, tit, tnet = target
        logger.info(f"proxy: twin-splice at byte {cur} — "
                    f"{cand.get('lbl')!r}@{net.get('node','')} -> "
                    f"{tw.get('lbl')!r}@{tnet.get('node','')}")
        telemetry.record_play(entry, cand, idx, served=served,
                              dur=time.monotonic() - t0, ttfb=ttfb,
                              reconnects=reconnects, reason="twin-spliced",
                              net=net, session=token,
                              up_mbps=(round(up_bytes / up_time / 1e6, 2)
                                       if up_time > 0.5 else None), slow=True)
        try:
            await resp.aclose()
        except Exception:
            pass
        resp, it, cand, net = tresp, tit, tw, tnet
        tried.add(tw["u"])
        up_bytes, up_time = 0, 0.0
        win.clear()

    try:
        for chunk in prebuf:
            yield chunk
        while True:                              # (re)connection / splice loop
            spliced = False
            try:
                while True:                      # read loop
                    r0 = time.monotonic()
                    try:
                        chunk = await it.__anext__()
                    except StopAsyncIteration:
                        chunk = None
                    if chunk is None:
                        reason = "eof"
                        break
                    up_time += time.monotonic() - r0
                    up_bytes += len(chunk)
                    served += len(chunk)
                    cur += len(chunk)
                    yield chunk
                    # Proactive twin-splice: if the source can't keep up over a
                    # real source-limited window and a healthier twin node exists,
                    # jump to it before the viewer's buffer drains.
                    if TWIN_PROACTIVE and splices < SPLICE_MAX and cand.get("need"):
                        win.append((up_time, up_bytes))
                        # Keep win[0] as an anchor just *past* the window so the
                        # measured span dt can actually reach SPLICE_WINDOW.
                        while len(win) >= 2 and up_time - win[1][0] >= SPLICE_WINDOW:
                            win.popleft()
                        dt, db = up_time - win[0][0], up_bytes - win[0][1]
                        if dt >= SPLICE_WINDOW and db / dt < cand["need"] * SPLICE_MARGIN:
                            target = await _open_twin(entry, cand, cur, end, tried)
                            if target:
                                splices += 1
                                await _swap_to(target)
                                spliced = True
                                break
                if spliced:
                    continue                     # read on from the twin node
                break                            # clean EOF
            except asyncio.CancelledError:
                reason = "client_gone"
                raise
            except Exception as e:
                # 1) Same-source reconnect — recovers a dropped TCP to a node
                #    that's still healthy (transient blip).
                if reconnects < RECONNECT_MAX:
                    reconnects += 1
                    logger.info(f"proxy: mid-stream drop ({type(e).__name__}), "
                                f"reconnect {reconnects} to same source at {cur}")
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    rh = f"bytes={cur}-" + (str(end) if end is not None else "")
                    try:
                        r2 = await _send(cand["u"], rh)
                    except Exception:
                        r2 = None
                    if r2 is not None and _range_response_ok(
                            r2, rh, expected_total=entry.get("total")):
                        resp, it = r2, r2.aiter_raw()
                        continue
                    if r2 is not None:
                        try:
                            await r2.aclose()
                        except Exception:
                            pass
                # 2) Same node won't come back — splice to a byte-identical twin
                #    on a different node (the failure reconnect can't fix).
                if splices < SPLICE_MAX:
                    target = await _open_twin(entry, cand, cur, end, tried)
                    if target:
                        splices += 1
                        await _swap_to(target)
                        continue
                reason = "upstream_dead"
                break
    finally:
        try:
            await resp.aclose()
        except Exception:
            pass
        up_mbps = round(up_bytes / up_time / 1e6, 2) if up_time > 0.5 else None
        # Judge the source only on a real sample where WE were the ones waiting.
        slow = bool(up_time >= 2 and up_bytes >= 8 * 1024 * 1024 and cand.get("need")
                    and up_bytes / up_time < cand["need"] * RATE_MARGIN)
        if reason == "upstream_dead" and served > 0:
            reputation.observe(cand["sig"], token, "mid-stream-dead", cand["lbl"],
                               node=net.get("node", ""))
            reputation.cooldown(cand["sig"])
            if cand["sig"].startswith("nzb:"):
                usenet_health.record_failure(
                    cand["sig"], cand["lbl"], cand.get("nzb_indexers") or [],
                    "mid-stream-dead", f"mid:{token}:{cand['sig']}")
        elif slow:
            logger.info(f"proxy: mid-stream slow — source fed {up_mbps} MB/s < need "
                        f"{cand['need']/1e6:.1f}, cooling {cand['lbl']!r}")
            reputation.observe(cand["sig"], token, "mid-stream-slow", cand["lbl"],
                               node=net.get("node", ""))
            reputation.cooldown(cand["sig"])
            if cand["sig"].startswith("nzb:"):
                usenet_health.record_failure(
                    cand["sig"], cand["lbl"], cand.get("nzb_indexers") or [],
                    "mid-stream-slow", f"mid:{token}:{cand['sig']}")
        telemetry.record_play(entry, cand, idx, served=served,
                              dur=time.monotonic() - t0, ttfb=ttfb,
                              reconnects=reconnects, reason=reason,
                              net=net, session=token, up_mbps=up_mbps, slow=slow)
        _note_consumer_close(
            entry.setdefault("_playfail", {}).setdefault(cand.get("sig") or "", {}),
            sig=cand.get("sig") or "", label=cand.get("lbl", ""),
            node=net.get("node", ""), token=token,
            picker=entry.get("picker", ""), media_id=entry.get("id", ""),
            served=served, dur=time.monotonic() - t0, offset=offset,
            immediate=True)


# ── server-side read-ahead buffer (producer/consumer cache on NVMe) ──────────
# Instead of streaming the debrid link straight through at the player's pace, a
# background *producer* downloads the file sequentially into a cache file on the
# server's NVMe, staying ahead of playback, while the *consumer* (the player's
# connection) reads from that cache. Any upstream hiccup, reconnect, or
# TorBox<->Real-Debrid twin switch then happens on the producer side and is
# invisible to the player as long as the buffer has runway — so it stabilises
# single-source streams too, not just twinned ones. Cache entries are keyed by
# release signature (twins and re-watches share one download), retained up to
# BUFFER_TTL / BUFFER_CACHE_BYTES (LRU), and wiped on restart (pure optimisation,
# never authoritative). Seeks ahead of the write head fall through to a direct
# pass-through; seeks behind it are served instantly from cache.
PROXY_BUFFER = os.environ.get("PROXY_BUFFER", "1") not in ("0", "false", "")
BUFFER_DIR = os.environ.get(
    "BUFFER_DIR", os.path.join(os.environ.get("TELEMETRY_DIR", "/data"), "bufcache"))
BUFFER_CACHE_BYTES = int(float(os.environ.get("BUFFER_CACHE_GB", "100")) * 1024 ** 3)
BUFFER_TTL = float(os.environ.get("BUFFER_TTL_SECONDS", str(24 * 3600)))
# How far ahead of the furthest reader the producer runs before pausing
# (backpressure). Bounds the read-ahead runway (outage tolerance) and the most we
# can over-download if a viewer abandons mid-watch. For files smaller than this
# the whole thing ends up cached, which is what makes seeks cheap.
BUFFER_AHEAD_BYTES = int(float(os.environ.get("BUFFER_AHEAD_GB", "8")) * 1024 ** 3)
BUFFER_WAIT_TIMEOUT = float(os.environ.get("BUFFER_WAIT_TIMEOUT", "45"))
BUFFER_REAP_INTERVAL = float(os.environ.get("BUFFER_REAP_INTERVAL", "60"))
BUFFER_READ_CHUNK = int(os.environ.get("BUFFER_READ_CHUNK", str(4 * 1024 * 1024)))
# Proactive producer-side switch: if the feeding source sustains below need*margin
# while a reader is waiting at the write head, jump to a byte-identical twin node.
BUFFER_SLOW_WINDOW = float(os.environ.get("BUFFER_SLOW_WINDOW", "8"))
BUFFER_SLOW_MARGIN = float(os.environ.get("BUFFER_SLOW_MARGIN", "0.9"))
# Next-episode prefetch: the moment a series episode starts playing, run the
# search+pick for episode E+1 and cache the *result list* — no stream bytes are
# downloaded — so opening the next episode is an instant cache hit.
PREFETCH_NEXT = os.environ.get("PREFETCH_NEXT", "1") not in ("0", "false", "")


class _Entry:
    __slots__ = ("sig", "path", "total", "avail", "complete", "failed", "source",
                 "cands", "producer", "consumers", "head", "last", "cond",
                 "content_type", "picker", "media_id", "node", "playfail")

    def __init__(self, sig, path, cands, source, content_type, total, picker, media_id):
        self.sig = sig
        self.playfail: dict = {}      # player-rejected detector state (_note_consumer_close)
        self.path = path
        self.total = total            # full byte length (Content-Range), or None
        self.avail = 0                # contiguous bytes present from 0 (write head)
        self.complete = False
        self.failed = False
        self.source = source          # cand dict currently feeding the file
        self.cands = cands            # all byte-identical sources for this release
        self.producer = None
        self.consumers = 0
        self.head = 0                 # furthest reader position (backpressure anchor)
        self.last = time.time()
        self.cond = asyncio.Condition()
        self.content_type = content_type or "application/octet-stream"
        self.picker = picker
        self.media_id = media_id
        self.node = ""                # delivery node of the current feeding source


_entries: dict[str, _Entry] = {}
_entries_start_lock = asyncio.Lock()
_reaper_task = None            # module ref so the reaper isn't GC'd mid-run
_bg_tasks: set = set()         # strong refs for fire-and-forget prefetch tasks


def active_streams() -> int:
    """Streams with a reader attached right now (buffered path only) — the
    settings page's 'you'd be cutting someone off' number before a restart."""
    return sum(1 for e in _entries.values() if e.consumers > 0)


def active_stream_details() -> list[dict]:
    """Return rich metadata for every stream with an active reader — used by
    the overview dashboard's "Now Playing" section.  Pure in-memory read."""
    out = []
    for e in _entries.values():
        if e.consumers <= 0:
            continue
        src = e.source or {}
        out.append({
            "media_id": e.media_id or "",
            "label":    src.get("lbl", ""),
            "debrid":   src.get("dbr", ""),
            "res":      src.get("res", 0),
            "node":     e.node or "",
            "avail":    e.avail,
            "total":    e.total,
            "consumers": e.consumers,
            "picker":   e.picker or "",
        })
    return out


def _safe_name(sig: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sig)[:120] or secrets.token_urlsafe(8)


def _evict(e: _Entry) -> None:
    if e.producer:
        e.producer.cancel()
    _entries.pop(e.sig, None)
    try:
        os.remove(e.path)
    except Exception:
        pass


def _retire_failed(e: _Entry) -> None:
    """Remove a failed generation without letting its eventual cleanup delete a
    replacement entry that uses the same signature/path namespace."""
    if e.producer:
        e.producer.cancel()

    async def cleanup() -> None:
        if e.consumers > 0:
            async with e.cond:
                await e.cond.wait_for(lambda: e.consumers <= 0)
        try:
            os.remove(e.path)
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("failed cache generation cleanup failed", exc_info=True)

    t = asyncio.create_task(cleanup())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def serve_hls(token: str, request) -> Response:
    """Route target for /proxy/{token}/hls — signed HLS sub-resources."""
    entry = _lookup(token)
    if entry is None:
        return Response(status_code=410)
    if not entry.get("hls"):
        return Response(status_code=404)
    return await hlsproxy.serve_resource(token, entry, request)


async def _reaper() -> None:
    """Keep the cache inside its budget: drop entries idle past BUFFER_TTL, and
    when total on-disk exceeds BUFFER_CACHE_BYTES evict least-recently-used ones
    that have no active reader until back under. Never touches a watched entry."""
    while True:
        await asyncio.sleep(BUFFER_REAP_INTERVAL)
        try:
            await hlsproxy.flush_idle()   # HLS sessions account per-session
            now = time.time()
            for e in list(_entries.values()):
                if e.consumers <= 0 and now - e.last > BUFFER_TTL:
                    logger.info(f"bufcache: TTL-evict {e.sig} ({e.avail // 1024 // 1024} MB)")
                    _evict(e)
            total = sum(e.avail for e in _entries.values())
            if total > BUFFER_CACHE_BYTES:
                for e in sorted(_entries.values(), key=lambda x: x.last):
                    if total <= BUFFER_CACHE_BYTES:
                        break
                    if e.consumers <= 0:
                        logger.info(f"bufcache: size-evict {e.sig} ({e.avail // 1024 // 1024} MB)")
                        total -= e.avail
                        _evict(e)
        except Exception:
            logger.exception("bufcache reaper error")


def _bufcache_startup() -> None:
    """Clear stale cache files on boot (a restart may have left partial ones; the
    cache is never authoritative) and launch the reaper. Called from load() at
    app startup. Deletes only our own *.bin files, never the whole directory, so a
    misconfigured BUFFER_DIR can't wipe unrelated data."""
    global _reaper_task
    if not PROXY_BUFFER:
        return
    try:
        os.makedirs(BUFFER_DIR, exist_ok=True)
        os.chmod(BUFFER_DIR, 0o700)
        for name in os.listdir(BUFFER_DIR):
            if name.endswith(".bin"):
                try:
                    os.remove(os.path.join(BUFFER_DIR, name))
                except Exception:
                    pass
    except Exception:
        logger.exception("bufcache: could not prepare cache dir")
        return
    _reaper_task = asyncio.create_task(_reaper())
    logger.info(f"bufcache: ready at {BUFFER_DIR} (cap {BUFFER_CACHE_BYTES // 1024 ** 3}GB, "
                f"ttl {BUFFER_TTL / 3600:.0f}h, ahead {BUFFER_AHEAD_BYTES // 1024 ** 3}GB)")


async def _connect_resume(e: _Entry, tried: set, prefer_new: bool = False) -> tuple | None:
    """(Re)connect a feeding source at the current write head e.avail — the same
    source first (transient blip) unless prefer_new, then byte-identical twins /
    other same-signature sources on different nodes. Requires a 206 at the exact
    offset and, when known, a matching total length, so we never resume from a
    different file. Returns (cand, resp, iterator) or None."""
    offset = e.avail
    others = [c for c in e.cands if c is not e.source]
    order = (others + ([e.source] if e.source else [])) if prefer_new \
        else (([e.source] if e.source else []) + others)
    for c in order:
        same_source = c is e.source
        if offset > 0 and not same_source and not e.total:
            continue                              # no byte-identity proof for a twin
        rh = f"bytes={offset}-"
        try:
            resp = await _send(c["u"], rh)
        except Exception:
            continue
        if not _range_response_ok(resp, rh, expected_total=e.total):
            await resp.aclose()
            continue
        gt = _total_of(resp)
        if e.total and gt and gt != e.total:            # not the same file
            await resp.aclose()
            continue
        if not e.total and gt:
            e.total = gt
        e.source = c
        tried.add(c["u"])
        return c, resp, resp.aiter_raw()
    return None


async def _produce(e: _Entry, token: str, resp, it, prebuf: list) -> None:
    """Background producer: fill e.path sequentially from the already-open initial
    source (resp/it/prebuf from _select_start), advancing e.avail and waking
    readers as bytes land. Reconnects / twin-switches on a drop or sustained
    slowness — invisible to readers behind the write head. Pauses (backpressure)
    once BUFFER_AHEAD_BYTES past the furthest reader."""
    f = None
    win: deque = deque()               # (t, up_bytes) for slow-source detection
    up_bytes = 0
    node = telemetry.netinfo(resp).get("node", "") if resp else ""
    e.node = node
    try:
        f = open(e.path, "wb", buffering=0)
        os.chmod(e.path, 0o600)
        for chunk in prebuf:
            f.write(chunk)
        async with e.cond:
            e.avail += sum(len(c) for c in prebuf)
            e.cond.notify_all()
        tried = {e.source["u"]} if e.source else set()
        while True:
            async with e.cond:            # backpressure + pause when nobody's watching
                await e.cond.wait_for(
                    lambda: e.consumers > 0 and e.avail - e.head <= BUFFER_AHEAD_BYTES)
            try:
                chunk = await it.__anext__()
            except StopAsyncIteration:
                chunk = None
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.info(f"bufcache {e.sig}: source drop at {e.avail} "
                            f"({type(ex).__name__}), reconnecting")
                telemetry.record_buffer(
                    "drop", sig=e.sig, picker=e.picker, media_id=e.media_id,
                    source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    offset=e.avail, total=e.total, reason=type(ex).__name__)
                try:
                    await resp.aclose()
                except Exception:
                    pass
                if e.avail > 0 and e.source:
                    reputation.observe(e.source.get("sig", e.sig), token,
                                       "mid-stream-dead", e.source.get("lbl", ""))
                    sig = e.source.get("sig", e.sig)
                    if sig.startswith("nzb:"):
                        usenet_health.record_failure(
                            sig, e.source.get("lbl", ""),
                            e.source.get("nzb_indexers") or [], "mid-stream-dead",
                            f"buffer:{token}:{sig}")
                nxt = await _connect_resume(e, tried)
                if nxt is None:
                    async with e.cond:
                        e.failed = True
                        e.cond.notify_all()
                    telemetry.record_buffer(
                        "failed", sig=e.sig, picker=e.picker, media_id=e.media_id,
                        offset=e.avail, total=e.total,
                        reason="all sources exhausted")
                    logger.info(f"bufcache {e.sig}: all sources failed at {e.avail}")
                    break
                _, resp, it = nxt
                node = telemetry.netinfo(resp).get("node", "")
                e.node = node
                telemetry.record_buffer(
                    "reconnect", sig=e.sig, picker=e.picker, media_id=e.media_id,
                    source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    offset=e.avail, total=e.total)
                win.clear()
                up_bytes = 0
                continue
            if chunk is None:                     # clean EOF: whole file cached
                async with e.cond:
                    e.complete = True
                    if e.total is None:
                        e.total = e.avail
                    e.cond.notify_all()
                logger.info(f"bufcache {e.sig}: complete ({e.avail} bytes)")
                telemetry.record_buffer(
                    "complete", sig=e.sig, picker=e.picker, media_id=e.media_id,
                    source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    avail=e.avail, total=e.total)
                break
            f.write(chunk)
            async with e.cond:
                e.avail += len(chunk)
                e.cond.notify_all()
            # Proactive twin-switch: a sustained-slow source with a reader sitting
            # at the write head (buffer drained) — jump to a healthier twin node.
            need = (e.source or {}).get("need") or 0
            if TWIN_SPLICE and need:
                now = time.monotonic()
                up_bytes += len(chunk)
                win.append((now, up_bytes))
                while len(win) >= 2 and now - win[1][0] >= BUFFER_SLOW_WINDOW:
                    win.popleft()
                dt, db = now - win[0][0], up_bytes - win[0][1]
                reader_at_edge = e.head >= e.avail - 4 * 1024 * 1024
                twins = [c for c in e.cands if c is not e.source
                         and c.get("dbr") != (e.source or {}).get("dbr")
                         and c["u"] not in tried]
                if (dt >= BUFFER_SLOW_WINDOW and reader_at_edge and twins
                        and db / dt < need * BUFFER_SLOW_MARGIN):
                    rate = round(db / dt / 1e6, 2)
                    logger.info(f"bufcache {e.sig}: source slow "
                                f"({rate} MB/s < need), twin-switch")
                    telemetry.record_buffer(
                        "slow", sig=e.sig, picker=e.picker, media_id=e.media_id,
                        source=(e.source or {}).get("lbl", ""),
                        dbr=(e.source or {}).get("dbr", ""), node=node,
                        offset=e.avail, total=e.total, mbps=rate,
                        reason="below need at write head")
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    nxt = await _connect_resume(e, tried, prefer_new=True)
                    if nxt is not None:
                        _, resp, it = nxt
                        node = telemetry.netinfo(resp).get("node", "")
                        e.node = node
                        telemetry.record_buffer(
                            "twin", sig=e.sig, picker=e.picker, media_id=e.media_id,
                            source=(e.source or {}).get("lbl", ""),
                            dbr=(e.source or {}).get("dbr", ""), node=node,
                            offset=e.avail, total=e.total, mbps=rate)
                    win.clear()
                    up_bytes = 0
    except asyncio.CancelledError:
        pass
    except Exception as ex:
        logger.exception(f"bufcache {e.sig}: producer crashed")
        async with e.cond:
            e.failed = True
            e.cond.notify_all()
        telemetry.record_buffer(
            "failed", sig=e.sig, picker=e.picker, media_id=e.media_id,
            offset=e.avail, total=e.total, reason=f"producer crash: {type(ex).__name__}")
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass
        try:
            if resp:
                await resp.aclose()
        except Exception:
            pass
        e.producer = None


async def _get_or_start_entry(token: str, session: dict, cands: list) -> _Entry | None:
    """Find the cache entry this token's release maps to (twins and re-watches
    reuse an existing one), or select a working source at offset 0 and start a
    producer. Returns the entry, or None if no source could even start."""
    retired: list[_Entry] = []
    reuse = None
    async with _entries_start_lock:
        sigs = [session.get("bufsig")] + [c.get("sig") for c in cands]
        for sig in dict.fromkeys(s for s in sigs if s):
            existing = _entries.get(sig)
            if existing is None:
                continue
            if existing.failed:
                if _entries.get(sig) is existing:
                    _entries.pop(sig, None)
                retired.append(existing)
                if session.get("bufsig") == sig:
                    session.pop("bufsig", None)
                continue
            if existing.playfail.get("rejected"):
                continue        # player can't open it — pick a different release
            reuse = existing
            session["bufsig"] = sig
            break
    for old in retired:
        _retire_failed(old)
    if reuse is not None:
        return reuse

    # Network evaluation is deliberately outside the global registry lock. Two
    # concurrent cold starts may perform duplicate probes, but unrelated viewers
    # no longer queue behind several seconds of another title's network I/O.
    sel = await _select_start(cands, "bytes=0-", 0, None, token)
    if sel is None:
        return None
    _, cand, resp, it, prebuf, _, _ = sel
    sig = cand.get("sig") or secrets.token_urlsafe(8)
    reuse = None
    retired = []
    async with _entries_start_lock:
        existing = _entries.get(sig)
        if existing is not None and existing.failed:
            _entries.pop(sig, None)
            retired.append(existing)
            existing = None
        if existing is not None:                    # another cold start won the race
            reuse = existing
            session["bufsig"] = sig
        else:
            pool = session.get("pool") or cands
            srcs = [c for c in pool if c.get("sig") == sig] or [cand]
            # A generation suffix lets a retired entry finish serving an already
            # open file descriptor while its replacement starts cleanly.
            path = os.path.join(
                BUFFER_DIR, f"{_safe_name(sig)}.{secrets.token_hex(4)}.bin")
            e = _Entry(sig, path, srcs, cand,
                       resp.headers.get("content-type", ""), _total_of(resp),
                       session.get("picker", ""), session.get("id", ""))
            _entries[sig] = e
            session["bufsig"] = sig
            e.producer = asyncio.create_task(_produce(e, token, resp, it, prebuf))
    for old in retired:
        _retire_failed(old)
    if reuse is not None:
        await resp.aclose()
        return reuse
    logger.info(f"bufcache {sig}: start (source {cand.get('lbl')!r}, total {e.total})")
    telemetry.record_buffer("start", sig=sig, picker=e.picker, media_id=e.media_id,
                            source=cand.get("lbl", ""), dbr=cand.get("dbr", ""),
                            node=telemetry.netinfo(resp).get("node", ""),
                            total=e.total)
    return e


async def _fire_prefetch(media_id: str, picker_label: str) -> None:
    """Ask the picker to prep the next episode. Lazy import to avoid an import
    cycle (proxy is imported by picker's callers)."""
    try:
        from app import picker
        await picker.prefetch_next("series", media_id, picker_label)
    except Exception:
        logger.exception(f"prefetch trigger failed for {media_id}")


def _cache_headers(e: _Entry, start: int, end: int | None, had_range: bool):
    total = e.total
    h = {"Accept-Ranges": "bytes", "Content-Type": e.content_type}
    if had_range:
        last = end if end is not None else ((total - 1) if total else None)
        if total is not None and last is not None:
            h["Content-Range"] = f"bytes {start}-{last}/{total}"
            h["Content-Length"] = str(last - start + 1)
        return 206, h
    if total is not None:
        h["Content-Length"] = str(total)
    return 200, h


async def _consume(e: _Entry, offset: int, end: int | None, token: str):
    """Stream bytes [offset, end] to the player out of the cache file, waiting on
    the producer for anything not written yet. Holds a consumer ref for its whole
    life (incremented before the first await) so the reaper can't evict mid-read."""
    e.consumers += 1
    t0 = time.monotonic()
    served, pos, reason, f = 0, offset, "eof", None
    limit = (end + 1) if end is not None else None      # exclusive upper bound
    try:
        for _ in range(100):                            # file appears the instant the producer starts
            try:
                f = open(e.path, "rb")
                break
            except FileNotFoundError:
                await asyncio.sleep(0.02)
        if f is None:
            reason = "no_cache_file"
            return
        while True:
            hi = limit if limit is not None else e.total
            if hi is not None and pos >= hi:
                break
            if pos < e.avail:
                upto = min(e.avail, hi) if hi is not None else e.avail
                f.seek(pos)
                data = f.read(min(upto - pos, BUFFER_READ_CHUNK))
                if not data:
                    await asyncio.sleep(0.02)
                    continue
                pos += len(data)
                served += len(data)
                async with e.cond:                      # advance backpressure anchor
                    if pos > e.head:
                        e.head = pos
                    e.cond.notify_all()
                yield data
            elif e.complete:
                break
            elif e.failed:
                reason = "upstream_dead"
                break
            else:
                async with e.cond:
                    try:
                        await asyncio.wait_for(
                            e.cond.wait_for(lambda: pos < e.avail or e.complete or e.failed),
                            timeout=BUFFER_WAIT_TIMEOUT)
                    except asyncio.TimeoutError:
                        reason = "stall_timeout"
                        break
    except asyncio.CancelledError:
        reason = "client_gone"
        raise
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass
        e.consumers -= 1
        e.last = time.time()
        async with e.cond:
            e.cond.notify_all()                         # let the producer re-evaluate
        try:
            telemetry.record_play({"picker": e.picker, "id": e.media_id},
                                  e.source or {}, 0, served=served,
                                  dur=time.monotonic() - t0, ttfb=0.0, reconnects=0,
                                  reason=reason, session=token,
                                  net={"node": e.node})
            _note_consumer_close(
                e.playfail, sig=e.sig, label=(e.source or {}).get("lbl", ""),
                node=e.node, token=token, picker=e.picker,
                media_id=e.media_id, served=served,
                dur=time.monotonic() - t0, offset=offset)
            _arm_reject_timer(e, token)
            if e.playfail.get("real_play") and not e.playfail.get("learned"):
                e.playfail["learned"] = True
                _spawn_learn(e, played=True)
                _note_recovery(e, token)
        except Exception:
            pass


def _skip_rejected(source: dict | None, cands: list) -> dict | None:
    """Swap a pass-through anchor away from a player-rejected release, so a
    tail/seek request landing mid-recovery can't hand the player bytes of the
    file it just gave up on (mixing files would make the GOOD file fail to
    parse too). Only *rejected* releases are skipped — the player never played
    them, so no open playback can depend on their bytes. A merely cooled (slow)
    release still serves its own seeks: a viewer mid-file must keep getting
    byte-identical data."""
    def ok(c):
        ent = _entries.get(c.get("sig") or "")
        return not (ent and ent.playfail.get("rejected"))
    if source is None or ok(source):
        return source
    return next((c for c in cands if ok(c)), source)


async def _serve_direct(session: dict, cands: list, request, token: str,
                        source: dict | None = None,
                        expected_total: int | None = None) -> Response:
    """Pass-through for a byte range we can't (yet) serve from cache — a seek ahead
    of the write head, or a session that never cached.  A nonzero/suffix seek is
    locked to one release; only byte-identical same-signature delivery twins may
    be tried after its preferred source."""
    rh = request.headers.get("range")
    offset, _, had_range = _parse_range(rh)
    seek = _suffix_length(rh) is not None or (had_range and offset > 0)
    anchor = source or (cands[0] if cands else None)
    order = ([anchor] if anchor else []) + [c for c in cands if c is not anchor]
    if seek and anchor is not None:
        sig = anchor.get("sig") or ""
        order = [c for c in order
                 if c is anchor or (sig and c.get("sig") == sig)]
    for c in order:
        try:
            resp = await _send(c["u"], rh)
        except Exception:
            continue
        if _range_response_ok(resp, rh, expected_total=expected_total):
            async def gen(resp=resp):
                try:
                    async for chunk in resp.aiter_raw():
                        yield chunk
                except asyncio.CancelledError:
                    raise
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
            return StreamingResponse(gen(), status_code=resp.status_code,
                                     headers=_fwd_headers(resp))
        await resp.aclose()
    return Response(status_code=502)


async def _serve_buffered(token: str, session: dict, cands: list, offset: int,
                          end: int | None, had_range: bool, request) -> Response:
    sig = session.get("bufsig")
    e = _entries.get(sig) if sig else None
    if e is not None and offset == 0 and e.playfail.get("rejected"):
        # The player provably can't open this release (see _note_consumer_close)
        # — a fresh open must select a different one, not re-serve the cache.
        session.pop("bufsig", None)
        e = None
    if (e is None or e.failed) and offset == 0:         # opening/retry starts a clean fill
        e = await _get_or_start_entry(token, session, cands)
    if e is None:                                       # seek-first, or no source at all
        pin = min(session.get("pin") or 0, len(cands) - 1) if cands else 0
        source = cands[pin] if cands and offset > 0 else None
        source = _skip_rejected(source, cands)
        return await _serve_direct(session, cands, request, token, source=source)
    e.last = time.time()
    # Seek ahead of what's cached (and not done): pass through directly rather than
    # block waiting for the sequential fill to reach it.
    if offset >= e.avail and not e.complete:
        if offset == 0 and e.failed:                    # producer never got going
            return await _serve_direct(session, e.cands, request, token,
                                       source=e.source, expected_total=e.total)
        if offset > 0:
            return await _serve_direct(session, e.cands, request, token,
                                       source=e.source, expected_total=e.total)
    status, headers = _cache_headers(e, offset, end, had_range)
    return StreamingResponse(_consume(e, offset, end, token),
                             status_code=status, headers=headers)


async def serve(token: str, request) -> Response:
    entry = _lookup(token)
    if entry is None:
        return Response(status_code=410)          # expired -> client re-fetches
    if entry.get("hls"):
        return await hlsproxy.serve_master(token, entry, request)
    if request.method == "HEAD":
        return await _head(entry)
    range_header = request.headers.get("range")
    offset, end, had_range = _parse_range(range_header)
    cands = entry["cands"]

    if range_header and not had_range:
        return Response(status_code=416, headers={"Accept-Ranges": "bytes"})

    # Suffix ranges address the tail of the file, so they cannot be represented
    # by the byte-zero sequential cache until the total is known.  Forward them
    # untouched, validate the returned tail, and never fail over to another
    # release merely because its URL happens to sit later in this token.
    suffix = _suffix_length(range_header)
    if suffix is not None:
        if suffix <= 0:
            return Response(status_code=416, headers={"Accept-Ranges": "bytes"})
        sig = entry.get("bufsig")
        cached = _entries.get(sig) if sig else None
        if cached is not None:
            return await _serve_direct(entry, cached.cands, request, token,
                                       source=cached.source,
                                       expected_total=cached.total)
        pin = min(entry.get("pin") or 0, len(cands) - 1) if cands else 0
        source = cands[pin] if cands else None
        source = _skip_rejected(source, cands)
        pool = entry.get("pool") or cands
        return await _serve_direct(entry, pool, request, token, source=source)

    # A series episode just started playing: search-and-cache the next episode
    # now (results only, no stream bytes), so it's an instant hit when opened.
    if (PREFETCH_NEXT and offset == 0 and ":" in (entry.get("id") or "")
            and not entry.get("nextfetched")):
        entry["nextfetched"] = True
        t = asyncio.create_task(_fire_prefetch(entry["id"], entry.get("picker", "")))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)

    if PROXY_BUFFER:
        try:
            return await _serve_buffered(token, entry, cands, offset, end,
                                         had_range, request)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bufcache: buffered serve failed, using direct path")

    if offset == 0:                                # opening request: failover here
        sel = await _select_start(cands, range_header, offset,
                                  end, token)
        if sel is None:
            telemetry.record_play(entry, cands[0] if cands else {}, -1,
                                  served=0, dur=0, ttfb=999, reconnects=0,
                                  reason="all_failed", session=token)
            return Response(status_code=502)
        idx, cand, resp, it, prebuf, ttfb, net = sel
        entry["pin"] = idx
    else:                                          # seek: locked to pinned source
        idx = min(entry.get("pin") or 0, len(cands) - 1)
        cand = cands[idx]
        try:
            resp = await _send(cand["u"], range_header)
        except Exception:
            return Response(status_code=502)
        if not _range_response_ok(resp, range_header,
                                  expected_total=entry.get("total")):
            await resp.aclose()
            return Response(status_code=502)
        it, prebuf, ttfb, net = resp.aiter_raw(), [], 0.0, telemetry.netinfo(resp)

    # Learn the file's full byte-length once, so a mid-stream twin-splice can
    # prove an alternate is the exact same file before switching to it.
    if not entry.get("total"):
        t = _total_of(resp)
        if t:
            entry["total"] = t

    headers = _fwd_headers(resp)
    status = resp.status_code
    gen = _body(entry, idx, cand, resp, it, prebuf, offset, end, ttfb, token, net)
    return StreamingResponse(gen, status_code=status, headers=headers)
