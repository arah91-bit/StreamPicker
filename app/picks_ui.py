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

import html
import json
import os

from app import adminui

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

_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--mut:#6b6b66;
--line:#e6e6e2;--bad:#c0392b;--warn:#9a6700;--good:#2e7d5b;--accent:#3b6ea5;
--soft:#eef3f9;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--fg:#e9e9e6;
--mut:#9a9a94;--line:#2c2f34;--bad:#ff6b5e;--warn:#e0b74a;--good:#5cc99a;
--accent:#6ea3d8;--soft:#232c37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,sans-serif;padding:24px 16px 100px}
.wrap{max-width:1000px;margin:auto}
h1{font-size:23px;margin:0 0 5px}h2{font-size:16px;margin:0 0 4px}
.sub,.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin:14px 0}
.who{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:2px}
.who .nm{font-weight:650;font-size:16px}
.pill{font-size:11.5px;padding:1px 7px;border-radius:99px;background:var(--soft);
color:var(--accent);border:1px solid var(--line)}
.meta{color:var(--mut);font-size:12.5px;margin-bottom:12px}
.row{display:flex;align-items:center;gap:12px;padding:9px 11px;border:1px solid var(--line);
border-radius:9px;margin-bottom:6px}
.row .pos{font:12px var(--mono);color:var(--mut);min-width:20px}
.row .info{flex:1}.row .t{font-size:14px}.row .d{color:var(--mut);font-size:12.5px}
.row.fixed{opacity:.7}
button.sw{font-size:12.5px;padding:5px 13px;border-radius:7px;cursor:pointer;
border:1px solid var(--line);white-space:nowrap}
button.sw.on{background:var(--good);border-color:var(--good);color:#fff}
button.sw.off{background:transparent;color:var(--mut)}
button.sw:disabled{cursor:default;opacity:.65}
a.url{display:block;font:12px var(--mono);color:var(--accent);word-break:break-all;
background:var(--soft);padding:7px 9px;border-radius:7px;margin:2px 0 8px;
text-decoration:none}a.url:hover{text-decoration:underline}
.lbl{font-size:12.5px;margin:8px 0 2px}
.lanelbl{font-size:11.5px;color:var(--mut);margin:6px 0 1px}
.acts{display:flex;gap:7px;flex-wrap:wrap}
.acts button{font-size:12.5px;padding:5px 11px;border-radius:7px;cursor:pointer;
border:1px solid var(--line);background:transparent;color:var(--fg)}
.acts button.danger{color:var(--bad);border-color:var(--bad)}
.acts button:disabled{opacity:.6;cursor:default}
.pill.err{background:transparent;color:var(--bad);border-color:var(--bad)}
.hidden{display:none}
.field{margin:8px 0}
.field label{display:block;font-size:12.5px;color:var(--mut);margin-bottom:3px}
.field input{width:100%;max-width:320px;padding:7px 9px;border-radius:7px;
border:1px solid var(--line);background:var(--bg);color:var(--fg);font-size:14px}
.chk{display:flex;align-items:center;gap:7px;font-size:14px;margin:10px 0}
.code{font:22px/1.3 var(--mono);letter-spacing:.22em;text-align:center;
background:var(--soft);border:1px dashed var(--line);border-radius:8px;
padding:12px;margin:10px 0;max-width:320px}
"""




def render(setup_secret: str) -> str:
    """The catalog builder page.

    `setup_secret` is embedded because every mutation proxies to app/recs'
    existing `/setup/<secret>/api/...` endpoints. That is the same secret the
    public onboarding page already uses, and this page is only ever served
    behind the LAN-only admin guard.
    """
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalog builder — {html.escape(ADDON_NAME)}</title>
<style>{adminui.NAV_CSS}{_CSS}</style></head><body>
{adminui.nav("picks", ADDON_NAME)}
<div class="wrap">
<h1>Catalog builder</h1>
<div class="sub">Which home rows each viewer gets, and in what order. Continue
watching and watch history are rebuilt every time the home screen opens;
everything else is built nightly. Every viewer is listed the same way — there
is no owner account with special treatment.</div>
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
</div>
</div>
<script>
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
</script></body></html>"""
