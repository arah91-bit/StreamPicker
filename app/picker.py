"""Source orchestration for two sibling addons that share one search.

Both pickers verify candidates by actually pulling video bytes (see probe.py)
and hand Nuvio a list where "just pick the first one" is safe. They differ in
temperament:

  * pick()      — the *fast* picker. Optimises for a verified answer as soon
                  as possible: a "fast lane" AIOStreams config can settle the
                  request in a few seconds, and even the full path bails the
                  moment it has enough. Bounded by TOTAL_DEADLINE.

  * pick_slow() — the *best quality* picker. Optimises for the best stream
                  that actually plays: it waits for every source to run its
                  course, merges *all* candidates, ranks them, and probes deep
                  before answering. Bounded by SLOW_TOTAL_DEADLINE.

Crucially they do NOT search independently. Every upstream call goes through
app.sources, which searches each title at most once and lets both pickers (and
retries, and other viewers) join that one search. That is what keeps the slow
picker from doubling API calls and tripping upstream rate limits — its job is
to dig through the fast picker's search results, not to launch its own.

Candidates are ranked best-quality-first before probing (resolution, then
source tier a la TRaSH guides: Remux > BluRay > WEB-DL > WEBRip > HDTV), so
the result is the best quality that actually plays, not just anything that
plays.

Whenever a deadline forces an early (unverified or partial) answer, a
background task finishes verification and caches it — a retry or the next
household viewer gets the verified list instantly. A probe of an nzbdav-backed
stream also has a useful side effect: it forces nzbdav to fetch the opening
segments, so by the time the user presses play the slow first byte is paid for.
"""

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

from app import (acquire, content_identity, decode_health, library, meta, probe,
                 reputation, sources, telemetry, usenet as nzb_lane, vprobe)

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
# when the first verified, language-eligible 1080p/4K source passes. This hard
# ceiling exists only because the player kills addon requests at 60 seconds.
TOTAL_DEADLINE = float(os.environ.get("TOTAL_DEADLINE", "55"))
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
PROBE_BATCH = int(os.environ.get("FAST_PROBE_BATCH", "3"))
# Safety cap only; the normal stop condition is the first good verified source.
FAST_RACE_DEADLINE = float(os.environ.get("FAST_RACE_DEADLINE", "55"))
CACHE_TTL = float(os.environ.get("CACHE_TTL", str(6 * 3600)))
# Release catalogs can live for hours, but a signed playback URL must not be
# treated as freshly verified for that long. Result lists get a short TTL and
# their leader is re-probed after the still-shorter freshness window.
RESULT_CACHE_TTL = min(CACHE_TTL, float(os.environ.get("RESULT_CACHE_TTL", "900")))
CACHE_REVERIFY_AFTER = min(
    RESULT_CACHE_TTL, float(os.environ.get("CACHE_REVERIFY_AFTER", "120")))
CACHE_REVERIFY_TTFB = float(os.environ.get("CACHE_REVERIFY_TTFB", "8"))
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
# The slow picker doesn't need to probe every link — it only needs the single
# best-quality one that *works* at #1, plus a few backups. So it probes the top
# SLOW_MAX_PROBES *by quality*, all at once (the household has no simultaneous-
# stream cap on its IP), and waits for that slice to settle. Because it verifies
# the highest-quality candidates and then ranks the verified ones by quality, the
# stream it puts first is the best quality that actually plays. Dead links drop
# out of the slice on their own via the probe timeout; the whole pass is bounded
# by the response/background deadline so a hung link never holds it up.
SLOW_MAX_PROBES = int(os.environ.get("SLOW_MAX_PROBES", "16"))
SLOW_CONCURRENCY = int(os.environ.get("SLOW_CONCURRENCY", "16"))
# Reserve a small part of the foreground probe wave for usable direct-Usenet
# candidates.  Otherwise a healthy 1080p NZB can sit forever below a dense page
# of nominal 4K debrid results and never get its one chance to prove itself.
SLOW_NZB_PROBES = max(0, int(os.environ.get("SLOW_NZB_PROBES", "4")))
# The background finisher is off Nuvio's clock, so it digs a little deeper (past
# the foreground slice) and refines the cached best-quality answer for the retry.
SLOW_FINISH_MAX_PROBES = int(os.environ.get("SLOW_FINISH_MAX_PROBES", "24"))
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
# excellent. Fast never waits beyond its seven-second response ceiling.
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

PROFILES = {
    "full": {"max_res": 10_000, "max_bps": None},
    # phones/tablets on cell data: 1080p cap, file bitrate <= ~12 Mbps
    "mobile": {"max_res": 1080, "max_bps": 1_500_000},
}

_client = httpx.AsyncClient(timeout=None, headers={"User-Agent": "Stremio"})

_cache: dict[str, tuple[float, list[dict]]] = {}
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


_SIZE_RE = re.compile(r"([\d.]+)\s*(GB|MB)", re.IGNORECASE)


def _size_bytes(s: dict) -> int | None:
    v = (s.get("behaviorHints") or {}).get("videoSize")
    if v:
        return int(v)
    m = _SIZE_RE.search(_stream_text(s))
    if m:
        mult = 1e9 if m.group(2).upper() == "GB" else 1e6
        return int(float(m.group(1)) * mult)
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
    size = (s.get("behaviorHints") or {}).get("videoSize")
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
    _assess_stream_identity(
        s, getattr(r, "media_secs", 0.0) or None, record=True)
    return _identity_leader(s)


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
# Identity, rather than a truthy value, makes this impossible to forge through
# an upstream addon's JSON response.  Raw streams are still scrubbed at every
# ingestion boundary so even a same-named private field cannot linger.
_VERIFIED_SENTINEL = object()
_INTERNAL_KEYS = ("_effres", "_vbitrate", "_vheight", "_vcodec", "_vcodec_real",
                  "_acodecs", "_audio_langs", "_content_kind", "_qbps",
                  "_speed", "_ttfb",
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
# beside the transport gate and make its evidence the first ranking dimension.
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

    s[_IDENTITY_STATE_KEY] = result.state
    s[_IDENTITY_RANK_KEY] = result.rank
    s[_IDENTITY_EVIDENCE_KEY] = result.evidence
    s[_IDENTITY_TEXT_KEY] = filename or label
    if record:
        _record_identity_once(s, result, " | ".join(filter(None, (filename, label))))
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


def _quality_key(s: dict):
    # Order: identity evidence, audio, decode-ok, clean-first, then resolution,
    # bitrate,
    # then source tier, then custom-format score, then size. Deliberate
    # departures from Radarr's resolution→source→size order, all from the
    # household's feedback:
    #  * decodability and clean-vs-hardsub are the TOP keys, so a release the
    #    player provably can't open (_decode_ok) or with burned-in foreign
    #    subtitles (_hardsub) sorts below every clean one regardless of
    #    resolution — it only surfaces as a last resort;
    #  * resolution is the bitrate-*capped* effective one (_effective_resolution),
    #    so a starved fake/upscaled 4K can't win on nominal pixels; and
    #  * bitrate (_qbps: true video bitrate when known, else overall) outranks the
    #    source label, so a fat WEBRip beats a lean WEB-DL at the same resolution
    #    instead of losing to it on the tier name alone.
    res = s.get("_effres")
    if res is None:
        res = _resolution(s)
    qbps = s.get("_qbps")
    if qbps is None:
        qbps = 0
    clean = 0 if _hardsub(s) else 1
    # audio_ok is the very top key: a wrong-language dub (no English, no original)
    # sorts below everything acceptable, whatever its resolution — you can't watch
    # a 4K you don't understand.
    identity_rank = s.get(_IDENTITY_RANK_KEY)
    if identity_rank is None:
        identity_rank = 5 if _identity_profile_ctx.get() is None else 1
    return (identity_rank, _audio_ok(s), _decode_ok(s), clean, res, qbps,
            _source_rank(s),
            _cf_score(s), _size_bytes(s) or 0)


# ── delivery-aware ranking of *verified* streams ─────────────────────────────
# _quality_key ranks candidates on their metadata alone; that's what we probe in.
# But once a stream is verified we also know how it actually delivered — its
# time-to-first-byte and sustained throughput — and those decide between sources
# the metadata calls a tie. Library streams (no probe) are treated as the most
# reliable: an instant start and effectively unbounded speed.
LIBRARY_SPEED = 1e12
_BAND_LOG = math.log(1 + QUALITY_BAND)


def _qbps_bucket(qbps: float) -> int:
    """Coarsen a bitrate into ~QUALITY_BAND-wide relative buckets, so encodes of
    effectively-equal quality tie on this key and let measured delivery speed —
    not a rounding-error of size — break them, while a genuinely fatter encode
    still lands in a higher bucket and wins outright."""
    if qbps <= 0:
        return 0
    return round(math.log(qbps) / _BAND_LOG)


def _delivery_key(qkey: tuple, ttfb: float, speed: float):
    """Sort key for a *verified* stream (descending). `clean` (no burned-in
    foreign subs) stays the very top component so a clean release always outranks
    a hardsub one, even a slow-starting clean over a fast hardsub — the household
    would rather wait than watch burned-in subtitles. Below that, two passes fall
    out of one key: `good_start` sorts every prompt-starting source above every
    slow-starting one — but when *all* survivors are slow they still rank among
    themselves, so a slow start beats no stream. Within a start class we keep
    quality first (resolution, then a *bucketed* bitrate so near-equal encodes
    tie, then source tier and custom-format score) and only then fall to
    throughput, so 'when quality is similar, pick the faster-streaming one'.
    Exact bitrate and size are last, purely deterministic tie-breaks."""
    identity, audio, decode, clean, res, qbps, srank, cf, size = qkey
    good_start = 1 if ttfb <= GOOD_TTFB else 0
    return (identity, audio, decode, clean, good_start, res,
            _qbps_bucket(qbps), srank, cf, speed, qbps, size)


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
    identity, audio, decode, clean, res, qbps, srank, cf, size = _quality_key(s)
    ttfb = 0.0 if r is None else r.ttfb
    speed = LIBRARY_SPEED if r is None else r.speed_bps
    good_start = 1 if ttfb <= GOOD_TTFB else 0
    return (identity, audio, decode, clean, res, _qbps_bucket(qbps),
            good_start, speed, srank, cf, qbps, size)


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
    return profile

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
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))


def invalidate(media_id: str) -> None:
    """Drop every cached pick result for a title. Called when playback
    evidence arrives that the cached ranking is wrong (a player-rejected
    release was #1): the next open re-picks with the cooldown applied, so the
    list the user sees matches what the proxy will actually serve."""
    if not media_id:
        return
    suffix = f":{media_id}"
    for k in [k for k in _cache if k.endswith(suffix)]:
        _cache.pop(k, None)


def _as_verified(lib: list[dict]) -> list[tuple[dict, None]]:
    """Local library (Jellio) files are reliable, so they enter the verified
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


async def _cached_pick(key: str, media: str, media_id: str, profile: dict,
                       slow: bool = False) -> list[dict] | None:
    """Serve a recent result instantly; periodically re-probe its verified #1."""
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
        return None
    if not _apply_probe_evidence(top, passed[0][1], runtime):
        logger.info(f"cache leader lost identity eligibility for {key}; repicking")
        invalidate(media_id)
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
    show the 'finding best stream' notice rather than an unverified #1, and make
    sure the background finisher is running so the retry gets a verified answer
    from cache. No _notice_until here — the fast race is cheap to re-run, and the
    cached verified result overrides on the retry the moment the finisher lands."""
    if cache_key not in _background:
        _background[cache_key] = asyncio.create_task(
            _finish_in_background(cache_key, media, media_id, profile,
                                  runtime, list(pool)))
    logger.info(f"{cache_key}: no verified link in fast budget — 'checking' "
                f"notice, finisher verifying {len(pool)} candidates in background")
    # Ambiguous candidates remain visible as lower manual fallbacks, but the
    # auto-picker sees the safe notice first until one earns strong identity +
    # transport evidence.
    lower = [_ingested_stream(s) for s in pool
             if s.get(_IDENTITY_STATE_KEY) != content_identity.CONTRADICTION]
    return [_notice_stream("checking"), *lower[:15]]


# ── fast picker ─────────────────────────────────────────────────────────────

async def _finish_in_background(cache_key: str, media: str, media_id: str,
                                profile: dict, runtime: float,
                                extra: list[dict]) -> None:
    """Finish every source/probe off-request and refresh the fast cache."""
    try:
        fast, stremthru, mediafusion, nzb, extras = await asyncio.gather(
            sources.get(sources.FAST, media, media_id, wait=15),
            sources.get(sources.STREMTHRU, media, media_id, wait=45),
            sources.get(sources.MEDIAFUSION, media, media_id, wait=60),
            sources.get(sources.NZB, media, media_id, wait=USENET_FINISH_WAIT),
            _gather_extras(media, media_id, wait=60),
        )
        complete_nzb = await nzb_lane.wait_complete(
            media, media_id, USENET_FINISH_WAIT)
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
        unprobed.sort(key=_quality_key, reverse=True)
        finish_slice = _slow_probe_slice(
            unprobed,
            SLOW_FINISH_MAX_PROBES, skip_idents=inherited_idents)
        probed = await _probe_bounded(
            finish_slice, runtime, USENET_TTFB_MAX, len(finish_slice),
            time.monotonic() + SLOW_FINISH_DEADLINE)
        verified = _combine_verified(inherited, probed)
        if verified:
            # Off-clock, so measure true video bitrate of the leaders too: the
            # cached answer a retry gets should be compression-honest, not
            # label-trusting (matters most for size-less free-addon streams).
            await _refine_video_bitrate(probed, runtime, 45)
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


def _count_tiers(verified: list[tuple[dict, probe.ProbeResult | None]]) -> tuple[int, int]:
    """(verified 2160p count, verified 1080p-or-1440p count)."""
    tiers = [int(s.get("_effres") or _resolution(s)) for s, _ in verified]
    n4k = sum(1 for res in tiers if res >= 2160)
    n1080 = sum(1 for res in tiers if 1080 <= res < 2160)
    return n4k, n1080


def _enough(verified: list[tuple[dict, probe.ProbeResult | None]]) -> bool:
    """First verified, language-eligible high-quality source is the stop bar."""
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
    deadline = min(t0 + FAST_RACE_DEADLINE, t0 + TOTAL_DEADLINE)

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
        # Local is usually excellent, but stale Jellyfin/Jellio URLs still
        # happen. Put library files through the same byte gate as every other
        # source before allowing one to become the automatic first result.
        _add_streams(streams)

    if lib_task and lib_task.done():
        _ingest_library(lib_task)

    try:
        while not _enough(verified) and time.monotonic() < deadline:
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
            for diverse_only in (True, False):
                for stream in (s for s in pool if s.get("url") not in probed):
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
                    probed.add(stream["url"])
                    if ident:
                        ident_inflight.add(ident)
                    if host:
                        active_hosts.add(host)
                    outcomes: list = []
                    task = asyncio.create_task(probe.probe_race(
                        [stream], need_bps, PROBE_TTFB_MAX, want=1,
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
    # query Jellio alongside the online search and put any library hit first.
    started = time.monotonic()
    lib_task = (asyncio.create_task(library.streams(media, media_id))
                if library.enabled() else None)
    streams = await _pick_online(media, media_id, profile_name, lib_task=lib_task)
    if lib_task:
        try:
            # A ready/cached library answer is effectively free. If online has
            # already produced a verified #1, never delay it waiting on Jellio;
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
                       lib_task: asyncio.Task | None = None) -> list[dict]:
    profile = PROFILES[profile_name]
    cache_key = f"{profile_name}:{media}:{media_id}"
    hit = await _cached_pick(cache_key, media, media_id, profile)
    if hit is not None:
        logger.info(f"cache hit for {cache_key}")
        return hit

    t0 = time.monotonic()

    def left() -> float:
        return TOTAL_DEADLINE - (time.monotonic() - t0)

    # Register all shared searches now so slower sources start at t=0
    # (and so a sibling slow-picker request can join these instead of starting
    # its own). We never cancel them: whoever bails early just detaches, and
    # the search finishes into the shared cache for the next joiner.
    for src in sources.search_all():
        sources.start(src, media, media_id)

    # Runtime and language metadata resolve while upstream searches are already
    # in flight. Bound cold metadata so it cannot consume the whole fast budget.
    runtime_task = asyncio.create_task(_runtime_seconds(media, media_id))
    _accept_langs.set(None)
    _original_lang_known.set(False)
    try:
        async with asyncio.timeout(max(min(left(), 8.5), 0.05)):
            await _resolve_accept_langs(media, media_id)
    except (TimeoutError, asyncio.TimeoutError):
        _accept_langs.set(None)
        _original_lang_known.set(False)
    # Semantic identity is a hard prerequisite for an automatic result. Resolve
    # it while the already-started sources search; a timeout installs an exact-
    # request but alias-empty fail-closed profile, under which only validated
    # Newznab/Jellyfin IMDb evidence may lead.
    await _resolve_identity_profile(
        media, media_id, timeout=max(min(left(), 8.5), 0.05))
    try:
        runtime = await asyncio.wait_for(
            runtime_task, timeout=max(min(left(), 6.5), 0.05))
    except Exception:
        if not runtime_task.done():
            runtime_task.cancel()
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
    """Collect streams from every user-added addon (app.sources.EXTRAS). Each is
    joined like any built-in source; failures/timeouts yield nothing and never
    break the pick."""
    if not sources.EXTRAS:
        return []
    results = await asyncio.gather(
        *(sources.get(k, media, media_id, wait=wait) for k in sources.EXTRAS),
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


def _slow_probe_slice(ok: list[dict], max_probes: int,
                      nzb_want: int = SLOW_NZB_PROBES,
                      skip_idents: set | frozenset = frozenset()) -> list[dict]:
    """Quality-ordered probe wave — one probe per *distinct release* — with a
    small direct-Usenet quota.

    Several addons often carry the same file (scrapers sharing an upstream), so
    the wave takes the best copy of each release (_release_ident) first, which
    lets it cover max_probes *different* releases instead of burning slots
    re-checking one. Only when the pool runs out of distinct releases do
    duplicate copies fill the remaining slots (a twin on another host doubles as
    failover evidence). `skip_idents` = releases already verified elsewhere
    (e.g. by the fast picker); their copies need no probe at all.

    The quota only decides what gets *tested*. Final verified results are still
    sorted by ``_verified_quality_key``, so admitting a lower-quality NZB to the
    wave cannot make it the slow picker's #1 unless it genuinely ranks highest.
    """
    limit = max(0, max_probes)
    if not ok or not limit:
        return []
    seen_idents = {i for i in skip_idents if i}
    selected: list[dict] = []
    selected_urls: set[str] = set()
    for s in ok:                       # pass 1: distinct releases, best copy each
        if len(selected) >= limit:
            break
        ident = _release_ident(s)
        if ident and ident in seen_idents:
            continue
        if ident:
            seen_idents.add(ident)
        selected.append(s)
        selected_urls.add(s.get("url"))
    if len(selected) < limit:          # pass 2: top up with best duplicate copies
        for s in ok:
            if len(selected) >= limit:
                break
            if s.get("url") in selected_urls:
                continue
            ident = _release_ident(s)
            if ident and ident in skip_idents:
                continue               # verified elsewhere — a probe proves nothing
            selected.append(s)
            selected_urls.add(s.get("url"))
    direct = [s for s in ok if _is_direct_nzb(s)]
    target = min(max(0, nzb_want), limit, len(direct))
    have = sum(_is_direct_nzb(s) for s in selected)

    for stream in direct:
        if have >= target:
            break
        if stream.get("url") in selected_urls:
            continue
        replace = next((i for i in range(len(selected) - 1, -1, -1)
                        if not _is_direct_nzb(selected[i])), None)
        if replace is None:
            break
        selected_urls.discard(selected[replace].get("url"))
        selected[replace] = stream
        selected_urls.add(stream.get("url"))
        have += 1

    # Keep the original pure-quality order inside the chosen wave.
    return [s for s in ok if s.get("url") in selected_urls][:limit]


async def _probe_bounded(ok: list[dict], runtime: float, ttfb_max: float,
                         max_probes: int, hard_deadline: float,
                         ) -> list[tuple[dict, probe.ProbeResult]]:
    """Verify the top `max_probes` candidates *by quality*, all at once, and wait
    for that slice to settle (or `hard_deadline`, a monotonic time). Probing the
    highest-quality candidates — rather than stopping at the first few links that
    happen to answer fastest — is what lets the caller put the genuine best
    quality that plays at #1. Dead links fail out of the slice on their own via
    the probe timeout; probe_race keeps every probe in flight until it settles or
    the deadline, then cancels whatever's left. Returns the ones that verified."""
    budget = hard_deadline - time.monotonic()
    if not ok or budget <= 1:
        return []
    cands = ok[:max_probes]                    # already quality-ranked, best first
    # probe_race owns this deadline and returns every success accumulated before
    # it.  Wrapping it in another timer at the exact same instant can cancel it
    # during cleanup and throw those already-verified successes away.
    results = await probe.probe_race(
        cands, _need_bps_fn(runtime), ttfb_max,
        want=len(cands),               # collect all that pass, no early bail
        concurrency=SLOW_CONCURRENCY, deadline=hard_deadline,
        expect_secs=runtime,
        deep_check_of=lambda s: (_size_bytes(s) if _is_direct_nzb(s) else None))
    # Transport success is necessary but not sufficient. Only candidates whose
    # identity was already strong, or became strong through a measured-runtime
    # corroboration, enter the verified tier.
    return [(s, r) for s, r in results
            if _apply_probe_evidence(s, r, runtime)]


async def _finish_slow(cache_key: str, media: str, media_id: str,
                       profile: dict, runtime: float) -> None:
    """Let every source run fully to completion (usenet is the long one), then
    do the deep merge/probe and cache the best-quality result for the retry."""
    try:
        fast, stremthru, mediafusion, nzb, extras = await asyncio.gather(
            sources.get(sources.FAST, media, media_id, wait=15),
            sources.get(sources.STREMTHRU, media, media_id, wait=45),
            sources.get(sources.MEDIAFUSION, media, media_id, wait=60),
            sources.get(sources.NZB, media, media_id, wait=60),
            _gather_extras(media, media_id, wait=60),
        )
        complete_nzb = await nzb_lane.wait_complete(
            media, media_id, USENET_FINISH_WAIT)
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
        unprobed.sort(key=_quality_key, reverse=True)
        # Off Nuvio's clock, so dig deeper by quality than the foreground slice
        # AND wait patiently (SLOW_FINISH_TTFB_MAX) for slow-to-start high-quality
        # sources, so they survive verification and can take #1 on the retry.
        finish_slice = _slow_probe_slice(
            unprobed,
            SLOW_FINISH_MAX_PROBES, skip_idents=fast_idents)
        verified = await _probe_bounded(
            finish_slice, runtime, SLOW_FINISH_TTFB_MAX, len(finish_slice),
            time.monotonic() + SLOW_FINISH_DEADLINE)
        await _refine_video_bitrate(verified, runtime, 45)
        all_verified = _combine_verified(fast_verified, verified)
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
    profile = PROFILES[profile_name]
    cache_key = f"slow:{profile_name}:{media}:{media_id}"
    hit = await _cached_pick(cache_key, media, media_id, profile, slow=True)
    if hit is not None:
        logger.info(f"cache hit for {cache_key}")
        return hit

    # Local library (Jellio) — a fast, reliable source; fire it now, use later.
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

    t0 = time.monotonic()
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
    unprobed.sort(key=_quality_key, reverse=True)
    inherited_nzb = sum(_is_direct_nzb(s) for s, _ in fast_verified)
    probe_slice = _slow_probe_slice(
        unprobed, SLOW_MAX_PROBES,
        max(SLOW_NZB_PROBES - inherited_nzb, 0), skip_idents=fast_idents)
    verified = await _probe_bounded(
        probe_slice,
        runtime, min(SLOW_TTFB_MAX, max(left(), 5)),
        len(probe_slice), resp_deadline - 3)
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
    # answer with a verified stream (a fast-picker inherit, or the first few
    # probes). The finisher waits out every source and caches the best-quality
    # result for the retry / the cache.
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
_prefetching: set[str] = set()


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


async def prefetch_next(media: str, media_id: str, picker_label: str) -> None:
    """Search-and-cache the next episode — fired by the proxy the moment a series
    episode starts playing. Runs the same picker the viewer is on for episode
    E+1, so its full search + verification lands in both pickers' result caches;
    opening the next episode is then an instant cache hit with a verified #1.
    Caches only the picked result list — no stream bytes are downloaded (the
    verification probes' few MB are the only transfer). Best-effort, deduped per
    next-episode id."""
    nxt = await _next_episode(media_id)
    if not nxt or nxt in _prefetching:
        return
    _prefetching.add(nxt)
    try:
        profile = "mobile" if "mob" in (picker_label or "") else "full"
        slow = "slow" in (picker_label or "")
        # Attribute the prefetch's probes to the episode being prepped, not the
        # one currently playing.
        telemetry.request_ctx.set({"media": media, "media_id": nxt,
                                   "picker": (picker_label or "") + "/prefetch"})
        streams = await (pick_slow if slow else pick)(media, nxt, profile)
        if not streams:
            logger.info(f"prefetch: no streams for next episode {nxt}")
            return
        # Notices deliberately have short, separately-managed lifetimes. Putting
        # one in the normal result cache turns a transient check/download state
        # into a six-hour answer, and a fast prefetch has no slow-key finisher to
        # replace the copy it would leave behind.
        if _contains_notice(streams):
            kind = streams[0].get(_NOTICE_STATE_KEY, "notice")
            logger.info(f"prefetch: next episode {nxt} still {kind}; not caching")
            return
        # Preserve picker semantics: a fast prefetch must not freeze its early
        # delivery-ranked answer into the slow best-quality cache (or vice
        # versa). Raw source and verification evidence are already shared.
        target = (f"slow:{profile}:{media}:{nxt}" if slow
                  else f"{profile}:{media}:{nxt}")
        _store(target, streams)
        top = " ".join((streams[0].get("name") or "").split())[:40]
        logger.info(f"prefetch: cached next episode {nxt} — #1 {top!r}")
    except Exception:
        logger.exception(f"prefetch: failed for {media_id}")
    finally:
        _prefetching.discard(nxt)


async def shutdown() -> None:
    """Cancel picker-owned background work and close its metadata client."""
    tasks = list(_background.values()) + list(_acquire_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _background.clear()
    _acquire_tasks.clear()
    await _client.aclose()
