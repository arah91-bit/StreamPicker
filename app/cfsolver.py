"""FlareSolverr integration — get past Cloudflare's anti-bot challenge for the
HTTPS stream hosts (torrentio/torbox resolvers, some HTTP scraper addons) that
answer a playback request with a Cloudflare block page instead of video.

FlareSolverr runs a headless browser that solves the challenge and hands back a
``cf_clearance`` cookie plus the exact User-Agent it used. That cookie is bound
to the egress IP *and* the User-Agent, so both must be replayed together. This
addon and FlareSolverr share the Docker host's outbound IP, so a cookie it earns
is valid for our own httpx requests as long as we send the same UA.

Solving a challenge is slow (a real browser hop, ~5-15s), so we never block a
probe on it: a challenge sighting *schedules* a background solve (deduped per
host, negative-cached on failure) that fills a per-host clearance cache, and
every probe/proxy request attaches the cached clearance proactively via
:func:`merge_headers`. Everything is gated on ``CF_SOLVER`` plus a reachable
``FLARESOLVERR_URL`` and is a no-op when off or unreachable — an operator without
FlareSolverr sees exactly today's behaviour.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("cfsolver")

# host -> (monotonic expiry, headers to attach)
_clearance: dict[str, tuple[float, dict]] = {}
# host -> monotonic time before which we won't try to solve again (in-flight or
# a recent failure). Stops a wall of 403s from spawning a wall of browser hops.
_cooldown: dict[str, float] = {}
_inflight: set[str] = set()

_SOLVE_COOLDOWN = 120.0        # back-off after a solve attempt (win or lose)
_MAXTIMEOUT_MS = 60000         # how long FlareSolverr may spend in the browser

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10, read=90, write=10, pool=10))

_CHALLENGE_MARKERS = (
    b"just a moment", b"attention required", b"cf-browser-verification",
    b"/cdn-cgi/challenge-platform", b"cf_chl_", b"_cf_chl_opt",
)


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def _endpoint() -> str:
    return os.environ.get(
        "FLARESOLVERR_URL", "http://flaresolverr:8191/v1").strip()


def enabled() -> bool:
    return _env_bool("CF_SOLVER", True) and bool(_endpoint())


def _ttl() -> float:
    try:
        return max(60.0, float(os.environ.get("CF_SOLVER_TTL", "1800")))
    except ValueError:
        return 1800.0


def _allowlist() -> set[str]:
    raw = os.environ.get("CF_SOLVER_HOSTS", "").strip()
    return {h.strip().lower() for h in raw.replace(",", " ").split() if h.strip()}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _targeted(host: str) -> bool:
    """Auto by default (any host that actually gets Cloudflare-challenged). An
    operator can pin an allowlist to avoid ever touching other hosts; a bare
    entry matches the host and its subdomains."""
    if not host:
        return False
    allow = _allowlist()
    if not allow:
        return True
    return any(host == a or host.endswith("." + a) for a in allow)


def looks_challenged(status: int, headers, body: bytes | None = None) -> bool:
    """A Cloudflare interstitial, not the real resource. The status alone is
    ambiguous (a legit 403 is not a challenge), so require a Cloudflare
    fingerprint: the edge server, its challenge header, or the block-page body."""
    if status not in (403, 429, 503):
        return False
    try:
        server = (headers.get("server") or "").lower()
        mitigated = headers.get("cf-mitigated") or headers.get("cf-chl-bypass")
    except AttributeError:
        server, mitigated = "", None
    if mitigated or "cloudflare" in server:
        return True
    if body:
        low = body[:8192].lower()
        return any(m in low for m in _CHALLENGE_MARKERS)
    return False


def clearance_headers(url: str) -> dict:
    """Fresh clearance for this URL's host, or {} — pure, cheap, no I/O."""
    host = _host(url)
    hit = _clearance.get(host)
    if not hit:
        return {}
    if time.monotonic() >= hit[0]:
        _clearance.pop(host, None)
        return {}
    return dict(hit[1])


def merge_headers(url: str, base: dict | None = None) -> dict:
    """`base` upstream headers with this host's Cloudflare clearance folded in.

    The clearance User-Agent must win — the cookie is bound to it — but any
    referer/origin the stream declared is preserved, and an existing Cookie is
    merged rather than replaced so a host that needs both keeps working."""
    merged = dict(base or {})
    cf = clearance_headers(url)
    if not cf:
        return merged
    # Non-cookie clearance headers (the bound User-Agent) overwrite, case-
    # insensitively. The cookie is merged part-by-part below so a host that
    # declared its own cookie keeps it alongside the clearance token.
    cf_cookie = ""
    for k, v in cf.items():
        if k.lower() == "cookie":
            cf_cookie = v
            continue
        merged = {mk: mv for mk, mv in merged.items() if mk.lower() != k.lower()}
        merged[k] = v
    if cf_cookie:
        base_cookie = ""
        for k in [k for k in merged if k.lower() == "cookie"]:
            base_cookie = merged.pop(k)
        parts = [p.strip() for p in base_cookie.split(";") if p.strip()]
        for p in (p.strip() for p in cf_cookie.split(";") if p.strip()):
            if p not in parts:
                parts.append(p)
        merged["Cookie"] = "; ".join(parts)
    return merged


def note_challenge(url: str) -> None:
    """A request to this host just hit a Cloudflare challenge. Schedule a
    background solve so the *next* request carries clearance. Deduped per host,
    rate-limited, and silent when disabled — safe to call from any hot path."""
    if not enabled():
        return
    host = _host(url)
    if not _targeted(host) or host in _inflight:
        return
    if time.monotonic() < _cooldown.get(host, 0.0):
        return
    if host in _clearance and time.monotonic() < _clearance[host][0]:
        return
    parts = urlsplit(url)
    origin = f"{parts.scheme or 'https'}://{parts.netloc}/"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _inflight.add(host)
    _cooldown[host] = time.monotonic() + _SOLVE_COOLDOWN
    loop.create_task(_solve(host, origin))


async def _flaresolverr_get(origin: str) -> dict:
    """One request.get to FlareSolverr. Separated so tests can stub the network."""
    resp = await _client.post(_endpoint(), json={
        "cmd": "request.get", "url": origin, "maxTimeout": _MAXTIMEOUT_MS})
    resp.raise_for_status()
    return resp.json()


async def _solve(host: str, origin: str) -> None:
    try:
        data = await _flaresolverr_get(origin)
        if (data.get("status") or "").lower() != "ok":
            logger.info("cfsolver: %s not solved (%s)", host,
                        data.get("message"))
            return
        sol = data.get("solution") or {}
        cookies = sol.get("cookies") or []
        jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                        if c.get("name") and c.get("value") is not None)
        headers: dict = {}
        if jar:
            headers["Cookie"] = jar
        ua = sol.get("userAgent")
        if ua:
            headers["User-Agent"] = ua
        has_clearance = any(c.get("name") == "cf_clearance" for c in cookies)
        if not headers or not has_clearance:
            logger.info("cfsolver: %s returned no cf_clearance", host)
            return
        _clearance[host] = (time.monotonic() + _ttl(), headers)
        logger.info("cfsolver: cleared %s (%d cookies)", host, len(cookies))
    except Exception as exc:
        logger.info("cfsolver: solve failed for %s: %s: %s", host,
                    type(exc).__name__, exc)
    finally:
        _inflight.discard(host)
        _cooldown[host] = time.monotonic() + _SOLVE_COOLDOWN


def reset() -> None:
    """Test hook — drop all cached clearance and back-off state."""
    _clearance.clear()
    _cooldown.clear()
    _inflight.clear()
