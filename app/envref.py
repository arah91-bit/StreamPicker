"""Generates the annotated .env views of the configuration.

Two consumers, one catalog (app.config + app.knobs), so neither can drift from
what the code actually reads:

- current_dotenv(): the addon's *current* effective config as a ready-to-edit
  .env, secrets redacted. Served at /{secret}/settings/export.env for backup,
  migration, or handing to an AI to inspect and adjust.
- reference_dotenv(): every key with its default, fully commented out — the
  blank menu an operator or AI fills in from scratch. Written to the committed
  .env.reference by tools/gen_env_reference.py.
"""

from app import config, knobs

_HEADER_CUR = [
    "# ── stream-picker · current configuration ───────────────────────────────",
    "# What the addon will use on its next restart. Secrets are redacted — put",
    "# them back before reusing this file. Every key below can be set here (mount",
    "# as .env) or from the Settings dashboard; the dashboard writes the same keys",
    "# to data/config.json. Blank a line to fall back to the built-in default.",
    "",
]

_HEADER_REF = [
    "# ── stream-picker · full environment reference ──────────────────────────",
    "# Every setting the addon understands, with its default, all commented out.",
    "# Uncomment and set the ones you want; leave the rest to their defaults.",
    "# This is the complete menu — most people only touch the connections and a",
    "# couple of settings, and can do all of it from the dashboard instead.",
    "#",
    "# Generated from app/knobs.py + app/config.py — do not hand-edit; regenerate",
    "# with:  python -m tools.gen_env_reference",
    "",
]


def _redacted(key: str, value: str) -> bool:
    return config.is_secret(key)


def _emit(lines: list[str], key: str, value: str, blurb: str,
          commented: bool, secret: bool, indent: str = "") -> None:
    if blurb:
        lines.append(f"{indent}# {blurb}")
    if secret:
        note = "set; redacted — re-enter to use" if value else "your value here"
        lines.append(f"{indent}#{key}=            # {note}")
    elif commented:
        lines.append(f"{indent}#{key}={value}")
    else:
        lines.append(f"{indent}{key}={value}")


def _body(value_fn, commented: bool) -> list[str]:
    L: list[str] = []

    L.append("# ── required ──")
    _emit(L, "ADDON_SECRET", "", "The unguessable path segment gating addon "
          "installation. Generate: "
          "openssl rand -hex 24", commented=commented,
          secret=True)
    L.append("")

    L.append("# ── identity ──")
    for key in ("ADDON_PUBLIC_URL", "ADDON_NAME", "SLOW_ADDON_NAME"):
        spec = config._SPECS.get(key) or knobs.spec(key) or {}
        blurb = spec.get("desc") or spec.get("blurb") or ""
        _emit(L, key, value_fn(key), blurb, commented=commented, secret=False)
    L.append("")

    L.append("# ── connections (each has a live Test in the dashboard) ──")
    for c in config.CONNECTIONS:
        L.append(f"# {c['name']} — {c['role']}")
        for f in c["fields"]:
            key = f["key"]
            _emit(L, key, value_fn(key), "", commented=commented,
                  secret=config.is_secret(key))
        L.append("")

    L.append("# ── settings (curated dashboard controls) ──")
    for s in config.SETTINGS:
        if s["key"] in ("ADDON_NAME", "ADDON_PUBLIC_URL"):
            continue                       # already under identity
        _emit(L, s["key"], value_fn(s["key"]), s.get("desc") or s["label"],
              commented=commented, secret=config.is_secret(s["key"]))
    L.append("")

    L.append("# ── advanced tuning ──")
    for gid, title in knobs.GROUPS:
        rows = knobs.by_group(gid)
        if not rows:
            continue
        L.append(f"# · {title} ·")
        for s in rows:
            _emit(L, s["key"], value_fn(s["key"]), s["blurb"],
                  commented=commented, secret=False)
        L.append("")
    return L


def current_dotenv() -> str:
    return "\n".join(_HEADER_CUR + _body(config.pending, commented=False)) + "\n"


def reference_dotenv() -> str:
    return "\n".join(_HEADER_REF + _body(config.default, commented=True)) + "\n"
