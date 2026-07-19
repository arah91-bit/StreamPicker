"""First-run setup wizard — /setup, and what "/" shows until a source exists.

The settings page assumes an operator who can produce configured manifest URLs
and knows which of a dozen services matter; a non-technical user cannot. So the
wizard is a plain "do you have this?" checklist: every kind of source, mount,
automation and metadata provider is a card you switch on only if you have it,
paste its details, and the wizard live-tests each one and saves only what
passed. Nobody has to set up something they don't own.

Two shapes get special handling:

  * Debrid services. A configured Comet or StremThru URL is just base64 around
    a debrid key, so the wizard asks only "which debrid?" and mints both:
      - Comet: standard *padded* base64 of a minimal config (Comet's strict
        config check rejects urlsafe/unpadded); omitted fields take Comet's
        defaults, cachedOnly stays true.
      - StremThru Torz: urlsafe unpadded base64 of {indexers,stores,cached}.
    Both minted URLs are lane-tested (real streams for a universally-cached
    title) before being saved, so a typo'd key fails visibly.

  * Every other card maps 1:1 onto connections.test() — the same live checks
    behind the Settings "Test" buttons — and its fields onto config keys, so
    the wizard is a thin front-end over machinery that already exists. The lone
    exception is the custom-addon card, whose manifest URL is tested as a bare
    addon and stored into the EXTRA_ADDONS JSON list.

Setup succeeds when at least one *stream source* came up (a debrid lane, usenet
indexers, a Jellyfin library, MediaFusion, or another addon); metadata and
automation help but don't play anything on their own. The public-URL card is
save-only and deliberately setup-agnostic — reverse proxy, tunnel, or LAN — as
there is no honest way to verify an external address from inside the server.

The debrid/Comet/StremThru defaults point at the public deployments so a
docker-compose-only install works with zero self-hosted extras; self-hosters
override the bases in the collapsed "advanced" section.
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
# home shows the wizard instead of an empty overview, and apply() will not
# report success — a config with only metadata/automation plays nothing.
_SOURCE_KEYS = ("FAST_BASE_URL", "STREMTHRU_BASE_URL", "MEDIAFUSION_BASE_URL",
                "NZB_INDEXERS", "EXTRA_ADDONS", "JELLYFIN_URL")


def _f(key: str, label: str, kind: str = "text", placeholder: str = "") -> dict:
    return {"key": key, "label": label, "kind": kind, "placeholder": placeholder}


# ── The optional "do you have this?" cards ────────────────────────────────
# Each card is a toggle that reveals its fields, is live-tested via
# connections.test(<test>), and — on success — has its field values saved.
# `source=True` marks the cards that actually yield streams (setup needs one).
# `test` is a connections.test service id; fields map onto config keys, except
# the custom-addon card (keys prefixed "__" are form-only, see _save_values).

SOURCE_CARDS = [
    dict(id="indexers", title="Usenet indexers", test="indexers", source=True,
         blurb="Newznab indexers you have accounts with. One per line.",
         fields=[_f("NZB_INDEXERS",
                    "name | api-url | apikey — one indexer per line",
                    "multiline",
                    "myindexer | https://api.myindexer.com/api | abcd1234")]),
    dict(id="jellyfin", title="Jellyfin library", test="jellyfin", source=True,
         blurb="Plays titles you already own through Jellyfin's native API. "
               "Credentials are encrypted and never sent to the player.",
         fields=[_f("JELLYFIN_URL", "Server URL reachable by StreamPicker", "url",
                    "http://jellyfin:8096"),
                 _f("JELLYFIN_USERNAME", "Playback-only username"),
                 _f("JELLYFIN_PASSWORD", "Password", "secret")]),
    dict(id="mediafusion", title="MediaFusion", test="mediafusion", source=True,
         blurb="A broad community scraper. Slower first hit; widens the search.",
         fields=[_f("MEDIAFUSION_BASE_URL", "Manifest base URL", "url",
                    "https://mediafusion.example")]),
    dict(id="addon", title="Another Stremio addon", test="addon", source=True,
         blurb="Any addon that returns streams — paste its manifest URL.",
         fields=[_f("__name", "A name for it", "text", "My addon"),
                 _f("url", "Manifest URL", "url",
                    "https://addon.example/manifest.json")]),
]

HELPER_CARDS = [
    dict(id="nzbdav", title="nzbdav", test="nzbdav", source=False,
         blurb="Mounts usenet releases so they stream directly. "
               "Pairs with the usenet indexers above.",
         fields=[_f("NZBDAV_URL", "Base URL", "url", "http://nzbdav:8080"),
                 _f("NZBDAV_USER", "WebDAV user", "text"),
                 _f("NZBDAV_PASS", "WebDAV password", "secret")]),
    dict(id="radarr", title="Radarr", test="radarr", source=False,
         blurb="Grabs missing movies on request.",
         fields=[_f("RADARR_URL", "Base URL", "url", "http://radarr:7878"),
                 _f("RADARR_API_KEY", "API key", "secret"),
                 _f("RADARR_QUALITY_PROFILE", "Quality profile (optional)",
                    "text", "HD-1080p")]),
    dict(id="sonarr", title="Sonarr", test="sonarr", source=False,
         blurb="Grabs missing series on request.",
         fields=[_f("SONARR_URL", "Base URL", "url", "http://sonarr:8989"),
                 _f("SONARR_API_KEY", "API key", "secret"),
                 _f("SONARR_QUALITY_PROFILE", "Quality profile (optional)",
                    "text", "HD-1080p")]),
    dict(id="jellyseerr", title="Jellyseerr", test="jellyseerr", source=False,
         blurb="Preferred path for requesting missing titles.",
         fields=[_f("JELLYSEERR_URL", "Base URL", "url",
                    "http://jellyseerr:5055"),
                 _f("JELLYSEERR_API_KEY", "API key", "secret")]),
]

META_CARDS = [
    dict(id="tmdb", title="TMDB", test="tmdb", source=False, recommended=True,
         key_url="https://www.themoviedb.org/settings/api",
         blurb="Better titles, languages and next-episode handling. Free.",
         fields=[_f("TMDB_API_KEY", "API key", "secret")]),
    dict(id="omdb", title="OMDb", test="omdb", source=False,
         key_url="https://www.omdbapi.com/apikey.aspx",
         blurb="Independent title / year / type corroboration. Free.",
         fields=[_f("OMDB_API_KEY", "API key", "secret")]),
    dict(id="tvdb", title="TVDB", test="tvdb", source=False,
         key_url="https://thetvdb.com/api-information",
         blurb="Season-rollover fallback for episode prefetch.",
         fields=[_f("TVDB_API_KEY", "API key", "secret")]),
]

ALL_CARDS = SOURCE_CARDS + HELPER_CARDS + META_CARDS
CARD_BY_ID = {c["id"]: c for c in ALL_CARDS}


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


def _result(r) -> dict:
    if isinstance(r, BaseException):
        return {"ok": False, "detail": type(r).__name__}
    return {"ok": bool(r.get("ok")), "detail": str(r.get("detail", ""))[:200]}


def _parse_debrids(raw) -> list[tuple[str, str]]:
    out = []
    for d in raw or []:
        sid = str((d or {}).get("service", ""))
        key = str((d or {}).get("key", "")).strip()
        if sid not in _ST_CODE:
            raise ValueError(f"unknown debrid service: {sid[:24]}")
        if not key:
            raise ValueError(f"missing API key for {sid}")
        out.append((sid, key))
    return out


def _parse_cards(raw) -> list[tuple[dict, dict]]:
    """(card spec, {field key: value}) for every card that was toggled on and
    has at least one value. Unknown card ids are ignored (forward-compat)."""
    active = []
    for cid, submitted in (raw or {}).items():
        card = CARD_BY_ID.get(cid)
        if card is None:
            continue
        vals = {fld["key"]: str((submitted or {}).get(fld["key"], "")).strip()
                for fld in card["fields"]}
        if any(vals.values()):
            active.append((card, vals))
    return active


def _test_overrides(card: dict, vals: dict) -> dict:
    """What connections.test() gets. The custom addon is tested as a bare
    manifest URL; every other card's field keys already are config keys."""
    if card["id"] == "addon":
        return {"url": vals.get("url", "")}
    return {k: v for k, v in vals.items() if not k.startswith("__")}


def _merge_addon(name: str, url: str) -> str:
    """Append this addon to the EXTRA_ADDONS JSON list (de-duped by URL),
    leaving any addon the operator already configured in place."""
    existing = []
    raw = config.pending("EXTRA_ADDONS")
    if raw:
        try:
            loaded = json.loads(raw)
            existing = [it for it in loaded if isinstance(it, dict)]
        except ValueError:
            existing = []
    url = url.strip()
    existing = [it for it in existing
                if str(it.get("url", "")).strip() != url]
    existing.append({"name": name.strip() or url, "url": url})
    return json.dumps(existing, separators=(",", ":"))


def _save_values(card: dict, vals: dict) -> dict:
    """The config keys to persist for a card whose test passed."""
    if card["id"] == "addon":
        return {"EXTRA_ADDONS": _merge_addon(vals.get("__name", ""),
                                             vals.get("url", ""))}
    return {k: v for k, v in vals.items() if not k.startswith("__") and v}


async def apply(body: dict) -> dict:
    """Live-test every switched-on card and mint+test the debrid lanes, then
    save only what passed. Succeeds when at least one stream source came up;
    never echoes a key back."""
    from app import connections

    debrids = _parse_debrids(body.get("debrids"))
    active = _parse_cards(body.get("cards"))
    public = str(body.get("public_url") or "").strip()
    if not debrids and not active:
        raise ValueError("Switch on at least one service you have and "
                         "fill in its details.")
    # Validate the minting bases up front so malformed input fails fast.
    comet_base = _clean_base(body.get("comet_base") or COMET_PUBLIC)
    st_base = _clean_base(body.get("stremthru_base") or STREMTHRU_PUBLIC)

    # Round 1: debrid key checks and every card test — all independent.
    probes: dict = {}
    for sid, key in debrids:
        probes[("key", sid)] = _key_check(sid, key)
    for card, vals in active:
        probes[("card", card["id"])] = connections.test(
            card["test"], _test_overrides(card, vals))
    settled = await asyncio.gather(*probes.values(), return_exceptions=True)
    got = {k: _result(r) for k, r in zip(probes, settled)}

    results: dict = {}
    values: dict = {}

    for card, vals in active:
        res = got[("card", card["id"])]
        results[card["id"]] = res
        if res["ok"]:
            values.update(_save_values(card, vals))

    keys_ok = bool(debrids)
    for sid, _ in debrids:
        res = got[("key", sid)]
        results[sid] = res
        keys_ok = keys_ok and res["ok"]

    # Round 2: only mint and lane-test the torrent lanes if every debrid key
    # was accepted (Comet lists cached streams even with a bad key).
    if keys_ok:
        fast = comet_url(debrids, comet_base)
        torz = stremthru_url(debrids, st_base)
        lanes = await asyncio.gather(_lane_check(fast), _lane_check(torz),
                                     return_exceptions=True)
        results["comet"] = _result(lanes[0])
        results["stremthru"] = _result(lanes[1])
        if results["comet"]["ok"]:
            values["FAST_BASE_URL"] = fast
        if results["stremthru"]["ok"]:
            values["STREMTHRU_BASE_URL"] = torz

    # Nothing playable => save nothing; the user must add a real stream source.
    if not any(k in values for k in _SOURCE_KEYS):
        return {"ok": False, "results": results, "saved": []}

    if public:
        values["ADDON_PUBLIC_URL"] = public
    config.save(values)
    logger.info(f"wizard: configured {sorted(values)} "
                f"({len(debrids)} debrid service(s), {len(active)} card(s))")
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
h2{font-size:16px;margin:30px 0 4px}
.blurb{color:var(--mut);font-size:13px;margin:0 0 12px;max-width:620px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:15px 16px;transition:border-color .15s}
.card.on{border-color:var(--accent)}
.chead{display:flex;justify-content:space-between;align-items:center;gap:10px}
.cname{font-weight:650;font-size:15px;display:flex;align-items:center}
.badge{font-size:9.5px;font-weight:700;color:var(--accent);background:var(--accent-soft);
border-radius:99px;padding:2px 7px;margin-left:8px;text-transform:uppercase;
letter-spacing:.04em}
.keylink{font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap}
.keylink:hover{text-decoration:underline}
.kfield{margin-top:12px}
.kfield[hidden]{display:none}
.kfield label,.flabel{display:block;font-size:11.5px;color:var(--mut);margin:10px 0 4px}
.kfield .flabel:first-of-type{margin-top:0}
.cblurb{font-size:12px;color:var(--mut);margin:0 0 8px;line-height:1.45}
.cnote{font-size:11.5px;color:var(--mut);margin:7px 0 0;font-style:italic}
input[type=password],input[type=text],input[type=url],textarea{width:100%;
background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:9px 11px;font:13px var(--mono)}
textarea{resize:vertical;min-height:66px;line-height:1.5}
input::placeholder,textarea::placeholder{color:var(--mut);opacity:.8}
.swi{appearance:none;-webkit-appearance:none;width:42px;height:24px;margin:0;
border-radius:99px;background:var(--line);position:relative;cursor:pointer;
transition:background .15s;flex-shrink:0}
.swi::before{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;
border-radius:50%;background:var(--card);box-shadow:0 1px 2px rgba(0,0,0,.25);
transition:transform .15s}
.swi:checked{background:var(--accent)}
.swi:checked::before{transform:translateX(18px)}
.hintlist{margin:6px 0 4px;padding-left:18px;font-size:12px;color:var(--mut)}
.hintlist li{margin:3px 0;line-height:1.45}
.hintlist code{font:11.5px var(--mono);background:var(--accent-soft);
border-radius:5px;padding:1px 5px;color:var(--fg)}
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
.checks{display:none;flex-direction:column;gap:3px;font:12.5px var(--mono);
max-height:34vh;overflow:auto}
.checks.show{display:flex}
.checks .ok{color:var(--good)}
.checks .bad{color:var(--bad)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
""" + adminui.NAV_CSS

_JS = """
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
$$('.swi').forEach(sw=>sw.addEventListener('change',()=>{
 const card=sw.closest('.card'),kf=card.querySelector('.kfield');
 card.classList.toggle('on',sw.checked);
 if(kf){kf.hidden=!sw.checked;
  if(sw.checked){const f=kf.querySelector('input,textarea');if(f)f.focus();}}
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
function cardVals(card){
 const o={};
 card.querySelectorAll('[data-key]').forEach(el=>{
  const v=el.value.trim();if(v)o[el.dataset.key]=v;});
 return o;
}
function collect(){
 const debrids=$$('.card[data-service]').filter(c=>c.querySelector('.swi').checked)
  .map(c=>({service:c.dataset.service,
   key:c.querySelector('input[type=password]').value.trim()}));
 const cards={};
 $$('.card[data-card]').forEach(c=>{
  if(c.querySelector('.swi').checked){
   const v=cardVals(c);if(Object.keys(v).length)cards[c.dataset.card]=v;}});
 return {debrids,cards,public_url:$('#publicurl').value.trim(),
  comet_base:$('#cometbase').value.trim(),stremthru_base:$('#stbase').value.trim()};
}
const NICE={comet:'Torrent lane (Comet)',stremthru:'Torrent lane (StremThru)',
 torbox:'TorBox key',realdebrid:'Real-Debrid key',alldebrid:'AllDebrid key',
 premiumize:'Premiumize key',indexers:'Usenet indexers',jellyfin:'Jellyfin library',
 mediafusion:'MediaFusion',addon:'Custom addon',nzbdav:'nzbdav',radarr:'Radarr',
 sonarr:'Sonarr',jellyseerr:'Jellyseerr',tmdb:'TMDB',omdb:'OMDb',tvdb:'TVDB'};
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
 const bad=v=>msg.innerHTML='<span style="color:var(--bad)">'+v+'</span>';
 if(body.debrids.some(d=>!d.key))
  return bad('Paste the API key for each debrid you switched on.');
 if(!body.debrids.length&&!Object.keys(body.cards).length)
  return bad('Switch on at least one service you have and fill in its details.');
 btn.disabled=true;msg.textContent='Testing your services…';
 try{
  const res=await post('/api/setup/apply',body);
  showChecks(res.results);
  if(res.ok){phase='restart';btn.textContent='Finish →';btn.disabled=false;
   const failed=Object.values(res.results).filter(v=>!v.ok).length;
   msg.innerHTML=failed?'<b>Ready.</b> Saved what worked; the ✗ items can '+
    'wait. One restart to finish.':'<b>All set.</b> One restart and your '+
    'addon links appear.';}
  else{btn.disabled=false;
   msg.innerHTML='<span style="color:var(--bad)">Nothing playable yet — '+
    'add a debrid, usenet indexers, a Jellyfin library, MediaFusion or '+
    'another addon, then try again.</span>';}
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


def _field_html(card_id: str, fld: dict) -> str:
    fid = f"{card_id}-{fld['key']}"
    label = html.escape(fld["label"])
    ph = html.escape(fld.get("placeholder", ""))
    key = html.escape(fld["key"])
    if fld["kind"] == "multiline":
        control = (f'<textarea data-key="{key}" id="{fid}" rows="3" '
                   f'autocomplete="off" spellcheck="false" '
                   f'placeholder="{ph}"></textarea>')
    else:
        typ = {"secret": "password", "url": "url"}.get(fld["kind"], "text")
        control = (f'<input type="{typ}" data-key="{key}" id="{fid}" '
                   f'autocomplete="off" spellcheck="false" placeholder="{ph}">')
    return f'<label class="flabel" for="{fid}">{label}</label>{control}'


def _source_card(card: dict) -> str:
    fields = "".join(_field_html(card["id"], f) for f in card["fields"])
    badge = ('<span class="badge">recommended</span>'
             if card.get("recommended") else "")
    blurb = (f'<p class="cblurb">{html.escape(card["blurb"])}</p>'
             if card.get("blurb") else "")
    key_link = ""
    if card.get("key_url"):
        key_link = (f'<a class="keylink" href="{html.escape(card["key_url"])}" '
                    f'target="_blank" rel="noopener noreferrer">'
                    f'where is my key?</a> ')
    return f"""<div class="card" data-card="{card['id']}">
 <div class="chead"><span class="cname">{html.escape(card['title'])}{badge}</span>
  <input type="checkbox" class="swi"
         aria-label="I have {html.escape(card['title'])}"></div>
 <div class="kfield" hidden>{blurb}{key_link}{fields}</div>
</div>"""


def render() -> str:
    debrid_cards = "".join(_debrid_card(sid, label, key_url)
                           for sid, label, _, key_url in DEBRIDS)
    source_cards = "".join(_source_card(c) for c in SOURCE_CARDS)
    helper_cards = "".join(_source_card(c) for c in HELPER_CARDS)
    meta_cards = "".join(_source_card(c) for c in META_CARDS)
    name = html.escape(ADDON_NAME)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{name} — setup</title><style>{_CSS}</style></head><body>
<div class="wrap">
{adminui.nav("overview", ADDON_NAME)}
<h1>Set up your streams</h1>
<p class="sub">Switch on the things you actually have and fill in their
details — leave the rest off. Each one is tested before it's saved, and it's
all changeable later in Settings.</p>

<h2>Debrid services</h2>
<p class="blurb">Where most streams come from. Switch on any you have — one is
enough — and the two torrent search lanes are built for you.</p>
<div class="cards">{debrid_cards}</div>

<h2>More sources of streams</h2>
<p class="blurb">Optional. Any of these can stand in for — or add to — a
debrid.</p>
<div class="cards">{source_cards}</div>

<h2>Usenet mount &amp; automation</h2>
<p class="blurb">Optional helpers. They don't provide streams on their own,
but improve or extend the sources above.</p>
<div class="cards">{helper_cards}</div>

<h2>Metadata</h2>
<p class="blurb">Optional, all free. Better titles, languages, and
episode handling.</p>
<div class="cards">{meta_cards}</div>

<h2>Watch away from home</h2>
<div class="cards">
 <div class="card">
  <div class="kfield">
   <p class="cblurb">The address a player <em>outside your home</em> would use
    to reach this server. Every setup is different — this is just however you
    already reach it from the internet:</p>
   <ul class="hintlist">
    <li><b>Reverse proxy</b> (Caddy, Nginx, Traefik): your public HTTPS
     hostname, e.g. <code>https://streams.example.com</code></li>
    <li><b>Tunnel</b> (Cloudflare Tunnel, Tailscale Funnel): the URL the
     tunnel hands you</li>
    <li><b>Home network only:</b> leave this blank</li>
   </ul>
   <label class="flabel" for="publicurl">Public address (optional)</label>
   <input type="url" id="publicurl" autocomplete="off" spellcheck="false"
          placeholder="https://streams.example.com — or leave blank">
   <p class="cnote">We can't verify this from here — it's whatever address
    works from outside your network.</p>
  </div>
 </div>
</div>

<details class="adv"><summary>Advanced — use different Comet / StremThru
servers</summary>
 <div class="card">
  <div class="kfield"><label class="flabel" for="cometbase">Comet server</label>
   <input type="url" id="cometbase" value="{COMET_PUBLIC}"></div>
  <div class="kfield"><label class="flabel" for="stbase">StremThru server</label>
   <input type="url" id="stbase" value="{STREMTHRU_PUBLIC}"></div>
 </div></details>

<p class="later">Everything here — and more knobs besides — lives in
<a href="/settings">Settings</a> too.</p>
</div>

<div class="gobar">
 <div id="checks" class="checks"></div>
 <div class="gorow"><span id="gomsg" class="gomsg">Takes about a minute.</span>
 <button id="gobtn" class="btn">Set up my streams</button></div>
</div>
<script>{_JS}</script></body></html>"""
