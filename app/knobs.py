"""The complete catalog of tuning knobs, beyond the curated dashboard settings.

Every environment variable the app reads is reachable from the dashboard: the
common ones as rich controls in app/config.SETTINGS, the upstreams as tested
connections in app/config.CONNECTIONS, and everything else — the long tail of
timeouts, budgets, and thresholds — here, rendered in the collapsible
"Advanced tuning" section and accepted by the same allowlisted save path.

This module is also the single source the env reference is generated from
(app.envref), so the file an AI or operator edits by hand never drifts from
what the code actually reads. tests/test_settings_dashboard.py asserts exactly
that: every key read anywhere in app/ is either cataloged here, a curated
setting, a connection field, or explicitly EXCLUDED — nothing is unreachable.

Each entry: (key, type, default, unit, blurb). type ∈ bool | num | text |
choice. default is the resolved code default (what you get with the var unset),
shown as the field's placeholder so the current value reads as an override.
"""

# Keys read by the app that are NOT user-editable from the web store, with why.
# The store lives inside TELEMETRY_DIR, so letting the store move those paths is
# a foot-gun; the secret is the gate to the dashboard itself; notice URLs are
# internal wiring derived from ADDON_PUBLIC_URL.
EXCLUDE = {
    "ADDON_SECRET": "the gate to this dashboard — set it in .env only",
    "CONFIG_FILE": "path of this settings store itself",
    "TELEMETRY_DIR": "data directory root; set via the volume mount",
    "BUFFER_DIR": "derived from the data directory",
    "NZB_HEALTH_DB": "derived from the data directory",
    "NOTICE_URL": "internal, derived from the public base URL",
    "NOTICE_URL_THEATRICAL": "internal, derived from the public base URL",
}

# Advanced groups render in this order. (id, title)
GROUPS = [
    ("fast", "Fast picker & sources"),
    ("slow", "Best-quality picker"),
    ("bitrate", "Quality thresholds"),
    ("proxy", "Proxy, buffer & failover"),
    ("usenet", "Direct usenet"),
    ("acquire", "Library & acquire"),
    ("reputation", "Reputation & blocking"),
    ("telemetry", "Telemetry & caches"),
    ("identity", "Identity"),
]

_S, _B = "s", "bytes"

# (key, group, type, default, unit, blurb)
CATALOG = [
    # ── fast picker & sources ────────────────────────────────────────────────
    ("FAST_TIMEOUT", "fast", "num", "8", _S,
     "Comet search HTTP timeout."),
    ("STREMTHRU_TIMEOUT", "fast", "num", "20", _S,
     "StremThru Torz search timeout."),
    ("MEDIAFUSION_TIMEOUT", "fast", "num", "60", _S,
     "MediaFusion search timeout (first hit for a title live-scrapes)."),
    ("NZB_TIMEOUT", "fast", "num", "45", _S,
     "Direct-usenet lane timeout (search + fetch + mount top NZBs)."),
    ("FAST_ENOUGH_4K", "fast", "num", "1", "",
     "Verified 4K streams that make the fast picker stop early."),
    ("FAST_ENOUGH_1080", "fast", "num", "1", "",
     "Verified 1080p streams that make the fast picker stop early."),
    ("FAST_PROBE_BATCH", "fast", "num", "3", "",
     "How many candidates the fast picker probes at once per wave."),
    ("TOTAL_DEADLINE", "fast", "num", "55", _S,
     "Absolute ceiling on a fast request, including probing."),
    ("PROBE_TTFB_MAX", "fast", "num", "12", _S,
     "Max first-byte wait before a debrid probe is a failure."),
    ("USENET_TTFB_MAX", "fast", "num", "35", _S,
     "Max first-byte wait for a usenet probe (mounts start slower)."),
    ("USENET_FINISH_WAIT", "fast", "num", "3600", _S,
     "How long a usenet mount may keep filling the shared cache after answer."),
    ("CACHE_TTL", "fast", "num", "21600", _S,
     "How long a picker's answer for a title is reused."),
    ("CHECKING_NOTICE_TTL", "fast", "num", "180", _S,
     "How long the 'still checking' placeholder is cached before re-picking."),
    ("RAW_CACHE_TTL", "fast", "num", "21600", _S,
     "How long a non-empty upstream search result is shared between pickers."),
    ("RAW_NEG_TTL", "fast", "num", "90", _S,
     "How long an empty search result is reused before re-searching."),
    ("FFPROBE_TIMEOUT", "fast", "num", "20", _S,
     "ffprobe timeout when measuring true video bitrate."),
    ("GOOD_TTFB", "fast", "num", "4.0", _S,
     "First-byte time target; above it a source counts as a slow start."),

    # ── best-quality (slow) picker ───────────────────────────────────────────
    ("SLOW_TOTAL_DEADLINE", "slow", "num", "55", _S,
     "Ceiling before the slow picker answers with its best so far."),
    ("SLOW_PROBE_RESERVE", "slow", "num", "18", _S,
     "Time held back from the deadline to guarantee probing happens."),
    ("SLOW_TTFB_MAX", "slow", "num", "35", _S,
     "Max first-byte wait for a slow-picker probe."),
    ("SLOW_CONCURRENCY", "slow", "num", "16", "",
     "How many streams the slow picker probes in parallel."),
    ("SLOW_NZB_PROBES", "slow", "num", "2", "",
     "Usenet candidates the slow picker will mount+probe per title."),
    ("SLOW_FINISH_MAX_PROBES", "slow", "num", "24", "",
     "Probe budget for the background 'finish' pass that keeps digging."),
    ("SLOW_FINISH_DEADLINE", "slow", "num", "240", _S,
     "How long the background finish pass may run."),
    ("SLOW_VIDEO_PROBE_N", "slow", "num", "4", "",
     "Top candidates ffprobe'd for true bitrate in the quality pass."),
    ("SLOW_VIDEO_PROBE_MIN_BUDGET", "slow", "num", "6", _S,
     "Skip the ffprobe pass when less than this much time is left."),

    # ── quality thresholds ───────────────────────────────────────────────────
    ("QUALITY_BAND", "bitrate", "num", "0.15", "",
     "Fractional window within which releases count as equal quality."),
    ("HARDSUB_DEMOTE", "bitrate", "bool", "1", "",
     "Demote releases with burned-in hardcoded subtitles below clean ones."),
    ("MIN_BPS_2160", "bitrate", "num", "10000000", "bps",
     "Minimum real bitrate to accept a stream as genuine 4K."),
    ("MIN_BPS_1080", "bitrate", "num", "3500000", "bps",
     "Minimum real bitrate to accept a stream as genuine 1080p."),
    ("MIN_BPS_720", "bitrate", "num", "1200000", "bps",
     "Minimum real bitrate to accept a stream as genuine 720p."),
    ("UNKNOWN_NEED_2160", "bitrate", "num", "8000000", "bps",
     "Assumed bitrate floor for an untagged release treated as 4K."),
    ("UNKNOWN_NEED_1080", "bitrate", "num", "2500000", "bps",
     "Assumed bitrate floor for an untagged release treated as 1080p."),
    ("UNKNOWN_NEED_720", "bitrate", "num", "1000000", "bps",
     "Assumed bitrate floor for an untagged release treated as 720p."),
    ("UNKNOWN_NEED_480", "bitrate", "num", "500000", "bps",
     "Assumed bitrate floor for an untagged release treated as 480p."),

    # ── proxy, buffer & failover ─────────────────────────────────────────────
    ("PROXY_WRAP_MAX", "proxy", "num", "8", "",
     "How many streams per list get proxy-wrapped for failover + stats."),
    ("PROXY_MAX_FAILOVER", "proxy", "num", "4", "",
     "Backup candidates carried in each playback token."),
    ("PROXY_SESSION_TTL", "proxy", "num", "86400", _S,
     "How long a playback token stays valid."),
    ("PROXY_SESSION_MAX_BYTES", "proxy", "num", "20971520", _B,
     "Rotate the playback-session log past this size."),
    ("PREFER_FILE_CONTAINERS", "proxy", "bool", "1", "",
     "Prefer mp4/mkv over transport streams when starting playback."),
    ("TWIN_SPLICE", "proxy", "bool", "1", "",
     "Allow splicing to a byte-identical copy on another debrid mid-stream."),
    ("TWIN_PROACTIVE", "proxy", "bool", "1", "",
     "Switch to a twin before a stall, on sustained slowness."),
    ("TWIN_SPLICE_MAX", "proxy", "num", "3", "",
     "Max twin switches within one playback."),
    ("TWIN_SPLICE_WINDOW", "proxy", "num", "8", _S,
     "Window over which slowness is measured before a proactive twin switch."),
    ("TWIN_SPLICE_MARGIN", "proxy", "num", "0.9", "",
     "Fraction of required bitrate below which a source counts as too slow."),
    ("BUFFER_TTL_SECONDS", "proxy", "num", "86400", _S,
     "How long an idle cached buffer survives before reaping."),
    ("BUFFER_WAIT_TIMEOUT", "proxy", "num", "45", _S,
     "How long a reader waits on the buffer before falling back to direct."),
    ("BUFFER_REAP_INTERVAL", "proxy", "num", "60", _S,
     "How often the buffer reaper runs."),
    ("BUFFER_READ_CHUNK", "proxy", "num", "4194304", _B,
     "Read size the buffer producer pulls from the source."),
    ("BUFFER_SLOW_WINDOW", "proxy", "num", "8", _S,
     "Window over which producer slowness is judged."),
    ("BUFFER_SLOW_MARGIN", "proxy", "num", "0.9", "",
     "Fraction of bitrate below which the producer switches source."),

    # ── direct usenet ────────────────────────────────────────────────────────
    ("NZB_MOUNT_MAX", "usenet", "num", "6", "",
     "Releases mounted per title (top quality first)."),
    ("NZB_SEARCH_TIMEOUT", "usenet", "num", "8", _S,
     "Per-indexer Newznab search timeout."),
    ("NZB_MOUNT_WAIT", "usenet", "num", "600", _S,
     "How long to wait for NZBs to finish mounting in nzbdav."),
    ("NZB_MOUNT_RETURN_WANT", "usenet", "num", "1", "",
     "Mounted candidates to expose immediately before returning."),
    ("NZB_MOUNT_EARLY_WAIT", "usenet", "num", "30", _S,
     "Grace period to surface the first mount to a waiting request."),
    ("NZB_MOUNT_STAGGER", "usenet", "num", "1.5", _S,
     "Delay between kicking off successive mounts."),
    ("NZB_IMPORT_CONCURRENCY", "usenet", "num", "2", "",
     "Concurrent nzbdav imports (shares the NNTP connection cap with playback)."),
    ("NZB_IMPORT_SLOT_HOLD", "usenet", "num", "150", _S,
     "How long an import slot is held before being reclaimed."),
    ("NZB_MOVIE_TARGET_4K_GB", "usenet", "num", "18", "GB",
     "Preferred size ceiling when picking a 4K movie release to mount early."),
    ("NZB_MOVIE_TARGET_1080_GB", "usenet", "num", "8", "GB",
     "Preferred size ceiling when picking a 1080p movie release to mount early."),
    ("NZB_TV_TARGET_4K_GB", "usenet", "num", "6", "GB",
     "Preferred size ceiling for a 4K episode."),
    ("NZB_TV_TARGET_1080_GB", "usenet", "num", "3", "GB",
     "Preferred size ceiling for a 1080p episode."),
    ("NZB_MOUNT_FAILURE_BUCKET", "usenet", "num", "1800", _S,
     "Window for grouping transient mount failures into one cooldown."),
    ("NZB_CONTENT_FAILURE_BUCKET", "usenet", "num", "86400", _S,
     "Window for grouping decisive content failures toward a block."),
    ("NZB_HEALTH", "usenet", "bool", "1", "",
     "Track per-indexer/release usenet health to order future mounts."),
    ("NZB_HEALTH_MAX_BYTES", "usenet", "num", "67108864", _B,
     "Rotate the usenet-health store past this size."),
    ("NZB_HARD_FAILURES_TO_BLOCK", "usenet", "num", "2", "",
     "Separated decisive failures before a usenet release is blocked."),
    ("NZB_HARD_RETRY_HOURS", "usenet", "num", "24", "h",
     "Cooldown after one decisive failure before retrying a release."),
    ("NZB_TRANSIENT_RETRY_MINUTES", "usenet", "num", "30", "min",
     "Cooldown after a transient network/provider failure."),
    ("NZB_INDEXER_HALF_LIFE_DAYS", "usenet", "num", "45", "d",
     "Half-life for time-decaying indexer evidence in the learned ordering."),

    # ── library & acquire ────────────────────────────────────────────────────
    ("JELLIO_DIRECT_PLAY", "acquire", "bool", "1", "",
     "Return direct-play library URLs from Jellyfin rather than transcoded."),
    ("JELLIO_ENRICH", "acquire", "bool", "1", "",
     "Enrich library hits with extra metadata."),
    ("JELLIO_CACHE_TTL", "acquire", "num", "300", _S,
     "How long a positive library lookup is cached."),
    ("JELLIO_NEG_TTL", "acquire", "num", "60", _S,
     "How long a 'not in library' result is cached."),
    ("JELLIO_TIMEOUT", "acquire", "num", "8", _S,
     "Library (Jellio) request timeout."),
    ("ACQUIRE_DEDUP_TTL", "acquire", "num", "1800", _S,
     "Window in which repeat requests for a title won't re-add it."),
    ("TMDB_DIGITAL_ASSUME_DAYS", "acquire", "num", "120", "d",
     "Days after theatrical to assume a digital release exists if none listed."),
    ("NOTICE_TTL_SECONDS", "acquire", "num", "1200", _S,
     "How long the 'being added' notice is shown before re-checking a title."),

    # ── reputation & blocking ────────────────────────────────────────────────
    ("REPUTATION_BLOCK", "reputation", "bool", "1", "",
     "Enable auto-blocking of releases that repeatedly play badly."),
    ("REPUTATION_MAX_ENTRIES", "reputation", "num", "100000", "",
     "Cap on tracked release-reputation entries."),
    ("MIN_BLOCK_SESSIONS", "reputation", "num", "2", "",
     "Separate bad plays before a torrent/debrid release is blocked."),
    ("COOLDOWN_MINUTES", "reputation", "num", "15", "min",
     "Cooldown applied to a source after it buffers mid-stream."),
    ("BLOCK_TTL_DAYS", "reputation", "num", "30", "d",
     "How long a block persists before it can be reconsidered."),

    # ── telemetry & caches ───────────────────────────────────────────────────
    ("TELEMETRY", "telemetry", "bool", "1", "",
     "Record probe/playback telemetry that powers the Source health page."),
    ("TELEMETRY_MAX_BYTES", "telemetry", "num", "1073741824", _B,
     "Rotate the telemetry log past this size."),
    ("TELEMETRY_SEGMENTS", "telemetry", "num", "8", "",
     "How many rotated telemetry segments to keep."),
    ("STATS_SLOW_MBPS", "telemetry", "num", "4.0", "MB/s",
     "Delivery below this on the Source health page is flagged as slow."),

    # ── identity ─────────────────────────────────────────────────────────────
    ("SLOW_ADDON_NAME", "identity", "text", "", "",
     "Name of the best-quality addon (defaults to '<addon> (Best Quality)')."),
]

_BY_KEY = {k: dict(key=k, group=g, type=t, default=d, unit=u, blurb=b)
           for (k, g, t, d, u, b) in CATALOG}


def spec(key: str) -> dict | None:
    return _BY_KEY.get(key)


def keys() -> list[str]:
    return [row[0] for row in CATALOG]


def by_group(group: str) -> list[dict]:
    return [_BY_KEY[k] for k in keys() if _BY_KEY[k]["group"] == group]
