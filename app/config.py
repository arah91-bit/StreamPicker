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
import os
import tempfile

from app import knobs

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
     "How this instance announces itself to Stremio clients."),
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
    dict(key="FAST_RACE_DEADLINE", group="picking", type="number",
         default="55", min=10, max=90, step=5, unit="s",
         label="Fast picker deadline",
         desc="Hard cap on how long the fast picker may hold a request "
              "before answering with what it has."),

    dict(key="ACQUIRE_ENABLED", group="acquire", type="bool", default="1",
         label="Add missing titles automatically",
         desc="When nothing plays anywhere and TMDB confirms a proper "
              "release exists, request it (Jellyseerr first, then Radarr/"
              "Sonarr) and show the 'being added' notice instead of a dead "
              "link."),

    dict(key="ADDON_NAME", group="identity", type="text",
         default="Auto Stream", label="Addon name",
         desc="Shown in Stremio's addon list and on every stream row."),
    dict(key="ADDON_PUBLIC_URL", group="identity", type="text",
         default="http://localhost:8011", label="Public base URL",
         desc="Where players reach this addon from outside — used to build "
              "proxy and notice URLs. Must be https and publicly routable."),
]

# field kind: url | text | secret | multiline. Secrets render masked and an
# empty submit means "keep what's stored". NZB_INDEXERS is one name|url|key
# per line in the UI, ';'-joined in storage (the format usenet.py parses).
CONNECTIONS = [
    dict(id="comet", name="Comet", role="Fast lane — cached debrid search",
         fields=[dict(key="FAST_BASE_URL", label="Manifest base URL",
                      kind="url",
                      hint="Your configured Comet base, without "
                           "/manifest.json. The URL embeds your debrid "
                           "keys — treat it like a password.")]),
    dict(id="stremthru", name="StremThru Torz",
         role="Long-tail crowdsourced hash index",
         fields=[dict(key="STREMTHRU_BASE_URL", label="Manifest base URL",
                      kind="url")]),
    dict(id="mediafusion", name="MediaFusion",
         role="Broad scrape — slow first hit, feeds the quality pass",
         fields=[dict(key="MEDIAFUSION_BASE_URL", label="Manifest base URL",
                      kind="url")]),
    dict(id="indexers", name="Usenet indexers",
         role="Direct usenet searches (Newznab)",
         fields=[dict(key="NZB_INDEXERS", label="One per line: name|api-url|apikey",
                      kind="multiline")]),
    dict(id="nzbdav", name="nzbdav",
         role="Mounts NZBs so usenet releases stream directly",
         fields=[dict(key="NZBDAV_URL", label="Base URL", kind="url"),
                 dict(key="NZBDAV_USER", label="WebDAV user", kind="text"),
                 dict(key="NZBDAV_PASS", label="WebDAV password",
                      kind="secret"),
                 dict(key="NZBDAV_API_KEY", label="API key (optional — queue "
                      "visibility)", kind="secret")]),
    dict(id="tmdb", name="TMDB",
         role="Titles, original language, release dates",
         fields=[dict(key="TMDB_API_KEY", label="API key", kind="secret")]),
    dict(id="tvdb", name="TVDB",
         role="Season-rollover fallback for episode prefetch",
         fields=[dict(key="TVDB_API_KEY", label="API key", kind="secret")]),
    dict(id="jellio", name="Jellyfin library (Jellio)",
         role="Serves titles you already have — checked before any search",
         fields=[dict(key="JELLIO_URL", label="Public manifest base URL",
                      kind="url",
                      hint="Public base including the Jellio token, so the "
                           "playback URLs it returns work from the player.")]),
    dict(id="jellyseerr", name="Jellyseerr",
         role="Preferred path for requesting missing titles",
         fields=[dict(key="JELLYSEERR_URL", label="Base URL", kind="url"),
                 dict(key="JELLYSEERR_API_KEY", label="API key",
                      kind="secret")]),
    dict(id="radarr", name="Radarr", role="Direct movie fallback",
         fields=[dict(key="RADARR_URL", label="Base URL", kind="url"),
                 dict(key="RADARR_API_KEY", label="API key", kind="secret"),
                 dict(key="RADARR_ROOT", label="Root folder", kind="text"),
                 dict(key="RADARR_QUALITY_PROFILE", label="Quality profile",
                      kind="text")]),
    dict(id="sonarr", name="Sonarr", role="Direct series fallback",
         fields=[dict(key="SONARR_URL", label="Base URL", kind="url"),
                 dict(key="SONARR_API_KEY", label="API key", kind="secret"),
                 dict(key="SONARR_ROOT", label="Root folder", kind="text"),
                 dict(key="SONARR_QUALITY_PROFILE", label="Quality profile",
                      kind="text")]),
]

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _specs() -> dict[str, dict]:
    out = {s["key"]: s for s in SETTINGS}
    for c in CONNECTIONS:
        for f in c["fields"]:
            out[f["key"]] = {**f, "type": "connection"}
    # The long-tail tuning knobs: accepted by save(), rendered in the Advanced
    # section. setdefault so a curated/connection spec always wins over these.
    for k in knobs.keys():
        s = knobs.spec(k)
        out.setdefault(k, {"key": k, "type": "number" if s["type"] == "num"
                           else s["type"], "default": s["default"],
                           "unit": s["unit"], "group": s["group"],
                           "desc": s["blurb"], "choices": s.get("choices"),
                           "advanced": True})
    return out


_SPECS = _specs()


def _path() -> str:
    # resolved per call, not at import, so tests can point it at a tempdir
    return os.environ.get("CONFIG_FILE") or os.path.join(
        os.environ.get("TELEMETRY_DIR", "/data"), "config.json")


def _read() -> dict[str, str]:
    try:
        with open(_path()) as f:
            data = json.load(f)
        env = data.get("env", {})
        return {str(k): str(v) for k, v in env.items()}
    except (OSError, ValueError):
        return {}


def _write(store: dict[str, str]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"env": store}, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)   # holds API keys — owner-only, like the env file
        os.replace(tmp, path)
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
    store = _read()
    for k, v in store.items():
        os.environ[k] = v
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
    return any(pending(k) != running(k) for k in _SPECS)


def is_secret(key: str) -> bool:
    return (_SPECS.get(key) or {}).get("kind") == "secret"


def mask(value: str) -> str:
    """Displayable stand-in for a secret: enough to recognize, never enough
    to use. Short values give no tail at all."""
    if not value:
        return ""
    return f"kept · ends …{value[-4:]}" if len(value) >= 12 else "kept"


def _normalize(spec: dict, raw: str):
    """Validate one submitted value against its schema entry. Returns the
    string to store, or None for 'no change' (blank secret), or the sentinel
    '' meaning 'store empty' / raises ValueError on garbage."""
    raw = (raw or "").strip()
    kind, typ = spec.get("kind"), spec.get("type")
    if kind == "secret":
        return None if raw == "" else raw
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
        if "min" in spec:                   # curated sliders clamp; advanced
            v = min(max(v, spec["min"]), spec["max"])  # knobs are free-form
        if v < 0:
            raise ValueError(f"{spec['key']}: must not be negative")
        return str(int(v)) if float(v).is_integer() else str(v)
    if typ == "choice":
        if raw not in [c[0] for c in spec["choices"]]:
            raise ValueError(f"{spec['key']}: not one of the choices")
        return raw
    if kind == "multiline":
        parts = [p.strip() for chunk in raw.split("\n")
                 for p in chunk.split(";")]
        return ";".join(p for p in parts if p)
    return raw


def save(values: dict[str, str]) -> dict:
    """Validate and persist submitted values. Unknown keys are rejected —
    this endpoint must not be a generic 'set any env var' primitive. A blank
    value reverts a setting to its env-file/code default (secrets: blank
    keeps the stored one). Returns what changed and whether a restart is
    needed to apply it."""
    store = _read()
    changed = []
    for key, raw in values.items():
        spec = _SPECS.get(key)
        if spec is None:
            raise ValueError(f"unknown setting: {key[:40]}")
        norm = _normalize(spec, str(raw))
        if norm is None:
            continue
        before = pending(key)
        if norm == "" and spec.get("type") != "connection":
            store.pop(key, None)            # revert knob to env/default
        elif spec.get("advanced") and norm == default(key):
            store.pop(key, None)            # setting a knob back to its default
        else:
            store[key] = norm
        if pending_from(store, key) != before:
            changed.append(key)
    if changed:
        _write(store)
    return {"changed": changed, "restart_needed": restart_pending()}


def pending_from(store: dict[str, str], key: str) -> str:
    if key in store:
        return store[key]
    return running(key)
