"""Renders /connect — the service catalog.

Every upstream thing this instance talks to, organized as cards you open
inline: the four lane master switches, the unified Sources panel (debrid
keys, Prowlarr, scraper engines, custom addons), then one expandable card
per connection — status dot on the summary row, fields + live Test inside.

Each card carries id="conn-<id>" so Home's at-a-glance tiles and the Ctrl/⌘K
palette deep-link straight to it. All writes go through the shared
settings_ui machinery (data-key inputs → /api/settings/save), keeping the
page at exact parity with /data/config.json and .env edits.
"""

import os

from app import adminui, config, settings_ui, uitheme

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

_esc = uitheme.esc

_CSS = """
/* connect page — tokens & shared components live in uitheme.BASE_CSS */
.svcgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
gap:12px;margin-bottom:10px;align-items:start}
details.svccard{display:block;overflow:hidden}
details.svccard>summary{cursor:pointer;list-style:none;display:flex;
align-items:center;gap:11px;padding:14px 16px}
details.svccard>summary::-webkit-details-marker{display:none}
details.svccard>summary .chev{margin-left:auto;color:var(--mut);
transition:transform .15s;flex-shrink:0}
details.svccard[open]>summary .chev{transform:rotate(90deg)}
details.svccard>summary:hover .svcname{color:var(--accent)}
.svcname{font-weight:650;font-size:14px;transition:color .12s}
.svcrole{color:var(--mut);font-size:11.5px;margin-top:1px}
.svcbody{padding:2px 16px 14px;border-top:1px solid var(--line);
display:flex;flex-direction:column;gap:10px}
.svcbody .f{padding-top:10px}
.svctally{margin-left:auto;font:11px var(--mono);color:var(--mut);
white-space:nowrap}
.svctally.ok{color:var(--ok)}
"""


def _svc_card(conn: dict) -> str:
    """One catalog card: summary row (dot, name, role, state) + inline fields.

    Keeps the `.conn`/`.dot`/`.tres`/`[data-key]` structure the shared _JS
    wires up for Test and dirty-tracking. Configured cards start open."""
    configured = settings_ui._conn_configured(conn)
    state = "ok" if configured else "idle"
    label = "configured" if configured else "not set"
    open_attr = " open" if configured else ""
    chev = uitheme.icon("arrow-right", size=14, cls="chev")
    return (
        f"<details class='card svccard conn' id='conn-{_esc(conn['id'])}'"
        f"{open_attr}><summary>"
        f"{uitheme.status_dot(state)}"
        f"<span><span class='svcname'>{_esc(conn['name'])}</span>"
        f"<div class='svcrole'>{_esc(conn['role'])}</div></span>"
        f"<span class='svctally {state}'>{label}</span>"
        f"{chev}</summary>"
        f"<div class='svcbody'>{settings_ui._conn_fields(conn)}"
        f"<div class='cfoot'><button class='btn ghost test' "
        f"data-service='{_esc(conn['id'])}'>Test</button>"
        f"<span class='tres'></span></div></div></details>")


def _catalog() -> str:
    """Every connection, grouped by category, each group a section + grid."""
    known = {gid for gid, _t, _b in config.CONNECTION_GROUPS}
    out = []
    for gid, title, blurb in config.CONNECTION_GROUPS:
        conns = [c for c in config.CONNECTIONS if c.get("cat") == gid]
        if not conns:
            continue
        n_set = sum(settings_ui._conn_configured(c) for c in conns)
        tally = (f"{n_set}/{len(conns)} configured" if n_set
                 else f"{len(conns)} available")
        cards = "".join(_svc_card(c) for c in conns)
        out.append(uitheme.section(gid.upper(), title, blurb, tally=tally,
                                   tone="ok" if n_set else "")
                   + f"<div class='svcgrid'>{cards}</div>")
    leftovers = [c for c in config.CONNECTIONS if c.get("cat") not in known]
    if leftovers:
        cards = "".join(_svc_card(c) for c in leftovers)
        out.append(uitheme.section("OTHER", "Other", "")
                   + f"<div class='svcgrid'>{cards}</div>")
    return "".join(out)


def render() -> str:
    """/connect — lanes, sources, then the expandable service catalog."""
    restart = "1" if config.restart_pending() else "0"
    body = f"""
<div class="pagehead"><p class="eyebrow">PLUGGED IN</p><h1>Connect</h1>
<p class="sub">Everything this instance talks to. Open a card, paste its key,
and hit <b>Test</b> before you save — stored keys stay hidden, and leaving a
masked field blank keeps the one already saved. Applied on restart.</p></div>
{settings_ui._lane_masters()}
{settings_ui._savebar(restart, top=True)}
{settings_ui._scrapers()}
{_catalog()}
{settings_ui._savebar(restart)}"""
    return uitheme.shell(
        title="Connect", name=ADDON_NAME, active="connect",
        csrf=adminui.csrf_token(), body=body,
        head=f"<style>{settings_ui._CSS}{_CSS}</style>",
        scripts=f"<script>{settings_ui._JS}</script>",
        search=settings_ui.search_index())
