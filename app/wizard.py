"""First-run setup wizard — /setup, and what "/" shows until a source exists.

The settings page assumes an operator who can produce a configured Comet or
StremThru manifest URL; a non-technical user cannot. But both of those URLs
are just base64 configs around a debrid API key — so the wizard asks only
"which debrid do you have?" and mints the rest itself:

  * Comet: standard *padded* base64 of a minimal config (Comet's strict
    config check rejects urlsafe/unpadded); all omitted fields take Comet's
    own defaults, cachedOnly stays true.
  * StremThru Torz: urlsafe unpadded base64 of {indexers,stores,cached}.

Both minted URLs are live-tested (connections.test) before anything is
saved, so a typo'd key fails visibly instead of configuring a dead lane.
One debrid key therefore yields the two torrent lanes; TMDB and the public
URL are optional extras. Everything else — usenet, *arr, Jellyfin, custom
addons — stays in Settings, linked from the page.

The defaults point at the public Comet / StremThru deployments so a
docker-compose-only install works with zero self-hosted extras; self-hosters
can override the bases in the collapsed "advanced" section.
"""

import asyncio
import base64
import html
import json
import logging
import os

import httpx

from app import adminui, config

logger = logging.getLogger("stream-picker")

# The lane check searches a real title: a manifest fetch succeeds even with a
# wrong API key, but a cached-only search with a bad key comes back empty —
# so "streams found" is the only honest green light. Shawshank: ancient,
# universally cached on every debrid.
_CHECK_TITLE = "tt0111161"

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

COMET_PUBLIC = "https://comet.feels.legal"
STREMTHRU_PUBLIC = "https://stremthru.13377001.xyz"

# (id, label, stremthru store code, where to find the key)
DEBRIDS = [
    ("torbox", "TorBox", "tb", "https://torbox.app/settings"),
    ("realdebrid", "Real-Debrid", "rd", "https://real-debrid.com/apitoken"),
    ("alldebrid", "AllDebrid", "ad", "https://alldebrid.com/apikeys"),
    ("premiumize", "Premiumize", "pm", "https://www.premiumize.me/account"),
]
_ST_CODE = {sid: code for sid, _, code, _ in DEBRIDS}

# A source of streams, from any lane. While none is configured the dashboard
# home shows the wizard instead of an empty overview.
_SOURCE_KEYS = ("FAST_BASE_URL", "STREMTHRU_BASE_URL", "MEDIAFUSION_BASE_URL",
                "NZB_INDEXERS", "EXTRA_ADDONS", "JELLIO_URL")


def needed() -> bool:
    return not any((os.environ.get(k) or "").strip()
                   or (config.pending(k) or "").strip()
                   for k in _SOURCE_KEYS)


def comet_url(debrids: list[tuple[str, str]], base: str = COMET_PUBLIC) -> str:
    """(service id, api key) pairs -> configured Comet manifest base URL."""
    cfg = {"cachedOnly": True, "removeTrash": True,
           "debridServices": [{"service": s, "apiKey": k} for s, k in debrids]}
    b64 = base64.b64encode(json.dumps(cfg).encode()).decode()
    return f"{base.rstrip('/')}/{b64}"


def stremthru_url(debrids: list[tuple[str, str]],
                  base: str = STREMTHRU_PUBLIC) -> str:
    stores = [{"c": _ST_CODE[s], "t": k} for s, k in debrids if s in _ST_CODE]
    cfg = {"indexers": None, "stores": stores, "cached": True}
    b64 = (base64.urlsafe_b64encode(
        json.dumps(cfg, separators=(",", ":")).encode())
        .decode().rstrip("="))
    return f"{base.rstrip('/')}/stremio/torz/{b64}"


def _clean_base(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("server addresses must start with http:// or https://")
    return url


# Validate an API key against the debrid's own account endpoint. The lane
# search alone is not enough: Comet lists cached results without ever using
# the key (learned live — a fake TorBox key still returned 30+ streams), so
# a typo would configure a lane that only fails at play time.
async def _key_check(sid: str, key: str) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            if sid == "torbox":
                r = await c.get("https://api.torbox.app/v1/api/user/me",
                                headers={"Authorization": f"Bearer {key}"},
                                timeout=20)
                ok = r.status_code == 200 and r.json().get("success") is True
            elif sid == "realdebrid":
                r = await c.get("https://api.real-debrid.com/rest/1.0/user",
                                headers={"Authorization": f"Bearer {key}"},
                                timeout=20)
                ok = r.status_code == 200
            elif sid == "alldebrid":
                r = await c.get("https://api.alldebrid.com/v4/user",
                                params={"agent": "streampicker",
                                        "apikey": key}, timeout=20)
                ok = r.status_code == 200 and r.json().get("status") == "success"
            elif sid == "premiumize":
                r = await c.get("https://www.premiumize.me/api/account/info",
                                params={"apikey": key}, timeout=20)
                ok = r.status_code == 200 and r.json().get("status") == "success"
            else:
                return {"ok": False, "detail": "unknown service"}
        return ({"ok": True, "detail": "key accepted"} if ok else
                {"ok": False, "detail": "the service rejected this API key"})
    except Exception as e:
        return {"ok": False, "detail": f"could not reach the service "
                f"({type(e).__name__})"}


async def _lane_check(base: str) -> dict:
    """Prove a minted source URL actually produces streams for a title every
    debrid has cached. Details are user-facing; never include the URL (it
    embeds the key)."""
    try:
        async with httpx.AsyncClient(follow_redirects=True,
                                     headers={"User-Agent": "Stremio"}) as c:
            r = await c.get(f"{base}/stream/movie/{_CHECK_TITLE}.json",
                            timeout=45)
        if r.status_code != 200:
            return {"ok": False, "detail": f"HTTP {r.status_code}"}
        streams = r.json().get("streams") or []
        real = [s for s in streams
                if (s.get("url") or s.get("infoHash"))
                and "OBSOLETE" not in (s.get("description") or "")
                and not (s.get("name") or "").startswith("[❌]")]
        if not real:
            return {"ok": False, "detail": "reachable, but no streams came "
                    "back — double-check the API key"}
        return {"ok": True, "detail": f"{len(real)} streams found"}
    except Exception as e:
        return {"ok": False, "detail": type(e).__name__}


async def apply(body: dict) -> dict:
    """Mint, live-test, and save. Saves only what passed its test; succeeds
    when at least one stream source lane came up. Never echoes keys back."""
    from app import connections
    debrids = []
    for d in body.get("debrids") or []:
        sid = str((d or {}).get("service", ""))
        key = str((d or {}).get("key", "")).strip()
        if sid not in _ST_CODE:
            raise ValueError(f"unknown debrid service: {sid[:24]}")
        if not key:
            raise ValueError(f"missing API key for {sid}")
        debrids.append((sid, key))
    if not debrids:
        raise ValueError("pick at least one debrid service and paste its key")

    # Keys first: every pasted key must be real before any lane is trusted.
    key_results = dict(zip(
        (sid for sid, _ in debrids),
        await asyncio.gather(*(_key_check(sid, key) for sid, key in debrids))))
    if not all(r["ok"] for r in key_results.values()):
        return {"ok": False, "results": key_results, "saved": []}

    fast = comet_url(debrids, _clean_base(body.get("comet_base")
                                          or COMET_PUBLIC))
    torz = stremthru_url(debrids, _clean_base(body.get("stremthru_base")
                                              or STREMTHRU_PUBLIC))
    tmdb = str(body.get("tmdb") or "").strip()
    public = str(body.get("public_url") or "").strip()

    checks = {"comet": _lane_check(fast), "stremthru": _lane_check(torz)}
    if tmdb:
        checks["tmdb"] = connections.test("tmdb", {"TMDB_API_KEY": tmdb})
    settled = await asyncio.gather(*checks.values(), return_exceptions=True)
    results = dict(key_results)
    for name, r in zip(checks, settled):
        if isinstance(r, BaseException):
            r = {"ok": False, "detail": type(r).__name__}
        results[name] = {"ok": bool(r.get("ok")),
                         "detail": str(r.get("detail", ""))[:200]}

    values = {}
    if results["comet"]["ok"]:
        values["FAST_BASE_URL"] = fast
    if results["stremthru"]["ok"]:
        values["STREMTHRU_BASE_URL"] = torz
    if tmdb and results.get("tmdb", {}).get("ok"):
        values["TMDB_API_KEY"] = tmdb
    if not (values.get("FAST_BASE_URL") or values.get("STREMTHRU_BASE_URL")):
        return {"ok": False, "results": results, "saved": []}
    if public:
        values["ADDON_PUBLIC_URL"] = public
    config.save(values)
    logger.info(f"wizard: configured {sorted(values)} "
                f"({len(debrids)} debrid service(s))")
    return {"ok": True, "results": results, "saved": sorted(values),
            "restart_needed": True}


_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--mut:#6b6b66;
--line:#e6e6e2;--bad:#c0392b;--good:#2e7d5b;--accent:#3b6ea5;
--accent-soft:#eef3f9;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--fg:#e9e9e6;
--mut:#9a9a94;--line:#2c2f34;--bad:#ff6b5e;--good:#5cc99a;
--accent:#6ea3d8;--accent-soft:#232c37;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
padding:24px 16px 130px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:24px;margin:6px 0 6px}
.sub{color:var(--mut);margin:0 0 26px;font-size:14px;max-width:600px}
h2{font-size:16px;margin:28px 0 4px}
.blurb{color:var(--mut);font-size:13px;margin:0 0 12px;max-width:620px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:15px 16px;transition:border-color .15s}
.card.on{border-color:var(--accent)}
.chead{display:flex;justify-content:space-between;align-items:center;gap:10px}
.cname{font-weight:650;font-size:15px}
.keylink{font-size:12px;color:var(--accent);text-decoration:none}
.keylink:hover{text-decoration:underline}
.kfield{margin-top:12px}
.kfield[hidden]{display:none}
.kfield label{display:block;font-size:11.5px;color:var(--mut);margin:0 0 4px}
input[type=password],input[type=text],input[type=url]{width:100%;
background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:9px 11px;font:13px var(--mono)}
input::placeholder{color:var(--mut);opacity:.8}
.swi{appearance:none;-webkit-appearance:none;width:42px;height:24px;margin:0;
border-radius:99px;background:var(--line);position:relative;cursor:pointer;
transition:background .15s;flex-shrink:0}
.swi::before{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;
border-radius:50%;background:var(--card);box-shadow:0 1px 2px rgba(0,0,0,.25);
transition:transform .15s}
.swi:checked{background:var(--accent)}
.swi:checked::before{transform:translateX(18px)}
details.adv{margin-top:26px;color:var(--mut)}
details.adv summary{cursor:pointer;font-size:13px}
details.adv .card{margin-top:10px}
.later{margin-top:26px;font-size:13px;color:var(--mut)}
.later a{color:var(--accent)}
.gobar{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;
display:flex;flex-direction:column;gap:8px;background:var(--card);
border:1px solid var(--line);border-radius:14px;padding:12px 18px;
box-shadow:0 6px 24px rgba(0,0,0,.14);z-index:10;min-width:320px;max-width:92vw}
.gorow{display:flex;align-items:center;gap:14px;justify-content:space-between}
.btn{font:600 14px inherit;color:#fff;background:var(--accent);border:0;
border-radius:9px;padding:10px 22px;cursor:pointer;white-space:nowrap}
.btn:disabled{opacity:.5;cursor:default}
.gomsg{font-size:13px;color:var(--mut)}
.gomsg b{color:var(--fg)}
.checks{display:none;flex-direction:column;gap:3px;font:12.5px var(--mono)}
.checks.show{display:flex}
.checks .ok{color:var(--good)}
.checks .bad{color:var(--bad)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
""" + adminui.NAV_CSS

_JS = """
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
$$('.card[data-service] .swi').forEach(sw=>sw.addEventListener('change',()=>{
 const card=sw.closest('.card');card.classList.toggle('on',sw.checked);
 card.querySelector('.kfield').hidden=!sw.checked;
 if(sw.checked)card.querySelector('input[type=password]').focus();
}));
const csrf=()=>document.querySelector('.adminnav').dataset.csrf;
async function post(url,body){
 const r=await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},
  body:JSON.stringify(body||{})});
 const data=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));
 return data;
}
function collect(){
 const debrids=$$('.card[data-service]').filter(c=>c.querySelector('.swi').checked)
  .map(c=>({service:c.dataset.service,
   key:c.querySelector('input[type=password]').value.trim()}));
 return {debrids,tmdb:$('#tmdbkey').value.trim(),
  public_url:$('#publicurl').value.trim(),
  comet_base:$('#cometbase').value.trim(),
  stremthru_base:$('#stbase').value.trim()};
}
const NICE={comet:'Torrent lane (Comet)',stremthru:'Torrent lane (StremThru)',
 tmdb:'TMDB metadata',torbox:'TorBox key',realdebrid:'Real-Debrid key',
 alldebrid:'AllDebrid key',premiumize:'Premiumize key'};
function showChecks(results){
 const box=$('#checks');box.classList.add('show');
 box.innerHTML=Object.entries(results).map(([k,v])=>
  `<span class="${v.ok?'ok':'bad'}">${v.ok?'✓':'✗'} ${NICE[k]||k}`+
  `${v.ok?'':' — '+(v.detail||'failed').replace(/</g,'&lt;')}</span>`).join('');
}
let phase='setup';
$('#gobtn').addEventListener('click',async()=>{
 const btn=$('#gobtn'),msg=$('#gomsg');
 if(phase==='restart'){
  btn.disabled=true;msg.innerHTML='Restarting… this takes a few seconds.';
  try{await post('/api/settings/restart')}catch(e){}
  for(let i=0;i<60;i++){
   await new Promise(r=>setTimeout(r,2000));
   try{const h=await fetch('/health/ready',{cache:'no-store'});
    if(h.ok){location.href='/';return}}catch(e){}
  }
  msg.textContent='Still restarting — reload this page in a moment.';return;
 }
 const body=collect();
 if(!body.debrids.length||body.debrids.some(d=>!d.key)){
  msg.innerHTML='<span style="color:var(--bad)">Switch on at least one '+
   'service and paste its API key.</span>';return;
 }
 btn.disabled=true;msg.textContent='Testing your services…';
 try{
  const res=await post('/api/setup/apply',body);
  showChecks(res.results);
  if(res.ok){phase='restart';btn.textContent='Finish →';btn.disabled=false;
   msg.innerHTML='<b>Ready.</b> One restart and your addon links appear.';}
  else{btn.disabled=false;
   msg.innerHTML='<span style="color:var(--bad)">No lane came up — '+
    'check the key and try again.</span>';}
 }catch(e){btn.disabled=false;
  msg.innerHTML='<span style="color:var(--bad)">'+
   String(e.message).replace(/</g,'&lt;')+'</span>';}
});
"""


def _debrid_card(sid: str, label: str, key_url: str) -> str:
    return f"""<div class="card" data-service="{sid}">
 <div class="chead"><span class="cname">{html.escape(label)}</span>
  <input type="checkbox" class="swi" aria-label="I have {html.escape(label)}"></div>
 <div class="kfield" hidden><label>API key
  <a class="keylink" href="{html.escape(key_url)}" target="_blank"
     rel="noopener noreferrer">where is my key?</a></label>
  <input type="password" autocomplete="off" spellcheck="false"
         placeholder="paste your {html.escape(label)} API key"></div>
</div>"""


def render() -> str:
    cards = "".join(_debrid_card(sid, label, key_url)
                    for sid, label, _, key_url in DEBRIDS)
    name = html.escape(ADDON_NAME)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{name} — setup</title><style>{_CSS}</style></head><body>
<div class="wrap">
{adminui.nav("overview", ADDON_NAME)}
<h1>Set up your streams</h1>
<p class="sub">Switch on what you have, paste its key, and everything else is
configured for you. It's all changeable later in Settings.</p>

<h2>Your debrid service</h2>
<p class="blurb">Where your streams come from. One is enough.</p>
<div class="cards">{cards}</div>

<h2>Nice to have</h2>
<div class="cards">
 <div class="card"><div class="chead"><span class="cname">TMDB</span>
  <a class="keylink" href="https://www.themoviedb.org/settings/api"
     target="_blank" rel="noopener noreferrer">get a free key</a></div>
  <div class="kfield"><label>API key — better titles, languages and
   next-episode handling</label>
  <input type="password" id="tmdbkey" autocomplete="off" spellcheck="false"
         placeholder="optional"></div></div>
 <div class="card"><div class="chead"><span class="cname">Watch away from
   home</span></div>
  <div class="kfield"><label>The public address this server is reachable on
   (leave empty for home-network use)</label>
  <input type="url" id="publicurl" autocomplete="off" spellcheck="false"
         placeholder="https://streams.example.com — optional"></div></div>
</div>

<details class="adv"><summary>Advanced — use different Comet / StremThru
servers</summary>
 <div class="card">
  <div class="kfield"><label>Comet server</label>
   <input type="url" id="cometbase" value="{COMET_PUBLIC}"></div>
  <div class="kfield"><label>StremThru server</label>
   <input type="url" id="stbase" value="{STREMTHRU_PUBLIC}"></div>
 </div></details>

<p class="later">Have usenet indexers, Radarr/Sonarr, a Jellyfin library or
extra addons? Add them any time in <a href="/settings">Settings</a>.</p>
</div>

<div class="gobar">
 <div id="checks" class="checks"></div>
 <div class="gorow"><span id="gomsg" class="gomsg">Takes about a minute.</span>
 <button id="gobtn" class="btn">Set up my streams</button></div>
</div>
<script>{_JS}</script></body></html>"""
