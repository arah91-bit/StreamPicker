"""Debrid provider registry and the Comet/StremThru URL surgery behind the
settings-page debrid manager.

Stream-picker never talks to a debrid service itself: the operator's debrid
keys are embedded in the Comet (``FAST_BASE_URL``) and StremThru
(``STREMTHRU_BASE_URL``) manifest URLs, and the picker consumes those. So
"configure debrid" here means *editing the debrid list inside those two URLs*
while preserving every other setting the operator already has in them (Comet in
particular carries languages, resolutions, size caps, sort order…).

The provider ids, badges and StremThru store codes below are the authoritative
set the bundled Comet/StremThru accept — Comet's ``VALID_DEBRID_SERVICES`` and
StremThru's store-code map. (put.io and Seedr are intentionally absent: the
bundled Comet's validator rejects them.) Multiple providers are supported on
purpose — the picker races every resulting stream and the proxy can splice two
sources together, so more debrids means a faster and more resilient pick.

Key validation for the four services with a cheap account endpoint reuses the
wizard's checks; the rest are proven by the source Test (the minted Comet URL
must actually return streams).
"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlsplit

from app import wizard

# (id == Comet service id, label, badge, StremThru store code, where to get the
#  key, key-field hint, optional referral code for the sign-up link)
PROVIDERS = [
    {"id": "torbox", "label": "TorBox", "badge": "TB", "code": "tb",
     "key_url": "https://torbox.app/settings", "hint": "API key",
     "referral": "9ca21adb-dbcb-4fb0-9195-412a5f3519bc",
     "signup": "https://torbox.app/subscription"},
    {"id": "realdebrid", "label": "Real-Debrid", "badge": "RD", "code": "rd",
     "key_url": "https://real-debrid.com/apitoken", "hint": "API token",
     "signup": "https://real-debrid.com/"},
    {"id": "alldebrid", "label": "AllDebrid", "badge": "AD", "code": "ad",
     "key_url": "https://alldebrid.com/apikeys", "hint": "API key",
     "signup": "https://alldebrid.com/"},
    {"id": "premiumize", "label": "Premiumize", "badge": "PM", "code": "pm",
     "key_url": "https://www.premiumize.me/account", "hint": "API key",
     "signup": "https://www.premiumize.me/"},
    {"id": "debridlink", "label": "Debrid-Link", "badge": "DL", "code": "dl",
     "key_url": "https://debrid-link.com/webapp/apikey", "hint": "API key",
     "signup": "https://debrid-link.com/"},
    {"id": "offcloud", "label": "Offcloud", "badge": "OC", "code": "oc",
     "key_url": "https://offcloud.com/#/account", "hint": "API key",
     "signup": "https://offcloud.com/"},
    {"id": "easydebrid", "label": "EasyDebrid", "badge": "ED", "code": "ed",
     "key_url": "https://paradise-cloud.com/products/easydebrid",
     "hint": "API key", "signup": "https://paradise-cloud.com/"},
    {"id": "debrider", "label": "Debrider", "badge": "DB", "code": "dr",
     "key_url": "https://debrider.app/dashboard/account", "hint": "API key",
     "signup": "https://debrider.app/"},
    {"id": "pikpak", "label": "PikPak", "badge": "PP", "code": "pp",
     "key_url": "https://mypikpak.com", "hint": "email:password",
     "signup": "https://mypikpak.com/"},
]
BY_ID = {p["id"]: p for p in PROVIDERS}
_BY_CODE = {p["code"]: p["id"] for p in PROVIDERS}

# The subset with a working account-endpoint key check (reused from the wizard);
# every other provider is validated only by the source Test on the minted URL.
_KEY_CHECKED = {"torbox", "realdebrid", "alldebrid", "premiumize"}


def signup_url(provider: dict) -> str:
    """Sign-up link, carrying the operator's referral code when one is set."""
    base = provider.get("signup") or provider.get("key_url") or ""
    ref = provider.get("referral")
    if ref and base:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}referral={ref}"
    return base


# ── Comet URL surgery ────────────────────────────────────────────────────────

def _b64_json(seg: str, urlsafe: bool = False) -> dict:
    seg = seg + "=" * (-len(seg) % 4)
    raw = (base64.urlsafe_b64decode if urlsafe else base64.b64decode)(seg)
    return json.loads(raw)


def parse_comet(url: str) -> tuple[str, dict]:
    """(base, config dict) for a Comet manifest URL. base is the origin (+ any
    path prefix) without the config segment; config is {} when the URL has no
    decodable config (a bare host, or something we don't recognise)."""
    url = (url or "").strip()
    if not url:
        return "", {}
    sp = urlsplit(url if "://" in url else "https://" + url)
    origin = f"{sp.scheme}://{sp.netloc}"
    segs = [s for s in sp.path.split("/") if s]
    if segs:
        try:
            cfg = _b64_json(segs[-1])
            if isinstance(cfg, dict):
                prefix = "/" + "/".join(segs[:-1]) if segs[:-1] else ""
                return origin + prefix, cfg
        except Exception:
            pass
    return origin + ("/" + "/".join(segs) if segs else ""), {}


def build_comet(existing_url: str, debrids: list[tuple[str, str]]) -> str:
    """Comet URL with its debrid list replaced, every other setting preserved."""
    base, cfg = parse_comet(existing_url)
    if not base:
        base = wizard.COMET_PUBLIC
    if not cfg:
        cfg = {"cachedOnly": True, "removeTrash": True}
    cfg["debridServices"] = [{"service": s, "apiKey": k} for s, k in debrids]
    b64 = base64.b64encode(json.dumps(cfg).encode()).decode()
    return f"{base.rstrip('/')}/{b64}"


# ── StremThru (Torz) URL surgery ─────────────────────────────────────────────

_TORZ_MARKER = "/stremio/torz/"


def parse_stremthru(url: str) -> tuple[str, dict]:
    url = (url or "").strip()
    if not url:
        return "", {}
    i = url.find(_TORZ_MARKER)
    if i == -1:
        return url.rstrip("/"), {}
    base = url[:i]
    seg = url[i + len(_TORZ_MARKER):].strip("/").split("/")[0]
    try:
        cfg = _b64_json(seg, urlsafe=True)
        if isinstance(cfg, dict):
            return base, cfg
    except Exception:
        pass
    return base, {}


def build_stremthru(existing_url: str, debrids: list[tuple[str, str]]) -> str:
    base, cfg = parse_stremthru(existing_url)
    if not base:
        base = wizard.STREMTHRU_PUBLIC
    if not cfg:
        cfg = {"indexers": None, "cached": True}
    cfg["stores"] = [{"c": BY_ID[s]["code"], "t": k}
                     for s, k in debrids if s in BY_ID]
    b64 = (base64.urlsafe_b64encode(
        json.dumps(cfg, separators=(",", ":")).encode())
        .decode().rstrip("="))
    return f"{base.rstrip('/')}/stremio/torz/{b64}"


# ── current state ────────────────────────────────────────────────────────────

def current(fast_url: str) -> list[dict]:
    """The debrid services configured in a Comet URL, in order, as
    [{"service", "key"}]. Unknown services are dropped."""
    _, cfg = parse_comet(fast_url)
    out = []
    for entry in cfg.get("debridServices") or []:
        sid = (entry or {}).get("service")
        if sid in BY_ID:
            out.append({"service": sid, "key": (entry or {}).get("apiKey") or ""})
    return out


def stremthru_current(url: str) -> list[dict]:
    """The debrid services configured in a StremThru Torz URL, mapped from its
    store codes back to provider ids, as [{"service", "key"}]."""
    _, cfg = parse_stremthru(url)
    out = []
    for store in cfg.get("stores") or []:
        sid = _BY_CODE.get((store or {}).get("c"))
        if sid:
            out.append({"service": sid, "key": (store or {}).get("t") or ""})
    return out


async def validate_key(service: str, key: str) -> dict:
    """Best-effort account-endpoint check. {"ok": True/False, "detail": ...} for
    the four checkable providers; {"ok": None, ...} (unchecked) for the rest, so
    a caller treats them as non-blocking and leans on the source Test."""
    if service in _KEY_CHECKED:
        return await wizard._key_check(service, key)
    if service in BY_ID:
        return {"ok": None, "detail": "verified by the source test"}
    return {"ok": False, "detail": "unknown service"}


# ── editing the live debrid list ─────────────────────────────────────────────

def _result(r) -> dict:
    if isinstance(r, BaseException):
        return {"ok": False, "detail": type(r).__name__}
    return {"ok": r.get("ok"), "detail": str(r.get("detail", ""))[:200]}


def _resolve(fast_url: str, stremthru_url: str,
             submitted) -> tuple[list[tuple[str, str]], set[str]]:
    """Turn the submitted provider list into (service, key) pairs, plus the set
    of ids whose key was *kept* (submitted blank). A blank key reuses whatever
    is already stored for that provider — Comet's copy first, then StremThru's —
    so an operator can re-order or drop providers without re-pasting every key.
    Raises ValueError on an unknown service, a duplicate, an empty list, or a
    blank key for a provider that has nothing stored to keep."""
    stored: dict[str, str] = {}
    for d in current(fast_url):
        stored[d["service"]] = d["key"]
    for d in stremthru_current(stremthru_url):
        stored.setdefault(d["service"], d["key"])

    resolved: list[tuple[str, str]] = []
    kept: set[str] = set()
    seen: set[str] = set()
    for item in submitted or []:
        sid = str((item or {}).get("service", ""))
        if sid not in BY_ID:
            raise ValueError(f"unknown debrid service: {sid[:24]}")
        if sid in seen:
            raise ValueError(f"{BY_ID[sid]['label']} is listed twice")
        seen.add(sid)
        key = str((item or {}).get("key", "")).strip()
        if not key:
            key = stored.get(sid, "")
            if not key:
                raise ValueError(f"paste the API key for {BY_ID[sid]['label']}")
            kept.add(sid)
        resolved.append((sid, key))
    if not resolved:
        raise ValueError("keep at least one debrid service")
    return resolved, kept


async def apply(fast_url: str, stremthru_url: str, submitted,
                *, dry_run: bool = False) -> dict:
    """Rewrite the debrid list inside both lane URLs, preserving every other
    setting in them. Freshly-pasted checkable keys are verified first; a
    definitive rejection aborts the save so a typo can't silently break a lane
    (kept keys are already known-good and aren't re-checked). ``dry_run`` only
    reports the checks. Returns {"ok", "results": {service: {ok, detail}},
    ["values": {config keys}]}; the caller persists ``values``. Never echoes a
    key back."""
    resolved, kept = _resolve(fast_url, stremthru_url, submitted)

    # On a dry-run test, check every provider so the operator sees each one's
    # status. On a real save, only re-check freshly-pasted checkable keys.
    to_check = [(s, k) for s, k in resolved
                if dry_run or (s not in kept and s in _KEY_CHECKED)]
    results: dict[str, dict] = {}
    if to_check:
        settled = await asyncio.gather(
            *(validate_key(s, k) for s, k in to_check),
            return_exceptions=True)
        results = {s: _result(r) for (s, _), r in zip(to_check, settled)}

    # ok is False only on a *definitive* rejection (ok is None == unchecked).
    ok = not any(r["ok"] is False for r in results.values())
    if dry_run or not ok:
        return {"ok": ok, "results": results}
    return {"ok": True, "results": results, "values": {
        "FAST_BASE_URL": build_comet(fast_url, resolved),
        "STREMTHRU_BASE_URL": build_stremthru(stremthru_url, resolved)}}
