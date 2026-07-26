"""Catalog builder — the admin page for each viewer's Daily Picks home rows.

Lives in stream-picker's admin chrome rather than app/recs' own standalone
configure page, so there is one place to administer the whole service. The
Trakt device-code onboarding flow stays on the public /setup/<secret> page:
that one has to be reachable by people who are not on this LAN, and this
dashboard is LAN-only.

The page is a thin client over app/recs' existing admin API, so nothing here
duplicates row logic — it only decides what is switchable.
"""

from __future__ import annotations

import json
import os

from app import uitheme

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

# Rows a viewer can be opted into, in the order the home screen renders them.
# `flag` is the field on the recs user record; `row` is the catalog id the
# toggle endpoint expects. The nightly slate is not listed: it is not a single
# switchable row but ~20 rows chosen per viewer per night.
ROW_SPECS = (
    {
        "row": "nr-continue-watching",
        "flag": "continue_watching_row",
        "title": "Continue watching",
        "desc": "Resume a part-watched movie or episode, or start the next "
                "episode of a show they finished. Rebuilt on every visit, so "
                "it is never stale.",
    },
    {
        "row": "nr-watch-history",
        "flag": "watch_history_row",
        "title": "Watch history",
        "desc": "Everything recently watched, newest first, one card per "
                "title.",
    },
)

# Whole row families that used to be separate add-ons or a hardcoded env list.
# They are not pinned above the slate — they are collection-oriented rows the
# viewer opts into — so they render after it.
FAMILY_SPECS = (
    {
        "row": "streaming-catalogs",
        "flag": "streaming_catalogs_row",
        "title": "Streaming catalogs",
        "desc": "Netflix, Prime, Disney+, Paramount+, Apple TV+ and Max, "
                "sorted by this viewer's taste. Import the collection file "
                "below after ticking this on.",
    },
    {
        "row": "asian-dramas",
        "flag": "asian_dramas_row",
        "title": "Asian dramas",
        "desc": "Korean, Chinese, Thai, Taiwanese and Japanese dramas by "
                "country and genre, plus rows for the actors the people who "
                "tick this watch most.",
    },
)

# Page-specific rules only: the palette, page frame, cards and typography all
# come from uitheme.BASE_CSS via shell(). Nothing here may hardcode a colour —
# a value that is not a token stops following the theme the moment someone
# picks one.
_CSS = """
.who{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:2px}
.who .nm{font-weight:650;font-size:16px}
.pill{font-size:11.5px;padding:1px 7px;border-radius:99px;background:var(--inset);
color:var(--accent);border:1px solid var(--line)}
.meta{color:var(--mut);font-size:12.5px;margin-bottom:12px}
.row{display:flex;align-items:center;gap:12px;padding:9px 11px;border:1px solid var(--line);
border-radius:9px;margin-bottom:6px}
.row .pos{font:12px var(--mono);color:var(--mut);min-width:20px}
.row .info{flex:1}.row .t{font-size:14px}.row .d{color:var(--mut);font-size:12.5px}
.row.fixed{opacity:.7}
button.sw{font:600 12.5px var(--sans);padding:5px 13px;border-radius:7px;
cursor:pointer;border:1px solid var(--line2);white-space:nowrap}
button.sw.on{background:var(--accent);border-color:transparent;color:var(--accent-ink)}
button.sw.off{background:transparent;color:var(--mut)}
button.sw:disabled{cursor:default;opacity:.65}
a.url{display:block;font:12px var(--mono);color:var(--accent);word-break:break-all;
background:var(--inset);padding:7px 9px;border-radius:7px;margin:2px 0 8px;
text-decoration:none}a.url:hover{text-decoration:underline}
.lbl{font-size:12.5px;margin:8px 0 2px}
.lanelbl{font-size:11.5px;color:var(--mut);margin:6px 0 1px}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
/* The viewer cards are built in JS, so the action buttons are matched to
   uitheme's ghost button by selector rather than by class. */
.acts button{font:600 12.5px var(--sans);padding:5px 12px;border-radius:7px;
cursor:pointer;border:1px solid var(--line2);background:transparent;
color:var(--accent);transition:border-color .12s,background .12s}
.acts button:hover{border-color:var(--accent);background:var(--accent-soft)}
.acts button.danger{color:var(--bad)}
.acts button.danger:hover{border-color:var(--bad);background:var(--bad-soft)}
.acts button:disabled{opacity:.5;cursor:default;background:transparent}
.pill.err{background:transparent;color:var(--bad);border-color:var(--bad)}
.hidden{display:none}
.field{margin:8px 0}
.field label{display:block;font-size:12.5px;color:var(--mut);margin-bottom:3px}
.field input{width:100%;max-width:320px}
.chk{display:flex;align-items:center;gap:7px;font-size:14px;margin:10px 0}
.code{font:22px/1.3 var(--mono);letter-spacing:.22em;text-align:center;
background:var(--inset);border:1px dashed var(--line2);border-radius:8px;
padding:12px;margin:10px 0;max-width:320px}
"""




_HEAD = uitheme.pagehead(
    "Catalog builder", "Daily Picks",
    "Which home rows each viewer gets, and in what order. Continue watching "
    "and watch history are rebuilt every time the home screen opens; "
    "everything else is built nightly. Every viewer is listed the same way — "
    "there is no owner account with special treatment.")


def render_unconfigured() -> str:
    """Shown when SETUP_SECRET is unset. Daily Picks proxies every mutation
    through app/recs' onboarding secret, so without one there is nothing this
    page could do — but it is in the nav, so it has to say why."""
    return uitheme.shell(
        title="Catalog builder", name=ADDON_NAME, active="picks",
        head=f"<style>{_CSS}</style>", robots="noindex,nofollow",
        body=_HEAD + uitheme.empty(
            "Daily Picks is not configured on this install: set SETUP_SECRET "
            "to the onboarding secret its catalogs are served under, then "
            "restart. Streaming is unaffected either way."))


def render(setup_secret: str) -> str:
    """The catalog builder page.

    `setup_secret` is embedded because every mutation proxies to app/recs'
    existing `/setup/<secret>/api/...` endpoints. That is the same secret the
    public onboarding page already uses, and this page is only ever served
    behind the LAN-only admin guard.
    """
    body = f"""{_HEAD}
<div id="out"><p class="mut">Loading…</p></div>

<div class="card" id="add">
  <h2>Add a viewer</h2>
  <div id="add-step1">
    <div class="field"><label for="nm">Name</label>
      <input id="nm" type="text" placeholder="e.g. Arah"></div>
    <label class="chk"><input id="kid" type="checkbox"> Kids profile</label>
    <div class="field hidden" id="agewrap">
      <label for="age">Age (2–17, grows automatically)</label>
      <input id="age" type="number" min="2" max="17" value="8"></div>
    <div class="acts"><button id="connect">Connect Trakt</button></div>
  </div>
  <div id="add-step2" class="hidden">
    <p class="mut">Have them open <a id="vlink" target="_blank" rel="noopener"></a>
      and enter this code:</p>
    <div class="code" id="ucode"></div>
    <div class="mut" id="pstat">Waiting for approval on Trakt…</div>
    <div class="acts"><button id="cancel">Cancel</button></div>
  </div>
</div>"""

    scripts = f"""<script>
const SETUP = {json.dumps(setup_secret)};
const SPECS = {json.dumps(list(ROW_SPECS))};
const FAMILIES = {json.dumps(list(FAMILY_SPECS))};
const api = p => "/setup/" + SETUP + "/api" + p;
const $ = id => document.getElementById(id);

function ago(ts) {{
  if (!ts) return "never";
  const h = (Date.now() / 1000 - ts) / 3600;
  if (h < 1) return Math.round(h * 60) + "m ago";
  if (h < 48) return Math.round(h) + "h ago";
  return Math.round(h / 24) + "d ago";
}}

function rowEl(u, spec, pos) {{
  const on = !!u[spec.flag];
  const el = document.createElement("div");
  el.className = "row";
  el.innerHTML = `<span class="pos">${{pos}}</span>
    <span class="info"><div class="t"></div><div class="d"></div></span>
    <button class="sw ${{on ? "on" : "off"}}">${{on ? "On" : "Off"}}</button>`;
  el.querySelector(".t").textContent = spec.title;
  el.querySelector(".d").textContent = spec.desc;
  el.querySelector("button").onclick = async ev => {{
    ev.target.disabled = true;
    await fetch(api("/watching/" + u.token), {{
      method: "POST", headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{row: spec.row, enabled: !on}}),
    }});
    load();
  }};
  return el;
}}

async function load() {{
  const out = $("out");
  let users;
  try {{
    users = (await (await fetch(api("/users"))).json()).users;
  }} catch (e) {{
    out.innerHTML = '<p class="mut">Could not reach the Daily Picks API.</p>';
    return;
  }}
  if (!users.length) {{
    out.innerHTML = '<p class="mut">No viewers yet — add one below.</p>';
    return;
  }}
  out.textContent = "";
  for (const u of users) {{
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="who"><span class="nm"></span>
        <span class="pill trakt"></span>
        ${{u.is_kid ? '<span class="pill">Kid · age ' + u.kid_age + '</span>' : ""}}
        ${{u.last_error ? '<span class="pill err">last build failed</span>' : ""}}
      </div>
      <div class="meta"></div>
      <div class="rows"></div>
      <div class="lbl">Add-ons to install (each one is tracked to this viewer)</div>
      <div class="lanes"></div>
      <div class="acts">
        <button class="copy">Copy main add-on URL</button>
        ${{u.streaming_collection_url
            ? '<button class="dl">Download ' + u.collection_filename + '</button>' : ""}}
        <button class="rf">Refresh now</button>
        <button class="rm danger">Remove</button>
      </div>`;
    card.querySelector(".nm").textContent = u.name;
    card.querySelector(".pill.trakt").textContent = u.trakt_username || "trakt";
    const err = card.querySelector(".pill.err");
    if (err) err.title = u.last_error;
    card.querySelector(".meta").textContent =
      u.catalogs + " nightly rows · refreshed " + ago(u.last_generated_at) +
      " · opened " + ago(u.last_served_at);
    const lanes = card.querySelector(".lanes");
    for (const lane of (u.lane_urls || [{{label: "Daily Picks", url: u.manifest_url}}])) {{
      const wrap = document.createElement("div");
      wrap.innerHTML = `<div class="lanelbl"></div>
        <a class="url" target="_blank" rel="noopener"></a>`;
      wrap.querySelector(".lanelbl").textContent = lane.label;
      const a = wrap.querySelector("a.url");
      a.textContent = lane.url;
      a.href = lane.url;
      a.onclick = ev => {{
        if (ev.metaKey || ev.ctrlKey) return;
        ev.preventDefault();
        navigator.clipboard.writeText(lane.url);
        const was = a.textContent;
        a.textContent = "Copied — paste under Nuvio Add-ons";
        setTimeout(() => a.textContent = was, 1600);
      }};
      lanes.appendChild(wrap);
    }}

    const rows = card.querySelector(".rows");
    SPECS.forEach((spec, i) => rows.appendChild(rowEl(u, spec, i + 1)));

    const slate = document.createElement("div");
    slate.className = "row fixed";
    slate.innerHTML = `<span class="pos">${{SPECS.length + 1}}+</span>
      <span class="info"><div class="t">Daily picks slate</div>
      <div class="d"></div></span>
      <button class="sw off" disabled>Always on</button>`;
    slate.querySelector(".d").textContent =
      u.catalogs + " rows chosen for this viewer each night — top picks, " +
      "watchlist, because-you-watched, genres, people, decades.";
    rows.appendChild(slate);

    FAMILIES.forEach(spec => rows.appendChild(rowEl(u, spec, "·")));

    card.querySelector(".copy").onclick = ev => {{
      navigator.clipboard.writeText(u.manifest_url);
      ev.target.textContent = "Copied";
      setTimeout(() => ev.target.textContent = "Copy add-on URL", 1800);
    }};
    const dl = card.querySelector(".dl");
    if (dl) dl.onclick = () => {{
      const a = document.createElement("a");
      a.href = u.streaming_collection_url;
      a.download = u.collection_filename;
      document.body.appendChild(a); a.click(); a.remove();
    }};
    card.querySelector(".rf").onclick = async ev => {{
      ev.target.disabled = true; ev.target.textContent = "Refreshing…";
      await fetch(api("/refresh/" + u.token), {{method: "POST"}});
      setTimeout(load, 4000);
    }};
    card.querySelector(".rm").onclick = async () => {{
      if (!confirm("Remove " + u.name + "? Their add-on URL stops working "
                   + "and their catalogs are deleted.")) return;
      await fetch(api("/delete/" + u.token), {{method: "POST"}});
      load();
    }};
    out.appendChild(card);
  }}
}}

// ── add a viewer (Trakt device code) ────────────────────────────────────
let deviceCode = null, polling = false;

$("kid").onchange = () => $("agewrap").classList.toggle("hidden", !$("kid").checked);

$("connect").onclick = async () => {{
  $("connect").disabled = true;
  const d = await (await fetch(api("/device-code"), {{method: "POST"}})).json();
  deviceCode = d.device_code;
  $("ucode").textContent = d.user_code;
  const l = $("vlink");
  l.href = d.verification_url; l.textContent = d.verification_url;
  $("add-step1").classList.add("hidden");
  $("add-step2").classList.remove("hidden");
  polling = true;
  poll(Math.max((d.interval || 5) * 1000, 3000));
}};

$("cancel").onclick = () => {{ polling = false; resetAdd(); }};

function resetAdd() {{
  deviceCode = null;
  $("add-step2").classList.add("hidden");
  $("add-step1").classList.remove("hidden");
  $("connect").disabled = false;
  $("nm").value = "";
  $("pstat").textContent = "Waiting for approval on Trakt…";
}}

async function poll(interval) {{
  if (!polling) return;
  const body = {{
    device_code: deviceCode,
    name: $("nm").value,
    is_kid: $("kid").checked,
    kid_age: parseInt($("age").value, 10) || 8,
  }};
  const d = await (await fetch(api("/poll"), {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify(body),
  }})).json();
  if (d.status === "ok") {{
    polling = false;
    $("pstat").textContent = "Connected — building their first catalogs…";
    resetAdd();
    load();
  }} else {{
    setTimeout(() => poll(interval), interval);
  }}
}}

load();
</script>"""

    return uitheme.shell(title="Catalog builder", name=ADDON_NAME,
                         active="picks", body=body, scripts=scripts,
                         head=f"<style>{_CSS}</style>",
                         robots="noindex,nofollow")
