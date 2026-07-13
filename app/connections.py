"""Live credential checks behind the Test buttons on /{secret}/settings.

Each test answers the operator's actual question — "will the picker be able
to use this service?" — with one cheap authenticated call, not a bare ping.
Tests read the values from the submitted form first (so a pasted key can be
verified before saving), falling back to the pending config, so what gets
tested is always what the next restart will run with.

Every failure detail is scrubbed of apikey/userinfo fragments before it goes
back to the page: httpx exception text embeds full request URLs.
"""

import asyncio
import re
import time

import httpx

from app import config

_client = httpx.AsyncClient(timeout=8, follow_redirects=True,
                            headers={"User-Agent": "stream-picker/1.0"})

_KEY_RE = re.compile(r"(api_?key=)[^&'\" ]+", re.I)
_USERINFO_RE = re.compile(r"//[^/@\s]+@")


def _scrub(text: str) -> str:
    text = _KEY_RE.sub(r"\1<key>", text)
    return _USERINFO_RE.sub("//<auth>@", text)


def _val(key: str, overrides: dict) -> str:
    v = ((overrides or {}).get(key) or "").strip()
    return v or config.pending(key)


def _ok(t0: float, detail: str) -> dict:
    return {"ok": True, "ms": int((time.monotonic() - t0) * 1000),
            "detail": detail}


def _fail(t0: float, e: Exception | str) -> dict:
    if isinstance(e, httpx.HTTPStatusError):
        msg = f"HTTP {e.response.status_code}"
    elif isinstance(e, Exception):
        msg = f"{type(e).__name__}: {e}"
    else:
        msg = str(e)
    return {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
            "detail": _scrub(msg)[:160]}


async def _manifest(key: str, overrides: dict) -> dict:
    base = _val(key, overrides).rstrip("/")
    t0 = time.monotonic()
    if not base:
        return _fail(t0, "no URL configured")
    try:
        r = await _client.get(f"{base}/manifest.json")
        r.raise_for_status()
        name = r.json().get("name") or r.json().get("id") or "unnamed"
        return _ok(t0, f"manifest: {name}")
    except Exception as e:
        return _fail(t0, e)


async def _tmdb(overrides: dict) -> dict:
    key = _val("TMDB_API_KEY", overrides)
    t0 = time.monotonic()
    if not key:
        return _fail(t0, "no API key configured")
    try:
        r = await _client.get("https://api.themoviedb.org/3/configuration",
                              params={"api_key": key})
        r.raise_for_status()
        return _ok(t0, "key accepted")
    except Exception as e:
        return _fail(t0, e)


async def _tvdb(overrides: dict) -> dict:
    key = _val("TVDB_API_KEY", overrides)
    t0 = time.monotonic()
    if not key:
        return _fail(t0, "no API key configured")
    try:
        r = await _client.post("https://api4.thetvdb.com/v4/login",
                               json={"apikey": key})
        r.raise_for_status()
        if not (r.json().get("data") or {}).get("token"):
            return _fail(t0, "login returned no token")
        return _ok(t0, "key accepted")
    except Exception as e:
        return _fail(t0, e)


async def _jellyseerr(overrides: dict) -> dict:
    base = _val("JELLYSEERR_URL", overrides).rstrip("/")
    key = _val("JELLYSEERR_API_KEY", overrides)
    t0 = time.monotonic()
    if not base:
        return _fail(t0, "no URL configured")
    try:
        # settings/main requires the key — /status would pass with a bad one
        r = await _client.get(f"{base}/api/v1/settings/main",
                              headers={"X-Api-Key": key})
        r.raise_for_status()
        return _ok(t0, "authenticated")
    except Exception as e:
        return _fail(t0, e)


async def _arr(prefix: str, overrides: dict) -> dict:
    base = _val(f"{prefix}_URL", overrides).rstrip("/")
    key = _val(f"{prefix}_API_KEY", overrides)
    profile = _val(f"{prefix}_QUALITY_PROFILE", overrides)
    t0 = time.monotonic()
    if not base:
        return _fail(t0, "no URL configured")
    try:
        headers = {"X-Api-Key": key}
        r = await _client.get(f"{base}/api/v3/system/status", headers=headers)
        r.raise_for_status()
        version = r.json().get("version", "?")
        detail = f"v{version}"
        if profile:
            rp = await _client.get(f"{base}/api/v3/qualityprofile",
                                   headers=headers)
            rp.raise_for_status()
            names = [p.get("name") for p in rp.json()]
            if profile in names:
                detail += f" · profile '{profile}' found"
            else:
                return _fail(t0, f"v{version} ok but no quality profile "
                                 f"named '{profile}' (has: "
                                 f"{', '.join(names[:6])})")
        return _ok(t0, detail)
    except Exception as e:
        return _fail(t0, e)


async def _nzbdav(overrides: dict) -> dict:
    base = _val("NZBDAV_URL", overrides).rstrip("/")
    user = _val("NZBDAV_USER", overrides)
    pw = _val("NZBDAV_PASS", overrides)
    t0 = time.monotonic()
    if not (base and user and pw):
        return _fail(t0, "URL, user and password are all required")
    try:
        # the exact operation the mount path depends on: authenticated WebDAV
        # against the watch-folder tree
        r = await _client.request("PROPFIND", f"{base}/nzbs/",
                                  auth=(user, pw), headers={"Depth": "0"})
        if r.status_code >= 400:
            return _fail(t0, f"WebDAV HTTP {r.status_code}")
        return _ok(t0, "WebDAV authenticated")
    except Exception as e:
        return _fail(t0, e)


async def _indexers(overrides: dict) -> dict:
    spec = _val("NZB_INDEXERS", overrides)
    t0 = time.monotonic()
    triples = []
    for part in spec.replace("\n", ";").split(";"):
        bits = part.strip().split("|")
        if len(bits) == 3 and all(b.strip() for b in bits):
            triples.append((bits[0].strip(), bits[1].strip().rstrip("/"),
                            bits[2].strip()))
    if not triples:
        return _fail(t0, "no indexers configured (name|api-url|apikey)")

    async def one(name, base, key):
        try:
            r = await _client.get(base, params={"t": "caps", "apikey": key})
            body = r.content[:2000].lower()
            if r.status_code != 200 or b"<error" in body:
                m = re.search(rb'description="([^"]{0,60})', body)
                why = (m.group(1).decode("utf-8", "replace") if m
                       else f"HTTP {r.status_code}")
                return name, why
            return name, None
        except Exception as e:
            return name, type(e).__name__

    results = await asyncio.gather(*(one(*t) for t in triples))
    bad = [(n, why) for n, why in results if why]
    if not bad:
        return _ok(t0, f"{len(results)}/{len(results)} indexers responded")
    detail = (f"{len(results) - len(bad)}/{len(results)} ok — failed: "
              + ", ".join(f"{n} ({_scrub(w)})" for n, w in bad))
    return _fail(t0, detail)


_TESTS = {
    "comet": lambda o: _manifest("FAST_BASE_URL", o),
    "stremthru": lambda o: _manifest("STREMTHRU_BASE_URL", o),
    "mediafusion": lambda o: _manifest("MEDIAFUSION_BASE_URL", o),
    "jellio": lambda o: _manifest("JELLIO_URL", o),
    "tmdb": _tmdb,
    "tvdb": _tvdb,
    "jellyseerr": _jellyseerr,
    "radarr": lambda o: _arr("RADARR", o),
    "sonarr": lambda o: _arr("SONARR", o),
    "nzbdav": _nzbdav,
    "indexers": _indexers,
}


async def test(service: str, overrides: dict | None = None) -> dict:
    fn = _TESTS.get(service)
    if fn is None:
        raise ValueError(f"unknown service: {service[:40]}")
    return await fn(overrides or {})
