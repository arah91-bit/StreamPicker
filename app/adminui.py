"""Shared chrome for the local admin dashboard.

The dashboard is one site with three tabs — Overview, Settings, Source health —
served at clean paths (/, /settings, /stats) on the container's own port, with
no secret in the URL. You reach it like any other self-hosted service's web UI:
point a browser at the mapped port. The addon's public endpoints
(manifest/stream/proxy) keep their secret gate; only the admin UI is local.

`is_local` is the guard that keeps that safe by default: the admin pages answer
only to clients on the loopback/LAN/Docker network, never to a request that
arrived through the public reverse proxy (which forwards a real public client
IP in X-Forwarded-For). Set DASHBOARD_LOCAL_ONLY=0 to lift it if you front the
dashboard with your own authentication.
"""

import html
import ipaddress

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


def nav(active: str, name: str) -> str:
    tabs = "".join(
        f'<a class="tab{" on" if tid == active else ""}" href="{href}">'
        f'{html.escape(label)}</a>'
        for tid, href, label in TABS)
    return (f'<header class="adminnav"><span class="brand">'
            f'<span class="dot"></span>{html.escape(name)}</span>'
            f'<nav class="tabs">{tabs}</nav></header>')


def _client_ip(request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def is_local(request) -> bool:
    """True when the request comes from loopback, a private LAN, or the Docker
    network — i.e. not from the public internet via the reverse proxy."""
    try:
        ip = ipaddress.ip_address(_client_ip(request))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local
