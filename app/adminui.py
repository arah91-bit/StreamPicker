"""Shared security boundary and chrome for the admin dashboard.

The dashboard is one site with three tabs — Overview, Settings, Source health —
served at clean paths (/, /settings, /stats) on the container's own port, with
no secret in the URL. The addon's public endpoints (manifest/stream/proxy) keep
their path/capability gates; the admin UI additionally requires HTTP Basic
authentication and is limited to local clients by default.

Forwarding headers are security-sensitive. They are considered only when the
immediate peer belongs to TRUSTED_PROXIES; an untrusted peer sending one is
rejected by the local guard instead of being allowed to choose its own IP.
"""

import base64
import binascii
import asyncio
import hashlib
import html
import ipaddress
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException

from app import admin_auth

# (id, href, label) — the tab order across every admin page.
TABS = [
    ("overview", "/", "Overview"),
    ("settings", "/settings", "Settings"),
    ("stats", "/stats", "Source health"),
]

NAV_CSS = """
.adminnav{position:sticky;top:12px;z-index:30;display:flex;justify-content:space-between;
align-items:center;gap:12px;margin:0 0 26px;padding:9px 10px 9px 16px;
background:var(--card);border:1px solid var(--line);border-radius:14px;
box-shadow:0 2px 10px rgba(0,0,0,.05)}
.adminnav .brand{font-weight:700;font-size:14.5px;letter-spacing:-.01em;
display:flex;gap:9px;align-items:center;white-space:nowrap}
.adminnav .brand .dot{width:9px;height:9px;border-radius:50%;
background:var(--accent);flex-shrink:0}
.adminnav .tabs{display:flex;gap:3px;flex-wrap:wrap;justify-content:flex-end}
.adminnav .tab{font-size:13.5px;color:var(--mut);text-decoration:none;
padding:7px 13px;border-radius:9px;white-space:nowrap;transition:background .12s,color .12s}
.adminnav .tab:hover{color:var(--fg);background:var(--line)}
.adminnav .tab.on{color:#fff;background:var(--accent)}
.adminnav .tab.on:hover{background:var(--accent)}
@media (max-width:520px){.adminnav{flex-direction:column;align-items:stretch}
.adminnav .tabs{justify-content:center}}
"""


_CSRF_TOKEN = secrets.token_urlsafe(32)
_FORWARDED_HEADERS = ("x-forwarded-for", "forwarded")
_HASH_SLOTS = threading.BoundedSemaphore(2)
_AUTH_CACHE: dict[tuple, float] = {}
_AUTH_CACHE_TTL = 300.0


def csrf_token() -> str:
    """Return the process-local token embedded in authenticated admin pages."""
    return _CSRF_TOKEN


def nav(active: str, name: str) -> str:
    tabs = "".join(
        f'<a class="tab{" on" if tid == active else ""}" href="{href}">'
        f'{html.escape(label)}</a>'
        for tid, href, label in TABS)
    # Source-health renders unblock/clear/retry actions as links. Intercept
    # those links here so the shared chrome upgrades them to CSRF-protected
    # POSTs without putting secrets in query paths or changing public routes.
    csrf = html.escape(_CSRF_TOKEN, quote=True)
    return (f'<header class="adminnav" data-csrf="{csrf}"><span class="brand">'
            f'<span class="dot"></span>{html.escape(name)}</span>'
            f'<nav class="tabs">{tabs}</nav></header>'
            "<script>document.addEventListener('click',async e=>{"
            "const a=e.target.closest('a[href]');if(!a)return;"
            "const u=new URL(a.href,location.href);"
            "if(!['/api/unblock','/api/decode/clear','/api/nzb-indexer/clear']"
            ".includes(u.pathname))return;"
            "e.preventDefault();const c=document.querySelector('.adminnav').dataset.csrf;"
            "const r=await fetch(u,{method:'POST',headers:{'X-CSRF-Token':c}});"
            "if(r.ok)location.href='/stats';else alert('Action failed: HTTP '+r.status);"
            "});</script>")


def setup_page(name: str) -> str:
    """One-time local enrollment page; it never embeds an existing secret."""
    csrf = html.escape(_CSRF_TOKEN, quote=True)
    title = html.escape(name)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title} — create administrator</title>
<style>
:root{{--bg:#f5f7fb;--card:#fff;--fg:#172033;--mut:#657086;--line:#dfe4ed;
--accent:#5965e8;--bad:#b42318}}*{{box-sizing:border-box}}body{{margin:0;
font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
.wrap{{min-height:100vh;display:grid;place-items:center;padding:24px}}.card{{width:min(440px,100%);
background:var(--card);border:1px solid var(--line);border-radius:18px;padding:30px;
box-shadow:0 14px 40px rgba(24,35,58,.10)}}.mark{{width:42px;height:42px;
border-radius:12px;background:var(--accent);display:grid;place-items:center;color:#fff;
font-weight:800;margin-bottom:18px}}h1{{font-size:24px;line-height:1.2;margin:0 0 8px}}
.sub{{color:var(--mut);margin:0 0 24px}}label{{display:block;font-weight:650;
margin:14px 0 6px}}input{{width:100%;padding:11px 12px;border:1px solid var(--line);
border-radius:10px;font:inherit;background:#fff}}input:focus{{outline:2px solid #cbd0ff;
border-color:var(--accent)}}button{{width:100%;margin-top:22px;padding:12px;border:0;
border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:700;
cursor:pointer}}button:disabled{{opacity:.6;cursor:wait}}.hint{{font-size:12.5px;
color:var(--mut);margin-top:5px}}.err{{min-height:22px;color:var(--bad);margin-top:12px}}
.ok{{padding:14px;border-radius:10px;background:#ecfdf3;color:#067647}}
</style></head><body><main class="wrap"><section class="card" data-csrf="{csrf}">
<div class="mark">SP</div><h1>Create your administrator account</h1>
<p class="sub">This is the first dashboard visit. Choose the username and
password you will use from now on.</p>
<form id="setup"><label for="username">Username</label>
<input id="username" name="username" required maxlength="128"
autocomplete="username" spellcheck="false" autofocus>
<label for="password">Password</label>
<input id="password" name="password" type="password" required minlength="12"
maxlength="1024" autocomplete="new-password">
<div class="hint">At least 12 characters.</div>
<label for="confirm">Confirm password</label>
<input id="confirm" name="confirm" type="password" required minlength="12"
maxlength="1024" autocomplete="new-password">
<button id="create" type="submit">Create account</button>
<div class="err" id="error" role="alert"></div></form></section></main>
<script>
const form=document.getElementById('setup'),err=document.getElementById('error');
form.addEventListener('submit',async e=>{{e.preventDefault();err.textContent='';
 const password=document.getElementById('password').value;
 if(password!==document.getElementById('confirm').value){{err.textContent='Passwords do not match.';return}}
 const button=document.getElementById('create');button.disabled=true;
 try{{const response=await fetch('/api/admin/setup',{{method:'POST',headers:{{
  'Content-Type':'application/json','X-CSRF-Token':document.querySelector('.card').dataset.csrf}},
  body:JSON.stringify({{username:document.getElementById('username').value,password,
   confirmation:document.getElementById('confirm').value}})}});
  const body=await response.json().catch(()=>({{}}));
  if(!response.ok)throw new Error(body.detail||('HTTP '+response.status));
  form.reset();form.innerHTML='<div class="ok"><strong>Account created.</strong><br>'+
   'Continue to the dashboard and sign in once with your new username and password.</div>'+
   '<button type="button" id="continue">Continue to dashboard</button>';
  document.getElementById('continue').onclick=()=>location.href='/';
 }}catch(ex){{err.textContent=ex.message}}finally{{if(button.isConnected)button.disabled=false}}
}});
</script></body></html>"""


def _networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get("TRUSTED_PROXIES", "127.0.0.0/8,::1/128")
    out = []
    for item in re.split(r"[\s,]+", raw.strip()):
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # Configuration validation normally catches this. Fail closed here
            # as well so a bad live environment never broadens trust.
            return ()
    return tuple(out)


def _ip(value: str):
    value = (value or "").strip()
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1 and "." in value:
        value = value.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _trusted(ip) -> bool:
    return ip is not None and any(ip in network for network in _networks())


def client_ip(request) -> str:
    """Resolve a client without allowing a caller to forge forwarding data.

    Walk X-Forwarded-For from the trusted peer toward the browser. The first
    address outside the trusted proxy set is the client. A forwarding header
    from an untrusted or malformed peer resolves to the empty string, which the
    local-only guard rejects.
    """
    peer = _ip(request.client.host if request.client else "")
    xff = request.headers.get("x-forwarded-for", "").strip()
    has_forwarded = any(request.headers.get(h, "") for h in _FORWARDED_HEADERS)
    if not has_forwarded:
        return str(peer) if peer is not None else ""
    if not xff or not _trusted(peer):
        return ""
    parts = [p.strip() for p in xff.split(",")]
    if not parts or len(parts) > 32:
        return ""
    chain = [_ip(p) for p in parts]
    if any(p is None for p in chain):
        return ""
    for address in reversed(chain):
        if not _trusted(address):
            return str(address)
    return str(chain[0])


def is_local(request) -> bool:
    """True when the request comes from loopback, a private LAN, or the Docker
    network — i.e. not from the public internet via the reverse proxy."""
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def setup_required() -> bool:
    return admin_auth.setup_required()


def _decoded_basic(request) -> tuple[str, str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic" or not payload or len(payload) > 8192:
        return None
    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return username, password, header


def _bounded_verify(username: str, password: str) -> bool:
    # Two simultaneous scrypt derivations cap memory at roughly 128 MiB even if
    # a publicly exposed dashboard is being brute-forced.
    with _HASH_SLOTS:
        return admin_auth.verify_credentials(username, password)


async def authenticated(request) -> bool:
    decoded = _decoded_basic(request)
    if decoded is None:
        return False
    username, password, header = decoded

    # Explicit legacy passwords are cheap constant-time comparisons. Persisted
    # accounts use scrypt off the event loop, with a short success-only cache so
    # normal dashboard navigation does not repeatedly pay the KDF cost.
    if not admin_auth.initialized():
        return admin_auth.verify_credentials(username, password)

    now = time.monotonic()
    generation = admin_auth.generation()
    digest = hashlib.sha256(header.encode("utf-8")).digest()
    cache_key = (generation, digest)
    if _AUTH_CACHE.get(cache_key, 0) > now:
        return True
    ok = await asyncio.to_thread(_bounded_verify, username, password)
    if ok:
        if len(_AUTH_CACHE) >= 256:
            for key, expires in list(_AUTH_CACHE.items()):
                if expires <= now:
                    _AUTH_CACHE.pop(key, None)
            while len(_AUTH_CACHE) >= 256:
                _AUTH_CACHE.pop(next(iter(_AUTH_CACHE)))
        _AUTH_CACHE[cache_key] = now + _AUTH_CACHE_TTL
    return ok


async def create_account(username: str, password: str) -> str:
    return await asyncio.to_thread(
        lambda: _with_hash_slot(admin_auth.create_account, username, password))


async def migrate_legacy() -> bool:
    if not admin_auth.legacy_configured() or admin_auth.initialized():
        return False
    return await asyncio.to_thread(
        lambda: _with_hash_slot(admin_auth.migrate_legacy))


def _with_hash_slot(fn, *args):
    with _HASH_SLOTS:
        return fn(*args)


async def require_auth(request) -> None:
    if not await authenticated(request):
        raise HTTPException(
            status_code=401, detail="administrator authentication required",
            headers={"WWW-Authenticate": 'Basic realm="stream-picker admin", charset="UTF-8"'},
        )


def require_csrf(request) -> None:
    """Require an unreadable-by-other-origins token and reject cross-site fetches."""
    supplied = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(supplied, _CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site request denied")
    origin = request.headers.get("origin", "")
    if origin:
        parsed = urlsplit(origin)
        expected = request.headers.get("host", "").lower()
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != expected:
            raise HTTPException(status_code=403, detail="cross-origin request denied")
