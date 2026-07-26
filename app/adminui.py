"""Shared security boundary and chrome for the admin dashboard.

The dashboard is one site served at clean paths on the container's own port,
with no secret in the URL; its tabs and chrome live in app/uitheme (see
uitheme.NAV), and this module owns only the guard in front of them. The
addon's public endpoints (manifest/stream/proxy) keep their path/capability
gates; the admin UI additionally requires HTTP Basic authentication and is
limited to local clients by default.

Forwarding headers are security-sensitive. They are considered only when the
immediate peer belongs to TRUSTED_PROXIES; an untrusted peer sending one is
rejected by the local guard instead of being allowed to choose its own IP.
"""

import base64
import binascii
import asyncio
import hashlib
import ipaddress
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException

from app import admin_auth
from app import uitheme

_CSRF_TOKEN = secrets.token_urlsafe(32)

# An opaque per-process nonce published on the public /health/live endpoint.
# It is deliberately NOT a secret — it exists so this server can recognise
# itself when the public-URL check calls its own advertised address, without
# ever sending the addon secret to whatever host was typed in.
INSTANCE_ID = secrets.token_urlsafe(8)
_FORWARDED_HEADERS = ("x-forwarded-for", "forwarded")
_HASH_SLOTS = threading.BoundedSemaphore(2)
_AUTH_CACHE: dict[tuple, float] = {}
_AUTH_CACHE_TTL = 300.0


def csrf_token() -> str:
    """Return the process-local token embedded in authenticated admin pages."""
    return _CSRF_TOKEN


# Page-specific CSS for the first-run enrollment card (everything else comes
# from uitheme.BASE_CSS).
_SETUP_CSS = """
.enroll{min-height:74vh;display:grid;place-items:center}
.enroll-card{width:min(460px,100%);padding:30px}
.enroll-card .mark{width:12px;height:24px;border-radius:4px;margin-bottom:20px}
.enroll-card h1{margin:0 0 8px}
.enroll-card .sub{margin:0 0 20px}
.enroll-card label{display:block;font-weight:600;font-size:13.5px;margin:14px 0 6px}
.enroll-card .hint{font-size:12.5px;color:var(--mut);margin-top:6px}
.enroll-card .btn{width:100%;margin-top:22px}
.enroll-card .err{min-height:22px;color:var(--bad);font-size:13px;margin-top:12px}
"""

_SETUP_JS = """<script>
const form=document.getElementById('setup'),err=document.getElementById('error');
form.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';
 const password=document.getElementById('password').value;
 if(password!==document.getElementById('confirm').value){err.textContent='Passwords do not match.';return}
 const button=document.getElementById('create');button.disabled=true;
 try{const response=await fetch('/api/admin/setup',{method:'POST',headers:{
  'Content-Type':'application/json','X-CSRF-Token':document.querySelector('.enroll-card').dataset.csrf},
  body:JSON.stringify({username:document.getElementById('username').value,password,
   confirmation:document.getElementById('confirm').value})});
  const body=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(body.detail||('HTTP '+response.status));
  form.reset();form.innerHTML='<div class="callout ok"><strong>Account created.</strong><br>'+
   'Continue to the dashboard and sign in once with your new username and password.</div>'+
   '<button type="button" class="btn" id="continue">Continue to dashboard</button>';
  document.getElementById('continue').onclick=()=>location.href='/';
 }catch(ex){err.textContent=ex.message}finally{if(button.isConnected)button.disabled=false}
});
</script>"""


def setup_page(name: str) -> str:
    """One-time local enrollment page; it never embeds an existing secret."""
    csrf = uitheme.esc(_CSRF_TOKEN)
    key = uitheme.icon("key")
    body = f"""
<div class="enroll"><section class="card hot enroll-card" data-csrf="{csrf}">
<div class="mark" aria-hidden="true"></div>
<p class="eyebrow">First run · local only</p>
<h1>Create your administrator account</h1>
<p class="sub">This is the first dashboard visit. Choose the username and
password you will use from now on.</p>
<form id="setup">
<label for="username">Username</label>
<input id="username" name="username" required maxlength="128"
autocomplete="username" spellcheck="false" autofocus>
<label for="password">Password</label>
<input id="password" name="password" type="password" required minlength="12"
maxlength="1024" autocomplete="new-password">
<div class="hint">At least 12 characters.</div>
<label for="confirm">Confirm password</label>
<input id="confirm" name="confirm" type="password" required minlength="12"
maxlength="1024" autocomplete="new-password">
<button id="create" class="btn" type="submit">{key}Create account</button>
<div class="err" id="error" role="alert"></div>
</form></section></div>"""
    return uitheme.page(
        title="create administrator", name=name,
        robots="noindex,nofollow", body=body,
        head=f"<style>{_SETUP_CSS}</style>", scripts=_SETUP_JS)


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
