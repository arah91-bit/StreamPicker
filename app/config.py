"""Runtime configuration store behind the /{secret}/settings dashboard.

Every knob in this app is an environment variable baked into a module constant
at import time. That stays true — this module just gives those variables a
second, UI-editable source: config.json in /data (bind-mounted, so it survives
rebuilds). main.py calls apply_env() before importing any other app module, so
stored values overlay the env-file and the constant-at-import pattern keeps
working unchanged everywhere. The corollary: edits never hot-patch a running
picker — they land on the next process start, and the dashboard offers a
one-click restart (the container's restart policy brings it back up).

The schema here is the single source of truth for what the dashboard shows.
CONNECTIONS are the upstream services someone deploying their own instance has
to plug in (each gets a live credential test in app/connections.py); SETTINGS
are the behavior knobs worth a switch or a slider. Everything else stays
env-file-only on purpose: the page is for the ~20 things an operator actually
adjusts, not a web copy of the env file.
"""

import json
import ipaddress
import logging
import math
import os
import re
import tempfile
import time
from urllib.parse import urlsplit

from app import knobs, secret_store

logger = logging.getLogger("stream-picker.config")

# ── schema ───────────────────────────────────────────────────────────────────

# Groups render in this order. (id, title, blurb)
GROUPS = [
    ("stream", "Stream path",
     "How bytes get from a source to the player."),
    ("picking", "Picking",
     "How hard the pickers verify and rank before answering."),
    ("acquire", "When nothing plays",
     "Fallback for titles no source can stream yet."),
    ("identity", "Addon identity",
     "How this instance announces itself to player clients."),
]

# type: bool | number | choice | text. Defaults mirror the code's own defaults
# so the page shows reality when nothing is overridden. "hidden" settings are
# real stored keys that the page renders through a custom control (the
# stream-mode switch) instead of a generic row.
SETTINGS = [
    dict(key="PROXY_PLAYBACK", group="stream", type="bool", default="1",
         hidden=True, label="Proxy playback"),
    dict(key="PROXY_BUFFER", group="stream", type="bool", default="1",
         hidden=True, label="Cache streams on disk"),
    dict(key="BUFFER_CACHE_GB", group="stream", type="number", default="100",
         min=10, max=500, step=10, unit="GB", mode="cache",
         label="Cache size on disk",
         desc="Total disk the stream cache may use. Wiped on restart — it is "
              "a read-ahead buffer, never a library."),
    dict(key="BUFFER_AHEAD_GB", group="stream", type="number", default="8",
         min=1, max=32, step=1, unit="GB", mode="cache",
         label="Read-ahead per stream",
         desc="How far past the playhead each stream is downloaded. More "
              "runway rides out longer source stalls; costs disk and "
              "source bandwidth."),
    dict(key="PREFETCH_NEXT", group="stream", type="bool", default="1",
         label="Prefetch the next episode",
         desc="When an episode starts, run the search for the next one so it "
              "starts instantly. Search only — no video bytes are downloaded "
              "ahead."),

    dict(key="AUDIO_GATE", group="picking", type="bool", default="1",
         label="Audio-language gate",
         desc="Demote releases whose only audio is neither English nor the "
              "title's original language. Untagged and multi-audio releases "
              "get the benefit of the doubt."),
    dict(key="DV_REJECT", group="picking", type="choice", default="bare",
         choices=[("off", "Allow all"),
                  ("bare", "Drop bare DV"),
                  ("all", "Drop all DV")],
         label="Dolby Vision guard",
         desc="Bare DV (no HDR10 base layer) shows green/purple tints on "
              "non-DV displays. 'Drop all DV' is for players that tint even "
              "on Profile 8."),
    dict(key="VERIFIED_WANT", group="picking", type="number", default="2",
         min=1, max=4, step=1, unit="",
         label="Verified streams wanted",
         desc="How many candidates must pass a real playback probe before "
              "the fast picker is satisfied."),
    dict(key="MAX_PROBES", group="picking", type="number", default="6",
         min=2, max=16, step=1, unit="",
         label="Fast picker probe budget",
         desc="Most streams verify in the first wave; a bigger budget helps "
              "obscure titles at the cost of a slower worst case."),
    dict(key="SLOW_MAX_PROBES", group="picking", type="number", default="16",
         min=4, max=48, step=2, unit="",
         label="Quality picker probe budget",
         desc="The slow picker probes only the top of its quality ranking. "
              "Raise to dig deeper when top releases keep failing."),
    dict(key="OMDB_DAILY_BUDGET", group="picking", type="number",
         default="750", min=0, max=900, step=50, unit="calls/day",
         label="OMDb daily budget",
         desc="Hard UTC-day ceiling for uncached OMDb identity lookups. "
              "Persistent title caching keeps normal use far below this and "
              "leaves headroom under OMDb's 1,000-call plan."),
    dict(key="FAST_RACE_DEADLINE", group="picking", type="number",
         default="55", min=10, max=90, step=5, unit="s",
         label="Fast picker deadline",
         desc="Hard cap on how long the fast picker may hold a request "
              "before answering with what it has."),
    dict(key="MAX_BITRATE_MBPS", group="picking", type="number",
         default="0", min=0, max=120, step=5, unit=" Mbps",
         zero_label="Unlimited", label="Max bitrate",
         desc="Skip any release whose average bitrate (file size ÷ runtime) "
              "runs higher than this — handy on a capped or slow connection so "
              "a 90 Mbps remux is never picked. 0 = unlimited: pick the best "
              "quality regardless of bitrate."),
    dict(key="FAST_SD_BEFORE_YEAR", group="picking", type="number",
         default="2000", min=0, max=2015, step=1, unit="", toggle_year=True,
         year_min=1940, label="Accept DVD quality for old titles",
         desc="For anything released before this year, a verified DVD/SD "
              "stream counts as good enough — so an old, SD-only title (a "
              "pre-HD TV show, say) returns quickly instead of holding the "
              "whole deadline hunting for an HD copy that was never made. HD "
              "still wins whenever it exists. Off = always hold out for HD."),

    dict(key="ACQUIRE_ENABLED", group="acquire", type="bool", default="1",
         label="Add missing titles automatically",
         desc="When nothing plays anywhere and TMDB confirms a proper "
              "release exists, request it (Jellyseerr first, then Radarr/"
              "Sonarr) and show the 'being added' notice instead of a dead "
              "link."),

    dict(key="ADDON_NAME", group="identity", type="text",
         default="Auto Stream", label="Addon name",
         desc="Shown in your player's addon list and on every stream row."),
    dict(key="ADDON_PUBLIC_URL", group="identity", type="text",
         default="http://localhost:8011", label="Public base URL",
         desc="Where players reach this addon from outside — used to build "
              "proxy and notice URLs. Must be https and publicly routable."),
    dict(key="DASHBOARD_LOCAL_ONLY", group="identity", type="bool",
         default="1", label="Restrict this dashboard to local/LAN",
         desc="Serve the dashboard only to loopback/LAN/Docker clients, never "
              "through the public reverse proxy. The addon's stream URLs are "
              "unaffected."),
    dict(key="ADMIN_USERNAME", group="identity", type="text",
         default="admin", hidden=True, label="Dashboard username",
         desc="Optional deployment-time username that skips first-run account "
              "creation when ADMIN_PASSWORD is also set."),
    dict(key="ADMIN_PASSWORD", group="identity", type="text", kind="secret",
         default="", hidden=True, label="Dashboard password",
         desc="Optional deployment-time password. If omitted, the first local "
              "dashboard visit creates a scrypt-protected account."),
    dict(key="TRUSTED_PROXIES", group="identity", type="text",
         default="127.0.0.0/8,::1/128", label="Trusted reverse proxies",
         desc="Comma-separated proxy IPs/CIDRs allowed to supply "
              "X-Forwarded-For. Keep this narrow; forwarded headers from all "
              "other peers are rejected."),
    # Managed by the "Custom addons" panel, not a generic row (hence hidden).
    dict(key="EXTRA_ADDONS", group="identity", type="addons", default="",
         hidden=True, label="Custom addons"),
]

# field kind: url | text | secret | multiline. Secrets render masked and an
# empty submit means "keep what's stored". NZB_INDEXERS is one name|url|key
# per line in the UI, ';'-joined in storage (the format usenet.py parses).
# Connections are grouped under these collapsible headings on the settings
# page, in this order. Each connection below carries a matching `cat`. (id,
# title, blurb)
CONNECTION_GROUPS = [
    ("sources", "Debrid & scrapers",
     "Where streams come from — cached-debrid search and broad scrapers."),
    ("usenet", "Usenet",
     "Direct usenet: indexer searches and the mount that streams NZBs."),
    ("metadata", "Metadata",
     "Titles, languages and runtimes — how the picker knows what it's matching."),
    ("library", "Your library",
     "Titles you already own, checked before any search runs."),
    ("acquire", "Requests & downloaders",
     "Fallbacks that fetch a missing title when nothing can stream it yet."),
]

CONNECTIONS = [
    dict(id="comet", name="Comet", role="Fast lane — cached debrid search",
        cat="sources",
        fields=[dict(key="FAST_BASE_URL", label="Manifest base URL",
                      kind="url", sensitive=True,
                      hint="Your configured Comet base, without "
                           "/manifest.json. The URL embeds your debrid "
                           "keys — treat it like a password.")]),
    dict(id="stremthru", name="StremThru Torz",
         role="Long-tail crowdsourced hash index", cat="sources",
         fields=[dict(key="STREMTHRU_BASE_URL", label="Manifest base URL",
                      kind="url", sensitive=True,
                      hint="The Torz URL embeds your debrid key in its path — "
                           "treat it like a password.")]),
    dict(id="mediafusion", name="MediaFusion",
         role="Broad scrape — slow first hit, feeds the quality pass",
         cat="sources",
         fields=[dict(key="MEDIAFUSION_BASE_URL", label="Manifest base URL",
                      kind="url", sensitive=True,
                      hint="A configured MediaFusion URL can encode your "
                           "debrid credentials — treat it like a password.")]),
    dict(id="indexers", name="Usenet indexers",
         role="Direct usenet searches (Newznab)", cat="usenet",
        fields=[dict(key="NZB_INDEXERS", label="One per line: name|api-url|apikey",
                      kind="multiline", sensitive=True)]),
    dict(id="nzbdav", name="nzbdav",
         role="Mounts NZBs so usenet releases stream directly", cat="usenet",
         fields=[dict(key="NZBDAV_URL", label="Base URL", kind="url"),
                 dict(key="NZBDAV_USER", label="WebDAV user", kind="text"),
                 dict(key="NZBDAV_PASS", label="WebDAV password",
                      kind="secret"),
                 dict(key="NZBDAV_API_KEY", label="API key (optional — queue "
                      "visibility)", kind="secret")]),
    dict(id="tmdb", name="TMDB",
         role="Titles, original language, release dates", cat="metadata",
         fields=[dict(key="TMDB_API_KEY", label="API key", kind="secret")]),
    dict(id="omdb", name="OMDb",
         role="Independent title, year, type and runtime corroboration",
         cat="metadata",
         fields=[dict(key="OMDB_API_KEY", label="API key", kind="secret")]),
    dict(id="tvdb", name="TVDB",
         role="Season-rollover fallback for episode prefetch", cat="metadata",
         fields=[dict(key="TVDB_API_KEY", label="API key", kind="secret")]),
    dict(id="jellyfin", name="Jellyfin library",
         role="Serves titles you already have — checked before any search",
        cat="library",
        fields=[dict(key="JELLYFIN_URL", label="Server URL", kind="url",
                     hint="URL reachable by StreamPicker, for example "
                          "http://jellyfin:8096 on a shared Docker network."),
                dict(key="JELLYFIN_USERNAME", label="Username", kind="text"),
                dict(key="JELLYFIN_PASSWORD", label="Password", kind="secret",
                     hint="Encrypted at rest. Prefer a dedicated playback-only "
                          "Jellyfin account rather than an administrator.")]),
    dict(id="jellyseerr", name="Jellyseerr",
         role="Preferred path for requesting missing titles", cat="acquire",
         fields=[dict(key="JELLYSEERR_URL", label="Base URL", kind="url"),
                 dict(key="JELLYSEERR_API_KEY", label="API key",
                      kind="secret")]),
    dict(id="radarr", name="Radarr", role="Direct movie fallback",
         cat="acquire",
         fields=[dict(key="RADARR_URL", label="Base URL", kind="url"),
                 dict(key="RADARR_API_KEY", label="API key", kind="secret"),
                 dict(key="RADARR_ROOT", label="Root folder", kind="text"),
                 dict(key="RADARR_QUALITY_PROFILE", label="Quality profile",
                      kind="text")]),
    dict(id="sonarr", name="Sonarr", role="Direct series fallback",
         cat="acquire",
         fields=[dict(key="SONARR_URL", label="Base URL", kind="url"),
                 dict(key="SONARR_API_KEY", label="API key", kind="secret"),
                 dict(key="SONARR_ROOT", label="Root folder", kind="text"),
                 dict(key="SONARR_QUALITY_PROFILE", label="Quality profile",
                      kind="text")]),
]

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")

# These values are consumed with int(...) in their owning modules. Keeping the
# distinction here prevents a dashboard save such as "2.5" from creating a
# restart crash that the generic float parser used to permit.
_INT_KEYS = {
    "BUFFER_CACHE_GB", "BUFFER_AHEAD_GB", "VERIFIED_WANT", "MAX_PROBES",
    "SLOW_MAX_PROBES", "FAST_ENOUGH_4K", "FAST_ENOUGH_1080",
    "FAST_SD_BEFORE_YEAR",
    "FAST_PROBE_BATCH", "PROBE_HOST_BENCH", "SLOW_CONCURRENCY",
    "SLOW_NZB_PROBES", "SLOW_FINISH_MAX_PROBES", "SLOW_VIDEO_PROBE_N",
    "UNPROVEN_MAX_RES", "PROXY_WRAP_MAX", "PROXY_MAX_FAILOVER",
    "PLAYER_REJECT_STARTS", "DECODE_BAD_REJECTS", "PROXY_SESSION_MAX_BYTES",
    "PROXY_SESSION_MAX", "HLS_BUFFER_CONCURRENCY", "TWIN_SPLICE_MAX",
    "BUFFER_READ_CHUNK", "NZB_MOUNT_MAX",
    "NZB_MOUNT_RETURN_WANT", "NZB_IMPORT_CONCURRENCY", "NZB_HEALTH_MAX_BYTES",
    "NZB_HARD_FAILURES_TO_BLOCK", "NZB_LANE_MAX_ACTIVE",
    "NZB_LANE_REGISTRY_MAX", "TMDB_DIGITAL_ASSUME_DAYS",
    "OMDB_DAILY_BUDGET",
    "REPUTATION_MAX_ENTRIES", "MIN_BLOCK_SESSIONS", "TELEMETRY_MAX_BYTES",
    "TELEMETRY_SEGMENTS", "MAX_BITRATE_MBPS",
}

_FRACTION_KEYS = {
    "DURATION_MIN_FRAC", "QUALITY_BAND", "TWIN_SPLICE_MARGIN",
    "BUFFER_SLOW_MARGIN",
}


def _advanced_bounds(spec: dict) -> tuple[float, float]:
    key, unit = spec["key"], spec.get("unit", "")
    if key == "ACQUIRE_FOREGROUND_WAIT":
        return 0.0, 60.0
    if key in _FRACTION_KEYS:
        return 0.0, 1.0
    maximum = {
        "s": 31 * 86400,
        "bytes": float(1 << 40),
        "GB": 10_000.0,
        "bps": 1_000_000_000_000.0,
        "h": 8760.0,
        "d": 3650.0,
        "min": 525_600.0,
        "MB/s": 100_000.0,
    }.get(unit, 1_000_000_000.0)
    return 0.0, maximum


def _specs() -> dict[str, dict]:
    out = {}
    for original in SETTINGS:
        s = dict(original)
        if s.get("type") == "number":
            s["number_kind"] = "int" if s["key"] in _INT_KEYS else "float"
        out[s["key"]] = s
    for c in CONNECTIONS:
        for f in c["fields"]:
            out[f["key"]] = {**f, "type": "connection"}
    # The long-tail tuning knobs: accepted by save(), rendered in the Advanced
    # section. setdefault so a curated/connection spec always wins over these.
    for k in knobs.keys():
        s = knobs.spec(k)
        candidate = {"key": k, "type": "number" if s["type"] == "num"
                     else s["type"], "default": s["default"],
                     "unit": s["unit"], "group": s["group"],
                     "desc": s["blurb"], "choices": s.get("choices"),
                     "advanced": True}
        if candidate["type"] == "number":
            candidate["number_kind"] = "int" if k in _INT_KEYS else "float"
            candidate["min"], candidate["max"] = _advanced_bounds(candidate)
        out.setdefault(k, candidate)
    return out


_SPECS = _specs()
_RETIRED_KEYS = {
    "NZB_TIMEOUT", "NZB_IMPORT_SLOT_HOLD",
    "JELLIO_URL", "JELLIO_DIRECT_PLAY", "JELLIO_ENRICH",
    "JELLIO_CACHE_TTL", "JELLIO_NEG_TTL", "JELLIO_TIMEOUT",
}


def _path() -> str:
    # resolved per call, not at import, so tests can point it at a tempdir
    return os.environ.get("CONFIG_FILE") or os.path.join(
        os.environ.get("TELEMETRY_DIR", "/data"), "config.json")


def _quarantine(path: str, reason: Exception | str) -> None:
    backup = f"{path}.corrupt-{int(time.time())}-{os.getpid()}"
    try:
        os.replace(path, backup)
        logger.error("config: quarantined invalid %s as %s: %s",
                     path, backup, reason)
    except OSError:
        logger.exception("config: invalid %s could not be quarantined: %s",
                         path, reason)


def _read(*, migrate_plaintext: bool = False) -> dict[str, str]:
    path = _path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
            raise ValueError("root and 'env' must be JSON objects")
        env = data.get("env", {})
        out = {}
        plaintext_sensitive = False
        for key, value in env.items():
            if key in _RETIRED_KEYS:
                logger.warning("config: ignoring retired setting %s", key)
                continue
            if not isinstance(key, str) or key not in _SPECS:
                raise ValueError(f"unknown setting in store: {str(key)[:40]}")
            if isinstance(value, (dict, list)) or value is None:
                raise ValueError(f"{key}: stored value must be scalar")
            raw = str(value)
            if is_secret(key):
                if secret_store.is_encrypted(raw):
                    raw = secret_store.decrypt(key, raw, path)
                elif raw:
                    plaintext_sensitive = True
            norm = _normalize(_SPECS[key], raw)
            if norm == "" and _SPECS[key].get("type") == "number":
                logger.warning("config: ignoring blank numeric override %s", key)
                continue
            # A blank sensitive field means "keep" only for a form submit. An
            # older store containing it is harmless and remains explicitly blank.
            out[key] = raw.strip() if norm is None else norm
        # Upgrade legacy plaintext dashboard secrets in place without ever
        # creating a plaintext backup. Authentication/key errors deliberately
        # escape this function and leave the original file untouched.
        if migrate_plaintext and plaintext_sensitive:
            _write(out)
            logger.info("config: encrypted legacy sensitive settings at rest")
        return out
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, OSError):
            logger.error("config: cannot read %s; using environment/defaults: %s",
                         path, exc)
        else:
            _quarantine(path, exc)
        return {}


def _write(store: dict[str, str]) -> None:
    path = _path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    persisted = {
        key: (secret_store.encrypt(key, value, path)
              if value and is_secret(key) else value)
        for key, value in store.items()
    }
    encoded = json.dumps({"env": persisted}, indent=1, sort_keys=True)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("configuration exceeds 256 KiB")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(encoded)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)   # holds API keys — owner-only, like the env file
        os.replace(tmp, path)
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_env() -> int:
    """Overlay stored values onto os.environ. Must run before any app module
    bakes env into constants — main.py calls it above its own app imports.
    Returns how many keys were applied (for the startup log line)."""
    store = _read(migrate_plaintext=True)
    try:
        effective = _validate_effective(store)
    except ValueError as exc:
        # A syntactically valid but contradictory/invalid saved config must not
        # trap the container in an import-time crash loop. Preserve it beside
        # the live path for repair, then boot on environment/defaults.
        if store and os.path.exists(_path()):
            _quarantine(_path(), exc)
            store = {}
            effective = _validate_effective(store)
        else:
            raise
    # Canonicalize explicit .env values too. This makes every boolean spelling
    # behave consistently in modules whose historic parsers accepted only
    # 0/false, and guarantees int/float consumers see the validated form.
    explicit = set(store) | {k for k in _SPECS if k in os.environ}
    for key in explicit:
        os.environ[key] = effective[key]
    return len(store)


def running(key: str) -> str:
    """The value the running process was started with (env override, else the
    code default from the spec)."""
    spec = _SPECS.get(key) or {}
    return os.environ.get(key, str(spec.get("default", "")))


def stored(key: str) -> str:
    """The raw override saved in config.json, or '' if the key isn't overridden
    (i.e. it's running on its env/code default)."""
    return _read().get(key, "")


def default(key: str) -> str:
    """The code default for a key, ignoring any env or stored override."""
    return str((_SPECS.get(key) or {}).get("default", ""))


def pending(key: str) -> str:
    """The value the NEXT start will use: store > env > code default."""
    store = _read()
    if key in store:
        return store[key]
    return running(key)


def restart_pending() -> bool:
    store = _read()
    return any(pending_from(store, k) != running(k) for k in _SPECS)


def is_secret(key: str) -> bool:
    spec = _SPECS.get(key) or {}
    return spec.get("kind") == "secret" or bool(spec.get("sensitive"))


def mask(value: str, key: str = "") -> str:
    """Displayable stand-in for a secret: enough to recognize, never enough
    to use. Short values give no tail at all."""
    if not value:
        return ""
    if key and (_SPECS.get(key) or {}).get("sensitive"):
        return "kept · hidden"
    return f"kept · ends …{value[-4:]}" if len(value) >= 12 else "kept"


def _http_url(key: str, raw: str) -> str:
    if not raw:
        return raw
    if len(raw) > 8192 or any(ord(c) < 32 for c in raw):
        raise ValueError(f"{key}: URL is too long or contains control characters")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{key}: must be an absolute http(s) URL")
    return raw


def _trusted_proxies(raw: str) -> str:
    parts = [p for p in re.split(r"[\s,]+", raw.strip()) if p]
    if len(parts) > 64:
        raise ValueError("TRUSTED_PROXIES: at most 64 networks are allowed")
    for part in parts:
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"TRUSTED_PROXIES: invalid IP/CIDR {part[:60]!r}") from None
    return ",".join(parts)


def _normalize(spec: dict, raw: str):
    """Validate one submitted value against its schema entry. Returns the
    string to store, or None for 'no change' (blank secret), or the sentinel
    '' meaning 'store empty' / raises ValueError on garbage."""
    raw = (raw or "").strip()
    kind, typ = spec.get("kind"), spec.get("type")
    sensitive = kind == "secret" or spec.get("sensitive")
    if sensitive and raw == "":
        return None
    if kind == "secret":
        if len(raw.encode("utf-8")) > 8192 or "\x00" in raw:
            raise ValueError(f"{spec['key']}: secret is too long or invalid")
        return raw
    if typ == "bool":
        low = raw.lower()
        if low in _TRUE:
            return "1"
        if low in _FALSE:
            return "0"
        raise ValueError(f"{spec['key']}: not a yes/no value")
    if typ == "number":
        if raw == "":
            return ""                       # revert to env/default
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"{spec['key']}: not a number") from None
        if not math.isfinite(v):
            raise ValueError(f"{spec['key']}: must be finite")
        if spec.get("number_kind") == "int" and not v.is_integer():
            raise ValueError(f"{spec['key']}: must be a whole number")
        if "min" in spec and v < float(spec["min"]):
            raise ValueError(f"{spec['key']}: must be at least {spec['min']}")
        if "max" in spec and v > float(spec["max"]):
            raise ValueError(f"{spec['key']}: must be at most {spec['max']}")
        return str(int(v)) if v.is_integer() else format(v, ".15g")
    if typ == "choice":
        if raw not in [c[0] for c in spec["choices"]]:
            raise ValueError(f"{spec['key']}: not one of the choices")
        return raw
    if kind == "multiline":
        if len(raw.encode("utf-8")) > 64 * 1024 or "\x00" in raw:
            raise ValueError(f"{spec['key']}: value is too long or invalid")
        parts = [p.strip() for chunk in raw.split("\n")
                 for p in chunk.split(";")]
        return ";".join(p for p in parts if p)
    if typ == "addons":
        if raw == "":
            return ""
        try:
            items = json.loads(raw)
        except ValueError:
            raise ValueError("custom addons: not valid JSON") from None
        if not isinstance(items, list):
            raise ValueError("custom addons: expected a JSON list")
        if len(items) > 64:
            raise ValueError("custom addons: at most 64 entries are allowed")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            url = str(it.get("url", "")).strip().rstrip("/")
            if url.endswith("/manifest.json"):
                url = url[:-len("/manifest.json")].rstrip("/")
            if not url:
                continue
            _http_url("custom addon URL", url)
            name = str(it.get("name", "")).strip()[:60] or url
            out.append({"name": name, "url": url})
        return json.dumps(out, separators=(",", ":")) if out else ""
    if kind == "url":
        return _http_url(spec["key"], raw)
    if spec["key"] == "ADDON_PUBLIC_URL":
        return _http_url(spec["key"], raw).rstrip("/")
    if spec["key"] == "TRUSTED_PROXIES":
        return _trusted_proxies(raw)
    if spec["key"] == "ADMIN_USERNAME":
        if not raw or len(raw) > 128 or ":" in raw or any(ord(c) < 33 for c in raw):
            raise ValueError("ADMIN_USERNAME: use 1-128 visible characters without ':'")
    if len(raw.encode("utf-8")) > 8192 or "\x00" in raw:
        raise ValueError(f"{spec['key']}: value is too long or invalid")
    return raw


def _validate_effective(store: dict[str, str]) -> dict[str, str]:
    """Validate the complete config that the next process would import."""
    effective = {}
    for key, spec in _SPECS.items():
        raw = store[key] if key in store else running(key)
        norm = _normalize(spec, raw)
        effective[key] = raw.strip() if norm is None else norm

    def num(key: str) -> float:
        return float(effective[key])

    if num("BUFFER_AHEAD_GB") > num("BUFFER_CACHE_GB"):
        raise ValueError("BUFFER_AHEAD_GB must not exceed BUFFER_CACHE_GB")
    if num("VERIFIED_WANT") > num("MAX_PROBES"):
        raise ValueError("VERIFIED_WANT must not exceed MAX_PROBES")
    if num("SLOW_PROBE_RESERVE") >= num("SLOW_TOTAL_DEADLINE"):
        raise ValueError("SLOW_PROBE_RESERVE must be below SLOW_TOTAL_DEADLINE")
    if num("FAST_RACE_DEADLINE") > num("TOTAL_DEADLINE"):
        raise ValueError("FAST_RACE_DEADLINE must not exceed TOTAL_DEADLINE")
    return effective


def validate_pending() -> dict[str, str]:
    """Raise ValueError unless the entire next-start configuration is valid."""
    return _validate_effective(_read())


def ensure_storage() -> str:
    """Create and verify the persistent config directory is writable."""
    directory = os.path.dirname(_path()) or "."
    os.makedirs(directory, exist_ok=True)
    fd, probe = tempfile.mkstemp(dir=directory, prefix=".write-test-")
    try:
        os.write(fd, b"ok")
        os.fsync(fd)
    finally:
        os.close(fd)
        try:
            os.unlink(probe)
        except OSError:
            pass
    return directory


def storage_ready() -> bool:
    directory = os.path.dirname(_path()) or "."
    if not os.path.isdir(directory):
        return False
    fd = None
    probe = ""
    try:
        fd, probe = tempfile.mkstemp(dir=directory, prefix=".health-")
        os.write(fd, b"ok")
        os.close(fd)
        fd = None
        os.unlink(probe)
        probe = ""
        return True
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe:
            try:
                os.unlink(probe)
            except OSError:
                pass
        return False


def save(values: dict[str, str]) -> dict:
    """Validate and persist submitted values. Unknown keys are rejected —
    this endpoint must not be a generic 'set any env var' primitive. A blank
    value reverts a setting to its env-file/code default (secrets: blank
    keeps the stored one). Returns what changed and whether a restart is
    needed to apply it."""
    original = _read()
    store = dict(original)
    changed = []
    for key, raw in values.items():
        spec = _SPECS.get(key)
        if spec is None:
            raise ValueError(f"unknown setting: {key[:40]}")
        norm = _normalize(spec, str(raw))
        if norm is None:
            continue
        before = pending_from(original, key)
        if norm == "" and spec.get("type") != "connection":
            store.pop(key, None)            # revert knob to env/default
        elif spec.get("advanced") and norm == default(key):
            store.pop(key, None)            # setting a knob back to its default
        else:
            store[key] = norm
        if pending_from(store, key) != before:
            changed.append(key)
    # Validate the complete prospective configuration, not just each changed
    # field. This catches cross-setting contradictions before restart.
    _validate_effective(store)
    if changed:
        _write(store)
    return {"changed": changed, "restart_needed": restart_pending()}


def pending_from(store: dict[str, str], key: str) -> str:
    if key in store:
        return store[key]
    return running(key)
