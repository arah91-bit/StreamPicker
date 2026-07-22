"""Source orchestration for two sibling addons that share one search.

Both pickers verify candidates by actually pulling video bytes (see probe.py)
and hand Nuvio a list where "just pick the first one" is safe. They differ in
temperament:

  * pick()      — the *fast* picker. Optimises for a verified answer as soon
                  as possible: a "fast lane" AIOStreams config can settle the
                  request in a few seconds, and even the full path bails the
                  moment it has enough. Bounded by TOTAL_DEADLINE.

  * pick_slow() — the *best quality* picker. Optimises for the best stream
                  that actually plays: it gives slower sources most of the
                  request window, probes the top prospects together, and after
                  one verifies lets the wave settle briefly before answering.
                  Bounded by SLOW_TOTAL_DEADLINE; unfinished leaders continue
                  in the background.

Crucially they do NOT search independently. Every upstream call goes through
app.sources, which searches each title at most once and lets both pickers (and
retries, and other viewers) join that one search. That is what keeps the slow
picker from doubling API calls and tripping upstream rate limits — its job is
to dig through the fast picker's search results, not to launch its own.

Candidates are ranked best-quality-first before probing — by measured video
detail (codec-adjusted bitrate: size- and probe-derived, since resolution and
"REMUX"/"BluRay" labels are only claims), then resolution, then source tier a
la TRaSH guides (Remux > BluRay > WEB-DL > WEBRip > HDTV) — so the result is
the best quality that actually plays, not just anything that plays.

Whenever a deadline forces a checking notice or a partial verified answer, a
background task finishes verification and caches it — a retry or the next
household viewer gets the verified list instantly. A probe of an nzbdav-backed
stream also has a useful side effect: it forces nzbdav to fetch the opening
segments, so by the time the user presses play the slow first byte is paid for.
"""

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import time
from contextvars import ContextVar
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from app import (acquire, anime, candidate_health, content_identity,
                 decode_health, library, meta, probe, reputation, sources,
                 tbcache, telemetry,
                 usenet as nzb_lane, vprobe)

logger = logging.getLogger("stream-picker")


def _env_bool(name: str, default: bool = True) -> bool:
    """Parse the common human spellings used for boolean environment knobs."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


# Public base of this addon, used to build the "being added" notice video URL.
ADDON_PUBLIC_URL = os.environ.get("ADDON_PUBLIC_URL",
                                  "http://localhost:8011").rstrip("/")
NOTICE_URL = os.environ.get("NOTICE_URL") or f"{ADDON_PUBLIC_URL}/notice.mp4"

# The fast picker is completion-first, not clock-first: it returns immediately
# when the first verified, language-eligible 1080p/4K source passes. Two to ten
# seconds is the user-facing target, not a stop condition. The only clock limit
# is the player-safe outer ceiling; until then it keeps looking for one verified
# playable source rather than falling back to an unverified candidate.
TOTAL_DEADLINE = float(os.environ.get("TOTAL_DEADLINE", "55"))
# Metadata is important for identity/language, but cannot consume the whole fast
# window before a byte probe even starts. Searches already run concurrently.
FAST_METADATA_BUDGET = float(os.environ.get("FAST_METADATA_BUDGET", "4"))
PROBE_TTFB_MAX = float(os.environ.get("PROBE_TTFB_MAX", "12"))
USENET_TTFB_MAX = float(os.environ.get("USENET_TTFB_MAX", "35"))
# The hard TTFB gates above only reject sources that won't start at all. This
# soft gate is the "good start" bar used when *ranking* verified streams: a
# source slower than this to deliver its first byte still qualifies (a slow
# start beats no stream) but sorts below any prompt-starting source, because a
# high TTFB reliably predicts a congested host that will stutter for the viewer
# even when our probe had bandwidth to spare. See _delivery_key.
GOOD_TTFB = float(os.environ.get("GOOD_TTFB", "4.0"))
# Two verified encodes whose bitrates land within this relative band count as
# the same quality, so measured delivery speed — not a rounding-error of size —
# decides between them. See _qbps_bucket.
QUALITY_BAND = max(0.01, float(os.environ.get("QUALITY_BAND", "0.15")))
VERIFIED_WANT = int(os.environ.get("VERIFIED_WANT", "2"))
MAX_PROBES = int(os.environ.get("MAX_PROBES", "6"))
# A host that keeps failing probes within one pick (a scraper addon's dead
# mirror domain, a trickling free CDN) is benched for the rest of that pick
# after this many failures with zero passes — its remaining copies are skipped
# so the probe budget goes to other hosts. Per-request only; 0 disables.
PROBE_HOST_BENCH = int(os.environ.get("PROBE_HOST_BENCH", "3"))
# ── fast-race sufficiency bar ────────────────────────────────────────────────
# The fast picker does not privilege any one source. It fires every source at
# once, probes candidates best-quality-first as each source lands, and returns
# the moment it has a "good enough" verified set — whichever source produced it.
# Good enough = one verified high-quality stream that actually plays. The
# fuller search keeps running and is finished into cache in the background, so a
# return visit (or the slow addon) gets the best stream immediately.
ENOUGH_4K = int(os.environ.get("FAST_ENOUGH_4K", "1"))
ENOUGH_1080 = int(os.environ.get("FAST_ENOUGH_1080", "1"))
# Safety-net grace: once the fast race holds a verified stream that definitely
# plays, this is how long it keeps looking for a genuinely better one before
# answering with the best it has. A verified 1080p/4K still returns at once (see
# _enough); this only bounds the chase for unproven "HD" labels that may never
# verify. The slow picker does the patient, thorough pass. Seconds.
FAST_VERIFIED_GRACE = float(os.environ.get("FAST_VERIFIED_GRACE", "5"))
# Until the race holds its first verified stream (the grace timer can't start
# without one), probe the fast-to-verify candidates first — direct debrid/HTTP
# links that first-byte in ~1s — ahead of a same-or-better NZB that needs a
# mount assembled before it can answer. This gets a floor result verified sooner
# on a hard title, so the viewer gets a decent stream in seconds while the
# background finisher and slow picker upgrade the cache to the best NZB. Once
# something has verified, ordering reverts to strict best-quality-first.
FAST_SPEED_FIRST = os.environ.get("FAST_SPEED_FIRST", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
PROBE_BATCH = int(os.environ.get("FAST_PROBE_BATCH", "3"))
# Player-safety cap only; the normal stop condition is the first good verified
# source, however long that takes within the addon's request window.
FAST_RACE_DEADLINE = float(os.environ.get("FAST_RACE_DEADLINE", "55"))
CACHE_TTL = float(os.environ.get("CACHE_TTL", str(6 * 3600)))
# Release catalogs can live for hours, but a signed playback URL must not be
# treated as freshly verified for that long. A result list holds for three
# hours — long enough that a prefetched next episode survives even the longest
# episode's playback — but its leader is re-probed on any access past the much
# shorter freshness window below. Past three hours, old verified candidates are
# retained only as revalidation/search hints; no expired proof reaches the player.
RESULT_CACHE_TTL = min(CACHE_TTL, float(os.environ.get("RESULT_CACHE_TTL", "10800")))
CACHE_REVERIFY_AFTER = min(
    RESULT_CACHE_TTL, float(os.environ.get("CACHE_REVERIFY_AFTER", "120")))
CACHE_REVERIFY_TTFB = float(os.environ.get("CACHE_REVERIFY_TTFB", "8"))
# Keep the old result list in memory for one additional day as probe hints. It
# can never be served from this tier; each URL must pass again, while release
# success/quality evidence survives durably for 30 days in candidate_health.
STALE_EVIDENCE_TTL = max(CACHE_TTL, RESULT_CACHE_TTL + 24 * 3600)
# How long a background finisher waits for the (slow) usenet search to finish.
USENET_FINISH_WAIT = float(os.environ.get("USENET_FINISH_WAIT", "3600"))

# Size-less candidates cannot derive a real average bitrate.  Give the probe a
# conservative per-resolution estimate instead of letting a nominal 4K stream
# auto-lead after clearing only the generic unknown-size floor.
UNKNOWN_NEED_2160 = float(os.environ.get("UNKNOWN_NEED_2160", "8000000"))
UNKNOWN_NEED_1080 = float(os.environ.get("UNKNOWN_NEED_1080", "2500000"))
UNKNOWN_NEED_720 = float(os.environ.get("UNKNOWN_NEED_720", "1000000"))
UNKNOWN_NEED_480 = float(os.environ.get("UNKNOWN_NEED_480", "500000"))

# ── slow picker knobs ───────────────────────────────────────────────────────
# Still bounded by Nuvio's 60s ceiling, but spends the whole budget waiting for
# sources and probing harder than the fast picker.
SLOW_TOTAL_DEADLINE = float(os.environ.get("SLOW_TOTAL_DEADLINE", "55"))
# Time held back from the source wait so there's always room to probe.
SLOW_PROBE_RESERVE = float(os.environ.get("SLOW_PROBE_RESERVE", "18"))
# Foreground first-byte patience. Bumped high on purpose: the foreground pass is
# already capped by min(this, remaining budget), so a big value just means "spend
# the whole SLOW_TOTAL_DEADLINE gate letting a high-quality but slow-to-unlock
# source (uncached RD, a usenet mount) prove it plays, rather than abandoning it
# at an arbitrary 35s." The gate still returns whatever verified so far — it never
# hangs past the deadline. Pure-quality ranking then puts the best survivor #1.
SLOW_TTFB_MAX = float(os.environ.get("SLOW_TTFB_MAX", "120"))
# The slow picker doesn't need to probe every link — it needs the best few
# *prospects*. Probe about ten distinct releases, primarily in quality order,
# while reserving one exploratory place for each available transport so label-
# only HTTPS or a healthy NZB still gets a chance to produce measured evidence.
SLOW_MAX_PROBES = int(os.environ.get("SLOW_MAX_PROBES", "10"))
SLOW_CONCURRENCY = int(os.environ.get("SLOW_CONCURRENCY", "10"))
# Once one candidate has passed both transport and identity verification, keep
# the rest of the foreground wave alive only this much longer. This catches a
# slightly slower superior encode without letting dead NZBs consume the full
# response window. The background pass retries unfinished leaders patiently.
SLOW_VERIFIED_GRACE = float(os.environ.get("SLOW_VERIFIED_GRACE", "20"))
# The background finisher is off Nuvio's clock, so it digs a little deeper (past
# the foreground slice) and refines the cached best-quality answer for the retry.
SLOW_FINISH_MAX_PROBES = int(os.environ.get("SLOW_FINISH_MAX_PROBES", "10"))
SLOW_FINISH_DEADLINE = float(os.environ.get("SLOW_FINISH_DEADLINE", "240"))
# Off Nuvio's clock, the finisher can be truly patient with first byte: a big
# remux behind an uncached debrid unlock, or a usenet mount still assembling its
# opening articles, may take a minute-plus to hand over byte 0 yet stream fine
# after. This is what lets a high-quality slow source survive verification and,
# on the retry, take #1 by quality — the payoff of the 55s-gate/120s-patience
# split. Bounded by SLOW_FINISH_DEADLINE so it still can't run forever.
SLOW_FINISH_TTFB_MAX = float(os.environ.get("SLOW_FINISH_TTFB_MAX", "120"))
# Direct nzbdav probes honor a longer TTFB in the slow/background picker because
# assembling the opening segments can be slow even when sustained playback is
# excellent. The fast request's player-safe outer ceiling still bounds it.
# After the slow picker gives up and hands a title to Sonarr/Radarr, show the
# "being added" notice fast on retries for this long (the library check still
# overrides it the moment the download lands).
NOTICE_TTL = float(os.environ.get("NOTICE_TTL_SECONDS", str(20 * 60)))
# The "finding best stream" notice is shown only while the background finisher is
# still verifying, so its window is short: long enough to keep rapid re-opens
# cheap (no re-running the foreground probe each poll), short enough that if the
# finisher dies silently a later open re-checks from scratch. A cached verified
# result always overrides it sooner.
CHECKING_NOTICE_TTL = float(os.environ.get("CHECKING_NOTICE_TTL", "180"))
ACQUIRE_FOREGROUND_WAIT = float(os.environ.get("ACQUIRE_FOREGROUND_WAIT", "3"))

# Files some of the household's players can't decode. 10-bit is fine.
_12BIT_RE = re.compile(r"12[\s._-]?bit", re.IGNORECASE)

# Optional operator cap on a stream's average bitrate (file size ÷ runtime).
# Entered in the dashboard as megabits/sec; 0 means unlimited. Stored internally
# as bytes/sec to match the per-profile ceilings below (mobile's 1_500_000 B/s ==
# 12 Mbit/s). 1 Mbit/s = 125_000 bytes/s.
_MAX_BITRATE_MBPS = float(os.environ.get("MAX_BITRATE_MBPS", "0") or 0)
_USER_MAX_BPS = int(_MAX_BITRATE_MBPS * 125_000) if _MAX_BITRATE_MBPS > 0 else None


def _cap_bps(base: int | None) -> int | None:
    """Fold the operator's max-bitrate cap into a profile's own ceiling, keeping
    whichever is tighter. None means uncapped."""
    if _USER_MAX_BPS is None:
        return base
    return _USER_MAX_BPS if base is None else min(base, _USER_MAX_BPS)


PROFILES = {
    "full": {"max_res": 10_000, "max_bps": _cap_bps(None)},
    # phones/tablets on cell data: 1080p cap, file bitrate <= ~12 Mbps
    "mobile": {"max_res": 1080, "max_bps": _cap_bps(1_500_000)},
}

_client = httpx.AsyncClient(timeout=None, headers={"User-Agent": "Stremio"})

_cache: dict[str, tuple[float, list[dict]]] = {}
# Expired result lists are retained up to STALE_EVIDENCE_TTL as *evidence*, never as
# verified answers. Their top links get one bounded revalidation attempt and
# their stable release fingerprints/quality facts warm the fresh search.
_stale_cache: dict[str, tuple[float, list[dict]]] = {}
_runtime_cache: dict[str, tuple[float, float]] = {}
_background: dict[str, asyncio.Task] = {}
# slow cache_key -> (monotonic deadline, kind) for a pending notice, where kind
# is one of "theatrical" (not out yet), "added" (downloading via *arr), or
# "checking" (sources still being verified). Cached-result lookups take
# precedence, so the moment the finisher stores a verified answer it wins.
_notice_until: dict[str, tuple[float, str]] = {}
# Strong refs for fire-and-forget acquire tasks: asyncio only holds weak refs, so
# an unsaved create_task can be garbage-collected mid-run (silently killing the
# Sonarr/Radarr call). Keep the task alive until it finishes.
_acquire_tasks: set[asyncio.Task] = set()

# Streams the fast picker has already probed OK for a title, shared with the slow
# picker (same search, same links). The slow picker folds these into its verified
# tier so it never re-probes a known-good link and — crucially — can never
# declare "no working source" and fire a Sonarr/Radarr request for a title the
# fast addon is at that moment serving a verified stream for. Keyed by the *fast*
# (online) cache_key "{profile}:{media}:{media_id}"; entries expire with CACHE_TTL.
_fast_verified: dict[str, tuple[float, list[tuple[dict, probe.ProbeResult]]]] = {}


def _publish_fast_verified(
        cache_key: str,
        verified: list[tuple[dict, probe.ProbeResult]]) -> None:
    """Record the fast picker's probe-verified streams for the slow picker to
    reuse. Additive and best-effort: keep the union of everything verified for
    this title (the fast race and its background usenet finisher each contribute
    over the life of a search), newest result winning on a URL collision."""
    if not verified:
        return
    merged: dict[str, tuple[dict, probe.ProbeResult]] = {}
    prev = _fast_verified.get(cache_key)
    if prev and time.monotonic() - prev[0] < RESULT_CACHE_TTL:
        for s, r in prev[1]:
            merged[s.get("url")] = (s, r)
    for s, r in verified:
        if not _identity_leader(s):
            continue
        clean = _ingested_stream(s)
        merged[clean.get("url")] = (clean, r)
    _fast_verified[cache_key] = (time.monotonic(), list(merged.values()))
    if len(_fast_verified) > 500:
        _fast_verified.pop(next(iter(_fast_verified)))


def _take_fast_verified(cache_key: str, profile: dict, runtime: float,
                        ) -> list[tuple[dict, probe.ProbeResult]]:
    """The fast picker's still-fresh verified streams for a slow request, keyed
    by the fast cache_key (derived by dropping the slow prefix). Re-checked
    against the caller's profile so a full-fat 4K verified for the desktop addon
    can't leak into a bandwidth-capped mobile answer. Empty if none/expired."""
    hit = _fast_verified.get(cache_key)
    if not hit or time.monotonic() - hit[0] >= RESULT_CACHE_TTL:
        return []
    return [(s, r) for s, r in hit[1]
            if _identity_leader(s) and _usable(s, profile, runtime)]


def _combine_verified(
        *tiers: list[tuple[dict, probe.ProbeResult | None]],
        ) -> list[tuple[dict, probe.ProbeResult | None]]:
    """Merge verified tiers (library, fast-inherited, freshly probed) into one,
    deduped by URL with the first tier winning — pass the most-trusted tier
    first. _assemble re-sorts by delivery, so tier order here is only the
    dedup tie-break, not the final ranking."""
    out: list[tuple[dict, probe.ProbeResult | None]] = []
    seen: set[str] = set()
    for tier in tiers:
        for s, r in tier:
            u = s.get("url")
            if not u or u in seen or not _identity_leader(s):
                continue
            seen.add(u)
            out.append((s, r))
    return out


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _acquire_tasks.add(t)
    t.add_done_callback(_acquire_tasks.discard)


# ── stream classification ───────────────────────────────────────────────────

def _stream_text(s: dict) -> str:
    bh = s.get("behaviorHints") or {}
    return " ".join(filter(None, (s.get("name"), s.get("title"),
                                  s.get("description"), bh.get("filename"))))


_RES_RES = [
    (re.compile(r"2160p|4k|\buhd\b", re.I), 2160),
    (re.compile(r"1080p", re.I), 1080),
    (re.compile(r"720p", re.I), 720),
    (re.compile(r"480p|\bsd\b", re.I), 480),
]

# TRaSH-guides source ordering, roughly: remux > bluray > web-dl > webrip
# > hdtv > dvd > cam/telesync
_SRC_RES = [
    (re.compile(r"remux", re.I), 60),
    (re.compile(r"blu-?ray|bd-?rip|bdmv|\bbr-?rip\b", re.I), 50),
    (re.compile(r"web-?dl|\bweb\b(?!-?rip)", re.I), 40),
    (re.compile(r"web-?rip|web-?mux", re.I), 30),
    (re.compile(r"hdtv", re.I), 20),
    (re.compile(r"dvd-?rip|\bdvd\b", re.I), 15),
    (re.compile(r"\bcam\b|telesync|\b(?:hd|hq)[\s._-]?ts\b|telecine|screener|\bscr\b", re.I), 0),
]

# Cam / telesync / telecine / screener / workprint — never served (a theatrical
# rip is never good enough). Hard-rejected in _usable; their presence also flags
# the "digital release not out yet" case.
_CAMTS_RE = re.compile(
    r"\bcam\b|\bcam-?rip\b|\bhd-?cam\b|telesync|\b(?:hd|hq)[\s._-]?ts\b|telecine|"
    r"\bscreener\b|\bscr\b|\bdvd-?scr\b|workprint|\bpdvd\b", re.I)


def _resolution(s: dict) -> int:
    text = _stream_text(s)
    for rx, res in _RES_RES:
        if rx.search(text):
            return res
    return 480


def _source_rank(s: dict) -> int:
    text = _stream_text(s)
    for rx, rank in _SRC_RES:
        if rx.search(text):
            return rank
    return 25  # unknown source: between webrip and web-dl


# Match one decimal number, not an arbitrary run of digits and dots.  Some
# addons have emitted labels such as ``.2.91 GB``; the old ``[\d.]+`` pattern
# accepted that token and ``float()`` then crashed the entire picker merge.
# The lookbehind also prevents the engine from salvaging the trailing
# ``.91 GB`` from an otherwise-malformed token.  A normal leading-decimal size
# such as ``.75 GB`` remains valid.
_SIZE_RE = re.compile(
    r"(?<![\d.])((?:\d+(?:\.\d+)?|\.\d+))\s*(GB|MB)\b",
    re.IGNORECASE,
)


def _size_bytes(s: dict) -> int | None:
    v = (s.get("behaviorHints") or {}).get("videoSize")
    if v is not None and not isinstance(v, bool):
        try:
            size = int(v)
        except (TypeError, ValueError, OverflowError):
            size = 0
        if size > 0:
            return size
    m = _SIZE_RE.search(_stream_text(s))
    if m:
        mult = 1e9 if m.group(2).upper() == "GB" else 1e6
        try:
            size = int(float(m.group(1)) * mult)
        except (ValueError, OverflowError):
            return None
        return size if size > 0 else None
    return None


# ── duplicate-release identity (what "the same thing" means when probing) ────
# Scraper addons share upstream catalogs, so one file arrives many times with
# different URLs (mirrors, wrappers) and often no filename hint — URL dedup and
# the filename signature both miss it. Probe selection collapses those copies
# via the strongest evidence available per stream. Output lists are NOT deduped:
# every copy is kept as failover/twin material; only probing skips duplicates.
_SIZE_IDENT_MIN = 256 * 1024 * 1024   # sizes below this aren't identifying
_TEXT_IDENT_MIN = 24                  # nor are very short display texts


def _release_ident(s: dict) -> str:
    """Best-effort identity of the underlying *file*, comparable across addons
    within one title's candidate pool. Strongest first: the normalised-filename
    signature; the exact byte size (observed byte-identical — or off by one —
    for the same rip across scraper addons; rounded to absorb that); the
    normalised display text (catches one addon listing the same file twice).
    Empty = no evidence; two such streams are never treated as the same."""
    sig = telemetry.signature(s)
    if sig:
        return sig
    size = _size_bytes(s)
    text = " ".join(filter(None, (s.get("name"), s.get("title"),
                                  s.get("description"))))
    norm = re.sub(r"[^a-z0-9]+", "", text.lower())
    if size and size >= _SIZE_IDENT_MIN:
        # Weak, title-local probe identity only. Size is combined with quality
        # traits so it can collapse scraper copies without ever authorizing
        # proxy byte reuse (the proxy requires its own strong content identity).
        traits = f"{_resolution(s)}:{_source_rank(s)}:{_codec_factor(text):.1f}"
        return f"weak:{round(size / 4096)}:{traits}"
    if len(norm) >= _TEXT_IDENT_MIN:
        return "text:" + hashlib.sha256(norm.encode()).hexdigest()
    return ""


def _probe_host(s: dict) -> str:
    """Hostname a probe of this stream would hit, for the per-pick host bench.
    Direct-usenet mounts are exempt: they all sit behind the one nzbdav host,
    and their health is already managed per-indexer by usenet_health."""
    if _is_direct_nzb(s):
        return ""
    try:
        return (urlsplit(s.get("url") or "").hostname or "").lower()
    except ValueError:
        return ""


def _systemic_probe_failure(reason: str) -> bool:
    """True only for evidence reasonably attributable to the whole host."""
    low = (reason or "").lower()
    if any(x in low for x in ("connecterror", "connecttimeout", "pooltimeout",
                              "name or service", "dns", "http 429")):
        return True
    m = re.search(r"http\s+(\d{3})", low)
    return bool(m and int(m.group(1)) >= 500)


# ── TRaSH-guides quality scoring (https://trash-guides.info) ─────────────────
# Radarr/Sonarr sort a release by Quality (resolution, then source: Remux >
# BluRay > WEB-DL > …) and only then by the sum of custom-format scores. We
# mirror that: resolution and source tier stay the dominant sort keys above,
# and everything below (HDR, audio, release-group tier, repack, minus the
# "unwanted" penalties) rolls into a single custom-format score that breaks
# ties within the same resolution+source — exactly TRaSH's ordering.

_HDR_RES = [
    # (regex, score) — checked in order, first match wins. Dolby Vision with an
    # HDR10 base plays everywhere and ranks top; bare DV (no fallback) is still
    # high but riskier on non-DV screens; then HDR10+ > HDR10 > HDR > PQ > HLG.
    (re.compile(r"\b(dv|dovi|dolby[\s._-]?vision)\b.*(hdr10\+|hdr10|\bhdr\b)"
                r"|(hdr10\+|hdr10|\bhdr\b).*\b(dv|dovi|dolby[\s._-]?vision)\b",
                re.I), 1000),
    (re.compile(r"\b(dv|dovi|dolby[\s._-]?vision)\b", re.I), 700),
    (re.compile(r"hdr10\+|hdr10plus", re.I), 500),
    (re.compile(r"hdr10\b", re.I), 400),
    (re.compile(r"\bhdr\b", re.I), 350),
    (re.compile(r"\bpq\b", re.I), 250),
    (re.compile(r"\bhlg\b", re.I), 150),
]

# Purple/green-tint guard for the household's non-DV displays. Bare Dolby Vision
# (typically Profile 5) has no HDR10 base layer, so a non-DV screen renders its
# raw ICtCp color as a purple/pink tint; DV that also carries an HDR10 base
# (Profile 8.x) falls back to clean HDR10 on the same screens. _DV_RE / _HDR_BASE_RE
# mirror the _HDR_RES 700-vs-1000 split above, so what ranks as "risky DV" is
# exactly what gets dropped. Env-tunable: "bare" (default) drops only DV with no
# HDR10 base; "all" drops every DV stream (use if a player tints even on Profile 8);
# "off" disables the guard.
DV_REJECT = os.environ.get("DV_REJECT", "bare").strip().lower()
_DV_RE = re.compile(r"\b(dv|dovi|dolby[\s._-]?vision)\b", re.I)
_HDR_BASE_RE = re.compile(r"hdr10\+|hdr10|\bhdr\b", re.I)


def _dv_rejected(text: str) -> bool:
    """True when a Dolby Vision stream should be dropped for non-DV displays."""
    if DV_REJECT == "off" or not _DV_RE.search(text):
        return False
    if DV_REJECT == "all":
        return True
    return not _HDR_BASE_RE.search(text)  # "bare": keep DV that falls back to HDR10

_AUDIO_RES = [
    # TRaSH "Audio Advanced" default scores, best-first (order matters:
    # atmos/x variants before their base codec).
    (re.compile(r"true[\s._-]?hd.*atmos|atmos.*true[\s._-]?hd", re.I), 5000),
    (re.compile(r"\bdts[\s._-]?x\b|dts[\s._-]?hd[\s._-]?x", re.I), 4500),
    (re.compile(r"(e-?ac-?3|ddp?\+?|dd\+).*atmos|atmos.*(e-?ac-?3|ddp)", re.I), 3000),
    (re.compile(r"\batmos\b", re.I), 3000),
    (re.compile(r"true[\s._-]?hd", re.I), 2750),
    (re.compile(r"dts[\s._-]?hd[\s._-]?ma|dts[\s._-]?ma\b", re.I), 2500),
    (re.compile(r"\bflac\b", re.I), 2250),
    (re.compile(r"\bl?pcm\b", re.I), 2250),
    (re.compile(r"dts[\s._-]?hd(?:[\s._-]?hra?)?\b", re.I), 2000),
    (re.compile(r"\be-?ac-?3\b|\bddp\b|\bdd\+", re.I), 1750),
    (re.compile(r"dts[\s._-]?es\b", re.I), 1500),
    (re.compile(r"\bdts\b", re.I), 1250),
    (re.compile(r"\bac-?3\b|\bdd\b|dolby[\s._-]?digital", re.I), 1000),
    (re.compile(r"\baac\b", re.I), 200),
    (re.compile(r"\bopus\b", re.I), 100),
    (re.compile(r"\bmp3\b", re.I), 50),
]

# A light-touch slice of TRaSH release-group tiers: internal/scene groups whose
# encodes are consistently trusted (bonus) vs. groups TRaSH flags as low quality
# (penalty). Not the full list — just the high-signal names.
_GOOD_GROUPS = re.compile(
    r"\b(framestor|bizkit|3l|terminal|flux|ntb|ntg|cinephiles|hymson|"
    r"ctrlhd|tommy|smurf|kings|d-z0n3|beyondhd|w4nk3r|hdmania|sicfoi|"
    r"pmtp|hdt|decibel)\b", re.I)
_BAD_GROUPS = re.compile(
    r"\b(yts|yify|rarbg|megusta|tgx|galaxyrg|ion10|ion265|psa|shqrip|"
    r"telly|d3si|mrn|nahom|bonsai|memento|afg|fgt|evo)\b", re.I)

_REPACK_RE = re.compile(r"\b(repack|proper)\b", re.I)
_3D_RE = re.compile(r"\b3d\b|half-?sbs|full-?sbs|\bh-?sbs\b", re.I)
_BRDISK_RE = re.compile(r"\bbr-?disk\b|\bbdmv\b|\biso\b|complete[\s._-]?blu-?ray"
                        r"|blu-?ray[\s._-]?(disc|untouched)|\bavc\b.*\bvc-?1\b", re.I)
_UPSCALE_RE = re.compile(r"upscal|\b\.?ai\.?\b.*(enhanc|upscal)", re.I)
_HEVC_RE = re.compile(r"\bx265\b|\bhevc\b|\bh\.?265\b", re.I)
_AV1_RE = re.compile(r"\bav1\b", re.I)


def _cf_score(s: dict) -> int:
    """Sum of TRaSH-style custom-format scores: HDR + audio + group tier +
    repack, minus 'unwanted' penalties. Used only to break ties within the
    same resolution and source tier."""
    text = _stream_text(s)
    score = 0
    for rx, val in _HDR_RES:
        if rx.search(text):
            score += val
            break
    for rx, val in _AUDIO_RES:
        if rx.search(text):
            score += val
            break
    if _GOOD_GROUPS.search(text):
        score += 300
    if _BAD_GROUPS.search(text):
        score -= 500
    if _REPACK_RE.search(text):
        score += 50
    # BR-DISK / 3D / upscaled are rejected in _usable(); the soft signals below
    # stay playable but rank a little lower within their resolution+source tier.
    if _AV1_RE.search(text):
        score -= 200         # decode support still patchy on some players
    if _HEVC_RE.search(text) and _resolution(s) < 2160:
        score -= 150         # HEVC is expected at 4K, less wanted at 1080p/below
    return score


# ── bitrate-plausibility (anti fake-4K) ──────────────────────────────────────
# A file's average bitrate is the strongest cheap signal of *real* quality: an
# over-compressed or AI-upscaled "2160p" carries far too few bits to actually be
# 2160p. For ranking we cap a stream's resolution at the highest one its bitrate
# can justify (never above what it claims), so a starved 1.6 GB "4K" sorts as the
# ~1080p (or lower) it effectively is and a fat, honest 1080p wins. Thresholds
# are AVC-equivalent minimum bitrates; HEVC/AV1 get a discount since they need
# fewer bits for the same look. All tunable via env.
_RES_MIN_BPS = [
    (2160, float(os.environ.get("MIN_BPS_2160", str(10_000_000)))),
    (1080, float(os.environ.get("MIN_BPS_1080", str(3_500_000)))),
    (720,  float(os.environ.get("MIN_BPS_720",  str(1_200_000)))),
    (480,  0.0),
]

# A resolution *claim* with no bitrate evidence at all (free HTTP addons often
# ship no size, no filename, nothing measurable) ranks no higher than this
# until something measures it — an HLS playlist declaration, ffprobe, or a
# size. Debrid/NZB candidates always carry sizes, so this only holds back the
# unverifiable "trust me it's 4K" labels the scraper addons are full of.
UNPROVEN_MAX_RES = int(os.environ.get("UNPROVEN_MAX_RES", "1080"))


def _codec_factor(text: str) -> float:
    """How many AVC bits one of this file's bits is 'worth' — HEVC/AV1 look the
    same as AVC at a lower bitrate, so their bitrate counts for more."""
    if _AV1_RE.search(text):
        return 2.0
    if _HEVC_RE.search(text):
        return 1.7
    return 1.0


def _video_bps(s: dict, runtime: float) -> float | None:
    """Best estimate of the *video* bitrate. When we know the true video rate
    (library files, from Jellyfin — `_vbitrate`) use it exactly; that's the
    signal that matters, since a big file bloated with 10 audio dubs has a high
    *overall* bitrate but mediocre video. Otherwise fall back to the overall
    bitrate (size ÷ runtime), which for online sources is all we have without
    demuxing — a coarse proxy that still catches the starved fake-4K case."""
    vbr = s.get("_vbitrate")
    if vbr:
        return float(vbr)
    size = _size_bytes(s)
    if not size or runtime <= 0:
        return None
    return size * 8 / runtime


def _height_tier(height: int) -> int:
    """Resolution tier a measured pixel height belongs to (conservative)."""
    if height >= 2000:
        return 2160
    if height >= 1000:
        return 1080
    if height >= 700:
        return 720
    return 480


def _effective_resolution(s: dict, runtime: float) -> int:
    """Resolution to *rank* by: the claimed resolution capped at the highest one
    the file's (codec-adjusted) video bitrate can actually justify. A measured
    pixel height (an HLS variant's declared RESOLUTION, `_vheight`) caps the
    claim outright. Unknown bitrate -> the claim counts only up to
    UNPROVEN_MAX_RES: a bare "4K" label is not evidence of 4K."""
    claimed = _resolution(s)
    height = s.get("_vheight")
    if height:
        claimed = min(claimed, _height_tier(int(height)))
    vbps = _video_bps(s, runtime)
    if vbps is None:
        return min(claimed, UNPROVEN_MAX_RES)
    avc_equiv = vbps * _codec_factor(
        " ".join(filter(None, (_stream_text(s), s.get("_vcodec")))))
    for res, min_bps in _RES_MIN_BPS:
        if avc_equiv >= min_bps:
            return min(claimed, res)
    return min(claimed, 480)


def _annotate_quality(streams: list[dict], runtime: float) -> None:
    """Stamp each stream with the two runtime-derived sort inputs — its
    bitrate-capped effective resolution and the bitrate we rank on within a
    resolution (`_qbps`: true video bitrate when known, else overall) — so the
    sort key below, which has no runtime, can rank on real quality. Idempotent."""
    for s in streams:
        # Historical media facts are stable for a release and useful for probe
        # ordering, but never mark a fresh URL verified. A later live probe
        # overwrites these hints with current evidence.
        hint = candidate_health.quality_hint(s)
        if hint:
            _apply_probe_quality(s, SimpleNamespace(**hint), runtime)
        s["_prior_success"] = candidate_health.prior_success(s)
        s["_qbps"] = _video_bps(s, runtime) or 0
        s["_effres"] = _effective_resolution(s, runtime)


def _apply_probe_quality(s: dict, r, runtime: float) -> None:
    """Fold what the probe learned about the *content* into the ranking
    annotations, then re-rank: an HLS master playlist's declared variant
    bandwidth/resolution/codecs (turns a labels-only "4K" into the 720p its
    own playlist admits to), and the real codecs ffprobe'd from the probe's
    bytes for direct files (feeds the learned decode-compatibility demotion)."""
    bps = getattr(r, "media_bps", 0)
    height = getattr(r, "media_height", 0)
    codecs = (getattr(r, "media_codecs", "") or "").lower()
    acodecs = getattr(r, "acodecs", ()) or ()
    audio_langs = getattr(r, "audio_langs", ()) or ()
    vcodec = (getattr(r, "vcodec", "") or "").lower()
    content_kind = getattr(r, "content_kind", "") or ""
    if not bps and not height and not acodecs and not audio_langs and not vcodec \
            and not content_kind:
        return
    if bps:
        # Declared BANDWIDTH is *peak* video+audio — biased high, so it only
        # demotes clear fakes, never a marginal honest encode.
        s["_vbitrate"] = float(bps)
    if height:
        s["_vheight"] = int(height)
    if any(c in codecs for c in ("hvc1", "hev1", "hevc")) or vcodec == "hevc":
        s["_vcodec"] = "hevc"
    elif "av01" in codecs or vcodec == "av1":
        s["_vcodec"] = "av1"
    if acodecs:
        s["_acodecs"] = list(acodecs)
    if audio_langs:
        s["_audio_langs"] = list(audio_langs)
    if vcodec:
        s["_vcodec_real"] = vcodec
    if content_kind:
        s["_content_kind"] = content_kind
    s["_qbps"] = _video_bps(s, runtime) or 0
    s["_effres"] = _effective_resolution(s, runtime)


def _apply_probe_evidence(s: dict, r, runtime: float) -> bool:
    """Fold transport/media evidence into one candidate and return #1 eligibility.

    Runtime can corroborate an exact, otherwise ambiguous title. It cannot
    rescue unknown text or an explicit wrong title/year/episode; that one-way
    rule lives in :func:`content_identity.assess`.
    """
    _apply_probe_quality(s, r, runtime)
    assessment = _assess_stream_identity(
        s, getattr(r, "media_secs", 0.0) or None, record=True)
    accepted = _identity_leader(s)
    if accepted:
        # A verified pack is reusable evidence, not a reusable episode URL.
        # Persist only its credential-free mount locator; the Usenet lane will
        # still resolve and probe an exact member for every sibling episode.
        candidate_health.remember_verified_pack(s)
        if s.get("_nzb_pack"):
            telemetry.record_cache_event(
                ("pack_member_reused" if s.get("_nzb_pack_seeded")
                 else "pack_member_verified"), count=1)
    else:
        telemetry.record_cache_event(
            "transport_ok_identity_rejected", count=1,
            detail=(f"{s.get('_source_key') or 'unknown'}:"
                    f"{assessment.state}:{assessment.evidence}:"
                    f"{int(s.get('_effres') or _resolution(s) or 0)}p"))
    return accepted


# Slow picker only: how many of the current best candidates to ffprobe for their
# true video bitrate, and the minimum time budget worth starting it for.
SLOW_VBR_N = int(os.environ.get("SLOW_VIDEO_PROBE_N", "4"))
VBR_MIN_BUDGET = float(os.environ.get("SLOW_VIDEO_PROBE_MIN_BUDGET", "6"))


async def _refine_video_bitrate(verified: list[tuple[dict, probe.ProbeResult]],
                                runtime: float, budget: float) -> None:
    """Measure the true video bitrate of the top few verified candidates (those
    we don't already have an exact rate for — i.e. online streams, not library)
    and re-annotate their effective resolution, so a fat-but-audio-heavy or a
    starved fake-4K encode drops below the genuine best. Bounded and best-effort:
    no ffprobe, tight budget, or a probe failure just leaves the cheap ranking."""
    if not vprobe.enabled() or budget < VBR_MIN_BUDGET:
        return
    ranked = [s for s, _ in sorted(verified, key=_verified_key, reverse=True)]
    targets = [s for s in ranked if s.get("url") and not s.get("_vbitrate")]
    targets = targets[:SLOW_VBR_N]
    if not targets:
        return
    pairs = [(s["url"], _video_bps(s, runtime)) for s in targets]
    try:
        results = await asyncio.wait_for(vprobe.video_bitrates(pairs),
                                         timeout=budget)
    except asyncio.TimeoutError:
        logger.info("video-bitrate refine timed out, keeping cheap ranking")
        return
    for s, vbr in zip(targets, results):
        if vbr:
            s["_vbitrate"] = vbr
            s["_qbps"] = _video_bps(s, runtime) or 0
            s["_effres"] = _effective_resolution(s, runtime)
            candidate_health.remember_stream_quality(s)
            label = " ".join((s.get("name") or "?").split())[:40]
            logger.info(f"vbr refine: {label} -> {vbr / 1e6:.1f} Mbps video,"
                        f" effres {s['_effres']}")


_VERIFIED_STATE_KEY = "_picker_verified"
_NOTICE_STATE_KEY = "_picker_notice"
_IDENTITY_STATE_KEY = "_identity_state"
_IDENTITY_RANK_KEY = "_identity_rank"
_IDENTITY_EVIDENCE_KEY = "_identity_evidence"
_IDENTITY_TEXT_KEY = "_identity_text"
_identity_profile_ctx: ContextVar[content_identity.IdentityProfile | None] = \
    ContextVar("identity_profile", default=None)
_identity_logged_ctx: ContextVar[set[str] | None] = \
    ContextVar("identity_logged", default=None)
# Anime episode expectation for this request (absolute/seasonal/per-cour), or
# None when the title is not mapped anime. Set beside the identity profile.
_anime_ctx: ContextVar["anime.Expectation | None"] = \
    ContextVar("anime_expectation", default=None)
# Identity, rather than a truthy value, makes this impossible to forge through
# an upstream addon's JSON response.  Raw streams are still scrubbed at every
# ingestion boundary so even a same-named private field cannot linger.
_VERIFIED_SENTINEL = object()
_INTERNAL_KEYS = ("_effres", "_vbitrate", "_vheight", "_vcodec", "_vcodec_real",
                  "_acodecs", "_audio_langs", "_content_kind", "_qbps",
                  "_speed", "_ttfb", "_prior_success",
                  _VERIFIED_STATE_KEY, _NOTICE_STATE_KEY,
                  _IDENTITY_STATE_KEY, _IDENTITY_RANK_KEY,
                  _IDENTITY_EVIDENCE_KEY, _IDENTITY_TEXT_KEY,
                  content_identity._AUTO_ELIGIBLE_KEY,
                  "_source_key", "_source_trust",
                  "_library_identity_confidence", "_library_identity_trust",
                  "_library_identity_evidence")


def _ingested_stream(s: dict) -> dict:
    """Copy an upstream stream while discarding picker-owned trust state."""
    out = dict(s)
    out.pop(_VERIFIED_STATE_KEY, None)
    out.pop(_NOTICE_STATE_KEY, None)
    out.pop(content_identity._AUTO_ELIGIBLE_KEY, None)
    return out


def _strip_internal(s: dict) -> dict:
    private_prefixes = ("_nzb_", "_identity_", "_picker_", "_library_",
                        "_source_")
    if any(k in s for k in _INTERNAL_KEYS) or any(
            str(k).startswith(private_prefixes) for k in s):
        return {k: v for k, v in s.items()
                if k not in _INTERNAL_KEYS
                and not str(k).startswith(private_prefixes)}
    return s


def clean_output(streams: list[dict]) -> list[dict]:
    """Strip internal ranking annotations from streams at the HTTP boundary, so
    they never leak to Nuvio but survive internal re-sorts and caching."""
    return [_strip_internal(s) for s in streams]


# ── semantic content identity ───────────────────────────────────────────────
# A byte probe answers "does this URL play?".  It cannot answer "is this the
# requested movie/episode?".  These helpers keep that independent identity gate
# beside the transport gate. The confirmed/unconfirmed state stays the first
# ranking dimension; the finer evidence grade only breaks same-quality ties
# (see _quality_key), so identity proof can never outrank better video.
_IMDB_TAG_RE = re.compile(r"(?<![A-Za-z0-9])(tt\d{7,10})(?!\d)", re.I)
_LEADING_BRACKETS_RE = re.compile(r"^\s*(?:\[[^\]\r\n]{0,100}\]\s*)+")
_LEADING_RANK_RE = re.compile(r"^\s*(?:[\W_]*\d+\s*[·|:]\s*)", re.UNICODE)
_FIELD_LINE_RE = re.compile(
    r"^\s*(?:source|size|audio|language|subtitles?|seeders?|verified)\s*:", re.I)
_RELEASEISH_RE = re.compile(
    r"(?:\.(?:mkv|mp4|m4v|avi|mov|m2ts|ts)\b|\b(?:19|20)\d{2}\b|"
    r"\bS\d{1,3}E\d{1,4}\b|\b\d{3,4}p\b|\b(?:4k|uhd|remux|web-?dl|"
    r"web-?rip|blu-?ray)\b)", re.I)


def _set_identity_profile(profile: content_identity.IdentityProfile | None) -> None:
    """Install one immutable request identity for all child/background tasks."""
    _identity_profile_ctx.set(profile)
    _identity_logged_ctx.set(set())


def _clean_identity_line(value: str) -> str:
    """Remove addon decorations before asking for an exact title boundary."""
    line = str(value or "").strip()
    line = _LEADING_BRACKETS_RE.sub("", line)
    line = _LEADING_RANK_RE.sub("", line)
    # Emoji/icons often precede the actual filename.  ``[^\w]`` is Unicode
    # aware, so native-script title letters are retained.
    line = re.sub(r"^[^\w]+", "", line, flags=re.UNICODE)
    line = re.sub(r"^(?:release|filename|file|title)\s*:\s*", "", line,
                  flags=re.I)
    # A few addons prefix the release with a quality badge and a delimiter.
    line = re.sub(
        r"^(?:(?:4320|2160|1440|1080|720|480)p|4k|8k|uhd|hdr(?:10\+?)?)"
        r"\s*(?:[|:·-]\s*)+", "", line, flags=re.I)
    return line.strip()


def _identity_evidence_strings(s: dict) -> tuple[str, str]:
    """Return (mounted/declared filename, best independent release label).

    The filename is strongest and any explicit contradiction in it wins.  For
    addons that omit ``behaviorHints.filename`` (the real wrong-Ghost-in-the-
    Shell case did), select the most release-like description/title line rather
    than the addon brand or its Size/Source fields.
    """
    filename = _clean_identity_line(
        ((s.get("behaviorHints") or {}).get("filename") or ""))
    if s.get("_source_key") == sources.NZB:
        label = _clean_identity_line(s.get("_nzb_label") or "")
        return filename, label

    groups: list[list[str]] = []
    for field in (s.get("description"), s.get("title"), s.get("name")):
        lines = []
        for raw in str(field or "").splitlines():
            if _FIELD_LINE_RE.search(raw):
                continue
            # Structured addon descriptions often put seed/size badges before
            # the release on the same line, separated with | or ·. Consider
            # each segment so the badge cannot turn a correct title into a
            # false contradiction.
            for segment in re.split(r"\s+[|·]\s+", raw):
                cleaned = _clean_identity_line(segment)
                if cleaned:
                    lines.append(cleaned)
        if lines:
            groups.append(lines)
    if not groups:
        return filename, ""
    # Prefer the first field that supplied content (description, then title,
    # then name), and within it the line that most resembles a release name.
    lines = groups[0]
    profile = _identity_profile_ctx.get()
    aliases = tuple(re.sub(r"[^\w]+", "", a.casefold(), flags=re.UNICODE)
                    for a in (profile.aliases if profile else ()) if a)

    def label_key(value: str) -> tuple[bool, bool, int]:
        folded = re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)
        has_alias = any(alias and alias in folded for alias in aliases)
        return has_alias, bool(_RELEASEISH_RE.search(value)), len(value)

    label = max(lines, key=label_key)
    return filename, label


def _identity_from_text(profile: content_identity.IdentityProfile,
                        filename: str, label: str,
                        measured_runtime_seconds: float | None = None
                        ) -> content_identity.IdentityAssessment:
    """Combine filename/label assessments with contradiction dominance."""
    assessments = [
        content_identity.assess(
            profile, value, measured_runtime_seconds=measured_runtime_seconds)
        for value in (filename, label) if value
    ]
    if not assessments:
        return content_identity.IdentityAssessment(
            content_identity.UNKNOWN, content_identity.EVIDENCE_UNKNOWN, 1)
    contradiction = next((a for a in assessments
                          if a.state == content_identity.CONTRADICTION), None)
    if contradiction:
        return contradiction
    best = max(assessments, key=lambda a: a.rank)

    # A visible exact IMDb tag is useful semantic evidence but is weaker than a
    # validated Newznab attribute/Jellyfin ProviderId.  It can resolve an
    # otherwise compatible/unknown label at canonical rank, never override a
    # title/year/episode contradiction (already handled above).
    tagged = {m.lower() for value in (filename, label)
              for m in _IMDB_TAG_RE.findall(value)}
    if tagged:
        if tagged != {profile.imdb_id}:
            return content_identity.IdentityAssessment(
                content_identity.CONTRADICTION,
                content_identity.EVIDENCE_CONTRADICTION, 0)
        if best.state == content_identity.COMPATIBLE:
            return content_identity.IdentityAssessment(
                content_identity.STRONG,
                content_identity.EVIDENCE_CANONICAL,
                content_identity.EVIDENCE_RANKS[
                    content_identity.EVIDENCE_CANONICAL])
    return best


def _record_identity_once(s: dict,
                          assessment: content_identity.IdentityAssessment,
                          evidence_text: str) -> None:
    seen = _identity_logged_ctx.get()
    digest = hashlib.sha256(
        f"{s.get('_source_key', '')}\0{assessment.state}\0{evidence_text}".encode()
    ).hexdigest()[:24]
    if seen is not None:
        if digest in seen:
            return
        seen.add(digest)
    observed = tuple(sorted({int(y) for y in re.findall(
        r"(?<!\d)((?:19|20)\d{2})(?!\d)", evidence_text)}))
    profile = _identity_profile_ctx.get()
    telemetry.record_identity(
        s, state=assessment.state, reason=assessment.evidence,
        source=str(s.get("_source_key") or ""), evidence=evidence_text,
        expected_years=tuple(sorted(profile.years)) if profile else (),
        observed_years=observed)


def _assess_stream_identity(
        s: dict, measured_runtime_seconds: float | None = None,
        *, record: bool = True) -> content_identity.IdentityAssessment:
    """Classify one candidate, honoring only in-process trusted provenance."""
    profile = _identity_profile_ctx.get()
    # Helper-level unit tests and legacy callers that deliberately do not set a
    # request profile retain their old behavior. Production pick paths always
    # install a profile before ingesting candidates.
    if profile is None:
        result = content_identity.IdentityAssessment(
            content_identity.STRONG,
            content_identity.EVIDENCE_TRUSTED_IMDB,
            content_identity.EVIDENCE_RANKS[
                content_identity.EVIDENCE_TRUSTED_IMDB])
        filename, label = _identity_evidence_strings(s)
    else:
        filename, label = _identity_evidence_strings(s)
        source = s.get("_source_key")
        if (source == sources.NZB and sources.trusted_nzb(s)
                and s.get("_nzb_identity_confidence") == content_identity.STRONG):
            evidence = tuple(s.get("_nzb_identity_evidence") or ())
            trusted_imdb = "newznab-imdb" in evidence
            tier = (content_identity.EVIDENCE_TRUSTED_IMDB if trusted_imdb
                    else content_identity.EVIDENCE_CANONICAL)
            result = content_identity.IdentityAssessment(
                content_identity.STRONG, tier,
                content_identity.EVIDENCE_RANKS[tier])
        elif (source == "library" and library.identity_trusted(s)
              and s.get("_library_identity_confidence")
              == content_identity.STRONG):
            result = content_identity.IdentityAssessment(
                content_identity.STRONG,
                content_identity.EVIDENCE_TRUSTED_IMDB,
                content_identity.EVIDENCE_RANKS[
                    content_identity.EVIDENCE_TRUSTED_IMDB])
        else:
            # ``compatible``/``unknown`` from the trusted Usenet validator are
            # lower fallbacks.  The common parser may still promote compatible
            # exact-title evidence after a measured runtime match.
            result = _identity_from_text(
                profile, filename, label, measured_runtime_seconds)
            nzb_state = (s.get("_nzb_identity_confidence")
                         if source == sources.NZB and sources.trusted_nzb(s)
                         else None)
            if nzb_state == content_identity.UNKNOWN:
                result = content_identity.IdentityAssessment(
                    content_identity.UNKNOWN,
                    content_identity.EVIDENCE_UNKNOWN, 1)
            elif nzb_state == content_identity.COMPATIBLE \
                    and result.state == content_identity.STRONG \
                    and result.evidence != content_identity.EVIDENCE_RUNTIME:
                result = content_identity.IdentityAssessment(
                    content_identity.COMPATIBLE,
                    content_identity.EVIDENCE_COMPATIBLE, 2)

    result = _anime_override(result, s, filename, label)

    s[_IDENTITY_STATE_KEY] = result.state
    s[_IDENTITY_RANK_KEY] = result.rank
    s[_IDENTITY_EVIDENCE_KEY] = result.evidence
    s[_IDENTITY_TEXT_KEY] = filename or label
    if record:
        _record_identity_once(s, result, " | ".join(filter(None, (filename, label))))
    return result


def _anime_override(result: content_identity.IdentityAssessment, s: dict,
                    filename: str, label: str) -> content_identity.IdentityAssessment:
    """Reconcile absolute/per-cour anime numbering with the filename verdict.

    Anime releases lead with ``[group]`` and set the episode off with a bare
    absolute number (``- 50``), which the ordinary filename gate can neither
    anchor a title to nor read as an episode — so it wrongly contradicts them.
    When the title is one of this show's known titles (English/romaji/native)
    *and* the episode is confirmed by absolute/per-cour number or episode title,
    promote to strong so the correct release can auto-play; a decisively
    different episode is contradicted.  One-way and conservative: it requires a
    positive show-title match (never rescues a wrong show) and never downgrades a
    trusted source or an already-strong verdict.
    """
    expectation = _anime_ctx.get()
    if expectation is None or s.get("_source_key") == "library":
        return result
    text = " ".join(filter(None, (filename, label)))
    verdict = anime.assess(expectation, text)
    if (verdict == anime.CONTRADICT
            and result.state != content_identity.STRONG):
        return content_identity.IdentityAssessment(
            content_identity.CONTRADICTION, content_identity.EVIDENCE_CONTRADICTION,
            content_identity.EVIDENCE_RANKS[content_identity.EVIDENCE_CONTRADICTION])
    if verdict == anime.CONFIRM and result.state != content_identity.STRONG:
        profile = _identity_profile_ctx.get()
        titles = expectation.show_titles + (profile.aliases if profile else ())
        if (result.state == content_identity.COMPATIBLE
                or anime.title_present(titles, text)):
            return content_identity.IdentityAssessment(
                content_identity.STRONG, content_identity.EVIDENCE_ANIME,
                content_identity.EVIDENCE_RANKS[content_identity.EVIDENCE_ANIME])
    return result


def _annotate_identity(streams: list[dict],
                       measured_runtime_seconds: float | None = None) -> None:
    for stream in streams:
        _assess_stream_identity(stream, measured_runtime_seconds)


def _identity_leader(s: dict) -> bool:
    """Whether this item has enough semantic evidence for automatic playback."""
    if _identity_profile_ctx.get() is None and _IDENTITY_STATE_KEY not in s:
        return True
    return s.get(_IDENTITY_STATE_KEY) == content_identity.STRONG


def _identity_leader_flag(s: dict) -> int:
    """Sortable form of the _identity_leader gate: 1 when the content identity
    is confirmed (STRONG), else 0. This is the only identity component that
    outranks video quality — the finer 0-5 evidence grade (trusted-IMDb vs
    canonical parse) only breaks same-quality ties, so a trusted-indexer NZB
    can no longer float above a higher-quality debrid source on evidence
    alone. See _quality_key."""
    return 1 if _identity_leader(s) else 0


# Hard-coded (burned-in) foreign subtitles — Chinese/Korean-market WEB rips
# (a CJK title prefix like "莫离.", or tags like "Chinese-Esub"/HC/KORSUB). They
# can't be switched off, so the household would rather not see them. DEMOTED
# below every clean release, never rejected — still served if it's the ONLY
# source. Env kill-switch HARDSUB_DEMOTE=0 to disable. Matched on the filename
# (else name+title), NOT the description, so a clean release that merely lists a
# foreign AKA title in its blurb isn't caught.
HARDSUB_DEMOTE = _env_bool("HARDSUB_DEMOTE")
_HARDSUB_RE = re.compile(
    r"hard[\s._-]?sub|hard[\s._-]?cod|\bhc\b|kor[\s._-]?sub|"
    r"burn(?:ed|t)[\s._-]?in|chinese[\s._-]?e?sub|chi[\s._-]?sub|\bchs\b|\bcht\b"
    r"|[぀-ヿ㐀-䶿一-鿿가-힯]",  # any CJK/Kana/Hangul
    re.I)


def _hardsub(s: dict) -> bool:
    if not HARDSUB_DEMOTE:
        return False
    bh = s.get("behaviorHints") or {}
    text = bh.get("filename") or " ".join(
        filter(None, (s.get("name"), s.get("title"))))
    return bool(_HARDSUB_RE.search(text))


# ── audio-language gate ──────────────────────────────────────────────────────
# A stream should carry English and/or the title's original-language audio. A
# release that provably carries *neither* (e.g. an English film with only an
# Italian dub) is demoted below every acceptable one — never removed, since audio
# detection is best-effort, so it still surfaces as a last resort. The acceptable
# set ({en, original}) is resolved once per request via TMDB and held here.
AUDIO_GATE = _env_bool("AUDIO_GATE")
_accept_langs: ContextVar[frozenset | None] = ContextVar("accept_langs", default=None)
_original_lang_known: ContextVar[bool] = ContextVar("original_lang_known", default=True)

# Flag emoji (by country) → spoken language (ISO-639-1); addons tag audio tracks
# with country flags after a 🎙️/🗣️ marker.
_FLAG_LANG = {
    "GB": "en", "US": "en", "AU": "en", "CA": "en", "IE": "en", "NZ": "en",
    "IT": "it", "FR": "fr", "BE": "fr", "DE": "de", "AT": "de", "CH": "de",
    "ES": "es", "MX": "es", "AR": "es", "CO": "es", "RU": "ru", "UA": "uk",
    "JP": "ja", "KR": "ko", "CN": "zh", "TW": "zh", "HK": "zh", "PL": "pl",
    "BR": "pt", "PT": "pt", "NL": "nl", "SE": "sv", "DK": "da", "NO": "no",
    "FI": "fi", "TR": "tr", "GR": "el", "CZ": "cs", "HU": "hu", "RO": "ro",
    "TH": "th", "VN": "vi", "ID": "id", "IN": "hi", "IL": "he", "SA": "ar",
}
# Scene/word language tokens → ISO-639-1. multi/dual are handled separately (OK).
_LANG_WORD = {
    "english": "en", "eng": "en", "italian": "it", "ita": "it", "italiano": "it",
    "french": "fr", "francais": "fr", "truefrench": "fr", "vff": "fr", "vfq": "fr",
    "vf": "fr", "fre": "fr", "fra": "fr", "german": "de", "deutsch": "de",
    "ger": "de", "deu": "de", "spanish": "es", "espanol": "es", "castellano": "es",
    "latino": "es", "spa": "es", "esp": "es", "russian": "ru", "rus": "ru",
    "japanese": "ja", "jpn": "ja", "korean": "ko", "kor": "ko", "chinese": "zh",
    "mandarin": "zh", "cantonese": "zh", "chi": "zh", "polish": "pl", "pol": "pl",
    "portuguese": "pt", "por": "pt", "dutch": "nl", "nld": "nl", "swedish": "sv",
    "danish": "da", "norwegian": "no", "finnish": "fi", "turkish": "tr",
    "czech": "cs", "hindi": "hi", "tamil": "ta", "telugu": "te", "arabic": "ar",
    "hebrew": "he", "thai": "th", "ukrainian": "uk",
}
_FLAG_PAIR_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
# German scene releases use a standalone ``DL`` for dual-language audio.  It
# must not match the format suffix in ``WEB-DL`` or every WEB-DL would bypass
# the language gate as if it were multilingual.
_MULTI_RE = re.compile(
    r"\b(multi|dual[\s._-]?audio|dual|(?<!web[\s._-])dl)\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# The audio-track section of a description starts at 🎙️/🗣️ and ends at the
# subtitle marker 💬 (or a field break) — flags before 💬 are audio, after are subs.
_AUDIO_SEG_RE = re.compile(r"[🎙🗣][️]?(.*?)(?:💬|\Z)", re.S)


def _flag_langs(text: str) -> set[str]:
    out = set()
    for m in _FLAG_PAIR_RE.finditer(text):
        cc = chr(ord(m.group()[0]) - 0x1F1E6 + 65) + chr(ord(m.group()[1]) - 0x1F1E6 + 65)
        if cc in _FLAG_LANG:
            out.add(_FLAG_LANG[cc])
    return out


def _audio_langs(s: dict) -> tuple[set[str], bool]:
    """Best-effort set of audio-track languages (ISO-639-1) for a stream, plus a
    'multi' flag. Reads, in order of reliability: the addon's explicit audio-track
    section (flags/words after 🎙️/🗣️, up to the 💬 subtitle marker), and the
    scene language codes in the *filename after the year* (so a title word like
    'Italian Job' isn't mistaken for Italian audio). Empty set = couldn't tell."""
    desc = s.get("description") or s.get("title") or ""
    name = s.get("name") or ""
    fname = (s.get("behaviorHints") or {}).get("filename") or ""
    # A successful ffprobe is stronger than filename/display heuristics.
    measured = {str(x).lower() for x in (s.get("_audio_langs") or ()) if x}
    if measured:
        return measured, len(measured) > 1
    langs: set[str] = set()
    # Subtitle descriptions often say "Multi Subs".  That is not evidence of
    # multilingual *audio*, so use release-name tags plus the structured audio
    # segment only; never the free-form subtitle tail after 💬.
    multi = bool(_MULTI_RE.search(" ".join(filter(
        None, (name, fname, s.get("_nzb_label"))))))
    # explicit audio-track section(s)
    for m in _AUDIO_SEG_RE.finditer(desc):
        seg = m.group(1)
        multi = multi or bool(_MULTI_RE.search(seg))
        langs |= _flag_langs(seg)
        for w in re.split(r"[^A-Za-z]+", seg.lower()):
            if w in _LANG_WORD:
                langs.add(_LANG_WORD[w])
    # Scene codes in release-name tails (after the year), where audio tags live.
    # Direct nzbdav filenames can be obfuscated, so also inspect the private,
    # authoritative NZB release label that is stripped before the HTTP response.
    for release_name in dict.fromkeys(
            filter(None, (fname, s.get("_nzb_label"), name))):
        ym = None
        for ym in _YEAR_RE.finditer(release_name):
            pass
        tail = release_name[ym.end():] if ym else release_name
        for tok in re.split(r"[^A-Za-z]+", tail.lower()):
            if tok in _LANG_WORD:
                langs.add(_LANG_WORD[tok])
    return langs, multi


def _audio_ok(s: dict) -> int:
    """Audio confidence: 2 confirmed acceptable, 1 unknown, 0 confirmed wrong."""
    accept = _accept_langs.get()
    if not AUDIO_GATE or not accept:
        return 1
    langs, multi = _audio_langs(s)
    if langs & accept:
        return 2
    if not langs or multi:
        return 1
    # When TMDB could not tell us the original language, explicit non-English
    # may itself be the original; demote it below confirmed English, do not drop.
    if not _original_lang_known.get():
        return 1
    return 0


async def _resolve_accept_langs(media: str, media_id: str) -> None:
    """Resolve the acceptable-audio set {English, original-language} for the title
    (via TMDB, cached) and stash it on the request context for _quality_key. Left
    as None — no gating — whenever TMDB can't tell us the original language, so a
    genuinely foreign-original title is never wrongly demoted."""
    orig = None
    try:
        if AUDIO_GATE and meta.enabled():
            orig = await meta.original_language(media, media_id)
    except Exception:
        orig = None
    # English remains positive evidence even if TMDB is unavailable. In that
    # case _audio_ok keeps other explicit languages in the unknown tier because
    # one of them may be the title's original language.
    _original_lang_known.set(bool(orig))
    _accept_langs.set(frozenset({"en", orig}) if orig else frozenset({"en"}))


def _decode_ok(s: dict) -> int:
    """0 when the stream carries a codec attribute the household's players have
    provably rejected (learned in app.decode_health from player-rejected events
    — probe-sniffed codecs when available, else explicit name declarations).
    Demoted below every clean candidate, never removed: another player may
    handle it, and it still surfaces as a last resort."""
    return 0 if decode_health.suspect(_stream_text(s),
                                      s.get("_acodecs") or (),
                                      s.get("_vcodec_real") or "") else 1


def _detail(s: dict, qbps: float) -> float:
    """How much real picture information the stream carries, in AVC-equivalent
    video bits/s: the measured/estimated video bitrate corrected for codec
    efficiency (HEVC/AV1 carry the same detail in fewer bits). This — not the
    resolution label, not the source-tier label — is the primary quality
    measure, because labels lie: an "1080p REMUX" tag on a 4 Mbps file is just
    text, while the size and the probe's HLS/ffprobe findings say what the file
    actually is. Detail re-ranks a starved "4K" WEBRip below an honest fat
    1080p BluRay and a real REMUX (any resolution) above a leaner WEB-DL."""
    text = " ".join(filter(None, (_stream_text(s), s.get("_vcodec"))))
    return qbps * _codec_factor(text)


def _quality_key(s: dict):
    # Order: identity leader gate, audio, decode-ok, clean-first, then video
    # DETAIL (codec-adjusted bitrate), resolution, source tier, custom-format
    # score, the identity evidence grade, then size. Deliberate
    # departures from Radarr's resolution→source→size order, all from the
    # household's feedback:
    #  * identity splits in two: the confirmed/unconfirmed gate
    #    (_identity_leader_flag) stays first — an attractive but ambiguous
    #    release never outranks the known requested work — while the 0-5
    #    evidence grade (trusted-IMDb vs canonical parse) drops below every
    #    video-quality key, so a trusted-indexer NZB no longer beats a
    #    higher-quality debrid source on evidence alone; among confirmed
    #    streams of equal quality it still prefers the cross-validated copy;
    #  * decodability and clean-vs-hardsub are the TOP quality keys, so a release the
    #    player provably can't open (_decode_ok) or with burned-in foreign
    #    subtitles (_hardsub) sorts below every clean one regardless of
    #    resolution — it only surfaces as a last resort;
    #  * detail outranks the resolution label: a 4K WEBRip often carries fewer
    #    real bits than a good 1080p BluRay, and a "REMUX" tag is unverifiable
    #    text until size/probe evidence backs it. Resolution still breaks the
    #    near-ties (more pixels hold more detail at equal bitrate) and the
    #    bitrate-capped effective resolution (_effective_resolution) stays as a
    #    gate so a starved fake/upscaled 4K can't claim its nominal tier; and
    #  * the source label (remux > bluray > web-dl > …) only breaks ties within
    #    the same detail+resolution — a name is a claim, not evidence.
    res = s.get("_effres")
    if res is None:
        res = _resolution(s)
    qbps = s.get("_qbps")
    if qbps is None:
        qbps = 0
    clean = 0 if _hardsub(s) else 1
    # audio_ok is the top quality key under the identity gate: a wrong-language
    # dub (no English, no original) sorts below everything acceptable, whatever
    # its resolution — you can't watch a 4K you don't understand.
    identity_rank = s.get(_IDENTITY_RANK_KEY)
    if identity_rank is None:
        identity_rank = 5 if _identity_profile_ctx.get() is None else 1
    return (_identity_leader_flag(s), _audio_ok(s), _decode_ok(s), clean,
            _detail(s, qbps), res, _source_rank(s), _cf_score(s), identity_rank,
            _size_bytes(s) or 0)


# ── delivery-aware ranking of *verified* streams ─────────────────────────────
# _quality_key ranks candidates on their metadata alone; that's what we probe in.
# But once a stream is verified we also know how it actually delivered — its
# time-to-first-byte and sustained throughput — and those decide between sources
# the metadata calls a tie. Library streams (no probe) are treated as the most
# reliable: an instant start and effectively unbounded speed.
LIBRARY_SPEED = 1e12
_BAND_LOG = math.log(1 + QUALITY_BAND)


def _qbps_bucket(qbps: float) -> int:
    """Coarsen a bitrate (or AVC-equivalent detail score) into ~QUALITY_BAND-wide
    relative buckets, so encodes of effectively-equal quality tie on this key
    and let measured delivery speed — not a rounding-error of size — break
    them, while a genuinely fatter encode still lands in a higher bucket and
    wins outright."""
    if qbps <= 0:
        return 0
    return round(math.log(qbps) / _BAND_LOG)


def _probe_key(s: dict) -> tuple:
    """Candidate-start order, distinct from final quality ordering.

    A release that verified recently gets first crack only among candidates in
    the same codec-adjusted detail band. This reuses good evidence without ever
    letting an old success outrank a materially better encode or count as proof
    that the candidate's current URL still plays.
    """
    leader, audio, decode, clean, detail, res, srank, cf, identity, size = \
        _quality_key(s)
    prior = int(s.get("_prior_success") or candidate_health.prior_success(s))
    return (leader, audio, decode, clean, _qbps_bucket(detail), prior,
            detail, res, srank, cf, identity, size)


def _delivery_key(qkey: tuple, ttfb: float, speed: float):
    """Sort key for a *verified* stream (descending). `clean` (no burned-in
    foreign subs) stays the very top quality component so a clean release always
    outranks a hardsub one, even a slow-starting clean over a fast hardsub — the
    household would rather wait than watch burned-in subtitles. Below that, two
    passes fall out of one key: `good_start` sorts every prompt-starting source
    above every slow-starting one — but when *all* survivors are slow they still
    rank among themselves, so a slow start beats no stream. Within a start class
    we keep measured quality first (a *bucketed* video-detail score so
    near-equal encodes tie, then resolution, source tier and custom-format
    score), then the identity evidence grade as a same-quality tiebreak, and
    only then fall to throughput, so 'when quality is similar, pick the
    faster-streaming one'. Exact detail and size are last, purely deterministic
    tie-breaks."""
    leader, audio, decode, clean, detail, res, srank, cf, identity, size = qkey
    good_start = 1 if ttfb <= GOOD_TTFB else 0
    return (leader, audio, decode, clean, good_start, _qbps_bucket(detail),
            res, srank, cf, identity, speed, detail, size)


def _verified_key(vr: tuple):
    """_delivery_key for a (stream, ProbeResult|None) pair from the probe stage.
    Used by the *fast* picker: among confirmed-playing streams it prefers the
    prompt-starting ones so the household doesn't buffer."""
    s, r = vr
    ttfb = 0.0 if r is None else r.ttfb
    speed = LIBRARY_SPEED if r is None else r.speed_bps
    return _delivery_key(_quality_key(s), ttfb, speed)


def _verified_quality_key(vr: tuple):
    """Best-all-around slow order: hard quality first, delivery inside a band."""
    s, r = vr
    leader, audio, decode, clean, detail, res, srank, cf, identity, size = \
        _quality_key(s)
    ttfb = 0.0 if r is None else r.ttfb
    speed = LIBRARY_SPEED if r is None else r.speed_bps
    good_start = 1 if ttfb <= GOOD_TTFB else 0
    return (leader, audio, decode, clean, _qbps_bucket(detail), res,
            good_start, speed, srank, cf, identity, detail, size)


def _marked_key(s: dict):
    """_delivery_key for an already-marked stream, reading the probe delivery
    stamped onto it by _mark (used when re-sorting after the ProbeResult objects
    are gone, e.g. folding the library in). Unstamped -> treated as slow/unknown."""
    return _delivery_key(_quality_key(s), s.get("_ttfb", 0.0), s.get("_speed", 0.0))


def _usable(s: dict, profile: dict, runtime: float) -> bool:
    if not s.get("url"):          # infoHash-only p2p entries can't be probed
        return False
    # An explicit title/year/episode mismatch is not a low-quality fallback; it
    # is different media and must never be probed, cached, or served.
    if s.get(_IDENTITY_STATE_KEY) == content_identity.CONTRADICTION:
        return False
    # Dropped for real: a release the proxy watched deliver badly (see
    # app.reputation) — 'never used again' until the strike decays or is
    # cleared. A cooled release (just delivered badly, or player-rejected for
    # 24h) is also excluded from fresh picks, so the list the user sees never
    # ranks first something the proxy would refuse to serve anyway.
    sig = telemetry.signature(s)
    if reputation.blocked(sig) or reputation.cooled(sig):
        return False
    # A release explicitly known to contain neither English nor the title's
    # original language can never be auto-picked. Unknown audio metadata still
    # gets the benefit of the doubt; only proven-wrong audio is rejected.
    if not _audio_ok(s):
        return False
    text = _stream_text(s)
    if _12BIT_RE.search(text):
        return False
    # Bare Dolby Vision (no HDR10 base) shows a purple/green tint on the
    # household's non-DV screens — see _dv_rejected / DV_REJECT.
    if _dv_rejected(text):
        return False
    # Never serve cam/telesync/screener rips regardless of anything else.
    if _CAMTS_RE.search(text):
        return False
    # TRaSH-default rejects: a full BluRay disc/ISO isn't a single playable
    # file, and 3D / upscaled releases are unwanted regardless of source tier.
    # (Note: these regexes are defined lower in the module; resolved at call
    # time, so referencing them here is fine.)
    if _BRDISK_RE.search(text) or _3D_RE.search(text) or _UPSCALE_RE.search(text):
        return False
    if _resolution(s) > profile["max_res"]:
        return False
    if profile["max_bps"]:
        size = _size_bytes(s)
        if size and size / runtime > profile["max_bps"]:
            return False
    return True


def _eligible_library(lib: list[dict], profile: dict,
                      runtime: float) -> list[dict]:
    """Annotate trusted local files, but keep the same content/language gate."""
    lib = [_ingested_stream(s) for s in lib]
    _annotate_identity(lib)
    _annotate_quality(lib, runtime)
    return [s for s in lib if _usable(s, profile, runtime)]


# ── metadata ────────────────────────────────────────────────────────────────

async def _resolve_identity_profile(
        media: str, media_id: str,
        timeout: float | None = None) -> content_identity.IdentityProfile:
    """Resolve and install the authoritative request profile, failing closed."""
    started = time.monotonic()
    try:
        operation = meta.identity_profile(media, media_id)
        profile = (await asyncio.wait_for(operation, timeout)
                   if timeout is not None else await operation)
    except Exception:
        parts = media_id.split(":")
        season = episode = None
        if media != "movie" and len(parts) == 3 \
                and parts[1].isdigit() and parts[2].isdigit():
            season, episode = int(parts[1]), int(parts[2])
        profile = content_identity.IdentityProfile(
            media=media, imdb_id=parts[0], aliases=(),
            season=season, episode=episode)
    _set_identity_profile(profile)
    remaining = (None if timeout is None else
                 max(timeout - (time.monotonic() - started), 0.0))
    await _set_anime_expectation(media, media_id, profile, remaining)
    return profile


async def _set_anime_expectation(
        media: str, media_id: str,
        profile: content_identity.IdentityProfile,
        timeout: float | None) -> None:
    """Install the anime episode expectation for this request, if any. Bounded
    and fail-open: a slow or unreachable anime source just leaves it unset, and
    the picker keeps its ordinary filename identity."""
    expectation = None
    try:
        if anime.ENABLED and media != "movie" and profile.season is not None:
            budget = min(timeout, 5.0) if timeout is not None else 5.0
            if budget <= 0:
                _anime_ctx.set(None)
                return
            show = await asyncio.wait_for(
                anime.resolve(media, media_id, profile.season), budget)
            if show is not None:
                expectation = show.expectation(profile.season, profile.episode)
                if expectation is not None:
                    logger.info("anime %s: S%dE%d ↔ absolute #%d%s", media_id,
                                profile.season, profile.episode,
                                expectation.absolute,
                                " (split season)" if expectation.split_season else "")
    except Exception:
        expectation = None
    _anime_ctx.set(expectation)


async def _runtime_seconds(media: str, media_id: str) -> float:
    """Exact OMDb movie/episode runtime, then Cinemeta, then strict fallback.

    The exact episode key is retained in the cache: using a show's typical
    runtime as if it described every episode weakens both bitrate estimation and
    same-name runtime corroboration.
    """
    base_id = media_id.split(":")[0]
    key = f"{media}:{media_id}"
    hit = _runtime_cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    fallback = 6600.0 if media == "movie" else 2400.0
    exact = await meta.expected_runtime(media, media_id)
    if exact:
        seconds = float(exact)
        _runtime_cache[key] = (time.monotonic(), seconds)
        return seconds
    try:
        r = await _client.get(
            f"https://v3-cinemeta.strem.io/meta/{media}/{base_id}.json",
            timeout=6,
        )
        m = re.search(r"(\d+)", (r.json().get("meta") or {}).get("runtime") or "")
        seconds = int(m.group(1)) * 60 if m else fallback
    except Exception:
        seconds = fallback
    _runtime_cache[key] = (time.monotonic(), seconds)
    if len(_runtime_cache) > 1000:
        _runtime_cache.pop(next(iter(_runtime_cache)))
    return seconds


# ── response assembly ───────────────────────────────────────────────────────

def _mark(s: dict, rank: int, r: probe.ProbeResult | None) -> dict:
    out = _ingested_stream(s)
    out[_VERIFIED_STATE_KEY] = _VERIFIED_SENTINEL
    # This is the only handoff that authorizes proxy failover.  _assemble feeds
    # us only transport-passed, strong-identity candidates; the unforgeable
    # sentinel is applied after _ingested_stream deliberately scrubbed any
    # upstream attempt to spell the same private key.
    if _identity_leader(s):
        content_identity.mark_auto_eligible(out)
    if r is None:        # local library file — reliable, but no probe speed
        out["_ttfb"], out["_speed"] = 0.0, LIBRARY_SPEED
        label = (s.get("name") or "Library").replace("📚", "").strip()
        out["name"] = f"📚 {rank} · {label}"
        return out
    out["_ttfb"], out["_speed"] = r.ttfb, r.speed_bps
    icon = "📚" if "📚" in (s.get("name") or "") else "✅"
    out["name"] = f"{icon} {rank} · " + (s.get("name") or "Stream")
    speed = f"verified {r.speed_bps / 1e6:.0f} MB/s, {r.ttfb:.1f}s start"
    if s.get("description"):
        out["description"] = f"{speed}\n{s['description']}"
    else:
        out["title"] = f"{speed}\n{s.get('title', '')}"
    return out


def _twins_first(picks: list[dict], rest: list[dict]) -> list[dict]:
    """Reorder `rest` so byte-identical twins of the verified `picks` come first.
    A twin = same release signature but a *different* debrid service (TB vs RD)
    → the same file on a different node. Pulling them ahead of the output cut
    guarantees the proxy always has a mid-stream twin-splice target for whatever
    it serves. Pure reordering — no adds, no drops."""
    want: dict[str, set[str]] = {}
    for s in picks:
        svc = telemetry.debrid_tag(s.get("name") or "").rstrip("+")
        sig = telemetry.signature(s)
        if sig:
            want.setdefault(sig, set()).add(svc)
    twins, others = [], []
    for s in rest:
        svc = telemetry.debrid_tag(s.get("name") or "").rstrip("+")
        sig = telemetry.signature(s)
        services = want.get(sig) if sig else None
        if services and svc and svc not in services:
            twins.append(s)          # same release, a debrid/node we aren't serving
        else:
            others.append(s)
    return twins + others


def _assemble(verified: list[tuple[dict, probe.ProbeResult | None]],
              leftovers: list[dict], fallback: dict | None,
              key=_verified_key) -> list[dict]:
    """Build the response: every *verified* stream first (sorted by `key`), then
    the unverified leftovers (already quality-ranked), then the fallback tail.
    The invariant the pickers rely on: #1 is always a confirmed-working link, and
    no unverified stream ever sits above a verified one. `key` picks the verified
    sort — delivery-aware for the fast picker, pure quality for the slow one."""
    # Defense in depth: callers may accidentally hand us a transport success
    # with only compatible/unknown identity.  Keep it as a manual fallback, but
    # never stamp it verified or place it above the checking/result leader.
    had_transport_success = bool(verified)
    identity_fallbacks = [s for s, _ in verified if not _identity_leader(s)]
    verified = [(s, r) for s, r in verified if _identity_leader(s)]
    verified = sorted(verified, key=key, reverse=True)
    vset = [v for v, _ in verified]
    vurls = {s.get("url") for s in vset}
    streams = [_mark(s, i + 1, r) for i, (s, r) in enumerate(verified)]
    # Twins of the verified picks jump the queue so they survive the [:15] cut
    # and reach the proxy as splice ammo.
    rest = _twins_first(vset, [_ingested_stream(s) for s in
                               (identity_fallbacks + leftovers)
                               if s.get("url") not in vurls])
    streams += rest[:15]
    if had_transport_success and not verified:
        streams.insert(0, _notice_stream("checking"))
    if fallback:
        streams.append(fallback)
    return streams


def _store(key: str, streams: list[dict]) -> None:
    _cache[key] = (time.monotonic(), streams)
    _stale_cache.pop(key, None)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))


def _archive_stale(key: str, hit: tuple[float, list[dict]]) -> None:
    """Retain an expired result as hints, never as current verification."""
    _stale_cache[key] = hit
    if len(_stale_cache) > 500:
        _stale_cache.pop(next(iter(_stale_cache)))


def invalidate(media_id: str) -> None:
    """Drop every cached pick result for a title. Called when playback
    evidence arrives that the cached ranking is wrong (a player-rejected
    release was #1): the next open re-picks with the cooldown applied, so the
    list the user sees matches what the proxy will actually serve."""
    if not media_id:
        return
    suffix = f":{media_id}"
    for k in [k for k in _cache if k.endswith(suffix)]:
        hit = _cache.pop(k, None)
        if hit:
            _archive_stale(k, hit)


def _as_verified(lib: list[dict]) -> list[tuple[dict, None]]:
    """Local Jellyfin library files are reliable, so they enter the verified
    tier as (stream, None) and get quality-sorted alongside probe-verified
    online streams — never pinned first. A faster or higher-quality online
    source therefore outranks a lower-res library copy in both pickers."""
    return [(s, None) for s in lib]


_RANK_RE = re.compile(r"^(\S+)\s+\d+\s+·\s+")


def _is_ranked(s: dict) -> bool:
    """True only for a stream stamped internally by :func:`_mark`.

    Display text is deliberately irrelevant: upstream addons control ``name``
    and can legitimately (or maliciously) emit something resembling our
    ``ICON N ·`` prefix.
    """
    return s.get(_VERIFIED_STATE_KEY) is _VERIFIED_SENTINEL


def _verified_first(streams: list[dict]) -> bool:
    """True if #1 is a confirmed-working link (a verified probe or a library/
    trusted entry, both stamped with a rank prefix by _mark) rather than an
    unverified leftover. Both pickers hold to this: never hand the user a #1 that
    might not play."""
    return bool(streams) and _is_ranked(streams[0])


def _renumber(s: dict, rank: int) -> dict:
    """Rewrite the 'ICON N ·' rank prefix to match a stream's final position
    after a reorder; a stream with no such prefix is returned unchanged."""
    name = s.get("name") or ""
    m = _RANK_RE.match(name)
    if not m:
        return s
    out = dict(s)
    out["name"] = f"{m.group(1)} {rank} · {name[m.end():]}"
    return out


def _cached_candidate(key: str) -> tuple[float, list[dict]] | None:
    """Fresh cache entry, re-filtered against current release health."""
    hit = _cache.get(key)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age >= RESULT_CACHE_TTL:
        _cache.pop(key, None)
        _archive_stale(key, hit)
        return None
    had_verified = any(_is_ranked(s) for s in hit[1])
    live: list[dict] = []
    for stream in hit[1]:
        sig = telemetry.signature(stream)
        if sig and (reputation.blocked(sig) or reputation.cooled(sig)):
            continue
        live.append(stream)
    if had_verified and not any(_is_ranked(s) for s in live):
        _cache.pop(key, None)
        return None
    rank = 0
    normalized: list[dict] = []
    for stream in live:
        if _is_ranked(stream):
            rank += 1
            stream = _renumber(stream, rank)
        normalized.append(stream)
    return age, normalized


def _stale_entry(key: str) -> tuple[float, list[dict]] | None:
    """Expired result evidence still young enough to warm a fresh decision."""
    hit = _stale_cache.get(key)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age >= STALE_EVIDENCE_TTL:
        _stale_cache.pop(key, None)
        return None
    return age, hit[1]


def _schedule_cache_refresh(cache_key: str, media: str, media_id: str,
                            profile: dict, runtime: float, slow: bool) -> None:
    """Refresh source URLs and improve a revived stale result off-request."""
    if cache_key in _background:
        return
    if slow:
        coro = _finish_slow(cache_key, media, media_id, profile, runtime)
    else:
        coro = _finish_in_background(
            cache_key, media, media_id, profile, runtime, [])
    _background[cache_key] = asyncio.create_task(coro)


async def _revalidate_stale(key: str, media: str, media_id: str,
                            profile: dict, slow: bool,
                            stale: tuple[float, list[dict]]) -> list[dict] | None:
    """Try old proven leaders once, then continue with freshly minted URLs.

    Old verification is only probe priority. Every stream returned from here has
    passed again now; unchecked expired URLs are omitted entirely.
    """
    age, old_streams = stale
    await _resolve_identity_profile(media, media_id)
    runtime = await _runtime_seconds(media, media_id)
    await _resolve_accept_langs(media, media_id)

    formerly_verified = [_ingested_stream(s) for s in old_streams
                         if _is_ranked(s)]
    _annotate_identity(formerly_verified)
    _annotate_quality(formerly_verified, runtime)
    eligible = [s for s in formerly_verified
                if _usable(s, profile, runtime)]
    skipped = [s for s in eligible if candidate_health.should_skip(s)]
    candidates = [s for s in eligible
                  if not candidate_health.should_skip(s)][:2]
    if skipped:
        telemetry.record_cache_event(
            "probe_avoided", target_id=media_id, count=len(skipped),
            age_seconds=age, detail="stale exact-url cooldown")

    # A result-list expiry must also expire URL-bearing addon responses. Keep the
    # release evidence above, but make every source mint a current playback URL.
    sources.invalidate(media, media_id)
    if not candidates:
        telemetry.record_cache_event(
            "stale_revalidate_fail", target_id=media_id,
            age_seconds=age, detail="no eligible stale links")
        return None

    ttfb = min(SLOW_TTFB_MAX, 20) if slow else CACHE_REVERIFY_TTFB
    outcomes: list = []
    passed = await probe.probe_race(
        candidates, _need_bps_fn(runtime), ttfb,
        want=1, concurrency=len(candidates),
        deadline=time.monotonic() + ttfb, expect_secs=runtime,
        deep_check_of=(lambda s: _size_bytes(s)
                       if slow and _is_direct_nzb(s) else None),
        outcomes=outcomes)
    verified = [(s, r) for s, r in passed
                if _apply_probe_evidence(s, r, runtime)]
    if not verified:
        telemetry.record_cache_event(
            "stale_revalidate_fail", target_id=media_id,
            age_seconds=age, count=len(candidates))
        return None

    streams = _assemble(
        verified, [], None,
        key=_verified_quality_key if slow else _verified_key)
    _store(key, streams)
    fast_key = key[len("slow:"):] if slow else key
    _publish_fast_verified(fast_key, verified)
    _schedule_cache_refresh(key, media, media_id, profile, runtime, slow)
    telemetry.record_cache_event(
        "stale_revalidate_ok", target_id=media_id,
        age_seconds=age, count=len(candidates))
    return streams


async def _cached_pick(key: str, media: str, media_id: str, profile: dict,
                       slow: bool = False) -> list[dict] | None:
    """Serve a recent result instantly; periodically re-probe its verified #1."""
    raw = _cache.get(key)
    if raw and time.monotonic() - raw[0] >= RESULT_CACHE_TTL:
        _cache.pop(key, None)
        _archive_stale(key, raw)
    stale = _stale_entry(key)
    if stale:
        return await _revalidate_stale(
            key, media, media_id, profile, slow, stale)

    hit = _cached_candidate(key)
    if not hit:
        return None
    age, streams = hit
    if age < CACHE_REVERIFY_AFTER or not streams or not _is_ranked(streams[0]):
        return streams
    await _resolve_identity_profile(media, media_id)
    runtime = await _runtime_seconds(media, media_id)
    await _resolve_accept_langs(media, media_id)
    top = streams[0]
    if not _usable(top, profile, runtime):
        invalidate(media_id)
        return None
    deadline = time.monotonic() + (min(SLOW_TTFB_MAX, 20) if slow
                                   else CACHE_REVERIFY_TTFB)
    passed = await probe.probe_race(
        [top], _need_bps_fn(runtime),
        min(SLOW_TTFB_MAX, 20) if slow else CACHE_REVERIFY_TTFB,
        want=1, concurrency=1, deadline=deadline, expect_secs=runtime,
        deep_check_of=(lambda s: _size_bytes(s)
                       if slow and _is_direct_nzb(s) else None))
    if not passed:
        logger.info(f"cache leader failed revalidation for {key}; repicking")
        invalidate(media_id)
        sources.invalidate(media, media_id)
        return None
    if not _apply_probe_evidence(top, passed[0][1], runtime):
        logger.info(f"cache leader lost identity eligibility for {key}; repicking")
        invalidate(media_id)
        sources.invalidate(media, media_id)
        return None
    _cache[key] = (time.monotonic(), streams)
    return streams


def _prepend_library(lib: list[dict], streams: list[dict]) -> list[dict]:
    """Fast picker only. Fold the local library copy in with the verified online
    results and rank by quality, keeping the library ahead of anything of equal
    quality: the sort is stable and the library is listed first, so a 1080p
    library copy still leads a 1080p web-dl (speed first), while a genuinely
    better source that turned up just as fast — a 4K — outranks it. Unverified
    leftovers and the fallback keep their tail position."""
    if not lib:
        return streams
    lib_marked = [_mark(s, 0, None) for s in lib]
    lib_urls = {s.get("url") for s in lib}
    online = [s for s in streams if s.get("url") not in lib_urls]
    ranked = [s for s in online if _is_ranked(s)]
    tail = [s for s in online if not _is_ranked(s)]
    lead = sorted(lib_marked + ranked, key=_marked_key, reverse=True)
    lead = [_renumber(s, i + 1) for i, s in enumerate(lead)]
    return lead + tail


def _prepend_probed(verified: list[tuple[dict, probe.ProbeResult]],
                    streams: list[dict]) -> list[dict]:
    """Fold newly probed library streams into an already assembled response."""
    verified = [(s, r) for s, r in verified if _identity_leader(s)]
    if not verified:
        return streams
    urls = {s.get("url") for s, _ in verified}
    existing = [s for s in streams if s.get("url") not in urls]
    lead = [_mark(s, 0, r) for s, r in verified]
    lead += [s for s in existing if _is_ranked(s)]
    tail = [s for s in existing if not _is_ranked(s)]
    lead.sort(key=_marked_key, reverse=True)
    return [_renumber(s, i + 1) for i, s in enumerate(lead)] + tail


def _has_camts(lists: list[list[dict]]) -> bool:
    """True if any raw candidate is a cam/telesync/screener rip — a real source
    exists but only as a theatrical rip, so no proper release is out yet."""
    return any(_CAMTS_RE.search(_stream_text(s)) for lst in lists for s in lst)


NOTICE_URL_THEATRICAL = os.environ.get("NOTICE_URL_THEATRICAL") or \
    f"{ADDON_PUBLIC_URL}/notice_theatrical.mp4"


def _notice_stream(kind: str = "added") -> dict:
    """Placeholder video shown when there's no *verified* stream to serve. The
    slow picker never puts an unverified link at #1, so instead of a maybe-broken
    stream it shows one of these:
      'theatrical' — only a cam/TS exists (or no digital release is out yet);
      'added'      — sent to the library via Sonarr/Radarr, downloading now;
      'checking'   — sources exist and are still being verified in the background;
                     the best working one will appear on a retry in a moment."""
    if kind == "theatrical":
        return {
            "name": "🎬 Not Out Yet",
            "title": ("No proper digital release exists yet (cam/theatrical "
                      "only).\nIt'll appear here automatically once a real "
                      "release lands."),
            "url": NOTICE_URL_THEATRICAL,
            _NOTICE_STATE_KEY: kind,
        }
    if kind == "checking":
        return {
            "name": "⏳ Finding Best Stream",
            "title": ("Verifying sources so the first one always plays.\n"
                      "Re-open in a few seconds and the best working stream "
                      "will be here."),
            "url": NOTICE_URL,
            _NOTICE_STATE_KEY: kind,
        }
    return {
        "name": "⏳ Being Added",
        "title": ("Not streamable right now — sent to your library "
                  "(Sonarr/Radarr).\nCheck back in a few minutes and it will "
                  "play from your library."),
        "url": NOTICE_URL,
        _NOTICE_STATE_KEY: kind,
    }


def _contains_notice(streams: list[dict]) -> bool:
    return any(s.get(_NOTICE_STATE_KEY) in ("checking", "theatrical", "added")
               for s in streams)


def _need_bps_fn(runtime: float):
    def need_bps(s: dict) -> float | None:
        size = _size_bytes(s)
        if size:
            return size / runtime
        claimed = _resolution(s)
        if claimed >= 2160:
            return UNKNOWN_NEED_2160
        if claimed >= 1080:
            return UNKNOWN_NEED_1080
        if claimed >= 720:
            return UNKNOWN_NEED_720
        return UNKNOWN_NEED_480
    return need_bps


def _fast_checking_notice(cache_key: str, media: str, media_id: str,
                          profile: dict, runtime: float,
                          pool: list[dict]) -> list[dict]:
    """Fast picker's fallback when it couldn't verify a single link in its budget:
    show only the controlled, playable 'finding best stream' notice — never an
    upstream candidate that did not pass — and make sure the background finisher
    is running so the retry gets a verified answer from cache. No _notice_until
    here: the cached verified result overrides on retry the moment it lands."""
    if cache_key not in _background:
        _background[cache_key] = asyncio.create_task(
            _finish_in_background(cache_key, media, media_id, profile,
                                  runtime, list(pool)))
    logger.info(f"{cache_key}: no verified link in fast budget — 'checking' "
                f"notice, finisher verifying {len(pool)} candidates in background")
    return [_notice_stream("checking")]


# ── fast picker ─────────────────────────────────────────────────────────────

async def _finish_in_background(cache_key: str, media: str, media_id: str,
                                profile: dict, runtime: float,
                                extra: list[dict]) -> None:
    """Finish every source/probe off-request and refresh the fast cache."""
    try:
        finish_deadline = time.monotonic() + SLOW_FINISH_DEADLINE

        def left() -> float:
            return max(finish_deadline - time.monotonic(), 0.0)

        fast, stremthru, mediafusion, nzb, extras = await asyncio.gather(
            sources.get(sources.FAST, media, media_id, wait=min(15, left())),
            sources.get(sources.STREMTHRU, media, media_id, wait=min(45, left())),
            sources.get(sources.MEDIAFUSION, media, media_id, wait=min(60, left())),
            sources.get(sources.NZB, media, media_id, wait=min(60, left())),
            _gather_extras(media, media_id, wait=min(60, left())),
        )
        # Let progressive mounts add candidates, but reserve enough of the fixed
        # background budget to probe them. USENET_FINISH_WAIT remains an upper
        # bound, never an extra hour added on top of SLOW_FINISH_DEADLINE.
        complete_nzb = await nzb_lane.wait_complete(
            media, media_id,
            min(USENET_FINISH_WAIT, max(left() - USENET_TTFB_MAX, 0.0)))
        if complete_nzb is not None:
            nzb = sources.normalize_nzb(complete_nzb)
        ok, _ = _merge_rank(list(extra) + list(fast), stremthru,
                            mediafusion, nzb, profile, runtime, extras=extras)
        inherited = _take_fast_verified(cache_key, profile, runtime)
        inherited_urls = {s.get("url") for s, _ in inherited}
        inherited_idents = {i for i in (_release_ident(s) for s, _ in inherited)
                            if i}
        lib = await library.streams(media, media_id) if library.enabled() else []
        if lib:
            lib = _eligible_library(lib, profile, runtime)
        unprobed = [s for s in ok + lib if s.get("url") not in inherited_urls]
        unprobed.sort(key=_probe_key, reverse=True)
        finish_slice = _slow_probe_slice(
            unprobed,
            SLOW_FINISH_MAX_PROBES, skip_idents=inherited_idents)
        probed = await _probe_bounded(
            finish_slice, runtime, USENET_TTFB_MAX, len(finish_slice),
            finish_deadline)
        verified = _combine_verified(inherited, probed)
        _tb_autocache(media, media_id, verified, runtime)
        if verified:
            # Off-clock, so measure true video bitrate of the leaders too: the
            # cached answer a retry gets should be compression-honest, not
            # label-trusting (matters most for size-less free-addon streams).
            await _refine_video_bitrate(probed, runtime, min(45, left()))
            if probed:
                _publish_fast_verified(cache_key, probed)
            vurls = {s.get("url") for s, _ in verified}
            streams = _assemble(
                verified, [s for s in ok + lib if s.get("url") not in vurls], None,
                key=_verified_key)
            _store(cache_key, streams)
            logger.info(f"{cache_key}: background verification cached "
                        f"{len(verified)} verified of {len(ok)}")
    except Exception:
        logger.exception(f"{cache_key}: background verification failed")
    finally:
        _background.pop(cache_key, None)


def _tb_autocache(media: str, media_id: str,
                  verified: list[tuple[dict, probe.ProbeResult | None]],
                  runtime: float) -> None:
    """Fire-and-forget: when this pick's outcome is weak (nothing verified, or
    only below what an uncached TorBox torrent offers), ask TorBox to start
    caching a better copy for the next visit. See app.tbcache."""
    if not tbcache.enabled():
        return
    best = max((int(s.get("_effres") or _resolution(s)) for s, _ in verified),
               default=0)
    t = asyncio.create_task(tbcache.maybe_cache(media, media_id, best, runtime))
    _acquire_tasks.add(t)
    t.add_done_callback(_acquire_tasks.discard)


def _count_tiers(verified: list[tuple[dict, probe.ProbeResult | None]]) -> tuple[int, int]:
    """(verified 2160p count, verified 1080p-or-1440p count)."""
    tiers = [int(s.get("_effres") or _resolution(s)) for s, _ in verified]
    n4k = sum(1 for res in tiers if res >= 2160)
    n1080 = sum(1 for res in tiers if 1080 <= res < 2160)
    return n4k, n1080


def _enough(verified: list[tuple[dict, probe.ProbeResult | None]]) -> bool:
    """A verified high-quality (1080p/4K) source returns the fast race at once."""
    n4k, n1080 = _count_tiers(verified)
    return n4k >= ENOUGH_4K or n1080 >= ENOUGH_1080


async def _race_fast(media: str, media_id: str, profile: dict, runtime: float,
                     need_bps, t0: float, lib_task: asyncio.Task | None = None,
                     ) -> tuple[list[tuple[dict, probe.ProbeResult | None]], list[dict]]:
    """Fire the fast online sources (Comet + AIOStreams) concurrently and probe
    their candidates best-quality-first as each source lands, stopping the moment
    the sufficiency bar is met — no source is privileged, the answer comes from
    whichever delivers a verified good stream first. Returns (verified, pool)
    where pool is every usable candidate seen (ranked), used as failover/twin
    material and as the unverified fallback list. The underlying searches are
    shielded in app.sources, so bailing early never cancels them — they finish
    into the shared cache for the background finisher and the slow picker."""
    srcs = sources.search_all()          # built-ins + user-added addons
    if not srcs:
        return [], []

    def left() -> float:
        return TOTAL_DEADLINE - (time.monotonic() - t0)

    source_tasks = {asyncio.create_task(
                        sources.get(s, media, media_id, wait=max(left(), 1))): s
                    for s in srcs}
    pending_sources = set(source_tasks)
    pending_library = lib_task if lib_task and not lib_task.done() else None
    running_probes: dict[asyncio.Task, tuple[dict, list]] = {}
    pool: list[dict] = []
    pool_urls: set[str] = set()
    probed: set[str] = set()
    ident_ok: set[str] = set()         # releases with a verified copy already
    ident_inflight: set[str] = set()   # releases with a probe running right now
    host_fails: dict[str, int] = {}    # per-pick probe failures by host
    host_ok: set[str] = set()          # hosts that have passed at least once
    verified: list[tuple[dict, probe.ProbeResult | None]] = []
    first_verified_at: float | None = None   # when the safety net first appeared
    deadline = min(t0 + FAST_RACE_DEADLINE, t0 + TOTAL_DEADLINE)

    # Completion-first with a safety net. A verified 1080p/4K returns at once
    # (_enough). Otherwise, the moment the race holds *any* verified stream that
    # definitely plays, it gives itself only FAST_VERIFIED_GRACE more seconds for
    # something genuinely better to verify, then answers with the best it has —
    # so a guaranteed DVD copy is never held hostage to a page of unproven "HD"
    # scraper labels that may never verify. The slow picker and the background
    # finisher do the patient, thorough verification.
    def _sufficient() -> bool:
        if _enough(verified):
            return True
        return (first_verified_at is not None
                and time.monotonic() - first_verified_at >= FAST_VERIFIED_GRACE)

    def _add_streams(streams: list[dict]) -> None:
        changed = False
        streams = [_ingested_stream(s) for s in streams]
        _annotate_identity(streams)
        _annotate_quality(streams, runtime)
        for stream in streams:
            url = stream.get("url")
            if not url or url in pool_urls or not _usable(stream, profile, runtime):
                continue
            pool_urls.add(url)
            pool.append(stream)
            changed = True
        if changed:
            pool.sort(key=_quality_key, reverse=True)

    def _ingest(done) -> None:
        for tk in done:
            try:
                streams = tk.result() or []
            except Exception:
                streams = []
            _add_streams(streams)

    def _ingest_library(task: asyncio.Task) -> None:
        try:
            streams = task.result() or []
        except Exception:
            streams = []
        # Local is usually excellent, but stale Jellyfin items can still
        # happen. Put library files through the same byte gate as every other
        # source before allowing one to become the automatic first result.
        _add_streams(streams)

    if lib_task and lib_task.done():
        _ingest_library(lib_task)

    try:
        while not _sufficient() and time.monotonic() < deadline:
            # Direct-NZB returns its first mount immediately and appends later
            # mounts to the shared list. Poll that live list so a failed first
            # release does not hide the second release from this same request.
            _add_streams(sources.peek(sources.NZB, media, media_id) or [])
            # Fold in any source that has finished, without blocking, so the
            # candidate pool stays best-first as new sources land.
            if pending_sources:
                done, pending_sources = await asyncio.wait(
                    pending_sources, timeout=0,
                    return_when=asyncio.FIRST_COMPLETED)
                _ingest(done)
            # Keep probe slots full in current quality order, but let source and
            # probe completions share one event loop. A hanging early candidate
            # can no longer hide a good source that arrived a moment later.
            # Duplicate releases get one probe at a time. Fill with distinct
            # hosts first so one wrapper/provider cannot monopolize all slots;
            # a second pass fills any capacity left when diversity is impossible.
            active_hosts = {_probe_host(s) for s, _ in running_probes.values()}
            # Speed-first opening: before anything has verified, put the
            # fast-to-verify candidates ahead of NZB-needs-a-mount so a floor
            # result lands sooner and starts the grace timer. Stable, so quality
            # order is preserved within each class; reverts once verified.
            probe_pool = sorted(pool, key=_probe_key, reverse=True)
            if FAST_SPEED_FIRST and first_verified_at is None:
                probe_pool = sorted(probe_pool, key=_is_direct_nzb)
            for diverse_only in (True, False):
                for stream in (s for s in probe_pool if s.get("url") not in probed):
                    if len(running_probes) >= PROBE_BATCH:
                        break
                    ident = _release_ident(stream)
                    if ident and ident in ident_ok:
                        probed.add(stream["url"])
                        continue
                    if ident and ident in ident_inflight:
                        continue
                    host = _probe_host(stream)
                    if diverse_only and host and host in active_hosts:
                        continue
                    if (PROBE_HOST_BENCH and host and host not in host_ok
                            and host_fails.get(host, 0) >= PROBE_HOST_BENCH):
                        probed.add(stream["url"])
                        continue
                    if candidate_health.should_skip(stream):
                        probed.add(stream["url"])
                        telemetry.record_cache_event(
                            "probe_avoided", target_id=media_id, count=1,
                            detail="fast exact-url cooldown")
                        continue
                    probed.add(stream["url"])
                    if ident:
                        ident_inflight.add(ident)
                    if host:
                        active_hosts.add(host)
                    outcomes: list = []
                    # A fresh nzbdav mount spends 20-35s assembling its opening
                    # segments before the first byte; the 12s debrid budget would
                    # fail it even when the mount is perfectly healthy, so it only
                    # surfaced on the slow finisher/a retry. Give direct-NZB the
                    # usenet first-byte allowance (the race deadline still caps it)
                    # so a good cold mount verifies inside this fast pass.
                    ttfb_budget = (USENET_TTFB_MAX if _is_direct_nzb(stream)
                                   else PROBE_TTFB_MAX)
                    task = asyncio.create_task(probe.probe_race(
                        [stream], need_bps, ttfb_budget, want=1,
                        concurrency=1, deadline=deadline, expect_secs=runtime,
                        outcomes=outcomes))
                    running_probes[task] = (stream, outcomes)
                if len(running_probes) >= PROBE_BATCH:
                    break

            active = set(pending_sources) | set(running_probes)
            if pending_library:
                active.add(pending_library)
            if not active:
                if nzb_lane.in_progress(media, media_id):
                    await asyncio.sleep(0.2)
                    continue
                break
            done, _ = await asyncio.wait(
                active, timeout=min(max(deadline - time.monotonic(), 0), 0.25),
                return_when=asyncio.FIRST_COMPLETED)
            source_done = done & pending_sources
            if source_done:
                pending_sources -= source_done
                _ingest(source_done)
            if pending_library and pending_library in done:
                _ingest_library(pending_library)
                pending_library = None
            for task in done & set(running_probes):
                stream, outcomes = running_probes.pop(task, ({}, []))
                ident = _release_ident(stream)
                ident_inflight.discard(ident)
                try:
                    passed = task.result() or []
                except Exception:
                    passed = []
                host = _probe_host(stream)
                if passed:
                    identity_passed = []
                    for vs, vr in passed:
                        if _apply_probe_evidence(vs, vr, runtime):
                            identity_passed.append((vs, vr))
                    verified.extend(identity_passed)
                    if identity_passed and first_verified_at is None:
                        first_verified_at = time.monotonic()   # start grace timer
                    if ident and identity_passed:
                        ident_ok.add(ident)
                    if host:
                        host_ok.add(host)
                elif host and any(_systemic_probe_failure(r.reason)
                                  for _, r in outcomes):
                    host_fails[host] = host_fails.get(host, 0) + 1
                    if (PROBE_HOST_BENCH and host not in host_ok
                            and host_fails[host] == PROBE_HOST_BENCH):
                        logger.info(f"probe: benching host {host} for this pick"
                                    f" ({PROBE_HOST_BENCH} failures, no passes)")
    finally:
        for tk in pending_sources:         # shielded searches keep running below
            tk.cancel()
        for tk in running_probes:
            tk.cancel()
        if pending_sources or running_probes:
            await asyncio.gather(*pending_sources, *running_probes,
                                 return_exceptions=True)
        if pending_library:
            _acquire_tasks.add(pending_library)
            pending_library.add_done_callback(_acquire_tasks.discard)
    # Best quality first — a real 4K leads a 1080p; _assemble re-sorts the same
    # way for the response, this is just so the sufficiency snapshot is coherent.
    verified.sort(key=_verified_quality_key, reverse=True)
    return verified, pool


async def pick(media: str, media_id: str, profile_name: str = "full") -> list[dict]:
    # Local library is a fast, reliable source, so the fast picker uses it too:
    # query Jellyfin alongside the online search and put any library hit first.
    started = time.monotonic()
    lib_task = (asyncio.create_task(library.streams(media, media_id))
                if library.enabled() else None)
    streams = await _pick_online(
        media, media_id, profile_name, lib_task=lib_task, started=started)
    if lib_task:
        try:
            # A ready/cached library answer is effectively free. If online has
            # already produced a verified #1, never delay it waiting on Jellyfin;
            # otherwise let the library use only the remainder of the safety cap.
            if lib_task.done():
                lib = lib_task.result()
            elif _verified_first(streams):
                _acquire_tasks.add(lib_task)
                lib_task.add_done_callback(_acquire_tasks.discard)
                lib = []
            else:
                budget = max(TOTAL_DEADLINE - (time.monotonic() - started), 0)
                lib = await asyncio.wait_for(asyncio.shield(lib_task),
                                             max(budget, 0.01))
        except asyncio.TimeoutError:
            _acquire_tasks.add(lib_task)
            lib_task.add_done_callback(_acquire_tasks.discard)
            lib = []
        except Exception:
            lib = []
        if lib:
            runtime = await _runtime_seconds(media, media_id)
            if _identity_profile_ctx.get() is None:
                await _resolve_identity_profile(media, media_id)
            await _resolve_accept_langs(media, media_id)
            lib = _eligible_library(lib, PROFILES[profile_name], runtime)
            present = {s.get("url") for s in streams}
            lib = [s for s in lib if s.get("url") not in present]
            budget = max(TOTAL_DEADLINE - (time.monotonic() - started), 0)
            if lib and budget > 0.1:
                checked = await probe.probe_race(
                    lib, _need_bps_fn(runtime), min(PROBE_TTFB_MAX, budget),
                    want=len(lib), concurrency=min(len(lib), PROBE_BATCH),
                    deadline=time.monotonic() + budget, expect_secs=runtime)
                checked = [(stream, result) for stream, result in checked
                           if _apply_probe_evidence(stream, result, runtime)]
                streams = _prepend_probed(checked, streams)
    return streams


async def _pick_online(media: str, media_id: str,
                       profile_name: str = "full",
                       lib_task: asyncio.Task | None = None,
                       started: float | None = None) -> list[dict]:
    t0 = started if started is not None else time.monotonic()
    profile = PROFILES[profile_name]
    cache_key = f"{profile_name}:{media}:{media_id}"
    hit = await _cached_pick(cache_key, media, media_id, profile)
    if hit is not None:
        logger.info(f"cache hit for {cache_key}")
        return hit

    def left() -> float:
        return TOTAL_DEADLINE - (time.monotonic() - t0)

    # Register all shared searches now so slower sources start at t=0
    # (and so a sibling slow-picker request can join these instead of starting
    # its own). We never cancel them: whoever bails early just detaches, and
    # the search finishes into the shared cache for the next joiner.
    for src in sources.search_all():
        sources.start(src, media, media_id)

    # Runtime and identity/language metadata resolve while upstream searches are
    # already in flight. Bound the preparation stage so a cold metadata API does
    # not consume the 2-10 second target window before byte verification starts.
    runtime_task = asyncio.create_task(_runtime_seconds(media, media_id))
    _accept_langs.set(None)
    _original_lang_known.set(False)
    prep_deadline = min(t0 + FAST_METADATA_BUDGET, t0 + TOTAL_DEADLINE)
    await _resolve_identity_profile(
        media, media_id, timeout=max(prep_deadline - time.monotonic(), 0.05))
    try:
        async with asyncio.timeout(max(prep_deadline - time.monotonic(), 0.05)):
            await _resolve_accept_langs(media, media_id)
    except (TimeoutError, asyncio.TimeoutError):
        _accept_langs.set(None)
        _original_lang_known.set(False)
    # Semantic identity is a hard prerequisite for an automatic result. Its
    # timeout above installs an exact-request but alias-empty fail-closed profile,
    # under which only validated Newznab/Jellyfin IMDb evidence may lead.
    try:
        if not runtime_task.done():
            await asyncio.wait_for(
                asyncio.shield(runtime_task),
                timeout=max(prep_deadline - time.monotonic(), 0.05))
        runtime = runtime_task.result()
    except Exception:
        if not runtime_task.done():
            runtime_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime_task
        runtime = 6600.0 if media == "movie" else 2400.0
    need_bps = _need_bps_fn(runtime)

    # ── fast race: every online source fires; return on the first sufficient
    #    verified set, whichever source it came from ──────────────────────────
    verified, pool = await _race_fast(media, media_id, profile, runtime,
                                      need_bps, t0, lib_task=lib_task)

    if verified:
        _publish_fast_verified(cache_key,
                               [(s, r) for s, r in verified if r is not None])
        # Quality-first: a real 4K leads, but a good 1080p still beats a small /
        # fake 4K (effective resolution caps the latter). #1 is always verified.
        streams = _assemble(verified, pool, None, key=_verified_key)
        _store(cache_key, streams)
        # Finish the fuller search in the background so a return visit (or the
        # slow addon) gets everything cached and best-ranked.
        if cache_key not in _background:
            _background[cache_key] = asyncio.create_task(
                _finish_in_background(cache_key, media, media_id, profile,
                                      runtime, list(pool)))
        n4k, n1080 = _count_tiers(verified)
        logger.info(f"{cache_key}: fast race → {len(verified)} verified "
                    f"({n4k}×2160p, {n1080}×1080p) in {time.monotonic() - t0:.1f}s")
        return streams

    # Fast deadline hit / nothing verified anywhere yet. We never hand back an
    # unverified #1 — show the 'finding best stream' notice and let the finisher
    # verify for the retry.
    return _fast_checking_notice(cache_key, media, media_id, profile, runtime, pool)


# ── slow / best-quality picker ──────────────────────────────────────────────

async def _gather_extras(media: str, media_id: str, wait: float) -> list[dict]:
    """Collect streams from every user-added addon (app.sources.EXTRAS) plus the
    native Prowlarr lane when it is enabled. Each is joined like any built-in
    source; failures/timeouts yield nothing and never break the pick. Folding
    Prowlarr in here threads it through every merge site (fast finisher, slow
    finish, slow foreground) that already concatenates these into the pool."""
    keys = list(sources.EXTRAS)
    if sources.has(sources.PROWLARR):
        keys.append(sources.PROWLARR)
    if not keys:
        return []
    results = await asyncio.gather(
        *(sources.get(k, media, media_id, wait=wait) for k in keys),
        return_exceptions=True)
    out: list[dict] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out


async def _latest_nzb_snapshot(media: str, media_id: str,
                               current: list[dict], wait: float) -> list[dict]:
    """Refresh a progressive Usenet snapshot whether its lane runs or finished."""
    newer = await nzb_lane.wait_for_more(
        media, media_id, len(current), wait if nzb_lane.in_progress(
            media, media_id) else 0)
    return sources.normalize_nzb(newer) if newer is not None else current


def _merge_rank(fast: list[dict], stremthru: list[dict],
                mediafusion: list[dict], nzb: list[dict], profile: dict,
                runtime: float,
                extras: list[dict] | None = None) -> tuple[list[dict], dict | None]:
    """Merge every source into one URL-deduped, quality-ranked candidate list
    (best first). `extras` carries streams from user-added addons."""
    everything = [_ingested_stream(s) for s in (
        list(fast) + list(stremthru) + list(mediafusion) + list(nzb)
        + list(extras or []))]
    _annotate_identity(everything)
    _annotate_quality(everything, runtime)
    seen: set[str] = set()
    merged: list[dict] = []
    for s in everything:
        u = s.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        merged.append(s)
    ok = sorted((s for s in merged if _usable(s, profile, runtime)),
                key=_quality_key, reverse=True)
    return ok, None


def _is_direct_nzb(s: dict) -> bool:
    return str(s.get("_nzb_release_key") or "").startswith("nzb:")


def _lane_of(s: dict) -> str:
    """Which probe-wave lane a stream belongs to: direct-Usenet mounts, the
    debrid lanes (comet / stremthru / mediafusion / native Prowlarr resolve,
    plus any stream carrying a visible debrid service tag like [TB+]), and
    everything else — free HTTP addons — in the catch-all https lane."""
    if _is_direct_nzb(s) or s.get("_source_key") == sources.NZB:
        return "usenet"
    if s.get("_source_key") in (sources.FAST, sources.STREMTHRU,
                                sources.MEDIAFUSION, sources.PROWLARR):
        return "debrid"
    if telemetry.debrid_tag(s.get("name") or ""):
        return "debrid"
    return "https"


def _slow_probe_slice(ok: list[dict], max_probes: int,
                      skip_idents: set | frozenset = frozenset()) -> list[dict]:
    """Quality-first probe wave with one exploratory slot per transport lane.

    Several addons often carry the same file (scrapers sharing an upstream), so
    the wave takes the best copy of each release (_release_ident) first, which
    lets it cover max_probes *different* releases instead of burning slots
    re-checking one. Only when the pool runs out of distinct releases do
    duplicate copies fill the remaining slots (a twin on another host doubles as
    failover evidence). `skip_idents` = releases already verified elsewhere
    (e.g. by the fast picker); their copies need no probe at all.

    The old equal-thirds split could spend five of sixteen foreground checks on
    much weaker candidates merely because they used another transport. Instead,
    each available lane gets one representative and every remaining slot goes to
    the best prospect overall. A type this install does not have consumes no
    space. This preserves enough diversity to turn a label-only HTTPS stream into
    measured evidence without sacrificing the quality objective.

    The lanes only decide what gets *tested*. Final verified results are still
    sorted by ``_verified_quality_key``, so admitting a lower-quality stream to
    the wave cannot make it #1 unless it genuinely ranks highest.
    """
    limit = max(0, max_probes)
    if not ok or not limit:
        return []
    cooled = [s for s in ok if candidate_health.should_skip(s)]
    if cooled:
        telemetry.record_cache_event(
            "probe_avoided", count=len(cooled),
            detail="slow exact-url cooldown")
        ok = [s for s in ok if not candidate_health.should_skip(s)]
    if not ok:
        return []
    lanes = {"usenet": [], "debrid": [], "https": []}
    for s in ok:
        lanes[_lane_of(s)].append(s)
    present = [cands for cands in lanes.values() if cands]
    # If a caller ever asks for fewer probes than there are transports, reserve
    # the scarce slots for the lanes whose best candidate ranks highest.
    present.sort(key=lambda cands: _quality_key(cands[0]), reverse=True)
    present = present[:limit]

    seen_idents = {i for i in skip_idents if i}
    selected_urls: set[str] = set()

    def take(cands: list[dict], want: int) -> None:
        """Best copy per distinct release from `cands`, up to `want` picks."""
        picked = 0
        for s in cands:
            if picked >= want or len(selected_urls) >= limit:
                return
            ident = _release_ident(s)
            if ident and ident in seen_idents:
                continue
            if ident:
                seen_idents.add(ident)
            selected_urls.add(s.get("url"))
            picked += 1

    for cands in present:              # one exploratory candidate per lane
        take(cands, 1)
    take(ok, limit)                    # every other slot is pure quality order
    for s in ok:                       # still room? top up with duplicate copies
        if len(selected_urls) >= limit:
            break
        url = s.get("url")
        if url in selected_urls:
            continue
        ident = _release_ident(s)
        if ident and ident in skip_idents:
            continue                   # verified elsewhere — a probe proves nothing
        selected_urls.add(url)

    # Keep the original pure-quality order inside the chosen wave.
    return [s for s in ok if s.get("url") in selected_urls][:limit]


async def _probe_bounded(ok: list[dict], runtime: float, ttfb_max: float,
                         max_probes: int, hard_deadline: float,
                         success_grace: float | None = None,
                         ) -> list[tuple[dict, probe.ProbeResult]]:
    """Verify the top `max_probes` candidates by quality, concurrently.

    Background callers leave `success_grace` unset and patiently collect the
    entire slice. The foreground slow picker sets it: once a result has passed
    both transport and semantic-identity checks, the remaining prospects get a
    short settling window and then stragglers are cancelled. That returns the best
    evidence available now without making a dead NZB dictate response latency;
    the background finisher retries unfinished leaders with its longer budget."""
    budget = hard_deadline - time.monotonic()
    if not ok or budget <= 1:
        return []
    cands = ok[:max_probes]                    # already quality-ranked, best first
    # probe_race owns this deadline and returns every success accumulated before
    # it.  Wrapping it in another timer at the exact same instant can cancel it
    # during cleanup and throw those already-verified successes away.
    eligible: set[int] = set()

    def settle_when(s: dict, r: probe.ProbeResult) -> bool:
        accepted = _apply_probe_evidence(s, r, runtime)
        if accepted:
            eligible.add(id(s))
        return accepted

    results = await probe.probe_race(
        cands, _need_bps_fn(runtime), ttfb_max,
        want=len(cands),               # collect all that pass, no early bail
        concurrency=SLOW_CONCURRENCY, deadline=hard_deadline,
        expect_secs=runtime,
        deep_check_of=lambda s: (_size_bytes(s) if _is_direct_nzb(s) else None),
        settle_after=success_grace,
        settle_when=settle_when if success_grace is not None else None)
    # Transport success is necessary but not sufficient. Only candidates whose
    # identity was already strong, or became strong through a measured-runtime
    # corroboration, enter the verified tier.
    if success_grace is not None:
        return [(s, r) for s, r in results if id(s) in eligible]
    return [(s, r) for s, r in results
            if _apply_probe_evidence(s, r, runtime)]


async def _finish_slow(cache_key: str, media: str, media_id: str,
                       profile: dict, runtime: float) -> None:
    """Finish the top quality prospects off-request and cache any upgrade."""
    try:
        finish_deadline = time.monotonic() + SLOW_FINISH_DEADLINE

        def left() -> float:
            return max(finish_deadline - time.monotonic(), 0.0)

        fast, stremthru, mediafusion, nzb, extras = await asyncio.gather(
            sources.get(sources.FAST, media, media_id, wait=min(15, left())),
            sources.get(sources.STREMTHRU, media, media_id, wait=min(45, left())),
            sources.get(sources.MEDIAFUSION, media, media_id, wait=min(60, left())),
            sources.get(sources.NZB, media, media_id, wait=min(60, left())),
            _gather_extras(media, media_id, wait=min(60, left())),
        )
        # Progressive Usenet may still be mounting. Wait only inside the fixed
        # finisher window and reserve SLOW_FINISH_TTFB_MAX for actual verification.
        complete_nzb = await nzb_lane.wait_complete(
            media, media_id,
            min(USENET_FINISH_WAIT,
                max(left() - SLOW_FINISH_TTFB_MAX, 0.0)))
        if complete_nzb is not None:
            nzb = sources.normalize_nzb(complete_nzb)
        ok, fallback = _merge_rank(fast, stremthru, mediafusion, nzb,
                                   profile, runtime, extras=extras)
        fast_verified = _take_fast_verified(cache_key[len("slow:"):], profile, runtime)
        fast_urls = {s.get("url") for s, _ in fast_verified}
        fast_idents = {i for i in (_release_ident(s) for s, _ in fast_verified) if i}
        lib = await library.streams(media, media_id) if library.enabled() else []
        if lib:
            lib = _eligible_library(lib, profile, runtime)
        unprobed = [s for s in ok + lib if s.get("url") not in fast_urls]
        unprobed.sort(key=_probe_key, reverse=True)
        # Off Nuvio's clock, so dig deeper by quality than the foreground slice
        # AND wait patiently (SLOW_FINISH_TTFB_MAX) for slow-to-start high-quality
        # sources, so they survive verification and can take #1 on the retry.
        finish_slice = _slow_probe_slice(
            unprobed,
            SLOW_FINISH_MAX_PROBES, skip_idents=fast_idents)
        verified = await _probe_bounded(
            finish_slice, runtime, SLOW_FINISH_TTFB_MAX, len(finish_slice),
            finish_deadline)
        await _refine_video_bitrate(verified, runtime, min(45, left()))
        all_verified = _combine_verified(fast_verified, verified)
        _tb_autocache(media, media_id, all_verified, runtime)
        if all_verified:
            vurls = {v.get("url") for v, _ in all_verified}
            streams = _assemble(all_verified,
                                [s for s in ok + lib if s.get("url") not in vurls],
                                fallback, key=_verified_quality_key)
            _store(cache_key, streams)
            logger.info(f"{cache_key}: background best-quality cached"
                        f" ({len(verified)} verified, +{len(fast_verified)} fast,"
                        f" {len(lib)} library of {len(ok)})")
            return
        # Exhausted the full search and nothing plays. Raw library URLs do not
        # count here: they went through the same probe gate and may be stale.
        # If a proper release should exist, acquire it; otherwise set the
        # "not out yet" notice and download nothing.
        if not ok and not await _release_expected(
                media, media_id, [fast, stremthru, mediafusion, nzb]):
            _notice_until[cache_key] = (time.monotonic() + NOTICE_TTL, "theatrical")
            logger.info(f"{cache_key}: full search — no proper release out yet")
            return
        if nzb_lane.in_progress(media, media_id):
            logger.info(f"{cache_key}: background budget ended while direct "
                        "usenet was still mounting; leaving acquisition for retry")
            return
        if acquire.enabled_for(media):
            if await acquire.request(media, media_id):
                _notice_until[cache_key] = (time.monotonic() + NOTICE_TTL, "added")
                logger.info(f"{cache_key}: no working source after full search"
                            f" — requested via "
                            f"{'radarr' if media == 'movie' else 'sonarr'}")
    except Exception:
        logger.exception(f"{cache_key}: background best-quality failed")
    finally:
        _background.pop(cache_key, None)


async def _release_expected(media: str, media_id: str,
                            raw_lists: list[list[dict]]) -> bool:
    """Should a proper (non-cam) release exist by now? Trust TMDB when it can
    answer; when it can't, fall back to the cam/TS heuristic — a cam rip in the
    results means it's still theatrical-only, so no proper release yet."""
    released = await meta.has_release(media, media_id)
    if released is not None:
        return released
    # An upstream outage and a genuinely empty unreleased search both look
    # empty to older source adapters. Unknown metadata plus no positive release
    # evidence must fail closed: never auto-add a title merely because APIs died.
    return any(raw_lists) and not _has_camts(raw_lists)


async def _no_source(cache_key: str, media: str, media_id: str,
                     raw_lists: list[list[dict]]) -> list[dict]:
    """Slow picker's foreground no-source outcome. Acquire via Sonarr/Radarr and
    show the "being added" notice only when a proper release should exist but we
    couldn't find it; if nothing is out yet (still theatrical / not aired), show
    the "not out yet" notice and download nothing."""
    if not await _release_expected(media, media_id, raw_lists):
        _notice_until[cache_key] = (time.monotonic() + NOTICE_TTL, "theatrical")
        logger.info(f"{cache_key}: no proper release out yet — 'not out' notice")
        return [_notice_stream("theatrical")]
    if acquire.enabled_for(media):
        task = asyncio.create_task(acquire.request(media, media_id))
        try:
            ok = await asyncio.wait_for(asyncio.shield(task),
                                        timeout=ACQUIRE_FOREGROUND_WAIT)
        except asyncio.TimeoutError:
            _acquire_tasks.add(task)
            task.add_done_callback(_acquire_tasks.discard)
            logger.info(f"{cache_key}: acquisition request still pending")
            return [_notice_stream("checking")]
        if ok:
            _notice_until[cache_key] = (time.monotonic() + NOTICE_TTL, "added")
            logger.info(f"{cache_key}: release should exist but none found — "
                        f"accepted by {'radarr' if media == 'movie' else 'sonarr'}")
            return [_notice_stream("added")]
        logger.warning(f"{cache_key}: acquisition request was not accepted")
    logger.info(f"{cache_key}: no source found (acquire disabled)")
    return []


async def pick_slow(media: str, media_id: str,
                    profile_name: str = "full") -> list[dict]:
    t0 = time.monotonic()
    profile = PROFILES[profile_name]
    cache_key = f"slow:{profile_name}:{media}:{media_id}"
    hit = await _cached_pick(cache_key, media, media_id, profile, slow=True)
    if hit is not None:
        logger.info(f"cache hit for {cache_key}")
        return hit

    # Local Jellyfin library — a fast, reliable source; fire it now, use later.
    lib_task = (asyncio.create_task(library.streams(media, media_id))
                if library.enabled() else None)

    # Recently classified as pending (downloading, or not out yet)? Serve the
    # matching notice fast — but re-check the library first so a finished
    # download plays immediately.
    nu = _notice_until.get(cache_key)
    if nu and nu[0] > time.monotonic():
        lib = (await lib_task) if lib_task else []
        if lib:
            runtime = await _runtime_seconds(media, media_id)
            await _resolve_identity_profile(media, media_id)
            await _resolve_accept_langs(media, media_id)
            lib = _eligible_library(lib, profile, runtime)
            if lib:
                checked = await probe.probe_race(
                    lib, _need_bps_fn(runtime), CACHE_REVERIFY_TTFB,
                    want=len(lib), concurrency=min(len(lib), PROBE_BATCH),
                    deadline=time.monotonic() + CACHE_REVERIFY_TTFB,
                    expect_secs=runtime)
                checked = [(stream, result) for stream, result in checked
                           if _apply_probe_evidence(stream, result, runtime)]
                if checked:
                    _notice_until.pop(cache_key, None)
                    streams = _assemble(checked, [], None)
                    _store(cache_key, streams)
                    logger.info(f"{cache_key}: download landed, serving library")
                    return streams
        logger.info(f"{cache_key}: still pending ({nu[1]}), showing notice")
        return [_notice_stream(nu[1])]

    # Hard ceiling on the whole response, kept safely inside Nuvio's 60s addon
    # timeout. Everything below stays within this; anything unfinished is left
    # to the background finisher and served from cache on the retry.
    resp_deadline = t0 + SLOW_TOTAL_DEADLINE

    def left() -> float:
        return resp_deadline - time.monotonic()

    # Join the fast picker's shared searches (or start them if the slow addon
    # was opened first). Either way, one search per title — no parallel
    # duplicate that would rate-limit the upstream APIs.
    for src in sources.search_all():
        sources.start(src, media, media_id)

    runtime = await _runtime_seconds(media, media_id)
    await _resolve_identity_profile(media, media_id)
    await _resolve_accept_langs(media, media_id)

    # Wait for every source to run its course, holding back SLOW_PROBE_RESERVE
    # so there's room to probe deeply before the deadline.
    src_wait = max(left() - SLOW_PROBE_RESERVE, 1)
    fast, stremthru, mediafusion, nzb, extras = await asyncio.gather(
        sources.get(sources.FAST, media, media_id, wait=src_wait),
        sources.get(sources.STREMTHRU, media, media_id, wait=src_wait),
        sources.get(sources.MEDIAFUSION, media, media_id, wait=src_wait),
        sources.get(sources.NZB, media, media_id, wait=src_wait),
        _gather_extras(media, media_id, wait=src_wait),
    )

    # The direct lane is progressive: its source call can return empty or with
    # one mount while the rest keep materializing.  Spend only the source-side
    # portion of the remaining foreground budget waiting for one more, leaving
    # the probe reserve intact.  The full tail continues in _finish_slow.
    # ``sources.get`` is intentionally an early progressive snapshot. By the
    # time slower HTTP addons settle, the detached lane may already have added
    # several mounts *and finished*. Always refresh its current output; gating
    # this on in_progress() used to freeze the one-item early snapshot forever.
    nzb = await _latest_nzb_snapshot(
        media, media_id, nzb, max(left() - SLOW_PROBE_RESERVE, 0))

    ok, fallback = _merge_rank(fast, stremthru, mediafusion, nzb,
                               profile, runtime, extras=extras)
    # Anything the fast picker already probed OK for this title is verified truth
    # — fold it straight in, and don't waste the probe budget re-checking it.
    fast_verified = _take_fast_verified(cache_key[len("slow:"):], profile, runtime)
    fast_urls = {s.get("url") for s, _ in fast_verified}
    fast_idents = {i for i in (_release_ident(s) for s, _ in fast_verified) if i}
    lib = []
    if lib_task:
        try:
            lib = await lib_task
        except Exception:
            lib = []
    if lib:
        lib = _eligible_library(lib, profile, runtime)
    unprobed = [s for s in ok + lib if s.get("url") not in fast_urls]
    unprobed.sort(key=_probe_key, reverse=True)
    probe_slice = _slow_probe_slice(
        unprobed, SLOW_MAX_PROBES, skip_idents=fast_idents)
    verified = await _probe_bounded(
        probe_slice,
        runtime, min(SLOW_TTFB_MAX, max(left(), 5)),
        len(probe_slice), resp_deadline - 3,
        success_grace=SLOW_VERIFIED_GRACE)
    distinct = len({_release_ident(s) or s.get("url") for s in ok})
    logger.info(f"{cache_key}: merged fast {len(fast)} / stremthru {len(stremthru)} /"
                f" mf {len(mediafusion)} / nzb {len(nzb)} / extras {len(extras)}"
                f" -> {len(ok)} usable ({distinct} distinct releases),"
                f" {len(verified)} verified (+{len(fast_verified)} fast),"
                f" {len(lib)} library in {time.monotonic() - t0:.1f}s")

    # Slow picker earns its name here: for the top few candidates, measure the
    # true *video* bitrate (ignoring audio-track bloat) and re-rank on it, so the
    # genuine best quality bubbles up rather than a fat-but-audio-heavy or a
    # starved fake-4K encode. Bounded to a handful so it stays within budget.
    await _refine_video_bitrate(verified, runtime, left())
    # Publish only finalized quality/audio evidence; copied pre-refinement
    # streams can otherwise overwrite a correct later slow-cache order.
    _publish_fast_verified(cache_key[len("slow:"):], verified)

    # Library copies passed through the same probe wave, so every member of the
    # leading tier has current playback evidence.
    all_verified = _combine_verified(fast_verified, verified)
    vurls = {v.get("url") for v, _ in all_verified}
    streams = _assemble(all_verified,
                        [s for s in ok + lib if s.get("url") not in vurls],
                        fallback, key=_verified_quality_key)

    # The slow picker's whole job is the *best* source, so whenever candidates
    # remain unprobed, keep digging in the background even if we can already
    # answer with a verified stream. The foreground settling window deliberately
    # stops waiting on stragglers; the finisher retries those leaders with its
    # patient budget and caches any upgrade for three hours.
    if cache_key not in _background:
        _background[cache_key] = asyncio.create_task(
            _finish_slow(cache_key, media, media_id, profile, runtime))

    if all_verified:
        _store(cache_key, streams)
        return streams

    # No playable source anywhere. A raw library URL is not proof of playback:
    # it may be stale and has already failed the byte gate above. Let Usenet
    # finish mounting or let the normal no-source/acquisition path decide.
    if not ok and nzb_lane.in_progress(media, media_id):
        logger.info(f"{cache_key}: direct usenet still mounting; showing "
                    "checking notice instead of acquiring prematurely")
        return [_notice_stream("checking")]
    if not ok:
        return await _no_source(cache_key, media, media_id,
                                [fast, stremthru, mediafusion, nzb])

    # Candidates exist but none verified in the foreground window. We never hand
    # the user an unverified #1 — it might not play — so show the "finding best
    # stream" notice and let the finisher (spawned above) verify and cache the
    # best working one for the retry. A short notice window keeps rapid re-opens
    # cheap; the cached verified result overrides it the moment it lands.
    _notice_until[cache_key] = (time.monotonic() + CHECKING_NOTICE_TTL, "checking")
    logger.info(f"{cache_key}: {len(ok)} candidates, none verified yet — "
                f"showing 'checking' notice while the finisher verifies")
    lower = [_ingested_stream(s) for s in ok
             if s.get(_IDENTITY_STATE_KEY) != content_identity.CONTRADICTION]
    return [_notice_stream("checking"), *lower[:15]]


# ── next-episode prefetch ────────────────────────────────────────────────────
# (operation, profile, target episode): mobile/full households do not suppress
# one another, and a force-refresh cannot collide with E+1 desired state.
_prefetching: set[tuple[str, str, str]] = set()


async def _next_episode(media_id: str) -> str | None:
    """imdb id of the episode after this one — season-boundary aware: a finale
    rolls over to next season's E1 via TMDB/TVDB episode counts (meta.next_episode
    falls back to naive E+1 when neither API knows the show, and returns None
    after the last episode of the last season). None for non-episode ids."""
    parts = media_id.split(":")
    if len(parts) != 3:
        return None
    base, season, ep = parts
    if not (season.isdigit() and ep.isdigit()):
        return None
    try:
        se = await meta.next_episode(base, int(season), int(ep))
    except Exception:
        se = (int(season), int(ep) + 1)
    return f"{base}:{se[0]}:{se[1]}" if se else None


_PREFETCH_QUIESCE_MAX = 600.0
_PREFETCH_RETRY_DELAY = 120.0
_PREFETCH_RETRY_MAX = 1


def _episode_work_active(media: str, media_id: str) -> bool:
    """Whether the playing episode's own search work is still running.

    The fast/slow background finishers and the nzb lane's mounts own the
    probe slots, NNTP connections, and indexer quota; the prefetch must not
    compete with them for a result the viewer needs twenty minutes from now.
    """
    suffix = f":{media}:{media_id}"
    if any(key.endswith(suffix) and not task.done()
           for key, task in _background.items()):
        return True
    return (sources.in_progress(media, media_id)
            or nzb_lane.in_progress(media, media_id))


async def _wait_episode_idle(media: str, media_id: str) -> tuple[bool, bool]:
    """Wait on real completion objects, not a fixed delay/polling loop.

    Returns ``(waited, timed_out)``. New work registered while the first task
    snapshot settles is caught by the loop. The safety cap prevents one broken
    provider task from suppressing E+1 forever; normal picker/usenet work is
    already bounded well below it.
    """
    deadline = time.monotonic() + _PREFETCH_QUIESCE_MAX
    waited = False
    suffix = f":{media}:{media_id}"
    while _episode_work_active(media, media_id):
        waited = True
        left = deadline - time.monotonic()
        if left <= 0:
            return waited, True
        tasks = [task for key, task in _background.items()
                 if key.endswith(suffix) and not task.done()]
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=left, return_when=asyncio.ALL_COMPLETED)
            if pending:
                return waited, True
        left = deadline - time.monotonic()
        if sources.in_progress(media, media_id) and left > 0:
            await sources.wait_complete(media, media_id, left)
        left = deadline - time.monotonic()
        if nzb_lane.in_progress(media, media_id) and left > 0:
            await nzb_lane.wait_complete(media, media_id, left)
    return waited, False


def _cache_ready(key: str) -> bool:
    hit = _cache.get(key)
    return bool(hit and time.monotonic() - hit[0] < CACHE_REVERIFY_AFTER
                and any(_is_ranked(s) for s in hit[1]))


async def _wait_target_ready(keys: tuple[str, ...], media: str,
                             media_id: str, wait: float) -> bool:
    """Keep a prewarm intent alive while its detached finisher works."""
    deadline = time.monotonic() + max(wait, 0.0)
    suffix = f":{media}:{media_id}"
    while not any(_cache_ready(key) for key in keys):
        tasks = [task for key, task in _background.items()
                 if key.endswith(suffix) and not task.done()]
        if not tasks:
            return False
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        done, _ = await asyncio.wait(
            tasks, timeout=left, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            return False
    return True


def note_playback(media: str, media_id: str, picker_label: str,
                  release_sig: str = "") -> None:
    """Let real proxy startup refresh the exact cache entry that supplied it.

    This replaces the old foreground-current-episode re-search. Only the exact
    selected, still-ranked release can refresh the timestamp; an unknown identity
    cannot make an unrelated stale leader current.
    """
    if not release_sig:
        return
    profile = "mobile" if "mob" in (picker_label or "") else "full"
    slow = "slow" in (picker_label or "")
    key = (f"slow:{profile}:{media}:{media_id}" if slow
           else f"{profile}:{media}:{media_id}")
    hit = _cached_candidate(key)
    if not hit:
        return
    _, streams = hit
    selected = next((s for s in streams
                     if _is_ranked(s) and telemetry.signature(s) == release_sig),
                    None)
    if selected is None:
        return
    ordered = [selected] + [s for s in streams if s is not selected]
    rank = 0
    normalized = []
    for stream in ordered:
        if _is_ranked(stream):
            rank += 1
            stream = _renumber(stream, rank)
        normalized.append(stream)
    _cache[key] = (time.monotonic(), normalized)
    telemetry.record_cache_event(
        "playback_cache_touch", target_id=media_id, count=1)


async def _prepare_and_cache(media: str, playing_id: str, target_id: str,
                             picker_label: str,
                             *, invalidate_first: bool, both: bool,
                             prefetch_cap: bool = False,
                             started_at: float | None = None,
                             ) -> tuple[list[str], float | None]:
    """Shared core for prefetch (next episode) and refresh (this episode).

    Waits for the *playing* episode's own search work (``playing_id``) to drain
    first — its fast/slow finishers and the nzb lane own the probe slots and NNTP
    connections the viewer needs, and neither prep may compete with them — then
    runs the picker(s) for ``target_id`` and stores each result under its own
    key. No stream bytes are downloaded; the verification probes' few MB are the
    only transfer. ``both`` runs the slow *and* fast pickers, viewer's first
    (Stremio shows both addons' lists); ``invalidate_first`` drops any cached
    result for the target so a refresh re-searches instead of re-storing the
    stale list. Raw searches and verification evidence are shared, so a second
    (or a repeat) pick reuses the first one's work."""
    profile = "mobile" if "mob" in (picker_label or "") else "full"
    slow = "slow" in (picker_label or "")
    _waited, timed_out = await _wait_episode_idle(media, playing_id)
    if timed_out:
        telemetry.record_cache_event(
            "prewarm_wait_timeout", target_id=target_id,
            seconds=_PREFETCH_QUIESCE_MAX)
    if invalidate_first:
        invalidate(target_id)
        suffix = f":{target_id}"
        for key in [key for key in _stale_cache if key.endswith(suffix)]:
            _stale_cache.pop(key, None)
        sources.invalidate(media, target_id)
    # Attribute these probes to the episode being prepped, not the one playing.
    telemetry.request_ctx.set({"media": media, "media_id": target_id,
                               "picker": (picker_label or "") + "/prefetch"})
    # Each result goes only to its own key — a fast delivery-ranked answer must
    # not freeze into the slow best-quality cache (or vice versa).
    pickers = [("slow", pick_slow), ("fast", pick)]
    if not slow:
        pickers.reverse()
    if not both:
        pickers = pickers[:1]        # refresh only the list the viewer is on
    # A next-episode prefetch caps the NZB lane to the prefetch mount budget so
    # it does not article-check the whole wave for an episode that may never be
    # opened; a refresh of the current episode mounts the full wave as usual.
    lane_scope = (nzb_lane.prefetch_scope() if prefetch_cap
                  else contextlib.nullcontext())
    cached = []
    first_ready: float | None = None
    with lane_scope:
        for kind, one_pick in pickers:
            streams = await one_pick(media, target_id, profile)
            if not streams:
                logger.info(f"prefetch: no streams for {target_id} ({kind})")
                continue
            # A notice (checking/downloading) has a short, separately-managed
            # lifetime; caching one freezes a transient state into a long answer.
            if _contains_notice(streams):
                state = streams[0].get(_NOTICE_STATE_KEY, "notice")
                logger.info(f"prefetch: {target_id} still {state} ({kind}); "
                            f"not caching")
                continue
            if not any(_is_ranked(s) for s in streams):
                logger.info(f"prefetch: no verified stream for {target_id} "
                            f"({kind}); not caching")
                continue
            _store(f"slow:{profile}:{media}:{target_id}" if kind == "slow"
                   else f"{profile}:{media}:{target_id}", streams)
            if first_ready is None and started_at is not None:
                first_ready = time.monotonic() - started_at
            top = " ".join((streams[0].get("name") or "").split())[:40]
            cached.append(f"{kind} #1 {top!r}")
    if cached:
        logger.info(f"prefetch: cached {target_id} — " + "; ".join(cached))
    return cached, first_ready


async def prefetch_next(media: str, media_id: str, picker_label: str) -> None:
    """Search-and-cache episode E+1 the moment E starts playing, so opening the
    next episode is an instant cache hit with a verified #1 on either addon.
    Runs both pickers, viewer's first. Best-effort, deduped per next-episode id."""
    started = time.monotonic()
    nxt = await _next_episode(media_id)
    if not nxt:
        return
    profile = "mobile" if "mob" in (picker_label or "") else "full"
    job = ("next", profile, nxt)
    active = _episode_work_active(media, media_id)
    telemetry.record_cache_event(
        "prewarm_intent", target_id=nxt, active=active)
    if job in _prefetching:
        telemetry.record_cache_event(
            "prewarm_join", target_id=nxt, active=active)
        return
    fast_key = f"{profile}:{media}:{nxt}"
    slow_key = f"slow:{profile}:{media}:{nxt}"
    if _cache_ready(fast_key) and _cache_ready(slow_key):
        telemetry.record_cache_event(
            "prewarm_cache_hit", target_id=nxt, seconds=0)
        return
    _prefetching.add(job)
    try:
        ready_keys = ((slow_key, fast_key) if "slow" in (picker_label or "")
                      else (fast_key, slow_key))
        for attempt in range(_PREFETCH_RETRY_MAX + 1):
            cached, ready = await _prepare_and_cache(
                media, media_id, nxt, picker_label,
                invalidate_first=False, both=True, prefetch_cap=True,
                started_at=started)
            if cached:
                telemetry.record_cache_event(
                    "prewarm_ready", target_id=nxt,
                    seconds=(ready if ready is not None
                             else time.monotonic() - started),
                    count=len(cached), detail=f"attempt {attempt + 1}")
                return
            if await _wait_target_ready(
                    ready_keys, media, nxt, SLOW_FINISH_DEADLINE):
                telemetry.record_cache_event(
                    "prewarm_ready", target_id=nxt,
                    seconds=time.monotonic() - started, count=1,
                    detail=f"background attempt {attempt + 1}")
                return
            if attempt < _PREFETCH_RETRY_MAX:
                telemetry.record_cache_event(
                    "prewarm_retry", target_id=nxt,
                    seconds=time.monotonic() - started,
                    detail=f"attempt {attempt + 2}")
                await asyncio.sleep(_PREFETCH_RETRY_DELAY)
                if any(_cache_ready(key) for key in ready_keys):
                    telemetry.record_cache_event(
                        "prewarm_ready", target_id=nxt,
                        seconds=time.monotonic() - started, count=1,
                        detail="became ready during backoff")
                    return
                # Refresh URL-bearing catalogs for the retry; durable release and
                # exact-link health evidence remains intact.
                sources.invalidate(media, nxt)
        telemetry.record_cache_event(
            "prewarm_exhausted", target_id=nxt,
            seconds=time.monotonic() - started)
    except Exception:
        logger.exception(f"prefetch: failed for {media_id}")
    finally:
        _prefetching.discard(job)


async def refresh(media: str, media_id: str, picker_label: str) -> None:
    """Explicit force-refresh retained for callers/admin tools.

    Normal playback now proves the selected current link and prioritizes E+1;
    it no longer burns bandwidth re-searching an episode that is already playing.
    """
    profile = "mobile" if "mob" in (picker_label or "") else "full"
    job = ("refresh", profile, media_id)
    if not media_id or job in _prefetching:
        return
    _prefetching.add(job)
    try:
        await _prepare_and_cache(media, media_id, media_id, picker_label,
                                 invalidate_first=True, both=False)
    except Exception:
        logger.exception(f"refresh: failed for {media_id}")
    finally:
        _prefetching.discard(job)


async def shutdown() -> None:
    """Cancel picker-owned background work and close its metadata client."""
    tasks = list(_background.values()) + list(_acquire_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _background.clear()
    _acquire_tasks.clear()
    candidate_health.flush()
    await _client.aclose()
