"""Scraper-engine catalog behind the unified Sources panel on /{secret}/settings.

One place to pick which upstream scrapers this instance searches, each minted
from the operator's central debrid key(s) — the same keys the debrid editor at
the top of the panel manages. Toggling an engine on rewrites the runtime config
key it owns; app.sources and the picker are unchanged (they still read those
runtime keys). This module is purely a config-authoring layer.

Engines come in three shapes:
  * client-mintable — Comet, StremThru, Torrentio, KnightCrawler: the debrid key
    goes straight into the URL, no network call.
  * addon-minted    — MediaFusion: its config is AES-encrypted by the addon's own
    server, so minting is one POST to its encrypt endpoint. If that fails we fall
    back to the operator's existing/custom URL, never breaking a working source.
  * custom-only     — Jackettio (needs your own Jackett/Prowlarr backend): the
    operator supplies a configured manifest URL; there is no public default.

Comet/StremThru/MediaFusion write their dedicated runtime keys (FAST_BASE_URL,
STREMTHRU_BASE_URL, MEDIAFUSION_BASE_URL). Every other engine is an entry in the
EXTRA_ADDONS list, which app.sources already races as a first-class online
source. SCRAPERS records which engines are enabled (+ any custom-URL overrides)
so the panel round-trips; the runtime keys are its derived output. Minted URLs
embed the debrid key — like the other lane URLs they live in config.json, which
is written owner-only (0600).
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx

from app import debrid, wizard

# ── public instances used when the operator hasn't supplied their own base ──
TORRENTIO_PUBLIC = "https://torrentio.strem.fun"
KNIGHTCRAWLER_PUBLIC = "https://knightcrawler.elfhosted.com"
MEDIAFUSION_PUBLIC = "https://mediafusion.elfhosted.com"

# runtime config keys the three built-in engines own (cleared when toggled off)
BUILTIN_KEYS = ("FAST_BASE_URL", "STREMTHRU_BASE_URL", "MEDIAFUSION_BASE_URL")

# (id, label, badge, blurb, owns runtime key | None -> EXTRA_ADDONS entry,
#  needs a debrid key, custom-URL only, docs link)
ENGINES = [
    {"id": "comet", "label": "Comet", "badge": "CO", "key": "FAST_BASE_URL",
     "needs_debrid": True, "custom_only": False,
     "blurb": "Fast cached-debrid search across public indexers — the fast lane.",
     "docs": "https://comet.feels.legal"},
    {"id": "stremthru", "label": "StremThru Torz", "badge": "TZ",
     "key": "STREMTHRU_BASE_URL", "needs_debrid": True, "custom_only": False,
     "blurb": "Crowdsourced DMM/Zilean hash index — deep long-tail coverage, "
              "answers fast.",
     "docs": "https://github.com/MunifTanjim/stremthru"},
    {"id": "mediafusion", "label": "MediaFusion", "badge": "MF",
     "key": "MEDIAFUSION_BASE_URL", "needs_debrid": True, "custom_only": False,
     "blurb": "Broad scrape (Prowlarr/Jackett/Zilean) — slower first hit, feeds "
              "the quality pass.",
     "docs": "https://mediafusion.elfhosted.com"},
    {"id": "prowlarr", "label": "Prowlarr", "badge": "PR", "key": None,
     "needs_debrid": True, "custom_only": False, "needs_prowlarr": True,
     "internal": True,
     "blurb": "Search your own Prowlarr indexers directly and stream the cached "
              "torrents through your debrid. Add your Prowlarr above first.",
     "docs": "https://prowlarr.com"},
    {"id": "torrentio", "label": "Torrentio", "badge": "TO", "key": None,
     "needs_debrid": True, "custom_only": False,
     "blurb": "Public torrent scraper — mainstream plus anime (Nyaa, "
              "AnimeTosho, and 20+ indexers).",
     "docs": "https://torrentio.strem.fun"},
    {"id": "knightcrawler", "label": "KnightCrawler", "badge": "KC", "key": None,
     "needs_debrid": True, "custom_only": False,
     "blurb": "Torrentio-compatible community scraper. The public instance can "
              "be flaky — paste your own base to be safe.",
     "docs": "https://github.com/knightcrawler-stremio/knightcrawler"},
    {"id": "jackettio", "label": "Jackettio", "badge": "JK", "key": None,
     "needs_debrid": False, "custom_only": True,
     "blurb": "Your own Jackett/Prowlarr trackers, resolved through debrid. "
              "Needs a configured instance URL — no public default.",
     "docs": "https://github.com/arvida42/jackettio"},
]
BY_ID = {e["id"]: e for e in ENGINES}
_BUILTIN_IDS = {e["id"] for e in ENGINES if e["key"]}


def engine_meta() -> list[dict]:
    """The catalog rows the settings page renders (no keys, no secrets)."""
    return [{"id": e["id"], "label": e["label"], "badge": e["badge"],
             "blurb": e["blurb"], "needs_debrid": e["needs_debrid"],
             "custom_only": e["custom_only"],
             "needs_prowlarr": e.get("needs_prowlarr", False),
             "internal": e.get("internal", False), "docs": e["docs"]}
            for e in ENGINES]


# ── Torrentio / KnightCrawler URL surgery ────────────────────────────────────
# Torrentio-family addons carry config as pipe-joined options in one path
# segment and accept a single debrid, keyed by the provider id itself
# (…/sort=…|realdebrid=KEY/manifest.json). We store the base without
# /manifest.json; app.sources appends /stream/… to it.
_TORRENTIO_OPTS = "sort=qualitysize|qualityfilter=480p,scr,cam,unknown"
# The providers Torrentio's URL accepts, where the debrid id equals its key name.
_TORRENTIO_DEBRID = {"realdebrid", "alldebrid", "premiumize", "debridlink",
                     "offcloud", "torbox"}


def _torrentio_like(debrids: list[tuple[str, str]], base: str) -> str:
    # Torrentio takes one debrid — use the primary (first) supported provider.
    pick = next(((s, k) for s, k in debrids if s in _TORRENTIO_DEBRID), None)
    opts = _TORRENTIO_OPTS + (f"|{pick[0]}={pick[1]}" if pick else "")
    return f"{base.rstrip('/')}/{opts}"


def build_torrentio(debrids: list[tuple[str, str]],
                    base: str = TORRENTIO_PUBLIC) -> str:
    return _torrentio_like(debrids, base)


def build_knightcrawler(debrids: list[tuple[str, str]],
                        base: str = KNIGHTCRAWLER_PUBLIC) -> str:
    return _torrentio_like(debrids, base)


# ── MediaFusion (addon-side AES config) ──────────────────────────────────────
# MediaFusion encrypts config server-side with its own SECRET_KEY, so we cannot
# mint a URL by hand — we POST the debrid config to its encrypt endpoint and get
# back an opaque secret that goes in the path.
_MEDIAFUSION_SVC = {"realdebrid": "real_debrid", "alldebrid": "alldebrid",
                    "premiumize": "premiumize", "debridlink": "debridlink",
                    "offcloud": "offcloud", "torbox": "torbox",
                    "pikpak": "pikpak", "easydebrid": "easydebrid",
                    "debrider": "debrider"}


def _origin(url: str) -> str:
    """The scheme://host(/prefix) of a manifest URL, without any config segment
    or trailing /manifest.json. Best-effort — returns '' for junk."""
    url = (url or "").strip()
    if not url:
        return ""
    sp = urlsplit(url if "://" in url else "https://" + url)
    if not sp.netloc:
        return ""
    return f"{sp.scheme}://{sp.netloc}"


async def build_mediafusion(existing_url: str, debrids: list[tuple[str, str]],
                            *, base: str = "", prowlarr: dict | None = None,
                            api_password: str = "") -> str:
    """Mint a MediaFusion manifest base by asking its encrypt endpoint to seal a
    minimal debrid config. Raises on any failure so the caller can fall back to
    the operator's existing/custom URL rather than break a working source.

    ``prowlarr`` ({"url", "api_key"}) is injected as MediaFusion's per-user
    ``indexer_config`` so the same Prowlarr the operator added drives its scrape
    — the one config-model field stream-picker can set that Comet's cannot.
    ``api_password`` authenticates the encrypt call (and is embedded so the
    minted URL streams) on a private MediaFusion instance; public instances
    leave it blank."""
    base = _origin(base) or _origin(existing_url) or MEDIAFUSION_PUBLIC
    pick = next(((s, k) for s, k in debrids if s in _MEDIAFUSION_SVC), None)
    provider = ({"service": _MEDIAFUSION_SVC[pick[0]], "token": pick[1],
                 "enable_watchlist_catalogs": False} if pick else None)
    body: dict = {"streaming_provider": provider}
    if api_password:
        body["api_password"] = api_password
    if prowlarr and prowlarr.get("url") and prowlarr.get("api_key"):
        body["indexer_config"] = {"prowlarr": {
            "enabled": True, "use_global": False,
            "url": prowlarr["url"], "api_key": prowlarr["api_key"]}}
    headers = {"X-API-Key": api_password} if api_password else {}
    async with httpx.AsyncClient(follow_redirects=True) as c:
        r = await c.post(f"{base}/encrypt-user-data", json=body,
                         headers=headers, timeout=20)
        r.raise_for_status()
        payload = r.json()
    secret = payload.get("encrypted_str") or payload.get("secret_str") or ""
    if not secret:
        raise ValueError(payload.get("message") or "MediaFusion returned no config")
    return f"{base.rstrip('/')}/{secret}"


# ── custom-URL handling ──────────────────────────────────────────────────────

def _clean_base(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if url.endswith("/manifest.json"):
        url = url[:-len("/manifest.json")].rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("manifest URLs must start with http:// or https://")
    return url


# ── reading current state for the panel ──────────────────────────────────────

def _match_extra(url: str) -> str:
    """Which preset an EXTRA_ADDONS url belongs to, by host, else '' (custom)."""
    host = urlsplit(url if "://" in url else "https://" + url).netloc.lower()
    if "torrentio" in host:
        return "torrentio"
    if "knightcrawler" in host:
        return "knightcrawler"
    return ""


def current(fast_url: str, stremthru_url: str, mediafusion_url: str,
            extra_addons: str, scrapers: str,
            prowlarr_source: str = "") -> list[dict]:
    """The enabled engines the panel should render as [{"id", "url"?, "name"?}].

    SCRAPERS is authoritative once written; otherwise reconstruct from the
    runtime keys so an existing install opens with its sources already on. Never
    emits a debrid key — only which engines are configured (custom URLs, which a
    user pasted themselves, are echoed back so they can edit them)."""
    if (scrapers or "").strip():
        try:
            items = json.loads(scrapers)
            out = []
            for it in items if isinstance(items, list) else []:
                sid = str((it or {}).get("id", ""))
                if sid:
                    row = {"id": sid}
                    if it.get("url"):
                        row["url"] = str(it["url"])
                    if it.get("name"):
                        row["name"] = str(it["name"])
                    out.append(row)
            return out
        except ValueError:
            pass
    out = []
    if (fast_url or "").strip():
        out.append({"id": "comet"})
    if (stremthru_url or "").strip():
        out.append({"id": "stremthru"})
    if (mediafusion_url or "").strip():
        out.append({"id": "mediafusion"})
    if str(prowlarr_source or "").strip().lower() in ("1", "true", "yes", "on"):
        out.append({"id": "prowlarr"})
    for it in _load_extras(extra_addons):
        preset = _match_extra(it["url"])
        if preset and preset not in {o["id"] for o in out}:
            out.append({"id": preset})
        elif not preset:
            out.append({"id": f"custom-{_slug(it['name'] or it['url'])}",
                        "name": it["name"], "url": it["url"]})
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "addon"


def _load_extras(raw: str) -> list[dict]:
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and str(it.get("url", "")).strip():
            out.append({"name": str(it.get("name", "")).strip(),
                        "url": str(it["url"]).strip()})
    return out


# ── applying the panel ───────────────────────────────────────────────────────

def _resolve_engines(submitted) -> list[dict]:
    """Validate/dedupe the submitted engine list into normalized rows. Unknown
    built-in/preset ids raise; ad-hoc 'custom-*' ids pass through with their
    pasted url. Raises ValueError on a custom_only/custom engine missing a URL."""
    seen: set[str] = set()
    out: list[dict] = []
    for it in submitted or []:
        sid = str((it or {}).get("id", "")).strip()
        if not sid:
            continue
        if sid in seen:
            raise ValueError(f"{sid} is listed twice")
        seen.add(sid)
        url = str((it or {}).get("url", "")).strip()
        name = str((it or {}).get("name", "")).strip()
        is_custom = sid.startswith("custom-")
        eng = BY_ID.get(sid)
        if eng is None and not is_custom:
            raise ValueError(f"unknown scraper: {sid[:24]}")
        if is_custom or (eng and eng["custom_only"]):
            if not url:
                label = name or (eng["label"] if eng else sid)
                raise ValueError(f"paste a manifest URL for {label}")
        out.append({"id": sid, "url": url, "name": name, "custom": is_custom})
    return out


async def apply(fast_url: str, stremthru_url: str, mediafusion_url: str,
                extra_addons: str, debrids_submitted, engines_submitted,
                *, prowlarr_submitted: dict | None = None,
                prowlarr_url: str = "", prowlarr_key: str = "",
                mf_api_password: str = "", dry_run: bool = False) -> dict:
    """Mint every enabled engine from the central debrid list and return the
    runtime-key updates to persist. Fresh checkable debrid keys are verified
    first (a definitive rejection aborts); dry_run only reports those checks.
    Returns {"ok", "results", ["values"]}; the caller persists ``values``.
    Never echoes a key back.

    The Prowlarr backend (``prowlarr_submitted`` {"url", "api_key"}, with
    ``prowlarr_url``/``prowlarr_key`` the stored values a blank submit keeps) is
    persisted here and, when the MediaFusion or Prowlarr engines are on, wired
    into them — MediaFusion via its per-user indexer config, Prowlarr via the
    native lane toggle (``PROWLARR_SOURCE``)."""
    engines = _resolve_engines(engines_submitted)
    ids = {e["id"] for e in engines}

    # Resolve the Prowlarr backend: a blank submitted key keeps the stored one.
    p_sub = prowlarr_submitted or {}
    p_url = str(p_sub.get("url", "")).strip() if prowlarr_submitted is not None \
        else str(prowlarr_url or "").strip()
    p_key_new = str(p_sub.get("api_key", "")).strip()
    eff_pkey = p_key_new or str(prowlarr_key or "").strip()
    prowlarr_cfg = ({"url": p_url, "api_key": eff_pkey}
                    if p_url and eff_pkey else None)
    wants_debrid = any((BY_ID.get(e["id"]) or {}).get("needs_debrid")
                       and not e["url"] for e in engines)

    # Resolve the debrid list exactly as the debrid editor does (blank key keeps
    # the stored one). Only required when a minted engine actually needs it.
    resolved: list[tuple[str, str]] = []
    kept: set[str] = set()
    if debrids_submitted or wants_debrid:
        resolved, kept = debrid._resolve(fast_url, stremthru_url, debrids_submitted)

    # Verify freshly-pasted checkable keys (all of them on a dry run).
    to_check = [(s, k) for s, k in resolved
                if dry_run or (s not in kept and s in debrid._KEY_CHECKED)]
    results: dict[str, dict] = {}
    if to_check:
        settled = await asyncio.gather(
            *(debrid.validate_key(s, k) for s, k in to_check),
            return_exceptions=True)
        results = {s: debrid._result(r) for (s, _), r in zip(to_check, settled)}
    ok = not any(r["ok"] is False for r in results.values())
    if dry_run or not ok:
        return {"ok": ok, "results": results}

    # Mint. Built-ins write their dedicated key; everything else becomes an
    # EXTRA_ADDONS entry. Engines not enabled that own a built-in key are cleared.
    values: dict[str, str] = {}
    extras: list[dict] = []
    for e in engines:
        sid = e["id"]
        eng = BY_ID.get(sid)
        if sid == "comet":
            values["FAST_BASE_URL"] = (_clean_base(e["url"]) if e["url"]
                                       else debrid.build_comet(fast_url, resolved))
        elif sid == "stremthru":
            values["STREMTHRU_BASE_URL"] = (
                _clean_base(e["url"]) if e["url"]
                else debrid.build_stremthru(stremthru_url, resolved))
        elif sid == "mediafusion":
            values["MEDIAFUSION_BASE_URL"] = await _mediafusion_url(
                mediafusion_url, resolved, e["url"],
                prowlarr=prowlarr_cfg, api_password=mf_api_password)
        elif sid == "prowlarr":
            # Internal lane — no manifest URL to mint. Enablement is the
            # PROWLARR_SOURCE flag, set below once creds are confirmed.
            continue
        elif sid == "torrentio":
            extras.append({"name": "Torrentio", "url": _clean_base(e["url"])
                           if e["url"] else build_torrentio(resolved)})
        elif sid == "knightcrawler":
            extras.append({"name": "KnightCrawler", "url": _clean_base(e["url"])
                           if e["url"] else build_knightcrawler(resolved)})
        elif eng and eng["custom_only"]:            # jackettio
            extras.append({"name": eng["label"], "url": _clean_base(e["url"])})
        else:                                       # ad-hoc custom-* addon
            extras.append({"name": e["name"] or e["url"],
                           "url": _clean_base(e["url"])})

    # Built-in lanes toggled off are *cleared* (their keys are sensitive, so a
    # blank save would keep them). EXTRA_ADDONS/SCRAPERS are plain, so "" clears.
    clears = [key for key in BUILTIN_KEYS
              if next(k for k in _BUILTIN_IDS if BY_ID[k]["key"] == key) not in ids]

    # Persist the Prowlarr backend and the native-lane toggle. Clearing the URL
    # removes the whole backend (and disables the lane); a blank key keeps the
    # stored one. PROWLARR_SOURCE is a secret-free flag, so it clears cleanly.
    if prowlarr_submitted is not None:
        if p_url:
            values["PROWLARR_URL"] = p_url
            if p_key_new:
                values["PROWLARR_API_KEY"] = p_key_new
        else:
            clears += ["PROWLARR_URL", "PROWLARR_API_KEY"]
            prowlarr_cfg = None
    if "prowlarr" in ids and prowlarr_cfg:
        values["PROWLARR_SOURCE"] = "1"
    else:
        clears.append("PROWLARR_SOURCE")

    values["EXTRA_ADDONS"] = json.dumps(extras, separators=(",", ":")) if extras else ""
    values["SCRAPERS"] = json.dumps(
        [{k: v for k, v in (("id", e["id"]),
                            ("url", e["url"]), ("name", e["name"])) if v}
         for e in engines], separators=(",", ":"))
    return {"ok": True, "results": results, "values": values, "clears": clears}


async def mint_for_test(engine_id: str, custom_url: str, debrids_submitted,
                        fast_url: str, stremthru_url: str,
                        mediafusion_url: str) -> str:
    """Mint one engine's manifest base for the per-engine Test button, resolving
    the submitted debrid rows the same way apply() does (blank key keeps the
    stored one). Raises ValueError with a user-facing message on bad input."""
    is_custom = engine_id.startswith("custom-")
    eng = BY_ID.get(engine_id)
    if eng is None and not is_custom:
        raise ValueError(f"unknown scraper: {engine_id[:24]}")
    if eng and eng.get("internal"):
        # No manifest to mint — its Test verifies the backend it depends on
        # (e.g. the Prowlarr connection), routed separately by the UI.
        raise ValueError(f"test the {eng['label']} connection instead")
    custom_url = (custom_url or "").strip()
    if is_custom or (eng and eng["custom_only"]) or custom_url:
        if not custom_url:
            raise ValueError("paste a manifest URL to test")
        return _clean_base(custom_url)
    resolved: list[tuple[str, str]] = []
    if eng["needs_debrid"] or debrids_submitted:
        resolved, _ = debrid._resolve(fast_url, stremthru_url, debrids_submitted)
    if engine_id == "comet":
        return debrid.build_comet(fast_url, resolved)
    if engine_id == "stremthru":
        return debrid.build_stremthru(stremthru_url, resolved)
    if engine_id == "mediafusion":
        return await _mediafusion_url(mediafusion_url, resolved, "")
    if engine_id == "torrentio":
        return build_torrentio(resolved)
    if engine_id == "knightcrawler":
        return build_knightcrawler(resolved)
    raise ValueError(f"cannot mint {engine_id}")


async def _mediafusion_url(existing_url: str, debrids, custom: str, *,
                           prowlarr: dict | None = None,
                           api_password: str = "") -> str:
    """Prefer a pasted custom URL; else mint via the encrypt endpoint (injecting
    the operator's Prowlarr when configured); else keep whatever is already
    configured — never break a working MediaFusion."""
    if custom:
        return _clean_base(custom)
    try:
        return await build_mediafusion(existing_url, debrids,
                                       prowlarr=prowlarr,
                                       api_password=api_password)
    except Exception:
        if existing_url.strip():
            return _clean_base(existing_url)
        raise ValueError("could not reach MediaFusion to configure it — paste a "
                         "configured manifest URL instead") from None
