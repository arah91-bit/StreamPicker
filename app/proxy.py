"""Playback proxy + start-of-play auto-switcher.

The picker rewrites each online stream URL it returns into a `/proxy/<token>`
URL on this server. When the player opens it we stream the real bytes through,
which does two things the direct URL can't:

  1. Auto-switch on a bad source. For picker results that passed both semantic
     identity and transport verification, the token maps to the identity-safe
     ranked tail from that position. On the opening request we try the first
     candidate; if it's dead (4xx/5xx), never sends a byte, or delivers below
     the file's bitrate, we transparently fall through to the next and *pin*
     the winner. Unverified leftovers get a single-candidate token and can
     still be opened explicitly, but can never become a silent substitute.
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
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
from collections import deque
from urllib.parse import urlsplit

import httpx
from starlette.responses import Response, StreamingResponse

from app import (cfsolver, content_identity, decode_health, display_caps,
                 hlsproxy, reputation, telemetry, usenet_health, vprobe)

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("PROXY_PLAYBACK", "1") not in ("0", "false", "")
PUBLIC = os.environ.get("ADDON_PUBLIC_URL", "http://localhost:8011").rstrip("/")

# Streams to proxy per list. Everything past this is handed to the player as a
# raw URL, which plays but silently opts out of every proxy feature: the byte
# cache, mid-stream failover, telemetry, reputation learning, player-rejection
# detection — and the next-episode prewarm, which is only triggered from inside
# the proxy (see _schedule_prefetch). A viewer who scrolls past the top few
# results therefore loses the resilience the picker exists to provide, and the
# next episode of whatever they picked is never warmed.
#
# Raised from 8 to cover the whole list _assemble() can show (verified rows plus
# rest[:15]). The cost is a minted token per stream, which is cheap; the benefit
# is that picking result #12 behaves like picking result #1.
WRAP_MAX = int(os.environ.get("PROXY_WRAP_MAX", "24"))    # streams to proxy per list
MAXFAIL = int(os.environ.get("PROXY_MAX_FAILOVER", "4"))  # candidates per token
SESS_TTL = float(os.environ.get("PROXY_SESSION_TTL", str(24 * 3600)))
SESS_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"), "sessions.jsonl")
# Compact the session file once it outgrows this (it appends ~8-16 records per
# stream-list request and previously only compacted at startup — weeks of uptime
# let it grow into the hundreds of MB).
SESS_MAX_BYTES = int(os.environ.get("PROXY_SESSION_MAX_BYTES", str(20 * 1024 * 1024)))
SESS_MAX = int(os.environ.get("PROXY_SESSION_MAX", "5000"))

# start-of-play evaluation
EVAL_BYTES = 2 * 1024 * 1024
EVAL_DEADLINE = 8.0
START_TTFB = 8.0        # play-time links may cold-start; more lenient than probe
# A source that starts and then goes silent for the rest of the evaluation
# window is judged on the whole window. With no known bitrate this is the
# floor it must still clear (bytes/s) — far below any real file's need, far
# above a connection that has simply stopped.
_STALLED_START_FLOOR = 1_000_000
# The whole opening decision, across every candidate. Sequentially each could
# take connect (10 s) + first byte (8.5 s) + evaluation, so four bad ones cost
# well over a minute — long after the player (the TV gives up at ~15 s) had
# left, still opening connections and handing out strikes for nobody.
START_BUDGET = float(os.environ.get("PROXY_START_BUDGET", "20"))
# When the leading candidate has not decided within this long, the next one is
# started beside it so its latency is already paid if the leader fails. Ranking
# still decides: a backup that passes is held, and served only once everything
# ranked above it has failed. 0 = strictly one at a time.
START_STAGGER = float(os.environ.get("PROXY_START_STAGGER", "2.5"))
_STAGGER_MAX = 2                  # attempts in flight at once


class _StartBudgetSpent(Exception):
    pass


def _staggerable(c: dict) -> bool:
    """May `c` be started as a backup while a better candidate is still being
    judged? Not when the extra connection costs someone else: nzbdav shares
    one NNTP pool with the leader's article fetches, Easynews grants about two
    transfers, and an uncached debrid link can start a real download."""
    if (c.get("sig") or "").startswith("nzb:"):
        return False
    if _rationed_source(c):
        return False
    dbr = c.get("dbr") or ""
    return not (dbr and not dbr.endswith("+"))
RATE_MARGIN = 0.85
RECONNECT_MAX = 3
# Reconnects are spaced out, never fired back to back: a CDN that refuses one
# request usually answers a second later, and three instant retries used to
# spend the whole budget inside a single hiccup. Delay before attempt n is
# _RECONNECT_BACKOFF[n-1] (the last value repeats).
_RECONNECT_BACKOFF = (0.25, 1.0, 3.0)
# The budget refills once a reconnected source has delivered this much, so it
# bounds *consecutive* failures, not failures per film: a two-hour stream that
# blips four times is not ended by the fourth.
_RECONNECT_RESET_BYTES = 32 * 1024 * 1024
# How long the buffered producer keeps trying to resume a dropped source —
# same source, twins, then the verified pool — before it gives up on the file.
# One refused reconnect used to fail the whole entry; behind the write head the
# reader is still being served from disk, so patience here costs nothing.
BUFFER_RESUME_BUDGET = float(os.environ.get("BUFFER_RESUME_BUDGET", "45"))


def _reconnect_delay(attempt: int) -> float:
    """Pause before reconnect attempt `attempt` (1-based)."""
    if attempt <= 0:
        return 0.0
    return _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF)) - 1]
# How long an HTTP source may go silent mid-stream before we reconnect at the
# same byte. Without it a stalled CDN connection was waited out on the client's
# 60 s read timeout — longer than any player's buffer (~13-45 s), so the viewer
# saw a mid-episode spinner before anything was retried. Usenet is exempt (see
# _stall_limit).
STREAM_STALL_SECS = float(os.environ.get("STREAM_STALL_SECS", "12"))
# A wait on the source at least this long, while the player is asking for
# bytes, is recorded as a `starve` buffer event: the server-side half of "it
# buffered". Without it a mid-episode stall left no trace anywhere, and the
# only way to tell "the server made the player wait" from "the network or the
# player did" was guessing.
_STARVE_LOG_SECS = 2.0
# Reads outside the player's own byte stream — a splice's verification window,
# the tail warm — are bounded on their own. Neither may wait out the client's
# 60 s read timeout: the first holds up a resume a player is waiting on, the
# second can hold one of Easynews' two connections. Long enough for a cold
# nzbdav seek (an interpolation search measured ~7.7 s to first byte).
_SIDE_READ_SECS = 20.0
_STARVE_EVENTS_MAX = 30             # per stream; the play record keeps the totals
# Transport-stream containers (.ts / .m2ts Blu-ray BDAV) are valid video, but
# some players — including the household's Nuvio player — can't demux them and
# error with ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED on an otherwise-fine remux.
# So the start-selector prefers a file container (MKV/MP4/AVI/…) and serves a
# transport stream only when nothing else works. Env kill-switch → 0.
PREFER_FILE_CONTAINERS = os.environ.get("PREFER_FILE_CONTAINERS", "1") not in ("0", "false", "")
_DEFER_CONTAINERS = {"ts", "m2ts"}

# ── mid-stream twin-splice ───────────────────────────────────────────────────
# A byte-identical *twin* is the same torrent+file identity served
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

# ── cross-source resume (proof, not assumption) ──────────────────────────────
# Twin-splice above only fires for a torrent+file identity, because a shared
# infoHash is the one thing that *proves* two URLs carry the same bytes. The
# lanes this household actually plays from — direct usenet, Easynews, a debrid
# CDN — have no such identity, so their entries hold exactly one source: when
# it stops coming back, playback truncates mid-film with nothing to fail over
# to, even though the identical release is usually sitting in other lanes.
#
# Filename and length are not proof, and this file has been careful never to
# treat them as such. So don't assume — *verify*. Ask the new source for a
# window that reaches back into bytes we already hold on disk and compare it
# byte for byte; splice only if it reproduces them exactly. That is real proof
# of identity at the one place it matters (the splice point), and it costs no
# extra round trip: the same response continues straight on into the bytes we
# still need.
SPLICE_VERIFY = os.environ.get("SPLICE_VERIFY", "1") not in ("0", "false", "")
SPLICE_VERIFY_BYTES = int(float(os.environ.get("SPLICE_VERIFY_KB", "64")) * 1024)
SPLICE_VERIFY_MAX = int(os.environ.get("SPLICE_VERIFY_MAX", "4"))

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
    headers={"User-Agent": "Stremio"},
)

# token -> (created_ts, entry). entry = {"cands":[...], "id":.., "picker":.., "pin":int|None}
_sessions: dict[str, tuple[float, dict]] = {}
_sess_compact_after = SESS_MAX_BYTES
_start_locks: dict[str, asyncio.Lock] = {}
# In-flight direct (unbuffered) reads, keyed by an id unique to the response.
# A buffered stream is discoverable through _entries; a direct one exists only
# for the life of its generator, so it has to announce itself to be visible on
# Home. Entries are removed in the generator's finally, including on cancel —
# a closed player must not leave a card behind.
_direct: dict[int, dict] = {}
_direct_seq = 0

# Keep only current full-length release identities for reputation. Byte-cache
# reuse uses the separate candidate ``cid`` created by _cand(), never this
# filename-derived value.
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
            # Session JSON may contain only the serializable eligibility fact,
            # never content_identity's process-local sentinel.  Normalize old
            # or malformed values instead of accepting generic truthiness.
            cand["auto"] = cand.get("auto") is True
    _sanitize_auto_pool(entry)
    # Buffer entries are intentionally wiped at startup, so a persisted mapping
    # is stale even when its signature is otherwise valid.
    entry.pop("bufsig", None)
    entry.pop("bufcid", None)
    # These contain process-local monotonic timestamps, task flags, and request
    # accounting. Candidate selection is durable; runtime playback state is not.
    for key in ("_hls", "_hlsseqs", "_hlscodecs", "_playfail"):
        entry.pop(key, None)
    return scrubbed


def _sanitize_auto_pool(entry: dict) -> None:
    """Reassert the semantic-failover boundary on persisted session data.

    New marked sessions may contain only marked candidates.  An unmarked or
    legacy session is deliberately single-candidate.  For a pinned legacy
    session preserve that selected candidate so an in-progress playback can
    resume without reviving its old cross-release failover tail.
    """
    marked = entry.get("auto") is True
    entry["auto"] = marked
    cands = [c for c in (entry.get("cands") or []) if isinstance(c, dict)]
    pool = [c for c in (entry.get("pool") or []) if isinstance(c, dict)]
    if marked:
        entry["cands"] = [c for c in cands if c.get("auto") is True]
        entry["pool"] = [c for c in pool if c.get("auto") is True]
        return

    selected = None
    wanted = entry.get("sel") or ""
    if wanted:
        selected = next((c for c in cands if _candidate_key(c) == wanted), None)
    pin = entry.get("pin")
    if selected is None and isinstance(pin, int) and 0 <= pin < len(cands):
        selected = cands[pin]
    if selected is None and cands:
        selected = cands[0]
    if selected is None and pool:
        selected = pool[0]
    if selected is not None:
        selected["auto"] = False
        entry["cands"] = [selected]
        entry["pool"] = [selected]
        entry["pin"] = 0
    else:
        entry["cands"], entry["pool"] = [], []


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
        # A fresh install still needs cache/HLS maintenance.  The old early
        # return left HLS accounting and the buffer directory uninitialised
        # until a sessions file happened to exist.
        pass
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
        _start_locks.pop(token, None)
        return None
    # Treat TTL/cap order as idle/LRU time so a currently playing token is not
    # evicted merely because many catalogue results were wrapped afterwards.
    _sessions[token] = (time.time(), entry)
    return entry


def _mint(cands: list[dict], pool: list[dict], media: str, media_id: str,
          picker: str, hls: bool = False, viewer: str = "",
          viewer_key: str = "", caps: str | None = None) -> str:
    token = secrets.token_urlsafe(9)
    # Defense in depth: wrap() builds these pools, but _mint is the persistence
    # boundary and must never serialize a marked leader beside an unverified
    # fallback.  Unmarked results remain individually playable with one URL.
    auto = bool(cands and cands[0].get("auto") is True)
    if auto:
        cands = [c for c in cands if c.get("auto") is True]
        pool = [c for c in pool if c.get("auto") is True]
    else:
        cands = cands[:1]
        pool = cands[:]
    # pool = every proxyable candidate for this title (the whole ranked list), so
    # a twin of whatever ends up playing can be found even if it sits outside this
    # token's short failover window. total = the file's byte-length, learned on
    # the opening request and used to prove a twin is byte-identical before we
    # splice to it.
    entry = {"cands": cands, "pool": pool, "id": media_id, "picker": picker,
             "pin": None, "total": None, "auto": auto, "viewer": viewer,
             "viewer_key": viewer_key}
    # The screen that asked for this list. Work this token starts later — the
    # next-episode prefetch — has no request header of its own, and must rank
    # for, and cache under, the same display (see _fire_prefetch).
    if caps is None:
        current = display_caps.current()
        caps = current.header() if current is not None else ""
    if caps:
        entry["caps"] = caps
    if hls:
        entry["hls"] = True
    _sessions[token] = (time.time(), entry)
    _persist(token, entry)
    _prune_sessions()
    return token


def _prune_sessions() -> None:
    """Enforce both age and a hard session bound.

    The previous implementation only removed expired sessions after crossing
    5,000; a busy server with fresh tokens could therefore grow without bound.
    """
    cutoff = time.time() - SESS_TTL
    for t in [t for t, (ts, _) in _sessions.items() if ts < cutoff]:
        _sessions.pop(t, None)
        _start_locks.pop(t, None)
    overflow = len(_sessions) - max(SESS_MAX, 1)
    if overflow > 0:
        oldest = sorted(_sessions, key=lambda t: _sessions[t][0])[:overflow]
        for t in oldest:
            _sessions.pop(t, None)
            _start_locks.pop(t, None)


# ── URL rewriting (called by the picker before it answers) ───────────────────
def _proxyable(s: dict) -> bool:
    if not s.get("url"):
        return False
    # Private-tracker URLs are already opaque local capabilities. Wrapping one
    # as an ordinary upstream would hide the activation boundary inside the
    # failover proxy and could make a probe/prefetch look like a user click.
    if s.get("_private_tracker"):
        return False
    name = s.get("name") or ""
    return not any(e in name for e in ("📚", "⏳", "🎬", "🔒"))


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


def _stream_is_hls(stream: dict) -> bool:
    """Honor probe evidence for extensionless playlist endpoints."""
    kind = stream.get("_content_kind")
    if kind in ("hls", "file"):
        return kind == "hls"
    return _is_hls(stream.get("url") or "")


def _cand(s: dict) -> dict:
    auto = content_identity.auto_eligible(s)
    ident = telemetry.identity(s)
    lbl = f"{ident['res']}p {ident['src']} {ident['grp']}".strip()
    url_key = hashlib.sha256(s["url"].encode()).hexdigest()
    info_hash = str(s.get("infoHash") or "").strip().lower()
    file_idx = s.get("fileIdx")
    # Cache/splice identity is deliberately separate from reputation identity.
    # A hashed filename is useful evidence about a release, but it is not proof
    # that two URLs contain identical bytes.  Torrent hash + file identity is;
    # otherwise only the exact upstream URL may reuse its own cached bytes.
    if (auto and re.fullmatch(r"[0-9a-f]{32,64}", info_hash)
            and isinstance(file_idx, (int, str)) and str(file_idx) != ""):
        content_id = "torrent:" + hashlib.sha256(
            f"{info_hash}|{file_idx}".encode()).hexdigest() + ":auto"
    else:
        # Unmarked candidates are intentionally URL-local even if they expose a
        # torrent hash: their single-candidate token may not silently reuse a
        # buffer or twin opened through another, semantically unverified URL.
        content_id = "url:" + url_key + (":auto" if auto else "")
    return {"u": s["url"], "key": url_key, "cid": content_id,
            "need": (s.get("_qbps") or 0) / 8,   # bytes/s the file needs
            "rt": float(s.get("_runtime") or 0),  # title runtime, seconds
            "src": ident["src"], "dbr": ident["debrid"], "grp": ident["grp"],
            "res": ident["res"], "size": ident["size"],
            "codec": ident["codec"], "hdr": ident["hdr"],
            "sig": telemetry.signature(s), "lbl": lbl,
            "rh": hlsproxy.request_headers(s),   # upstream request headers
            # The file's own name, for a player deciding whether a standby is
            # the very file that just failed (see standby()).
            "fn": str((s.get("behaviorHints") or {}).get("filename") or "")[:200],
            "nzb_indexers": list(s.get("_nzb_indexers") or []),
            "auto": bool(auto)}


def wrap(streams: list[dict], media: str, media_id: str, picker: str,
         viewer: str = "", viewer_key: str = "") -> list[dict]:
    """Return a copy of the list with online stream URLs replaced by proxy URLs
    whose token carries the identity-eligible failover tail from each position.
    Unmarked entries receive an isolated one-candidate token. Library and notice
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
    tail = [s for s in streams if _proxyable(s) and not _stream_is_hls(s)]
    marked_tail = [s for s in tail if content_identity.auto_eligible(s)]
    marked_pool = [_cand(x) for x in marked_tail]
    hls_tail = [s for s in streams if _proxyable(s) and _stream_is_hls(s)]
    marked_hls_tail = [s for s in hls_tail
                       if content_identity.auto_eligible(s)]
    marked_hls_pool = [_cand(x) for x in marked_hls_tail]
    made = 0
    out = []
    for s in streams:
        if _proxyable(s) and _stream_is_hls(s):
            if hlsproxy.ENABLED:
                # Playlist-rewriting proxy (app.hlsproxy): the player fetches
                # everything from us, we fetch upstream with the declared
                # headers from one IP — fixes referer-gated/IP-locked hosts
                # and makes credentialed playlists servable. Failover
                # candidates are the other HLS releases for this title.
                if content_identity.auto_eligible(s):
                    pos = marked_hls_tail.index(s)
                    cands = marked_hls_pool[pos:pos + MAXFAIL]
                    pool = marked_hls_pool
                else:
                    cands = pool = [_cand(s)]
                token = _mint(cands, pool, media, media_id, picker, hls=True,
                              viewer=viewer, viewer_key=viewer_key)
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
            if content_identity.auto_eligible(s):
                pos = marked_tail.index(s)
                cands = marked_pool[pos:pos + MAXFAIL]
                pool = marked_pool
            else:
                cands = pool = [_cand(s)]
            token = _mint(cands, pool, media, media_id, picker, viewer=viewer,
                          viewer_key=viewer_key)
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


def _candidate_key(c: dict) -> str:
    return c.get("key") or hashlib.sha256((c.get("u") or "").encode()).hexdigest()


def _cache_id(c: dict) -> str:
    """Exact byte identity for cache/reconnect use.

    The sig fallback exists only for sparse legacy/unit-test candidates. New
    wrapped candidates always have a URL- or torrent-bound cid.
    """
    return c.get("cid") or c.get("sig") or ""


def _pin_selection(token: str, entry: dict, cands: list[dict], cand: dict) -> None:
    """Persist the exact release selected by start evaluation.

    A numeric rank alone is insufficient once the in-memory buffer disappears;
    ``sel`` lets restart/seek resolve the same candidate and fail closed if the
    saved pool no longer contains it.
    """
    try:
        idx = next(i for i, c in enumerate(cands)
                   if c is cand or _candidate_key(c) == _candidate_key(cand))
    except StopIteration:
        return
    entry["pin"] = idx
    entry["sel"] = _candidate_key(cand)
    entry["selected_cid"] = _cache_id(cand)
    _persist(token, entry)


def _selected_candidate(entry: dict, cands: list[dict]) -> dict | None:
    selected = entry.get("sel") or ""
    if selected:
        return next((c for c in cands if _candidate_key(c) == selected), None)
    # Legacy pass-through sessions persisted a valid pin. Buffered sessions did
    # not, so an absent pin must not silently become candidate zero on a seek.
    pin = entry.get("pin")
    if isinstance(pin, int) and 0 <= pin < len(cands):
        return cands[pin]
    return None


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
        if resp.status_code == 200:
            return True
        parsed = _content_range(resp) if resp.status_code == 206 else None
        return bool(parsed is not None and parsed[0] == 0)
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


async def _send(cand: dict | str, range_header: str | None):
    """Open a streamed upstream GET with the candidate's safe declared headers.

    Probing already honored behaviorHints.proxyHeaders, but playback formerly
    dropped them, making a Referer/Cookie-gated URL pass verification and fail
    for the player. All proxy paths go through this helper.
    """
    if isinstance(cand, dict):
        url = cand["u"]
        headers = dict(cand.get("rh") or {})
    else:                         # compatibility for direct internal callers
        url, headers = cand, {}
    if range_header:
        headers["Range"] = range_header
    headers = cfsolver.merge_headers(url, headers)
    req = _client.build_request("GET", url, headers=headers)
    resp = await _client.send(req, stream=True, follow_redirects=True)
    # A Cloudflare block page must never reach the player: callers already fail
    # over on a non-2xx, and this schedules a background solve so the retry (and
    # every later fetch of this host) carries clearance and streams real bytes.
    # A side observation only — never let it interfere with returning the stream.
    if cfsolver.looks_challenged(getattr(resp, "status_code", 0),
                                 getattr(resp, "headers", {})):
        cfsolver.note_challenge(url)
    return resp


class _SourceStalled(Exception):
    """An upstream that went silent past STREAM_STALL_SECS mid-stream."""


def _stall_limit(cand: dict | None) -> float | None:
    """Seconds of upstream silence after which a stream reconnects, or None to
    wait out the HTTP client's own read timeout. Usenet (nzbdav) sources wait:
    their article fetches legitimately pause, and reconnecting mid-file there
    re-runs an interpolation search of NNTP header fetches — the cure would
    cost more than the pause."""
    if STREAM_STALL_SECS <= 0 or ((cand or {}).get("sig") or "").startswith("nzb:"):
        return None
    return STREAM_STALL_SECS


async def _next_chunk(it, cand: dict | None) -> bytes:
    """`it.__anext__()`, raising _SourceStalled when the source says nothing
    for _stall_limit seconds. Only ever awaited when the reader wants bytes,
    so a paused player can never be mistaken for a stalled source."""
    limit = _stall_limit(cand)
    if limit is None:
        return await it.__anext__()
    try:
        return await asyncio.wait_for(it.__anext__(), limit)
    except asyncio.TimeoutError:
        raise _SourceStalled(f"no bytes for {limit:.0f}s") from None


class _WaitLog:
    """Times a stream spends waiting on its source while the player wants
    bytes. Long waits become `starve` buffer events (capped per stream); the
    totals ride on the play record."""

    def __init__(self, *, picker: str, media_id: str, mode: str):
        self.picker, self.media_id, self.mode = picker, media_id, mode
        self.count, self.total, self.worst = 0, 0.0, 0.0

    def note(self, waited: float, *, cand: dict | None, offset: int,
             total: int | None = None, avail: int | None = None,
             node: str = "") -> None:
        if waited < _STARVE_LOG_SECS:
            return
        self.count += 1
        self.total += waited
        self.worst = max(self.worst, waited)
        if self.count > _STARVE_EVENTS_MAX:
            return
        c = cand or {}
        logger.info(f"proxy: player waited {waited:.1f}s on {c.get('lbl', '')!r} "
                    f"at byte {offset} ({self.mode})")
        telemetry.record_buffer(
            "starve", sig=c.get("sig", ""), picker=self.picker,
            media_id=self.media_id, source=c.get("lbl", ""),
            dbr=c.get("dbr", ""), node=node, offset=offset, avail=avail,
            total=total, reason=f"{self.mode} waited {waited:.1f}s")

    def summary(self) -> dict:
        return {"waits": self.count, "wait_secs": round(self.total, 1),
                "max_wait": round(self.worst, 1)}


def _fwd_headers(resp) -> dict:
    out = {"Accept-Ranges": "bytes"}
    for k, name in (("content-type", "Content-Type"),
                    ("content-length", "Content-Length"),
                    ("content-range", "Content-Range")):
        v = resp.headers.get(k)
        if v:
            out[name] = v
    return out


def _response_span(resp, expected_total: int | None = None) -> tuple[int, int | None]:
    """Absolute response start and promised body length, when knowable."""
    cr = _content_range(resp)
    if cr is not None:
        start, end, _ = cr
        return start, end - start + 1
    length = resp.headers.get("content-length") or ""
    if length.isdigit():
        return 0, int(length)
    return 0, expected_total


async def _head(entry: dict) -> Response:
    """Describe the file a GET on this token would actually serve.

    A HEAD that answers from a different candidate than the one playback will
    select hands the player a length for one release and then a body from
    another. Players cache that length and can seek to the wrong place."""
    # A live buffer entry has already chosen, and knows the real length.
    cache_id = entry.get("bufcid") or entry.get("bufsig")
    e = _entries.get(cache_id) if cache_id else None
    if e is not None and not e.failed and e.total is not None:
        return Response(status_code=200, headers={
            "Accept-Ranges": "bytes",
            "Content-Type": e.content_type or "application/octet-stream",
            "Content-Length": str(e.total)})
    # Otherwise probe — but through the same filter _select_start uses, so the
    # candidate we describe is the candidate it would pick.
    available = [c for c in entry["cands"]
                 if not reputation.blocked(c.get("sig") or "")
                 and not _candidate_rejected(c)]
    if available and not all(reputation.cooled(c.get("sig") or "")
                             for c in available):
        available = [c for c in available
                     if not reputation.cooled(c.get("sig") or "")]
    for c in available:
        try:
            # Headers only, so bounded like a first byte: a HEAD that waited
            # out the 60 s read timeout on a silent candidate would stall the
            # player's open just as surely as a GET.
            resp = await asyncio.wait_for(_send(c, "bytes=0-0"), START_TTFB)
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
                        offset: int, end: int | None, token: str,
                        judge: bool = True):
    """Try candidates in order; return the first that connects, starts promptly,
    and (for a full/large opening range) streams fast enough. Returns
    (idx, cand, resp, iterator, prebuffer, ttfb, net) or None if all failed.
    Each rejection is recorded against the release for *this* session — a release
    only gets blocked once several separate sessions go bad (see app.reputation).

    `judge=False` (a warm) applies exactly the same tests but records no
    verdict either way: no strike, no cooldown, no usenet health. Nobody was
    waiting, so a candidate that failed here has not failed anyone; the
    viewer's own open judges it again if it comes to that."""
    eval_full = end is None or (end - offset) >= EVAL_BYTES
    # Skip releases in short-term cooldown (one just delivered badly) so 'hit
    # play again' lands on the next source — unless they're ALL cooled, in which
    # case serve the best of them rather than nothing.
    cands = [c for c in cands
             if not reputation.blocked(c.get("sig") or "")
             and not _candidate_rejected(c)]
    if not cands:
        return None
    skip_cooled = not all(reputation.cooled(c.get("sig") or "") for c in cands)

    def _bad(c, reason, node="", extreme=False, detail=""):
        if not judge:
            telemetry.record_buffer(
                "start_error", sig=c.get("sig", ""),
                source=c.get("lbl", ""), dbr=c.get("dbr", ""), node=node,
                reason="warm:" + (detail or reason))
            return
        reputation.observe(c["sig"], token, reason, c["lbl"], node=node, extreme=extreme)
        reputation.cooldown(c["sig"])
        telemetry.record_buffer(
            "start_error", sig=c.get("sig", ""),
            source=c.get("lbl", ""), dbr=c.get("dbr", ""), node=node,
            reason=detail or reason)
        if c["sig"].startswith("nzb:"):
            usenet_health.record_failure(
                c["sig"], c["lbl"], c.get("nzb_indexers") or [], reason,
                f"play:{token}:{c['sig']}")

    def _good(c):
        if judge and c["sig"].startswith("nzb:"):
            usenet_health.record_success(
                c["sig"], c["lbl"], c.get("nzb_indexers") or [],
                f"play-ok:{token}:{c['sig']}")

    async def _attempt(idx, c):
        """Connect, read the opening bytes, and validate the source is prompt,
        fast enough, and actually media. Returns (idx, c, resp, it, prebuf, ttfb,
        net, container) with resp left OPEN on success; else None (resp closed,
        failure recorded). Cancelled — it lost a race, or the budget ran out —
        it closes its response and records nothing: that is not the source's
        fault."""
        opened: list = []
        try:
            return await _attempt_once(idx, c, opened)
        except asyncio.CancelledError:
            for r in opened:
                try:
                    await r.aclose()
                except BaseException:
                    pass
            raise

    async def _attempt_once(idx, c, opened):
        t0 = time.monotonic()
        try:
            resp = await _send(c, range_header)
            opened.append(resp)
        except Exception as e:
            logger.info(f"proxy start: cand {idx} connect fail ({type(e).__name__})")
            _bad(c, "connect-fail", detail=f"connect-fail:{type(e).__name__}")
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
        stalled = False
        try:
            while True:
                try:
                    if t_first is None:
                        # Waiting past the TTFB budget for a *first* byte is
                        # pointless — the candidate is rejected at START_TTFB
                        # regardless. Without this cap a dead source blocks
                        # here until the client's 60s read timeout (observed
                        # live: 17.6s of "Starting stream" before a 502).
                        chunk = await asyncio.wait_for(
                            it.__anext__(), START_TTFB + 0.5)
                    else:
                        # The same cap after byte one. EVAL_DEADLINE was only
                        # checked when a chunk *arrived*, so a source that sent
                        # its opening chunk and then went silent held the gate
                        # for the client's whole 60 s read timeout — and, with
                        # under 512 KB in hand, skipped the rate check and was
                        # served. Silence for the rest of the window is the
                        # verdict; it is judged below.
                        chunk = await asyncio.wait_for(
                            it.__anext__(),
                            max(t0 + EVAL_DEADLINE - time.monotonic(), 0.05))
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    if t_first is not None:
                        stalled = True
                        break
                    logger.info(f"proxy start: cand {idx} dead "
                                f"(no first byte in {START_TTFB:.0f}s)")
                    await resp.aclose()
                    _bad(c, "dead", node=node, extreme=True,
                         detail=f"dead:no-first-byte-{START_TTFB:.0f}s")
                    return None
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
            _bad(c, "read-fail", node=node,
                 detail=f"read-fail:{type(e).__name__}")
            return None
        if got == 0:
            # Connected, replied, delivered nothing. That is a corpse, not a
            # slow link — labelling it "slow" let it burn a second session's
            # patience before reputation caught up.
            logger.info(f"proxy start: cand {idx} dead (connected, zero bytes)")
            await resp.aclose()
            _bad(c, "dead", node=node, extreme=True, detail="dead:zero-bytes")
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
        if not c.get("need") and c.get("rt"):
            # No addon declared this file's size, so nothing knew its bitrate
            # and the rate check below used to be skipped outright — which is
            # how a 1.1 MB/s copy of a 7 GB REMUX got through. The
            # response's own Content-Range says how big it is.
            total = _total_of(resp)
            if total:
                c["need"] = total / c["rt"]
        if stalled:
            # It started, then said nothing for the rest of the evaluation
            # window. Measured over that whole window it is either still fast
            # enough for its file (a burst with a short gap at the end) or it
            # is a stall the player would sit through — judged against the
            # file's own need when known, else a 1 MB/s floor. Reason "dead"
            # keeps a transport outage visible to the usenet watchdog.
            floor = (c["need"] * RATE_MARGIN if c.get("need")
                     else _STALLED_START_FLOOR)
            if speed < floor:
                logger.info(f"proxy start: cand {idx} stalled after "
                            f"{got} bytes ({speed / 1e6:.2f} MB/s over the "
                            f"window), trying next")
                await resp.aclose()
                _bad(c, "dead", node=node, extreme=True,
                     detail=f"dead:stalled-after-{got}-bytes")
                return None
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

    deadline = (time.monotonic() + START_BUDGET) if START_BUDGET > 0 else None

    def left() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    async def _bounded(idx, c):
        budget = left()
        if budget is not None and budget <= 0:
            raise _StartBudgetSpent
        if budget is None:
            return await _attempt(idx, c)
        try:
            return await asyncio.wait_for(_attempt(idx, c), budget)
        except asyncio.TimeoutError:
            raise _StartBudgetSpent from None

    # Pass 1: prefer a file container (MKV/MP4/…). Transport-stream sources that
    # otherwise pass are held back — real video, but our players can't demux them.
    deferred: list[tuple[int, dict]] = []
    order: list[tuple[int, dict]] = []
    for idx, c in enumerate(cands):
        if skip_cooled and reputation.cooled(c.get("sig") or ""):
            logger.info(f"proxy start: cand {idx} in cooldown, skipping")
            continue
        order.append((idx, c))

    async def _race():
        """Judge `order` best-ranked first, starting a staggerable backup beside
        an undecided leader. Returns the best-ranked pass once everything above
        it has failed, or — when the budget runs out — the best pass in hand."""
        running: dict[int, asyncio.Task] = {}
        outcome: dict[int, tuple | None] = {}
        nxt, last_start, winner = 0, 0.0, None
        try:
            while True:
                for pos in range(len(order)):
                    if pos not in outcome:
                        break
                    if outcome[pos] is not None:
                        winner = pos
                        return outcome[pos]
                else:
                    return None                       # every candidate failed
                now = time.monotonic()
                can_add = (nxt < len(order) and START_STAGGER > 0
                           and len(running) < _STAGGER_MAX
                           and _staggerable(order[nxt][1]))
                if nxt < len(order) and (not running or (
                        can_add and now - last_start >= START_STAGGER)):
                    if running:
                        logger.info(f"proxy start: cand {order[nxt][0]} "
                                    f"started beside an undecided leader")
                    running[nxt] = asyncio.create_task(_attempt(*order[nxt]))
                    nxt, last_start = nxt + 1, now
                    continue
                if not running:
                    return None
                timeout = left()
                if can_add:
                    stagger_at = max(last_start + START_STAGGER - now, 0.0)
                    timeout = (stagger_at if timeout is None
                               else min(timeout, stagger_at))
                if timeout is not None and timeout <= 0 and left() is not None \
                        and left() <= 0:
                    raise _StartBudgetSpent
                done, _ = await asyncio.wait(
                    set(running.values()), timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED)
                for pos, task in list(running.items()):
                    if task not in done:
                        continue
                    running.pop(pos)
                    try:
                        res = task.result()
                    except Exception:
                        logger.exception("proxy start: attempt crashed")
                        res = None
                    if (res is not None and PREFER_FILE_CONTAINERS
                            and res[-1] in _DEFER_CONTAINERS):
                        await res[2].aclose()
                        deferred.append(order[pos])
                        logger.info(f"proxy start: cand {res[0]} is {res[-1]} "
                                    f"(transport stream), deferring in favor of "
                                    f"a file container")
                        res = None
                    outcome[pos] = res
                budget = left()
                if budget is not None and budget <= 0:
                    held = [pos for pos in sorted(outcome)
                            if outcome[pos] is not None]
                    if held:
                        winner = held[0]
                        return outcome[winner]
                    raise _StartBudgetSpent
        finally:
            for task in running.values():
                task.cancel()
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)
            for pos, res in outcome.items():
                if res is not None and pos != winner:
                    try:
                        await res[2].aclose()
                    except Exception:
                        pass

    try:
        res = await _race()
        if res is not None:
            if res[0]:
                logger.info(f"proxy: failed over to candidate {res[0]} "
                            f"({res[5]:.1f}s ttfb)")
            _good(res[1])
            return res[:-1]
        # Pass 2: no file container worked — serve the best transport stream
        # we held.
        deferred.sort(key=lambda ic: ic[0])
        for idx, c in deferred:
            res = await _bounded(idx, c)
            if res is not None:
                logger.info(f"proxy start: no file-container source; serving "
                            f"transport-stream cand {idx} ({res[-1]}) as last "
                            f"resort")
                _good(c)
                return res[:-1]
        # Pass 3: one short second chance for NZB sources. nzbdav mount
        # readiness flaps while an import settles — observed live: probe OK at
        # 5 MB/s, dead 11s later, playing fine on the player's own third retry.
        # Two seconds of patience here turns that whole 502 loop into one
        # slightly slower start.
        retry = [(idx, c) for idx, c in enumerate(cands)
                 if (c.get("sig") or "").startswith("nzb:")]
        if retry and NZB_START_RETRY_SECS > 0:
            budget = left()
            if budget is not None and budget <= NZB_START_RETRY_SECS:
                raise _StartBudgetSpent
            await asyncio.sleep(NZB_START_RETRY_SECS)
            for idx, c in retry:
                res = await _bounded(idx, c)
                if res is not None:
                    logger.info(f"proxy start: nzb cand {idx} recovered on "
                                f"retry after {NZB_START_RETRY_SECS:.0f}s")
                    _good(c)
                    return res[:-1]
    except _StartBudgetSpent:
        logger.info(f"proxy start: no candidate started within the "
                    f"{START_BUDGET:.0f}s budget")
        telemetry.record_buffer("start_error", reason="start-budget-spent")
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
    """Byte-identical twins of `cand`: same strong content identity, different debrid
    (so a different node). Drawn from the whole ranked pool, not just this
    token's failover window."""
    cid = _cache_id(cand)
    if not cid or not cid.startswith("torrent:"):
        # Exact-URL cache identities are safe for reconnecting that URL, but do
        # not prove a different delivery URL contains identical bytes.
        return []
    pool = entry.get("pool") or entry.get("cands") or []
    auto = entry.get("auto") is True
    return [c for c in pool
            if _cache_id(c) == cid and c.get("dbr") != cand.get("dbr")
            and c.get("u") != cand.get("u")
            and (c.get("auto") is True) == auto]


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
            resp = await _send(tw, rh)
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
    try:
        from app import picker      # lazy: picker does not import proxy back
        picker.invalidate(media_id)
    except Exception:
        pass


# Players that reject a file give up in one of two shapes, and confirming only
# the first one left the other in an endless spinner.
#
# *Quiet.* Most go silent, then re-request the same URL on their own every ~30s.
# After PLAYER_REJECT_STARTS false starts, silence IS the confirmation: no new
# connection and no active reader for _REJECT_SILENCE_SECS means the player has
# given up — reject then, so its very next self-retry is served the next
# candidate. Recovery without the viewer touching anything.
#
# *Loud.* Some retry immediately instead, reopening at byte 0 roughly once a
# second forever. Live case: an Easynews fansub remux whose filename said AAC
# and whose audio was FLAC. The player pulled 30-60 MB, dropped, and reopened
# ~0.8s later, for 67 seconds — so `e.last` was never stale, silence never
# arrived, and the release was never rejected, never cooled, never learned from.
# The viewer had to pick another source by hand. A retry loop is exactly as
# strong a verdict as silence, so it also confirms: false starts that keep
# accumulating *through the whole window* mean the player is reopening the file,
# not watching it.
#
# The false-positive guard is what separates the two from a working stream. A
# healthy player either latches `real_play` (a 20s connection or 256 MB) or
# stops opening at byte 0 and reads on at advancing offsets, which scores no
# strikes at all — so neither branch can fire on one. A startup burst scores a
# couple of strikes in its first second or two and then plays; it cannot keep
# scoring them for a full window.
_REJECT_SILENCE_SECS = 15.0


def _arm_reject_timer(e: "_Entry", token: str) -> None:
    state = e.playfail
    if (not PLAYER_REJECT_STARTS or state.get("rejected")
            or state.get("real_play")
            or state.get("false_starts", 0) < PLAYER_REJECT_STARTS
            or state.get("timer_armed")):
        return
    state["timer_armed"] = True
    armed_at = state.get("false_starts", 0)

    async def _fire():
        try:
            await asyncio.sleep(_REJECT_SILENCE_SECS)
            if state.get("rejected") or state.get("real_play"):
                return
            # Strikes only ever come from sub-5s opens at byte 0, so any that
            # landed while we slept are the player reopening the file rather
            # than reading on through it.
            churn = state.get("false_starts", 0) - armed_at
            quiet = (e.consumers == 0
                     and time.time() - (e.last or 0.0) >= _REJECT_SILENCE_SECS)
            if not quiet and churn < PLAYER_REJECT_STARTS:
                # Reattached or resolved meanwhile. `consumers` alone was too
                # strict twice over: a range past the write head is served
                # directly with no consumer ref, and a player streaming from
                # its own buffer holds no connection between cache reads —
                # observed live as a release cooled *while the viewer was
                # watching it*. Any request that touched this entry inside the
                # silence window (e.last) means the player is still here.
                return
            starts = state.get("false_starts", 0)
            _mark_rejected(state, sig=_release_sig(e),
                           label=(e.source or {}).get("lbl", ""), node=e.node,
                           token=token, picker=e.picker, media_id=e.media_id,
                           detail=(f"{starts} false starts then "
                                   f"{_REJECT_SILENCE_SECS:.0f}s silence"
                                   if quiet else
                                   f"{starts} false starts, {churn} of them in "
                                   f"the last {_REJECT_SILENCE_SECS:.0f}s — the "
                                   f"player is reopening, not watching"))
            if e.avail >= DECODE_LEARN_MIN_BYTES or e.complete:
                _spawn_learn(e, played=False)
            else:
                # The producer never delivered enough for the player to make a
                # codec decision — this is starvation wearing a rejection's
                # clothes. Cooling the release above is still right; blaming
                # its codecs is not (that is how AAC and H.264 earned ~17%
                # "failure" rates here).
                logger.info(
                    f"decode-health: not striking codecs for "
                    f"{(e.source or {}).get('lbl', '')!r} — only "
                    f"{e.avail // 1024 // 1024} MB was ever available "
                    f"(starved, not undecodable)")
        finally:
            state["timer_armed"] = False

    t = asyncio.create_task(_fire())
    _bg_tasks.add(t)
    t.add_done_callback(_reap_task)


def _reap_task(t) -> None:
    _bg_tasks.discard(t)
    if not t.cancelled() and t.exception() is not None:
        logger.warning(f"proxy: background task failed: {t.exception()!r}")


def _spawn_learn(e: "_Entry", played: bool) -> None:
    """Learn the *class* of file from this entry, off the serving path."""
    if not vprobe.enabled():
        return
    t = asyncio.create_task(_learn_codecs(e, played))
    _bg_tasks.add(t)
    t.add_done_callback(_reap_task)


async def _learn_codecs(e: "_Entry", played: bool) -> None:
    """ffprobe the entry's cached bytes and feed decode_health: a rejection
    strikes the file's codec attributes, real playback credits them. This is
    what turns one FLAC spinner into 'FLAC stops ranking first' instead of
    re-learning the same lesson release by release."""
    try:
        ac, vc, _secs = await vprobe.codecs_of(e.path)
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
    telemetry.record_buffer("recovery_ok", sig=_release_sig(e), picker=e.picker,
                            media_id=e.media_id,
                            source=(e.source or {}).get("lbl", ""), node=e.node,
                            reason="played after player-rejected swap")


async def _body(entry, idx, cand, resp, it, prebuf, offset, end, ttfb, token, net):
    t0 = time.monotonic()
    served = sum(len(c) for c in prebuf)
    cur = offset + served
    reconnects, splices, reason = 0, 0, "eof"
    # Reconnects since the source last made real progress, and the byte at
    # which the previous drop happened.
    streak, streak_mark = 0, cur
    tried = {cand["u"]}                      # source URLs already used this session
    # Time spent *waiting on the source* vs bytes it delivered in that time. This
    # isolates a slow SOURCE from a satisfied client throttling its reads: when
    # the client's buffer is full it stops pulling, so we stop awaiting upstream
    # and this rate stays clean. up_rate < needed bitrate over a real sample = the
    # mid-stream buffering the start-eval can't catch.
    up_bytes, up_time = 0, 0.0
    win: deque = deque()                     # (up_time, up_bytes) for buffering detect
    waitlog = _WaitLog(picker=entry.get("picker", ""), media_id=entry.get("id", ""),
                       mode="direct")

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
                                       if up_time > 0.5 else None), slow=True,
                              waits=waitlog.summary())
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
                        chunk = await _next_chunk(it, cand)
                    except StopAsyncIteration:
                        chunk = None
                    finally:
                        waitlog.note(time.monotonic() - r0, cand=cand,
                                     offset=cur, total=entry.get("total"),
                                     node=net.get("node", ""))
                    if chunk is None:
                        expected_stop = entry.get("total")
                        if end is not None:
                            expected_stop = min(end + 1, expected_stop) \
                                if expected_stop is not None else end + 1
                        if expected_stop is not None and cur < expected_stop:
                            # A clean iterator end is still a truncated transfer
                            # when Content-Length/Content-Range promised more.
                            # Route it through the normal reconnect/twin logic.
                            raise httpx.RemoteProtocolError(
                                f"premature EOF at {cur}, expected {expected_stop}")
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
                #    that's still healthy (transient blip). Attempts are spaced
                #    out, and the streak resets once the source has delivered
                #    _RECONNECT_RESET_BYTES since the last one: RECONNECT_MAX
                #    bounds consecutive failures, never blips per film.
                if cur - streak_mark >= _RECONNECT_RESET_BYTES:
                    streak = 0
                streak_mark = cur
                try:
                    await resp.aclose()
                except Exception:
                    pass
                reopened = False
                while streak < RECONNECT_MAX and not reopened:
                    streak += 1
                    reconnects += 1
                    logger.info(f"proxy: mid-stream drop ({type(e).__name__}), "
                                f"reconnect {streak} to same source at {cur}")
                    await asyncio.sleep(_reconnect_delay(streak))
                    rh = f"bytes={cur}-" + (str(end) if end is not None else "")
                    try:
                        r2 = await _send(cand, rh)
                    except Exception:
                        continue
                    if _range_response_ok(r2, rh,
                                          expected_total=entry.get("total")):
                        resp, it = r2, r2.aiter_raw()
                        reopened = True
                    else:
                        try:
                            await r2.aclose()
                        except Exception:
                            pass
                if reopened:
                    continue
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
                              net=net, session=token, up_mbps=up_mbps, slow=slow,
                              waits=waitlog.summary())
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
# strong content identity (proven twins and exact-URL re-watches share one),
# retained up to
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
# The cache shares its volume with everything else on the host, and a full disk
# fails in the worst way: every write raises, every producer dies, every
# buffered stream truncates mid-scene. Keep this much free on BUFFER_DIR's
# volume whatever BUFFER_CACHE_GB says — evicting idle entries first, then
# letting new streams pass through unbuffered — so the 100 GB default on a
# 64 GB disk bounds itself instead of breaking playback.
BUFFER_MIN_FREE_BYTES = int(float(os.environ.get("BUFFER_MIN_FREE_GB", "5"))
                            * 1024 ** 3)
_SPACE_CHECK_BYTES = 64 * 1024 * 1024      # how often a producer re-checks
# How far past a running producer's write head a request may land and still be
# served from the cache (waiting on the fill) rather than passed through on a
# second upstream connection. A few seconds of fill at any real source's rate.
_NEAR_HEAD_BYTES = 4 * 1024 * 1024
# Proactive producer-side switch: if the feeding source sustains below need*margin
# while a reader is waiting at the write head, jump to a byte-identical twin node.
BUFFER_SLOW_WINDOW = float(os.environ.get("BUFFER_SLOW_WINDOW", "8"))
BUFFER_SLOW_MARGIN = float(os.environ.get("BUFFER_SLOW_MARGIN", "0.9"))
# How long after the last reader touch the producer keeps filling with no reader
# attached. Players that fetch in discrete ranges — request a chunk, disconnect,
# come back a minute later — drop `consumers` to zero between requests, and a
# request landing past the write head is served directly and takes no consumer
# ref at all. Pausing strictly on `consumers > 0` therefore parked the producer
# permanently: the buffer froze and every later range was fetched cold from
# upstream, which is what a viewer experiences as endless buffering. Keeping the
# fill alive across the gap costs at most this much read-ahead on a stream that
# really was abandoned, and the reaper still evicts it afterwards.
BUFFER_IDLE_GRACE = float(os.environ.get("BUFFER_IDLE_GRACE", "180"))
# The same grace for a source that rations concurrent connections rather than
# bandwidth (see _reader_interested). Hosts are matched by name because that is
# the one thing a candidate always carries honestly.
BUFFER_IDLE_GRACE_RATIONED = float(
    os.environ.get("BUFFER_IDLE_GRACE_RATIONED", "10"))
_RATIONED_HOSTS = ("easynews.com",)


def _rationed_source(source) -> bool:
    """Whether this candidate's host limits us to a couple of transfers."""
    url = (source or {}).get("u", "") if isinstance(source, dict) else (source or "")
    host = (urlsplit(str(url)).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _RATIONED_HOSTS)
# The tail of the file, fetched eagerly when a producer starts. Players read
# the container index (MKV cues / MP4 moov-at-end) from EOF the moment they
# open a stream — observed live: a reader at offset 368,449,152 of a
# 368,477,225-byte file while the sequential cache held only the front. Without
# this, every open and every seek pays a cold upstream fetch for those bytes.
BUFFER_TAIL_BYTES = int(float(os.environ.get("BUFFER_TAIL_MB", "8")) * 1024 * 1024)
# How long the producer holds off its bulk fill to let that tail warm land
# first. The index is on the critical path to the first frame and the fill is
# not, so the fill can wait; letting them race cost 50s of black screen on
# Easynews (see _produce). Bounded, so an expensive tail degrades to today's
# background behaviour instead of stalling the start. 0 restores the race.
TAIL_WARM_HEADSTART = float(os.environ.get("BUFFER_TAIL_HEADSTART", "3"))
# …and how long on a source that rations connections. Much longer, because
# there the fill and the warm are contending for the *same* scarce connection,
# so making the fill wait is not a cost — it is what lets the warm through.
# Three seconds was not enough: a second stream opened while an earlier fill
# was still running (the idle grace, or a next-episode prefetch) saw its warm
# take 39.6 s again. Read-ahead is the only thing delayed; the opening bytes
# are already in hand from the start gate's prebuf.
TAIL_WARM_HEADSTART_RATIONED = float(
    os.environ.get("BUFFER_TAIL_HEADSTART_RATIONED", "30"))
# One quick second chance for an NZB source that failed to start: nzbdav mount
# readiness flaps while an import settles (observed: probe OK at 5 MB/s, dead
# 11s later, playing fine 26s after that). Public debrid links don't flap this
# way, so the retry is nzb-scoped. 0 disables.
NZB_START_RETRY_SECS = float(os.environ.get("NZB_START_RETRY_SECS", "2.0"))
# A rejection may only strike the file's codecs when the player provably had
# enough of the file to make a codec decision. Below this, the verdict is
# "starved", and blaming codecs would poison decode-health — which is exactly
# how AAC and H.264 ended up with ~17% "failure" rates in the live store.
DECODE_LEARN_MIN_BYTES = int(float(os.environ.get(
    "DECODE_LEARN_MIN_MB", "8")) * 1024 * 1024)
# Next-episode prefetch: the moment a series episode starts playing, run the
# search+pick for episode E+1 and cache the *result list* — no stream bytes are
# downloaded — so opening the next episode is an instant cache hit.
PREFETCH_NEXT = os.environ.get("PREFETCH_NEXT", "1") not in ("0", "false", "")
# Details-page warm (POST /proxy/<token>/warm, see warm()): a player that knows
# what is about to be played asks for the expensive half of a cold start — the
# start gate's choice and the container index — while the viewer is still
# reading the synopsis. 0 answers 204 and does nothing.
WARM = os.environ.get("PROXY_WARM", "1") not in ("0", "false", "")
# How far a warm may read ahead before anyone presses Play. The index
# (BUFFER_TAIL_MB) is what gates the first frame; this is the opening seconds
# of video on top of it. The idle grace bounds a warm in time as well, so on a
# connection-rationed host it is ~10 s, whichever comes first.
WARM_AHEAD_BYTES = int(float(os.environ.get("BUFFER_WARM_MB", "64")) * 1024 * 1024)
_WARM_MIN_INTERVAL = 30.0     # per token: a repeat inside this starts nothing
_WARM_MAX_INFLIGHT = 2        # warms choosing a source at once, all tokens


class _Entry:
    __slots__ = ("sig", "path", "total", "avail", "complete", "failed", "source",
                 "cands", "pool", "producer", "consumers", "head", "last", "cond",
                 "content_type", "picker", "media_id", "node", "playfail",
                 "tail", "tail_off", "reviving", "grew_at", "warm")

    def __init__(self, sig, path, cands, source, content_type, total, picker,
                 media_id, pool=None):
        self.sig = sig                    # exact cache id (not reputation signature)
        self.playfail: dict = {}      # player-rejected detector state (_note_consumer_close)
        self.path = path
        self.total = total            # full byte length (Content-Range), or None
        self.avail = 0                # contiguous bytes present from 0 (write head)
        self.complete = False
        self.failed = False
        self.source = source          # cand dict currently feeding the file
        self.cands = cands            # all byte-identical sources for this release
        # Every ranked candidate for this title. Not byte-identical by
        # construction — a member may only feed this file after proving itself
        # against bytes already on disk (see _open_verified).
        self.pool = list(pool or cands)
        self.producer = None
        self.tail = None                  # last BUFFER_TAIL_BYTES of the file
        self.tail_off = 0                 # absolute offset of tail[0]
        self.consumers = 0
        self.head = 0                 # furthest reader position (backpressure anchor)
        self.last = time.time()
        self.cond = asyncio.Condition()
        self.content_type = content_type or "application/octet-stream"
        self.picker = picker
        self.media_id = media_id
        self.node = ""                # delivery node of the current feeding source
        self.reviving = False         # a _revive attempt is in flight
        self.grew_at = time.monotonic()   # when avail last advanced
        # Started by a warm and not yet read by any player: no verdicts, a
        # bounded read-ahead, and not "playing". Cleared by the first real
        # request (_serve_buffered). See warm().
        self.warm = False


def _release_sig(e: _Entry) -> str:
    return (e.source or {}).get("sig") or ""


_entries: dict[str, _Entry] = {}
_entries_start_lock = asyncio.Lock()
_reaper_task = None            # module ref so the reaper isn't GC'd mid-run
_bg_tasks: set = set()         # strong refs for fire-and-forget prefetch tasks


def _candidate_rejected(c: dict) -> bool:
    e = _entries.get(_cache_id(c))
    return bool(e and e.playfail.get("rejected"))


# A player that fetches in discrete ranges holds no connection between them,
# so "has a reader right now" reads zero for most of a film. Anything read this
# recently is someone watching.
_PLAYING_RECENT = 30.0


def _playing(e: "_Entry", now: float) -> bool:
    # A warm's entry is fresh but nobody is watching it: counting it would put
    # a phantom on Home's Now Playing and on the restart warning.
    return e.consumers > 0 or (not e.warm
                               and now - (e.last or 0.0) < _PLAYING_RECENT)


def active_streams() -> int:
    """Streams someone is watching — the settings page's 'you'd be cutting
    someone off' number before a restart. Counted from every delivery path:
    buffered entries read recently, direct reads in flight, and HLS sessions
    that fetched recently. Counting only buffered entries with a connection
    open said 0 between a player's range requests, mid-film."""
    now, mono = time.time(), time.monotonic()
    hls = sum(1 for entry in list(hlsproxy._active.values())
              if mono - float((entry.get("_hls") or {}).get("last") or 0)
              < _PLAYING_RECENT)
    return (sum(1 for e in _entries.values() if _playing(e, now))
            + len(_direct) + hls)


def active_stream_details() -> list[dict]:
    """Return rich metadata for every stream someone is watching — a live
    reader, or one read within _PLAYING_RECENT — for the overview dashboard's
    "Now Playing" section.  Pure in-memory read.

    Both delivery paths, because both reach a screen. A buffered entry counts
    readers on the cache file; a direct read (a seek, or a session that never
    cached) has no entry to count, so it registers itself in `_direct` for as
    long as bytes are moving. Reporting only the buffered half is what left
    Home blank while something was plainly playing.
    """
    out = []
    now = time.time()
    for e in _entries.values():
        if not _playing(e, now):
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
            "mode":     "buffered",
        })
    out.extend(_direct.values())
    return out


def _safe_name(sig: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sig)[:120] or secrets.token_urlsafe(8)


def _evict(e: _Entry) -> None:
    if e.producer:
        e.producer.cancel()
    _entries.pop(e.sig, None)
    _fill_marks.pop(e.sig, None)
    try:
        os.remove(e.path)
    except Exception:
        pass


class _DiskFull(Exception):
    """The cache volume is under its free-space floor with nothing idle left
    to evict. Not the source's fault: it must never cost the release a strike."""


def _free_bytes() -> int | None:
    try:
        return shutil.disk_usage(BUFFER_DIR).free
    except OSError:
        return None


def _room_to_write(keep: "_Entry | None" = None,
                   need: int | None = None) -> bool:
    """Whether the cache volume has `need` bytes free (default: the floor),
    evicting idle entries — least recently used first, never one with a reader,
    never `keep` — until it does."""
    need = BUFFER_MIN_FREE_BYTES if need is None else need
    free = _free_bytes()
    if free is None or free >= need:
        return True
    for e in sorted(list(_entries.values()), key=lambda x: x.last):
        if e is keep or e.consumers > 0:
            continue
        logger.info(f"bufcache: space-evict {e.sig} "
                    f"({e.avail // 1024 // 1024} MB; "
                    f"{free // 1024 // 1024} MB free)")
        _evict(e)
        free = _free_bytes()
        if free is None or free >= need:
            return True
    return False


def _write_all(f, data: bytes) -> None:
    """Write every byte or raise. An unbuffered file may write *short* — exactly
    when the disk is filling — and `avail` must never advance past what is
    really on disk, or readers would be served bytes that were never written."""
    view = memoryview(data)
    while view:
        n = f.write(view)
        if not n:
            raise OSError(errno.ENOSPC, "short write")
        view = view[n:]


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


# sig -> (when, avail) at the previous watchdog tick, for rate + stall detection
_fill_marks: dict[str, tuple[float, int]] = {}


async def _watch_fill(now: float) -> None:
    """Record how fast each live entry is filling, and alarm when it isn't.

    Written because a producer once parked with a viewer still asking for
    bytes and *nothing was recorded at all*: the buffer silently stopped
    growing, every later range was fetched cold from upstream, and the only
    way to establish that was stat()-ing the cache file by hand twice. A
    stalled fill is the single most useful fact about a misbehaving stream,
    so it is now an event with the numbers attached rather than an inference.

    Emits `fill` while bytes are moving and `stalled` when they are not,
    for entries a reader still wants. Cheap: one pass per reap interval.
    """
    for e in list(_entries.values()):
        if e.complete or e.failed:
            _fill_marks.pop(e.sig, None)
            continue
        mark = _fill_marks.get(e.sig)
        _fill_marks[e.sig] = (now, e.avail)
        if mark is None:
            continue
        elapsed = now - mark[0]
        if elapsed <= 0:
            continue
        gained = e.avail - mark[1]
        mbps = (gained * 8 / elapsed) / 1e6
        wanted = _reader_interested(e)
        if gained <= 0 and wanted:
            logger.warning(
                "bufcache %s: fill STALLED at %d/%s bytes for %.0fs "
                "(readers=%d, head=%d) — reader still waiting",
                e.sig, e.avail, e.total, elapsed, e.consumers, e.head)
            telemetry.record_buffer(
                "stalled", sig=_release_sig(e), picker=e.picker,
                media_id=e.media_id, source=(e.source or {}).get("lbl", ""),
                dbr=(e.source or {}).get("dbr", ""), node=e.node,
                offset=e.head, avail=e.avail, total=e.total, mbps=0.0,
                reason=f"no bytes in {elapsed:.0f}s, readers={e.consumers}")
            # Belt and braces: whatever parked it, wake it. The producer
            # re-evaluates its own pause condition, so this is a no-op when it
            # is legitimately paused and a rescue when it is not.
            async with e.cond:
                e.cond.notify_all()
        elif gained > 0:
            telemetry.record_buffer(
                "fill", sig=_release_sig(e), picker=e.picker,
                media_id=e.media_id, source=(e.source or {}).get("lbl", ""),
                dbr=(e.source or {}).get("dbr", ""), node=e.node,
                offset=e.head, avail=e.avail, total=e.total, mbps=round(mbps, 2),
                reason=f"readers={e.consumers}")


async def _reaper() -> None:
    """Keep the cache inside its budget: drop entries idle past BUFFER_TTL, and
    when total on-disk exceeds BUFFER_CACHE_BYTES evict least-recently-used ones
    that have no active reader until back under. Never touches a watched entry."""
    while True:
        await asyncio.sleep(BUFFER_REAP_INTERVAL)
        try:
            await hlsproxy.flush_idle()   # HLS sessions account per-session
            _prune_sessions()
            if PROXY_BUFFER:
                now = time.time()
                await _watch_fill(now)
                for e in list(_entries.values()):
                    if e.consumers <= 0 and now - e.last > BUFFER_TTL:
                        logger.info(f"bufcache: TTL-evict {e.sig} "
                                    f"({e.avail // 1024 // 1024} MB)")
                        _evict(e)
                if not _room_to_write():
                    logger.warning("bufcache: cache volume is under its "
                                   "free-space floor and every entry is in "
                                   "use; new streams pass through unbuffered")
                total = sum(e.avail for e in _entries.values())
                if total > BUFFER_CACHE_BYTES:
                    for e in sorted(_entries.values(), key=lambda x: x.last):
                        if total <= BUFFER_CACHE_BYTES:
                            break
                        if e.consumers <= 0:
                            logger.info(f"bufcache: size-evict {e.sig} "
                                        f"({e.avail // 1024 // 1024} MB)")
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
    if PROXY_BUFFER:
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
    # HLS session flushing is maintenance too and must run even when the file
    # read-ahead buffer is disabled.
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.create_task(_reaper())
    free = _free_bytes() if PROXY_BUFFER else None
    if free is not None and BUFFER_CACHE_BYTES > free - BUFFER_MIN_FREE_BYTES:
        logger.warning(
            f"bufcache: BUFFER_CACHE_GB ({BUFFER_CACHE_BYTES // 1024 ** 3} GB) "
            f"exceeds the free space on {BUFFER_DIR} "
            f"({free // 1024 ** 3} GB); the "
            f"{BUFFER_MIN_FREE_BYTES // 1024 ** 3} GB free-space floor will "
            f"bound the cache instead")
    if PROXY_BUFFER:
        logger.info(f"bufcache: ready at {BUFFER_DIR} "
                    f"(cap {BUFFER_CACHE_BYTES // 1024 ** 3}GB, "
                    f"ttl {BUFFER_TTL / 3600:.0f}h, "
                    f"ahead {BUFFER_AHEAD_BYTES // 1024 ** 3}GB)")
    else:
        logger.info("proxy: HLS/session maintenance ready (file buffer disabled)")


async def shutdown() -> None:
    """Gracefully stop producers/maintenance, flush state, and close clients."""
    global _reaper_task
    tasks = list(_bg_tasks)
    if _reaper_task is not None:
        tasks.append(_reaper_task)
    tasks.extend(e.producer for e in _entries.values() if e.producer is not None)
    for task in dict.fromkeys(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*dict.fromkeys(tasks), return_exceptions=True)
    _bg_tasks.clear()
    _reaper_task = None
    try:
        _compact()
    except Exception:
        logger.debug("session shutdown compact failed", exc_info=True)
    await hlsproxy.shutdown()
    await _client.aclose()


def _cached_slice(e: _Entry, start: int, length: int) -> bytes | None:
    """Bytes we already hold on disk, or None if that window isn't fully written."""
    if start < 0 or length <= 0 or start + length > e.avail:
        return None
    try:
        with open(e.path, "rb") as f:
            f.seek(start)
            data = f.read(length)
    except OSError:
        return None
    return data if len(data) == length else None


async def _chain(head: bytes, it):
    """The verification window's leftover bytes, then the rest of the response."""
    if head:
        yield head
    async for chunk in it:
        yield chunk


async def _open_verified(e: _Entry, c: dict, offset: int) -> tuple | None:
    """Open `c` a little *behind* the write head and splice only if it replays
    the bytes we already have. Returns (cand, resp, iterator) positioned exactly
    at `offset`, or None if it can't be proven identical.

    The overlap is the whole point: matching length and a well-formed 206 say
    only that some file of the right size exists there. Replaying bytes we hold
    says it is *this* file. Nothing is written from an unverified source."""
    want = _cached_slice(e, offset - SPLICE_VERIFY_BYTES, SPLICE_VERIFY_BYTES)
    if want is None:
        return None
    rh = f"bytes={offset - len(want)}-"
    try:
        resp = await _send(c, rh)
    except Exception:
        return None
    spliced = False
    try:
        if not _range_response_ok(resp, rh, expected_total=e.total):
            return None
        got_total = _total_of(resp)
        if e.total and got_total and got_total != e.total:
            return None
        it = resp.aiter_raw()
        seen = b""
        while len(seen) < len(want):
            try:
                seen += await asyncio.wait_for(it.__anext__(), _SIDE_READ_SECS)
            except (StopAsyncIteration, asyncio.TimeoutError):
                return None
        if seen[:len(want)] != want:
            logger.info(f"bufcache {e.sig}: {c.get('lbl', '')!r} is a different "
                        f"file at byte {offset} — refusing splice")
            return None
        spliced = True
        return c, resp, _chain(seen[len(want):], it)
    except Exception:
        return None
    finally:
        if not spliced:
            try:
                await resp.aclose()
            except Exception:
                pass


async def _resume_elsewhere(e: _Entry, tried: set) -> tuple | None:
    """Last resort when nothing byte-identical can resume: try the rest of this
    title's ranked pool, each one proving itself against bytes already cached."""
    offset = e.avail
    if not SPLICE_VERIFY or offset <= 0 or not e.total:
        return None
    seen = {c.get("u") for c in e.cands} | set(tried)
    attempts = 0
    for c in (e.pool or []):
        if attempts >= SPLICE_VERIFY_MAX:
            break
        url = c.get("u")
        if not url or url in seen:
            continue
        seen.add(url)
        attempts += 1
        got = await _open_verified(e, c, offset)
        if got is None:
            continue
        cand, resp, it = got
        e.source = cand
        tried.add(url)
        logger.info(f"bufcache {e.sig}: cross-source splice at {offset} -> "
                    f"{cand.get('lbl', '')!r} "
                    f"({SPLICE_VERIFY_BYTES // 1024} KB verified)")
        telemetry.record_buffer(
            "splice", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
            source=cand.get("lbl", ""), dbr=cand.get("dbr", ""),
            offset=offset, total=e.total, reason="cross-source-verified")
        return cand, resp, it
    return None


async def _connect_resume(e: _Entry, tried: set, prefer_new: bool = False) -> tuple | None:
    """(Re)connect a feeding source at the current write head e.avail — the same
    source first (transient blip) unless prefer_new, then byte-identical twins /
    other same-content sources on different nodes. Requires a 206 at the exact
    offset and, when known, a matching total length, so we never resume from a
    different file. Returns (cand, resp, iterator) or None."""
    offset = e.avail
    others = [c for c in e.cands if c is not e.source]
    current = [e.source] if e.source else []

    async def attempt(order: list) -> tuple | None:
        for c in order:
            same_source = c is e.source
            if offset > 0 and not same_source and not e.total:
                continue                          # no byte-identity proof for a twin
            rh = f"bytes={offset}-"
            try:
                resp = await _send(c, rh)
            except Exception:
                continue
            if not _range_response_ok(resp, rh, expected_total=e.total):
                await resp.aclose()
                continue
            gt = _total_of(resp)
            if e.total and gt and gt != e.total:        # not the same file
                await resp.aclose()
                continue
            if not e.total and gt:
                e.total = gt
            e.source = c
            tried.add(c["u"])
            return c, resp, resp.aiter_raw()
        return None

    if prefer_new:
        # Called because *this* source is the problem (sustained slow), so
        # reconnecting to it is the one outcome that helps nobody. Exhaust every
        # genuinely different source — twins first, then the verified pool —
        # before coming back to it.
        return (await attempt(others)
                or await _resume_elsewhere(e, tried)
                or await attempt(current))
    # Nothing with a provable identity came back. Rather than truncate the film
    # mid-scene, let the rest of the title's pool try to earn the splice.
    return await attempt(current + others) or await _resume_elsewhere(e, tried)


async def _resume_patiently(e: _Entry, tried: set) -> tuple | None:
    """_connect_resume, retried with backoff for up to BUFFER_RESUME_BUDGET.

    A single refused reconnect used to fail the whole entry — reproduced: one
    connect blip ended a buffer at 50 % while the very next request would have
    been answered — and from then on every remaining range of the film was a
    cold upstream fetch. Readers behind the write head keep reading from disk
    meanwhile, so waiting here is cheap; giving up is not."""
    deadline = time.monotonic() + max(BUFFER_RESUME_BUDGET, 0.0)
    attempt = 0
    while True:
        attempt += 1
        nxt = await _connect_resume(e, tried)
        if nxt is not None:
            return nxt
        delay = min(0.5 * 2 ** (attempt - 1), 8.0)
        if time.monotonic() + delay > deadline:
            return None
        logger.info(f"bufcache {e.sig}: resume attempt {attempt} at {e.avail} "
                    f"failed, retrying in {delay:.1f}s")
        await asyncio.sleep(delay)


async def _revive(e: _Entry, token: str) -> bool:
    """Restart a failed entry's producer at its write head, if its source is back.

    A failed entry is usually the rest of a film someone is part-way through:
    everything before `avail` is on disk and still valid. Abandoning it degraded
    the remainder of the title to a cold upstream fetch per range, with no
    read-ahead to absorb the next blip. Called when a reader reaches the edge of
    a failed entry; one attempt, because a player is waiting on the answer."""
    if (not e.failed or e.complete or e.producer is not None or e.reviving
            or not e.source or not e.total or e.avail <= 0):
        return False
    if not _room_to_write(keep=e):
        return False                      # it failed for want of disk, not source
    e.reviving = True
    try:
        nxt = await _connect_resume(e, set())
        if nxt is None:
            return False
        _, resp, it = nxt
        if _entries.get(e.sig) is not e or e.producer is not None:
            # Retired (an opening request replaced it) while we reconnected.
            try:
                await resp.aclose()
            except Exception:
                pass
            return False
        async with e.cond:
            e.failed = False
            e.cond.notify_all()
        e.producer = asyncio.create_task(
            _produce(e, token, resp, it, [], resume=True))
        logger.info(f"bufcache {e.sig}: revived at {e.avail}/{e.total} "
                    f"from {(e.source or {}).get('lbl', '')!r}")
        telemetry.record_buffer(
            "revived", sig=_release_sig(e), picker=e.picker,
            media_id=e.media_id, source=(e.source or {}).get("lbl", ""),
            dbr=(e.source or {}).get("dbr", ""), offset=e.avail, total=e.total)
        return True
    finally:
        e.reviving = False


async def _produce(e: _Entry, token: str, resp, it, prebuf: list,
                   resume: bool = False) -> None:
    """Background producer: fill e.path sequentially from the already-open initial
    source (resp/it/prebuf from _select_start), advancing e.avail and waking
    readers as bytes land. Reconnects / twin-switches on a drop or sustained
    slowness — invisible to readers behind the write head. Pauses (backpressure)
    once BUFFER_AHEAD_BYTES past the furthest reader. `resume` continues an
    existing cache file at e.avail (see _revive) instead of starting one."""
    f = None
    win: deque = deque()               # (t, up_bytes) for slow-source detection
    up_bytes = 0
    resume_failures = 0
    # Slow-switch backoff. When a switch finds nothing new it can only come back
    # to the source it is fleeing; doing that every window repays the first-byte cost each time and makes a slow fill slower.
    switch_misses = 0
    next_switch_at = 0.0
    space_due = 0                      # bytes until the next free-space check
    node = telemetry.netinfo(resp).get("node", "") if resp else ""
    e.node = node
    try:
        if resume:
            f = open(e.path, "r+b", buffering=0)
            f.seek(e.avail)
        else:
            f = open(e.path, "wb", buffering=0)
            os.chmod(e.path, 0o600)
        for chunk in prebuf:
            _write_all(f, chunk)
        async with e.cond:
            e.avail += sum(len(c) for c in prebuf)
            e.cond.notify_all()
        tried = {e.source["u"]} if e.source else set()
        if not resume and e.total and e.total > BUFFER_TAIL_BYTES * 2:
            t = asyncio.create_task(_fetch_tail(e))
            _bg_tasks.add(t)
            t.add_done_callback(_reap_task)
            # Give the warm a head start before the bulk fill begins competing
            # with it. The player cannot show a single frame until it has the
            # container index, which lives at EOF — and the sequential fill
            # starting at byte 0 will never contain it. Letting the two race
            # measured catastrophically on Easynews: an 8 MB tail read that
            # takes 0.4 s unopposed took 50-56 s against a 40 MB/s fill, and
            # the picture appeared the instant it finally landed (ffprobe:
            # 1.3 s direct vs 49.5 s through here). The wait is bounded, so a
            # source whose tail is genuinely expensive (a cold nzbdav mount
            # costs ~7.7 s) just falls back to warming in the background
            # exactly as before, having cost us TAIL_WARM_HEADSTART at most.
            headstart = (TAIL_WARM_HEADSTART_RATIONED
                         if _rationed_source(e.source) else TAIL_WARM_HEADSTART)
            if headstart > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(t), headstart)
                except Exception:       # timeout, or the warm failed on its own
                    pass
        while True:
            if e.warm and it is not None and not _reader_interested(e):
                # A warm that has what it came for, and nobody has pressed
                # Play. Let go of the source instead of parking on it: on a
                # rationed host the open connection is one of about two, and a
                # parked one the server times out would come back as a
                # "mid-stream drop" — a strike — the moment the viewer arrived.
                try:
                    await resp.aclose()
                except Exception:
                    pass
                resp = it = None
                logger.info(f"bufcache {e.sig}: warm holding at {e.avail} "
                            f"bytes; source released until a reader arrives")
            async with e.cond:            # backpressure + pause when nobody's watching
                await e.cond.wait_for(
                    lambda: _reader_interested(e)
                    and e.avail - e.head <= BUFFER_AHEAD_BYTES)
            if it is None:
                # The viewer arrived at a warm that let go of its source: pick
                # it up at the write head. We closed it, so this is no drop and
                # no strike; readers meanwhile read what the warm left on disk.
                nxt = await _resume_patiently(e, tried)
                if nxt is None:
                    async with e.cond:
                        e.failed = True
                        e.cond.notify_all()
                    telemetry.record_buffer(
                        "failed", sig=_release_sig(e), picker=e.picker,
                        media_id=e.media_id, offset=e.avail, total=e.total,
                        reason="no source after a warm")
                    break
                _, resp, it = nxt
                node = telemetry.netinfo(resp).get("node", "")
                e.node = node
            try:
                chunk = await _next_chunk(it, e.source)
            except StopAsyncIteration:
                chunk = None
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.info(f"bufcache {e.sig}: source drop at {e.avail} "
                            f"({type(ex).__name__}), reconnecting")
                telemetry.record_buffer(
                    "drop", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
                    source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    offset=e.avail, total=e.total, reason=type(ex).__name__)
                try:
                    await resp.aclose()
                except Exception:
                    pass
                if e.avail > 0 and e.source and not e.warm:   # a warm judges nothing
                    reputation.observe(e.source.get("sig", ""), token,
                                       "mid-stream-dead", e.source.get("lbl", ""))
                    sig = e.source.get("sig", "")
                    reputation.cooldown(sig)
                    if sig.startswith("nzb:"):
                        usenet_health.record_failure(
                            sig, e.source.get("lbl", ""),
                            e.source.get("nzb_indexers") or [], "mid-stream-dead",
                            f"buffer:{token}:{sig}")
                resume_failures += 1
                nxt = (await _resume_patiently(e, tried)
                       if resume_failures <= RECONNECT_MAX else None)
                if nxt is None:
                    async with e.cond:
                        e.failed = True
                        e.cond.notify_all()
                    telemetry.record_buffer(
                        "failed", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
                        offset=e.avail, total=e.total,
                        reason="all sources exhausted")
                    logger.info(f"bufcache {e.sig}: all sources failed at {e.avail}")
                    break
                _, resp, it = nxt
                node = telemetry.netinfo(resp).get("node", "")
                e.node = node
                telemetry.record_buffer(
                    "reconnect", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
                    source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    offset=e.avail, total=e.total)
                win.clear()
                up_bytes = 0
                continue
            if chunk is None and e.total is not None and e.avail < e.total:
                # Some CDNs close cleanly after a truncated body. Treat the
                # declared total as authoritative and reconnect at the exact
                # write head; declaring this entry complete would permanently
                # cache a partial movie as a healthy source.
                logger.info(f"bufcache {e.sig}: premature EOF at {e.avail}/"
                            f"{e.total}, reconnecting")
                telemetry.record_buffer(
                    "drop", sig=_release_sig(e), picker=e.picker,
                    media_id=e.media_id, source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    offset=e.avail, total=e.total, reason="premature-eof")
                if e.source and not e.warm:
                    sig = e.source.get("sig", "")
                    reputation.observe(sig, token, "premature-eof",
                                       e.source.get("lbl", ""), node=node)
                    reputation.cooldown(sig)
                    if sig.startswith("nzb:"):
                        usenet_health.record_failure(
                            sig, e.source.get("lbl", ""),
                            e.source.get("nzb_indexers") or [], "premature-eof",
                            f"buffer-eof:{token}:{sig}")
                try:
                    await resp.aclose()
                except Exception:
                    pass
                resume_failures += 1
                nxt = (await _resume_patiently(e, tried)
                       if resume_failures <= RECONNECT_MAX else None)
                if nxt is None:
                    async with e.cond:
                        e.failed = True
                        e.cond.notify_all()
                    telemetry.record_buffer(
                        "failed", sig=_release_sig(e), picker=e.picker,
                        media_id=e.media_id, offset=e.avail, total=e.total,
                        reason="premature EOF; all sources exhausted")
                    break
                _, resp, it = nxt
                node = telemetry.netinfo(resp).get("node", "")
                e.node = node
                continue
            if chunk is None:                     # verified clean EOF: whole file cached
                async with e.cond:
                    e.complete = True
                    if e.total is None:
                        e.total = e.avail
                    e.cond.notify_all()
                logger.info(f"bufcache {e.sig}: complete ({e.avail} bytes)")
                telemetry.record_buffer(
                    "complete", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
                    source=(e.source or {}).get("lbl", ""),
                    dbr=(e.source or {}).get("dbr", ""), node=node,
                    avail=e.avail, total=e.total)
                break
            space_due -= len(chunk)
            if space_due <= 0:
                space_due = _SPACE_CHECK_BYTES
                if not _room_to_write(keep=e):
                    raise _DiskFull(f"under {BUFFER_MIN_FREE_BYTES // 1024 ** 2} "
                                    f"MB free on the cache volume")
            try:
                _write_all(f, chunk)
            except OSError as ex:
                if ex.errno not in (errno.ENOSPC, errno.EDQUOT):
                    raise
                # Something else filled the disk under us. Make room once and
                # rewrite the whole chunk at the write head, or stop filling.
                if not _room_to_write(keep=e,
                                      need=BUFFER_MIN_FREE_BYTES + len(chunk)):
                    raise _DiskFull("disk full") from ex
                f.seek(e.avail)
                try:
                    _write_all(f, chunk)
                except OSError as again:
                    if again.errno in (errno.ENOSPC, errno.EDQUOT):
                        raise _DiskFull("disk full") from again
                    raise
            resume_failures = 0
            async with e.cond:
                e.avail += len(chunk)
                e.grew_at = time.monotonic()
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
                # A warm has no reader, so it can never be at the edge — and a
                # slow switch is a verdict (it cools the source).
                reader_at_edge = (not e.warm
                                  and e.head >= e.avail - 4 * 1024 * 1024)
                twins = [c for c in e.cands if c is not e.source
                         and c.get("dbr") != (e.source or {}).get("dbr")
                         and c["u"] not in tried]
                # A single-source entry (the usenet/Easynews lanes have no twin
                # identity) still has the rest of the title's pool to fall back
                # on — each member has to prove itself byte for byte first, so
                # an alternative existing is not the same as one being used.
                elsewhere = bool(SPLICE_VERIFY and e.total and e.pool
                                 and any(c.get("u") not in tried
                                         and c.get("u") != (e.source or {}).get("u")
                                         for c in e.pool))
                if (dt >= BUFFER_SLOW_WINDOW and reader_at_edge
                        and (twins or elsewhere) and now >= next_switch_at
                        and db / dt < need * BUFFER_SLOW_MARGIN):
                    rate = round(db / dt / 1e6, 2)
                    logger.info(f"bufcache {e.sig}: source slow "
                                f"({rate} MB/s < need), twin-switch")
                    telemetry.record_buffer(
                        "slow", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
                        source=(e.source or {}).get("lbl", ""),
                        dbr=(e.source or {}).get("dbr", ""), node=node,
                        offset=e.avail, total=e.total, mbps=rate,
                        reason="below need at write head")
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    fleeing = (e.source or {}).get("u")
                    nxt = await _connect_resume(e, tried, prefer_new=True)
                    if nxt is None or (nxt[0] or {}).get("u") == fleeing:
                        # Nothing better exists: back off (15 s, doubling to
                        # 2 min) and make sure the *next* play of this title
                        # doesn't choose this copy again.
                        switch_misses += 1
                        next_switch_at = now + min(120.0, 15.0 * 2 ** (switch_misses - 1))
                        if switch_misses == 1 and e.source:
                            sig = e.source.get("sig", "")
                            reputation.observe(sig, token, "mid-stream-slow",
                                               e.source.get("lbl", ""), node=node)
                            reputation.cooldown(sig)
                    else:
                        switch_misses = 0
                    if nxt is not None:
                        _, resp, it = nxt
                        node = telemetry.netinfo(resp).get("node", "")
                        e.node = node
                        telemetry.record_buffer(
                            "twin", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
                            source=(e.source or {}).get("lbl", ""),
                            dbr=(e.source or {}).get("dbr", ""), node=node,
                            offset=e.avail, total=e.total, mbps=rate)
                    win.clear()
                    up_bytes = 0
    except asyncio.CancelledError:
        pass
    except _DiskFull as ex:
        # Stop filling; readers already behind the write head finish from
        # disk, and anything past it is passed straight through. The source
        # did nothing wrong, so no strike and no cooldown.
        logger.warning(f"bufcache {e.sig}: stopped filling at {e.avail}: {ex}; "
                       f"the rest of this stream passes through unbuffered")
        async with e.cond:
            e.failed = True
            e.cond.notify_all()
        telemetry.record_buffer(
            "failed", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
            offset=e.avail, total=e.total, reason=f"cache volume: {ex}")
    except Exception as ex:
        logger.exception(f"bufcache {e.sig}: producer crashed")
        async with e.cond:
            e.failed = True
            e.cond.notify_all()
        telemetry.record_buffer(
            "failed", sig=_release_sig(e), picker=e.picker, media_id=e.media_id,
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


async def _get_or_start_entry_once(token: str, session: dict, cands: list,
                                   warm: bool = False) -> _Entry | None:
    """Find the cache entry this token's release maps to (twins and re-watches
    reuse an existing one), or select a working source at offset 0 and start a
    producer. Returns the entry, or None if no source could even start.
    `warm` selects without verdicts and marks a new entry as a warm's."""
    retired: list[_Entry] = []
    reuse = None
    available = [c for c in cands
                 if not reputation.blocked(c.get("sig") or "")
                 and not _candidate_rejected(c)]
    skip_cooled = bool(available) and not all(
        reputation.cooled(c.get("sig") or "") for c in available)
    reusable_ids = [_cache_id(c) for c in available
                    if not (skip_cooled and reputation.cooled(c.get("sig") or ""))]
    async with _entries_start_lock:
        sticky = session.get("bufcid") or session.get("bufsig")
        cache_ids = ([sticky] if sticky in reusable_ids else []) + reusable_ids
        for cache_id in dict.fromkeys(s for s in cache_ids if s):
            existing = _entries.get(cache_id)
            if existing is None:
                continue
            if existing.failed:
                if _entries.get(cache_id) is existing:
                    _entries.pop(cache_id, None)
                retired.append(existing)
                if (session.get("bufcid") == cache_id
                        or session.get("bufsig") == cache_id):
                    session.pop("bufsig", None)
                    session.pop("bufcid", None)
                continue
            if existing.playfail.get("rejected"):
                continue        # player can't open it — pick a different release
            reuse = existing
            session["bufcid"] = cache_id
            session["bufsig"] = cache_id       # legacy observability/tests
            if existing.source:
                _pin_selection(token, session, cands, existing.source)
            break
    for old in retired:
        _retire_failed(old)
    if reuse is not None:
        return reuse

    # Network evaluation is deliberately outside the global registry lock. Two
    # concurrent cold starts may perform duplicate probes, but unrelated viewers
    # no longer queue behind several seconds of another title's network I/O.
    sel = await _select_start(cands, "bytes=0-", 0, None, token,
                              judge=not warm)
    if sel is None:
        return None
    _, cand, resp, it, prebuf, _, _ = sel
    cache_id = _cache_id(cand) or ("session:" + token)
    _pin_selection(token, session, cands, cand)
    reuse = None
    retired = []
    async with _entries_start_lock:
        existing = _entries.get(cache_id)
        if existing is not None and existing.failed:
            _entries.pop(cache_id, None)
            retired.append(existing)
            existing = None
        if existing is not None:                    # another cold start won the race
            reuse = existing
            session["bufcid"] = cache_id
            session["bufsig"] = cache_id
        else:
            pool = session.get("pool") or cands
            srcs = [c for c in pool if _cache_id(c) == cache_id] or [cand]
            # A generation suffix lets a retired entry finish serving an already
            # open file descriptor while its replacement starts cleanly.
            path = os.path.join(
                BUFFER_DIR, f"{_safe_name(cache_id)}.{secrets.token_hex(4)}.bin")
            e = _Entry(cache_id, path, srcs, cand,
                       resp.headers.get("content-type", ""), _total_of(resp),
                       session.get("picker", ""), session.get("id", ""),
                       pool=pool)
            e.warm = warm
            _entries[cache_id] = e
            session["bufcid"] = cache_id
            session["bufsig"] = cache_id
            e.producer = asyncio.create_task(_produce(e, token, resp, it, prebuf))
    for old in retired:
        _retire_failed(old)
    if reuse is not None:
        await resp.aclose()
        return reuse
    logger.info(f"bufcache {cache_id}: start ({'warm, ' if warm else ''}"
                f"source {cand.get('lbl')!r}, total {e.total})")
    telemetry.record_buffer("start", sig=cand.get("sig", ""), picker=e.picker,
                            media_id=e.media_id,
                            source=cand.get("lbl", ""), dbr=cand.get("dbr", ""),
                            node=telemetry.netinfo(resp).get("node", ""),
                            total=e.total, reason="warm" if warm else "")
    return e


async def _get_or_start_entry(token: str, session: dict, cands: list,
                              warm: bool = False) -> _Entry | None:
    """Token-scoped single-flight around cold selection and producer creation.

    Two simultaneous opens of one token must never independently select two
    different releases. Unrelated titles still evaluate concurrently. A warm
    takes the same lock, so a Play pressed mid-warm waits for its choice and
    reuses it rather than choosing again beside it.
    """
    lock = _start_locks.setdefault(token, asyncio.Lock())
    async with lock:
        return await _get_or_start_entry_once(token, session, cands, warm)


async def _fire_prefetch(media_id: str, picker_label: str,
                         release_sig: str = "", caps: str = "") -> None:
    """On confirmed startup, credit the playing link and ensure E+1 is warm.

    Playback itself is better evidence than re-searching the current episode.
    E+1 waits event-driven only while that episode's existing background work is
    active, then starts immediately. Lazy import avoids the proxy/picker cycle.

    `caps` is the X-Display-Caps of the screen that asked for the list. A media
    request carries none, so without it E+1 was ranked for no display and
    cached under a key a caps-reporting player never reads — every prefetch for
    the household's own televisions was wasted.
    """
    try:
        from app import picker
        display_caps.current_caps.set(display_caps.parse(caps) if caps else None)
        telemetry.request_ctx.set({
            "media": "series", "media_id": media_id,
            "picker": (picker_label or "") + "/play",
        })
        picker.note_playback("series", media_id, picker_label, release_sig)
        await picker.prefetch_next("series", media_id, picker_label)
    except Exception:
        logger.exception(f"prefetch trigger failed for {media_id}")


def _schedule_prefetch(entry: dict, release_sig: str = "") -> None:
    """Single-flight playback hook shared by buffered/direct/HLS startup."""
    if (not PREFETCH_NEXT or ":" not in (entry.get("id") or "")
            or entry.get("nextfetched")):
        return
    entry["nextfetched"] = True
    task = asyncio.create_task(_fire_prefetch(
        entry["id"], entry.get("picker", ""), release_sig,
        entry.get("caps") or ""))
    _bg_tasks.add(task)
    task.add_done_callback(_reap_task)


def _cache_headers(e: _Entry, start: int, end: int | None, had_range: bool):
    total = e.total
    h = {"Accept-Ranges": "bytes", "Content-Type": e.content_type}
    if had_range:
        last = end if end is not None else ((total - 1) if total else None)
        if total is not None and last is not None:
            last = min(last, total - 1)
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
    revives = 0
    limit = (end + 1) if end is not None else None      # exclusive upper bound
    waitlog = _WaitLog(picker=e.picker, media_id=e.media_id, mode="buffered")
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
                # The producer gave up. Before truncating this response, see
                # whether the source is back: every byte before `avail` is
                # still good, so a revived producer continues the same file and
                # the player never sees the gap. Bounded per response so a
                # source that accepts and then drops cannot loop us.
                if revives < 2 and await _revive(e, token):
                    revives += 1
                    continue
                reason = "upstream_dead"
                break
            else:
                # The reader caught the write head: the player is now waiting
                # on the source, exactly as if there were no buffer.
                w0 = time.monotonic()
                timed_out = False
                async with e.cond:
                    try:
                        await asyncio.wait_for(
                            e.cond.wait_for(lambda: pos < e.avail or e.complete or e.failed),
                            timeout=BUFFER_WAIT_TIMEOUT)
                    except asyncio.TimeoutError:
                        timed_out = True
                waitlog.note(time.monotonic() - w0, cand=e.source, offset=pos,
                             total=e.total, avail=e.avail, node=e.node)
                if timed_out:
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
            # Attribute from the session, not from `e`. A buffered entry is
            # keyed by cache id and deliberately shared between viewers, so the
            # viewer on it is whoever opened the file first — crediting them
            # for someone else's play would quietly corrupt watch history. The
            # token is the per-viewer capability.
            _sess = _lookup(token) or {}
            telemetry.record_play({"picker": e.picker, "id": e.media_id,
                                   "viewer": _sess.get("viewer", ""),
                                   "viewer_key": _sess.get("viewer_key", ""),
                                   "pos": _sess.get("pos", 0),
                                   "total": _sess.get("total") or e.total},
                                  e.source or {}, 0, served=served,
                                  dur=time.monotonic() - t0, ttfb=0.0, reconnects=0,
                                  reason=reason, session=token,
                                  net={"node": e.node}, waits=waitlog.summary())
            _note_consumer_close(
                e.playfail, sig=_release_sig(e), label=(e.source or {}).get("lbl", ""),
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
        ent = _entries.get(_cache_id(c))
        return not (ent and ent.playfail.get("rejected"))
    if source is None or ok(source):
        return source
    cid = _cache_id(source)
    # A tail/seek may only move to a byte-identical delivery twin. Switching to
    # an unrelated "clean" release here corrupts the file the player parsed.
    return next((c for c in cands if c is not source and ok(c)
                 and cid and _cache_id(c) == cid), None)


async def _serve_direct(session: dict, cands: list, request, token: str,
                        source: dict | None = None,
                        expected_total: int | None = None) -> Response:
    """Pass-through for a byte range we can't (yet) serve from cache — a seek ahead
    of the write head, or a session that never cached.  A nonzero/suffix seek is
    locked to one release; only byte-identical same-content delivery twins may
    be tried after its preferred source."""
    rh = request.headers.get("range")
    offset, _, had_range = _parse_range(rh)
    suffix = _suffix_length(rh)
    seek = suffix is not None or (had_range and offset > 0)
    if suffix is not None and expected_total:
        # Rewrite `bytes=-N` to its absolute equivalent before asking upstream.
        # Both forms are legal HTTP; not every server agrees. nzbdav parses the
        # header by splitting on "-" and dropping empty parts, so the suffix
        # form comes back as the *head* of the file — which _range_response_ok
        # rightly refuses, turning a player's opening index read into a 502.
        # Asking the unambiguous way costs nothing. `seek` is decided above,
        # from the original header, so this cannot change failover behaviour.
        rh = f"bytes={max(0, expected_total - suffix)}-{expected_total - 1}"
    anchor = source or (cands[0] if cands else None)
    if seek and source is None:
        # A seek-first request without a persisted selection has no safe release
        # identity. Candidate zero is only a ranking position, not proof that it
        # is the file whose header the player parsed before restart.
        return Response(status_code=409, headers={"Accept-Ranges": "bytes"})
    order = ([anchor] if anchor else []) + [c for c in cands if c is not anchor]
    if seek and anchor is not None:
        cid = _cache_id(anchor)
        order = [c for c in order
                 if c is anchor or (cid and _cache_id(c) == cid)]
    for c in order:
        try:
            resp = await _send(c, rh)
        except Exception as exc:
            telemetry.record_buffer(
                "direct_error", sig=c.get("sig", ""),
                picker=session.get("picker", ""), media_id=session.get("id", ""),
                source=c.get("lbl", ""), dbr=c.get("dbr", ""),
                reason=f"connect:{type(exc).__name__}")
            continue
        if _range_response_ok(resp, rh, expected_total=expected_total):
            if not seek:
                _pin_selection(token, session, session.get("cands") or cands, c)
            async def gen(resp=resp, cand=c):
                global _direct_seq
                current = resp
                base, promised = _response_span(current, expected_total)
                sent, retries, retry_mark = 0, 0, 0
                _direct_seq += 1
                card_id = _direct_seq
                card = {
                    "media_id": session.get("id", "") or "",
                    "label":    cand.get("lbl", ""),
                    "debrid":   cand.get("dbr", ""),
                    "res":      cand.get("res", 0),
                    "node":     "",
                    # A direct read has no cache file behind it, so there is no
                    # buffered-ahead figure to show. `total` stays None and the
                    # card renders without a progress meter rather than with a
                    # meaningless full one.
                    "avail":    0,
                    "total":    None,
                    "consumers": 1,
                    "picker":   session.get("picker", "") or "",
                    "mode":     "direct",
                }
                _direct[card_id] = card
                waitlog = _WaitLog(picker=session.get("picker", "") or "",
                                   media_id=session.get("id", "") or "",
                                   mode="seek" if seek else "direct")
                try:
                    while True:
                        failed = False
                        try:
                            it = current.aiter_raw()
                            while True:
                                r0 = time.monotonic()
                                try:
                                    chunk = await _next_chunk(it, cand)
                                except StopAsyncIteration:
                                    break
                                finally:
                                    waitlog.note(time.monotonic() - r0, cand=cand,
                                                 offset=base + sent,
                                                 total=expected_total)
                                sent += len(chunk)
                                yield chunk
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            failed = True
                        if not failed and (promised is None or sent >= promised):
                            break
                        # Same budget rules as _body: spaced attempts, and the
                        # streak refills after real progress. A failed attempt
                        # retries the *reconnect*, never the closed response —
                        # re-reading that used to burn a second retry instantly.
                        if sent - retry_mark >= _RECONNECT_RESET_BYTES:
                            retries = 0
                        retry_mark = sent
                        try:
                            await current.aclose()
                        except Exception:
                            pass
                        absolute = base + sent
                        last = base + promised - 1 if promised is not None else None
                        resume = f"bytes={absolute}-" + (str(last) if last is not None else "")
                        nxt = None
                        while nxt is None and retries < RECONNECT_MAX:
                            retries += 1
                            await asyncio.sleep(_reconnect_delay(retries))
                            try:
                                attempt = await _send(cand, resume)
                            except Exception:
                                continue
                            if _range_response_ok(
                                    attempt, resume, expected_total=expected_total):
                                nxt = attempt
                            else:
                                await attempt.aclose()
                        if nxt is None:
                            sig = cand.get("sig") or ""
                            reputation.observe(sig, token, "premature-eof",
                                               cand.get("lbl", ""))
                            reputation.cooldown(sig)
                            telemetry.record_buffer(
                                "direct_error", sig=sig,
                                picker=session.get("picker", ""),
                                media_id=session.get("id", ""),
                                source=cand.get("lbl", ""),
                                dbr=cand.get("dbr", ""),
                                reason=f"premature-eof:{sent}/{promised}")
                            break
                        current = nxt
                except asyncio.CancelledError:
                    raise
                finally:
                    _direct.pop(card_id, None)
                    try:
                        await current.aclose()
                    except Exception:
                        pass
            return StreamingResponse(gen(), status_code=resp.status_code,
                                     headers=_fwd_headers(resp))
        telemetry.record_buffer(
            "direct_error", sig=c.get("sig", ""),
            picker=session.get("picker", ""), media_id=session.get("id", ""),
            source=c.get("lbl", ""), dbr=c.get("dbr", ""),
            reason=(f"http-{resp.status_code}" if resp.status_code not in (200, 206)
                    else "bad-content-range"))
        await resp.aclose()
    return Response(status_code=502)


async def _fetch_tail(e: _Entry) -> None:
    """Pull the file's tail into memory so index reads never go upstream.

    Best-effort and silent on failure: without it, tail ranges simply keep
    falling through to the direct path exactly as before.
    """
    want = min(BUFFER_TAIL_BYTES, e.total or 0)
    if want <= 0 or not e.source:
        return
    # Absolute range, never `bytes=-N`. The suffix form is the natural way to
    # ask for a tail and it is what this used to send — but nzbdav parses it by
    # splitting on "-" and discarding empties, so `bytes=-8388608` comes back as
    # the first 8 MB of the file. _range_response_ok correctly refuses that, and
    # tail warming then silently never happened on the one lane it was written
    # for. We always know `total` here (`want` is derived from it), so there is
    # no reason to use the ambiguous form at all.
    rh = f"bytes={e.total - want}-{e.total - 1}"
    try:
        resp = await _send(e.source, rh)
    except Exception as ex:
        # Say so. This used to fail completely silently, and the absence of the
        # success line below is indistinguishable from "the feature is off" —
        # which cost a full investigation when an Easynews connection cap
        # started starving it. A warm that does not happen is a 7-second index
        # read later, so it is worth one line.
        logger.info(f"bufcache {e.sig}: tail warm failed ({type(ex).__name__})")
        return
    why = ""
    try:
        if not _range_response_ok(resp, rh):
            why = f"bad range response (HTTP {resp.status_code})"
            return
        chunks, got = [], 0
        it = resp.aiter_raw()
        while True:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), _SIDE_READ_SECS)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                why = f"source went silent for {_SIDE_READ_SECS:.0f}s"
                return
            chunks.append(chunk)
            got += len(chunk)
            if got > want:                       # server ignored the range
                why = "source ignored the range"
                return
        if got != want:
            why = f"short read ({got} of {want} bytes)"
            return
        e.tail = b"".join(chunks)
        e.tail_off = (e.total or want) - want
        async with e.cond:
            e.cond.notify_all()
        logger.info(f"bufcache {e.sig}: tail warmed ({want // 1024} KB @ "
                    f"{e.tail_off})")
    except Exception as ex:
        why = type(ex).__name__
    finally:
        if why:
            logger.info(f"bufcache {e.sig}: tail warm failed ({why})")
        try:
            await resp.aclose()
        except Exception:
            pass


def _tail_response(e: _Entry, offset: int, end: int | None):
    """Serve a range entirely inside the warmed tail from memory, or None."""
    if e.tail is None or e.total is None or offset < e.tail_off:
        return None
    last = min(end if end is not None else e.total - 1, e.total - 1)
    if last < offset:
        return None
    data = e.tail[offset - e.tail_off:last + 1 - e.tail_off]
    return Response(
        content=data, status_code=206,
        headers={"Accept-Ranges": "bytes",
                 "Content-Range": f"bytes {offset}-{last}/{e.total}",
                 "Content-Length": str(len(data)),
                 "Content-Type": e.content_type or "application/octet-stream"})


def _reader_interested(e: _Entry) -> bool:
    """Whether the producer should keep filling this entry.

    A live reader obviously counts. So does one that touched the entry within
    the idle grace: a player fetching in discrete ranges holds no connection
    between them, and a range past the write head is served directly without
    ever taking a consumer ref. Judging solely on `consumers` mistakes that for
    an abandoned stream.

    The grace is shorter for a source that rations connections. On an ordinary
    CDN, reading ahead for an absent reader costs only bandwidth we have. On
    Easynews it costs a *connection*, of which there are about two, and the
    next stream's index read then queues behind this one — measured: a tail
    warm that takes 0.36 s unopposed took 48.7 s with one abandoned fill still
    running at 40 MB/s. Keeping a read-ahead warm for a viewer who left is not
    worth a 48-second black screen for the one who is here.

    A warm (nobody has pressed Play yet) gets the same grace, counted from
    when it chose its source, and at most WARM_AHEAD_BYTES on top of it.
    """
    grace = (BUFFER_IDLE_GRACE_RATIONED if _rationed_source(e.source)
             else BUFFER_IDLE_GRACE)
    idle = time.time() - (e.last or 0.0)
    if e.warm and e.consumers <= 0:
        return e.avail < WARM_AHEAD_BYTES and idle < grace
    return e.consumers > 0 or idle < grace


async def _serve_buffered(token: str, session: dict, cands: list, offset: int,
                          end: int | None, had_range: bool, request) -> Response:
    cache_id = session.get("bufcid") or session.get("bufsig")
    e = _entries.get(cache_id) if cache_id else None
    if e is not None and offset == 0 and e.playfail.get("rejected"):
        # The player provably can't open this release (see _note_consumer_close)
        # — a fresh open must select a different one, not re-serve the cache.
        session.pop("bufsig", None)
        session.pop("bufcid", None)
        e = None
    if (e is None or e.failed) and offset == 0:         # opening/retry starts a clean fill
        e = await _get_or_start_entry(token, session, cands)
    if e is None:                                       # seek-first, or no source at all
        if offset == 0:
            return Response(status_code=502)
        source = _selected_candidate(session, cands) if offset > 0 else None
        source = _skip_rejected(source, cands)
        return await _serve_direct(session, cands, request, token, source=source)
    e.last = time.time()
    if e.warm:
        # The viewer pressed Play on a warmed title. From here it is an
        # ordinary stream — normal grace, read-ahead and verdicts — and a
        # producer that let go of its source picks it back up.
        e.warm = False
        async with e.cond:
            e.cond.notify_all()
    if e.total is not None and (offset >= e.total or
                                (end is not None and end < offset)):
        return Response(status_code=416,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Range": f"bytes */{e.total}"})
    if e.total is not None and end is not None:
        end = min(end, e.total - 1)
    if e.failed and offset > 0:
        # A viewer part-way through a film whose producer gave up: resume the
        # fill at its write head rather than leave the rest of the title to a
        # cold upstream fetch per range (see _revive).
        await _revive(e, token)
    # A reader landing at (or just past) a running producer's write head — the
    # usual shape of a player reopening after its connection dropped — waits for
    # the fill instead of opening a second upstream connection beside it. On a
    # host that rations connections (Easynews allows about two) that second
    # connection is exactly what starves the producer and the tail warm.
    near_head = (e.producer is not None and not e.failed
                 and offset - e.avail <= _NEAR_HEAD_BYTES)
    # Seek ahead of what's cached (and not done): pass through directly rather than
    # block waiting for the sequential fill to reach it.
    if offset >= e.avail and not e.complete and not near_head:
        # Index/cue reads land here: serve them from the warmed tail with zero
        # upstream round-trips. This is the range every player asks for on open.
        tail = _tail_response(e, offset, end)
        if tail is not None:
            return tail
        # Serving this range directly must not read as "nobody is watching".
        # This request is a reader — it just wants bytes the fill hasn't reached
        # — so wake the producer. Without this it stays parked on its consumer
        # check and the entry never fills again, degrading the whole title to a
        # cold fetch per range.
        #
        # Deliberately NOT advancing e.head. That is the backpressure anchor,
        # and a player reads the container index from the tail the moment it
        # opens a file: pinning head at EOF from one such probe leaves
        # avail-head permanently negative, so the producer would never pause
        # and would fetch the whole file however little of it gets watched.
        async with e.cond:
            e.cond.notify_all()
        if offset == 0 and e.failed:                    # producer never got going
            return await _serve_direct(session, e.cands, request, token,
                                       source=e.source, expected_total=e.total)
        if offset > 0:
            return await _serve_direct(session, e.cands, request, token,
                                       source=e.source, expected_total=e.total)
    status, headers = _cache_headers(e, offset, end, had_range)
    return StreamingResponse(_consume(e, offset, end, token),
                             status_code=status, headers=headers)


# ── standby: what a player should switch to ──────────────────────────────────
# A byte-identical copy can be spliced into the buffer invisibly, and the
# producer does that itself. A *different* encode cannot — its byte offsets are
# not this file's — so it can only be a player-side re-open and seek to the
# same wall-clock time. The player decides when; this answers with what.
#
# Ranked by seek cost first, because the re-open *is* a seek to an arbitrary
# timestamp: Easynews and debrid CDNs answer one in ~0.3 s, while direct usenet
# re-runs an interpolation search over NNTP headers (7.7 s cold, measured) —
# a high-quality usenet standby would spend the very buffer the switch exists
# to protect. Within a cost class the picker's own order stands.
_STANDBY_MAX = 3
_STALL_REPORT_SECS = 15.0
_NEAR_READER_BYTES = 64 * 1024 * 1024


def _seek_cost(c: dict) -> str:
    return "high" if (c.get("sig") or "").startswith("nzb:") else "low"


def _fill_state(session: dict) -> dict:
    cache_id = session.get("bufcid") or session.get("bufsig")
    e = _entries.get(cache_id) if cache_id else None
    if e is None:
        return {"state": "unbuffered"}
    out = {"avail": e.avail, "total": e.total, "head": e.head}
    if e.complete:
        return {"state": "complete", **out}
    if e.failed:
        return {"state": "failed", **out}
    idle = time.monotonic() - e.grew_at
    if (idle >= _STALL_REPORT_SECS and e.avail - e.head < _NEAR_READER_BYTES
            and _reader_interested(e)):
        return {"state": "stalled", "stalled_for": round(idle, 1), **out}
    return {"state": "filling", **out}


def _standby_candidates(session: dict) -> list[dict]:
    """Untried copies of this title, best first by seek cost then rank, that
    the server cannot splice itself. Only an identity-verified (auto) session
    has any: an unverified token was minted for one explicit file and must not
    turn into a silent substitute."""
    if session.get("auto") is not True:
        return []
    cands = session.get("cands") or []
    pool = session.get("pool") or cands
    current = _selected_candidate(session, cands) or (cands[0] if cands else {})
    playing = {_cache_id(current)}
    e = _entries.get(session.get("bufcid") or session.get("bufsig") or "")
    if e is not None:
        playing |= {_cache_id(c) for c in (e.cands or [])}
        if e.source:
            playing.add(_cache_id(e.source))
    seen, out = set(), []
    for rank, c in enumerate(pool):
        cid, url = _cache_id(c), c.get("u")
        if (not url or cid in playing or url == current.get("u")
                or url in seen or c.get("auto") is not True
                or reputation.blocked(c.get("sig") or "")
                or reputation.cooled(c.get("sig") or "")
                or _candidate_rejected(c)):
            continue
        seen.add(url)
        out.append((_seek_cost(c) == "high", rank, c))
    out.sort(key=lambda item: item[:2])
    return [c for _, _, c in out[:_STANDBY_MAX]]


def standby(token: str) -> dict | None:
    """The fill state of the stream on `token` and, ranked, what to switch to.
    Each standby is a freshly minted single-file token for the same viewer and
    display. None when the token is unknown or expired."""
    session = _lookup(token)
    if session is None or session.get("hls"):
        return None
    rows = []
    for c in _standby_candidates(session):
        t = _mint([c], [c], "", session.get("id", ""), session.get("picker", ""),
                  viewer=session.get("viewer", ""),
                  viewer_key=session.get("viewer_key", ""),
                  caps=session.get("caps") or "")
        rows.append({"url": f"{PUBLIC}/proxy/{t}", "name": c.get("lbl", ""),
                     "filename": c.get("fn", ""), "res": c.get("res", 0),
                     "seek": _seek_cost(c)})
    return {"fill": _fill_state(session), "standby": rows}


def _buffer_usable(session: dict) -> bool:
    """Buffer this request unless it would need a *new* cache file on a volume
    with no room for one. A stream already being buffered keeps its entry; a
    new one passes straight through until space is back."""
    cache_id = session.get("bufcid") or session.get("bufsig")
    e = _entries.get(cache_id) if cache_id else None
    if e is not None and not e.failed:
        return True
    if _room_to_write():
        return True
    logger.warning("bufcache: cache volume under its free-space floor; "
                   "serving this stream unbuffered")
    return False


# ── warm: the top pick, ready before Play ────────────────────────────────────
# A player knows what is about to be played well before Play is pressed (a
# details page: 47 s measured between opening a card and pressing Play). The
# cold-start cost that gates the first frame is the container index at EOF —
# 7.68 s cold on the usenet lane — and the start gate's own verdict. Both can
# be paid in that gap.
#
# A HEAD cannot do it (it only probes headers) and a ranged GET must not: a
# short open at byte 0 followed by silence is precisely what the player-
# rejection detector scores as "the player can't open this file". So a warm is
# its own request, and its whole contract is that it is not playback.
_warming: set[str] = set()              # tokens whose warm is choosing now
_warmed_at: dict[str, float] = {}       # token -> monotonic time of last warm


def warm(token: str) -> tuple[int, dict]:
    """Get the release this token would play ready: choose it through the
    normal start gate, start its producer — which warms the tail (the index)
    and reads ahead at most WARM_AHEAD_BYTES — and answer at once while that
    runs in the background.

    No consumer, no false start, no play record, no prefetch, no verdict: a
    candidate that fails a warm is not struck or cooled ("out of time is
    nobody's fault"). Once the warm has its bytes, or the idle grace runs out
    (BUFFER_IDLE_GRACE_RATIONED on Easynews), the producer lets go of the
    source; the first real request clears the warm and picks it back up.

    Idempotent and cheap to repeat: an existing entry is reported without any
    work, a warm already choosing is joined, and a token is warmed at most
    once per _WARM_MIN_INTERVAL. Returns (status, body) for the route."""
    session = _lookup(token)
    if session is None:
        return 410, {"state": "expired"}
    if not (WARM and PROXY_BUFFER) or session.get("hls"):
        return 204, {}
    cache_id = session.get("bufcid") or session.get("bufsig")
    e = _entries.get(cache_id) if cache_id else None
    if e is not None and not e.failed and not e.playfail.get("rejected"):
        return 200, _fill_state(session)
    if token in _warming:
        return 202, {"state": "warming"}
    now = time.monotonic()
    since = now - _warmed_at.get(token, float("-inf"))
    if since < _WARM_MIN_INTERVAL:
        return 429, {"state": "rate-limited",
                     "retry_after": math.ceil(_WARM_MIN_INTERVAL - since)}
    if len(_warming) >= _WARM_MAX_INFLIGHT:
        return 429, {"state": "busy", "retry_after": 5}
    if not _buffer_usable(session):
        return 204, {}
    if len(_warmed_at) > 1024:
        for t, at in list(_warmed_at.items()):
            if now - at >= _WARM_MIN_INTERVAL:
                _warmed_at.pop(t, None)
    _warmed_at[token] = now
    _warming.add(token)
    task = asyncio.create_task(_warm(token, session))
    _bg_tasks.add(task)
    task.add_done_callback(_reap_task)
    return 202, {"state": "warming"}


async def _warm(token: str, session: dict) -> None:
    try:
        e = await _get_or_start_entry(token, session, session["cands"], True)
        if e is None:
            logger.info(f"proxy warm: nothing started for {session.get('id')!r} "
                        f"(no verdicts recorded)")
    except Exception:
        logger.exception("proxy warm failed")
    finally:
        _warming.discard(token)


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
    # Where this player is reading. Every byte-range request for this token
    # passes through here, so the last offset plus what we then deliver against
    # it is the read head — the basis for a real resume point rather than a
    # count of bytes shipped.
    entry["pos"] = offset
    cands = entry["cands"]

    if range_header and not had_range:
        return Response(status_code=416, headers={"Accept-Ranges": "bytes"})
    if had_range and end is not None and end < offset:
        headers = {"Accept-Ranges": "bytes"}
        if entry.get("total") is not None:
            headers["Content-Range"] = f"bytes */{entry['total']}"
        return Response(status_code=416, headers=headers)
    if (had_range and entry.get("total") is not None
            and offset >= entry["total"] and _suffix_length(range_header) is None):
        return Response(status_code=416,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Range": f"bytes */{entry['total']}"})

    # Suffix ranges address the tail of the file, so they cannot be represented
    # by the byte-zero sequential cache until the total is known.  Forward them
    # untouched, validate the returned tail, and never fail over to another
    # release merely because its URL happens to sit later in this token.
    suffix = _suffix_length(range_header)
    if suffix is not None:
        if suffix <= 0:
            return Response(status_code=416, headers={"Accept-Ranges": "bytes"})
        cache_id = entry.get("bufcid") or entry.get("bufsig")
        cached = _entries.get(cache_id) if cache_id else None
        if cached is not None:
            # A suffix range is the classic on-open index read; the warmed
            # tail answers it from memory.
            if cached.total is not None:
                tail = _tail_response(cached, max(0, cached.total - suffix),
                                      None)
                if tail is not None:
                    return tail
            return await _serve_direct(entry, cached.cands, request, token,
                                       source=cached.source,
                                       expected_total=cached.total)
        source = _selected_candidate(entry, cands)
        source = _skip_rejected(source, cands)
        if source is None:
            return Response(status_code=409, headers={"Accept-Ranges": "bytes"})
        pool = [c for c in (entry.get("pool") or cands)
                if _cache_id(c) == _cache_id(source)]
        return await _serve_direct(entry, pool, request, token, source=source)

    if PROXY_BUFFER and _buffer_usable(entry):
        try:
            response = await _serve_buffered(token, entry, cands, offset, end,
                                             had_range, request)
            # _serve_buffered has selected/reused a source that passed the proxy
            # start gate. Trigger only now, not on the player's speculative first
            # request before we know any link can start.
            if offset == 0 and response.status_code < 400:
                selected = _selected_candidate(entry, cands) or {}
                _schedule_prefetch(entry, selected.get("sig", ""))
            return response
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
        _pin_selection(token, entry, cands, cand)
        _schedule_prefetch(entry, cand.get("sig", ""))
    else:                                          # seek: locked to pinned source
        cand = _selected_candidate(entry, cands)
        if cand is None:
            return Response(status_code=409, headers={"Accept-Ranges": "bytes"})
        idx = cands.index(cand)
        try:
            resp = await _send(cand, range_header)
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
