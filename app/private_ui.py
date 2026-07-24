"""Dedicated admin page for the opt-in private-tracker subsystem."""

from __future__ import annotations

import html
import json
import os

from app import adminui, config

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

KEYS = (
    "PRIVATE_TRACKERS_ENABLED", "PRIVATE_PROWLARR_URL",
    "PRIVATE_PROWLARR_API_KEY", "PRIVATE_QBITTORRENT_URL",
    "PRIVATE_QBITTORRENT_USERNAME", "PRIVATE_QBITTORRENT_PASSWORD",
    "PRIVATE_STREAM_ENGINE", "PRIVATE_RQBIT_URL",
    "PRIVATE_RQBIT_USERNAME", "PRIVATE_RQBIT_PASSWORD",
    "PRIVATE_RQBIT_OUTPUT_PATH", "PRIVATE_RQBIT_VPN_URL",
    "PRIVATE_RQBIT_VPN_API_KEY",
    "PRIVATE_QBITTORRENT_SAVE_PATH", "PRIVATE_TRACKER_DOWNLOAD_ROOT",
    "PRIVATE_QBITTORRENT_CATEGORY", "PRIVATE_TRACKER_CANDIDATES",
    "PRIVATE_TRACKER_RELEASE_ORDER", "PRIVATE_TRACKER_INDEXER_SCORES",
    "PRIVATE_TRACKER_MIN_SEEDERS", "PRIVATE_TRACKER_SEARCH_TIMEOUT",
    "PRIVATE_TRACKER_START_TIMEOUT", "PRIVATE_TRACKER_SEARCH_TTL",
    "PRIVATE_TRACKER_MAX_TORRENT_GB",
    "PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS",
    "PRIVATE_TRACKER_WHOLE_TORRENT",
)

_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--mut:#6b6b66;
--line:#e6e6e2;--bad:#c0392b;--warn:#9a6700;--good:#2e7d5b;--accent:#3b6ea5;
--soft:#eef3f9;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--fg:#e9e9e6;
--mut:#9a9a94;--line:#2c2f34;--bad:#ff6b5e;--warn:#e0b74a;--good:#5cc99a;
--accent:#6ea3d8;--soft:#232c37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,sans-serif;padding:24px 16px 100px}.wrap{max-width:1000px;margin:auto}
h1{font-size:23px;margin:0 0 5px}h2{font-size:16px;margin:28px 0 8px}.sub,.mut{color:var(--mut)}
.banner{border:1px solid #d7b45a;background:color-mix(in srgb,var(--warn) 9%,var(--card));
border-radius:12px;padding:15px 17px;margin:18px 0}.banner b{color:var(--warn)}
.master{display:flex;align-items:center;gap:16px;margin:8px 0 16px;
border-color:color-mix(in srgb,var(--accent) 35%,var(--line))}.mastercopy{flex:1}
.mastertitle{font-weight:700}.masterstate{font:12px var(--mono);color:var(--mut)}
.masterstate.ok{color:var(--good)}.masterstate.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h3{font-size:15px;margin:0 0 12px}.field{margin:11px 0}.field label{display:block;
font-size:12px;color:var(--mut);margin-bottom:4px}.field input,.field select{width:100%;border:1px solid var(--line);
border-radius:8px;padding:9px 10px;background:var(--bg);color:var(--fg);font:13px var(--mono)}
.switch{display:flex;justify-content:space-between;align-items:center;gap:18px}.swi{appearance:none;
width:44px;height:25px;border:0;border-radius:20px;background:var(--line);position:relative}
.swi:before{content:'';position:absolute;width:19px;height:19px;left:3px;top:3px;border-radius:50%;
background:var(--card);transition:.15s}.swi:checked{background:var(--accent)}.swi:checked:before{transform:translateX(19px)}
.btn{border:0;border-radius:8px;padding:9px 15px;background:var(--accent);color:#fff;
font:600 13px system-ui;cursor:pointer}.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.btn.warn{background:var(--bad)}.btn:disabled{opacity:.55}.actions{display:flex;gap:9px;flex-wrap:wrap;
align-items:center;margin-top:16px}.result{font:12px var(--mono);color:var(--mut)}.ok{color:var(--good)}.bad{color:var(--bad)}
.policy{margin:0;padding-left:20px}.policy li{margin:7px 0}.statusline{display:flex;gap:9px;align-items:center;margin:6px 0}
.policyboard{display:grid;gap:9px;margin:12px 0}.policyblock{display:grid;
grid-template-columns:auto 1fr auto;align-items:center;gap:12px;border:1px solid var(--line);
border-radius:10px;padding:12px;background:var(--bg);transition:.15s}.policyblock.dragging{opacity:.45}
.policyblock.disabled{opacity:.58}.grab{cursor:grab;color:var(--mut);font-size:20px;
line-height:1;user-select:none}.policyname{font-weight:700}.policydesc{font-size:12px;color:var(--mut)}
.policytools{display:flex;align-items:center;gap:6px}.move{border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:7px;width:29px;height:29px;cursor:pointer}
.include{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--mut)}
.prefboard{display:grid;gap:8px;margin:12px 0}.prefrow{display:grid;
grid-template-columns:1fr auto;align-items:center;gap:12px;border:1px solid var(--line);
border-radius:10px;padding:9px 12px;background:var(--bg)}.prefname{font-weight:600;
overflow-wrap:anywhere}.prefscore{width:82px;border:1px solid var(--line);border-radius:8px;
padding:7px 9px;background:var(--card);color:var(--fg);font:13px var(--mono);text-align:center}
.doclink{display:inline-flex;align-items:center;gap:6px;margin-top:12px;color:var(--accent);
font-weight:650;text-decoration:none}.doclink:hover{text-decoration:underline}.steps h3{margin:22px 0 7px}
.steps pre{overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:9px;
padding:12px;font:12px/1.55 var(--mono)}code{font-family:var(--mono)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--line)}.dot.ok{background:var(--good)}.dot.bad{background:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.tile{background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:13px}.tile .n{font:700 23px var(--mono)}.tile .k{font-size:12px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-size:11px;text-transform:uppercase}.scroll{overflow:auto}.bar{height:6px;background:var(--line);border-radius:9px;overflow:hidden;min-width:90px}
.bar span{display:block;height:100%;background:var(--accent)}.restart{display:none;margin-left:auto}
@media(max-width:540px){body{padding:12px 10px 80px}.card{padding:13px}.restart{margin-left:0}}
"""

_JS = r"""
const DATA=JSON.parse(document.getElementById('data').textContent),$=q=>document.querySelector(q);
const csrf=()=>document.querySelector('.adminnav').dataset.csrf;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)});let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.detail||`HTTP ${r.status}`);return d}
function values(){const out={};document.querySelectorAll('[data-key]').forEach(el=>{out[el.dataset.key]=el.type==='checkbox'?(el.checked?'1':'0'):el.value.trim()});return out}
const policy=$('#release_policy'),policyValue=$('#PRIVATE_TRACKER_RELEASE_ORDER');
function syncPolicy(){if(!policy||!policyValue)return;const enabled=[];policy.querySelectorAll('.policyblock').forEach(block=>{const box=block.querySelector('.include input');block.classList.toggle('disabled',!box.checked);if(box.checked)enabled.push(block.dataset.kind)});policyValue.value=enabled.join(',')}
if(policy){let dragged=null;policy.querySelectorAll('.policyblock').forEach(block=>{block.addEventListener('dragstart',()=>{dragged=block;block.classList.add('dragging')});block.addEventListener('dragend',()=>{block.classList.remove('dragging');dragged=null;syncPolicy()});block.addEventListener('dragover',e=>{e.preventDefault();if(!dragged||dragged===block)return;const rect=block.getBoundingClientRect();policy.insertBefore(dragged,e.clientY<rect.top+rect.height/2?block:block.nextSibling)});block.querySelector('.include input').addEventListener('change',syncPolicy);block.querySelector('[data-move=\"up\"]').onclick=()=>{const prev=block.previousElementSibling;if(prev)policy.insertBefore(block,prev);syncPolicy()};block.querySelector('[data-move=\"down\"]').onclick=()=>{const next=block.nextElementSibling;if(next)policy.insertBefore(next,block);syncPolicy()}});syncPolicy()}
$('#private_master').onchange=async()=>{const el=$('#private_master'),r=$('#master_result'),before=el.dataset.saved;el.disabled=true;r.className='masterstate';r.textContent='saving…';try{const d=await post('/api/private-trackers/save',{values:{PRIVATE_TRACKERS_ENABLED:el.checked?'1':'0'}});el.dataset.saved=el.checked?'1':'0';r.className='masterstate ok';r.textContent='Saved · restart to apply';$('#restart').style.display='inline-block'}catch(e){el.checked=before==='1';r.className='masterstate bad';r.textContent=e.message}el.disabled=false};
$('#save').onclick=async()=>{const b=$('#save'),r=$('#result');b.disabled=true;r.className='result';r.textContent='saving…';try{const d=await post('/api/private-trackers/save',{values:values()});r.className='result ok';r.textContent='Saved. Restart to apply.';$('#restart').style.display='inline-block'}catch(e){r.className='result bad';r.textContent=e.message}b.disabled=false};
$('#test').onclick=async()=>{const b=$('#test'),r=$('#result');b.disabled=true;r.className='result';r.textContent='testing private connections…';try{const d=await post('/api/private-trackers/test',{values:values()});const parts=Object.entries(d).filter(([k,v])=>v&&typeof v==='object'&&'ok'in v).map(([k,v])=>`${v.ok?'✓':'✗'} ${k}: ${v.detail}`);r.className='result '+(d.ok?'ok':'bad');r.textContent=parts.join(' · ')}catch(e){r.className='result bad';r.textContent=e.message}b.disabled=false};
$('#restart').onclick=async()=>{const b=$('#restart');b.disabled=true;try{await post('/api/settings/restart',{});b.textContent='Restarting…'}catch(e){b.disabled=false;alert(e.message)}};
function speed(n){n=Number(n)||0;return n?`${(n/1e6).toFixed(1)} MB/s`:'—'}
async function refresh(){try{const r=await fetch('/api/private-trackers/status.json',{cache:'no-store'}),d=await r.json();$('#enabled').textContent=d.enabled?'Enabled':'Disabled';$('#enabled').className='n '+(d.enabled?'ok':'');const ready=!!(d.configured&&d.prowlarr&&d.qbittorrent&&d.vpn&&d.rqbit&&d.storage);$('#ready').textContent=ready?'Ready':'Incomplete';$('#ready').className='n '+(ready?'ok':'bad');$('#pdot').className='dot '+(d.prowlarr?'ok':'bad');$('#pstate').textContent=d.prowlarr?`${d.private_torrent_indexers||0} private torrent indexers`:'Unavailable';$('#qdot').className='dot '+(d.qbittorrent?'ok':'bad');$('#qstate').textContent=d.qbittorrent?'Authenticated for seeding':'Unavailable';$('#vdot').className='dot '+(d.vpn?'ok':'bad');$('#vstate').textContent=d.stream_engine==='rqbit'?(d.vpn?'PIA tunnel running':'Tunnel unavailable'):'Not required';$('#rdot').className='dot '+(d.rqbit?'ok':'bad');$('#rstate').textContent=d.stream_engine==='rqbit'?(d.rqbit?'Progressive engine ready':'Unavailable'):'Compatibility mode';$('#sdot').className='dot '+(d.storage?'ok':'bad');$('#sstate').textContent=d.storage?'Shared download folder readable':'Read-only mount missing';const rows=d.downloads||[];$('#active').textContent=rows.filter(x=>x.progress<100).length;$('#complete').textContent=rows.filter(x=>x.progress>=100).length;$('#downloads').innerHTML=rows.length?rows.map(x=>`<tr><td>${esc(x.engine||'—')}</td><td>${esc(x.name)}</td><td><div class='bar'><span style='width:${Math.max(0,Math.min(100,x.progress))}%'></span></div>${x.progress.toFixed(1)}%</td><td>${esc(x.state)}</td><td>${speed(x.download_speed)}</td><td>${speed(x.upload_speed)}</td><td>${Number(x.ratio).toFixed(2)}</td></tr>`).join(''):`<tr><td colspan='7' class='mut'>No private-tracker torrents yet.</td></tr>`}catch(e){$('#pstate').textContent='Status unavailable';$('#qstate').textContent='Status unavailable';$('#vstate').textContent='Status unavailable';$('#rstate').textContent='Status unavailable';$('#sstate').textContent='Status unavailable'}}
const scoreBox=$('#PRIVATE_TRACKER_INDEXER_SCORES');
let storedScores={};try{storedScores=JSON.parse(scoreBox.value||'{}')||{}}catch{storedScores={}}
let prefLoaded=false;
function syncScores(){if(!prefLoaded)return;const merged={};for(const[k,v]of Object.entries(storedScores)){if(Number(v)!==50)merged[k]=Number(v)}document.querySelectorAll('#indexers [data-indexer]').forEach(inp=>{let v=Math.round(Number(inp.value));if(!Number.isFinite(v))v=50;v=Math.min(100,Math.max(1,v));inp.value=v;if(v!==50)merged[inp.dataset.indexer]=v;else delete merged[inp.dataset.indexer]});scoreBox.value=JSON.stringify(merged)}
async function loadIndexers(){const box=$('#indexers'),state=$('#pref_state');try{const r=await fetch('/api/private-trackers/indexers.json',{cache:'no-store'}),d=await r.json();if(!d.ok||!(d.indexers||[]).length){box.innerHTML='';state.className='result';state.textContent=d.detail?('Couldn’t list trackers — '+d.detail):'No private torrent indexers found in Prowlarr yet.';return}box.innerHTML=d.indexers.map(x=>`<div class='prefrow'><span class='prefname'>${esc(x.name)}</span><input class='prefscore' type='number' min='1' max='100' step='1' data-indexer='${esc(x.name)}' value='${Number(x.score)||50}' aria-label='Preference score for ${esc(x.name)}'></div>`).join('');prefLoaded=true;state.className='result';state.textContent=`${d.indexers.length} tracker${d.indexers.length===1?'':'s'} · 1 = first, 100 = last, 50 = neutral`;box.querySelectorAll('[data-indexer]').forEach(inp=>inp.addEventListener('input',syncScores));syncScores()}catch(e){state.textContent='Tracker list unavailable'}}
$('#pref_reset').onclick=()=>{storedScores={};scoreBox.value='{}';document.querySelectorAll('#indexers [data-indexer]').forEach(inp=>{inp.value=50});syncScores()};
loadIndexers();
refresh();setInterval(refresh,10000);
"""


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _field(key: str, label: str, *, secret: bool = False,
           typ: str = "text", readonly: bool = False,
           minimum: int | None = None, maximum: int | None = None,
           step: str | None = None) -> str:
    value = config.pending(key)
    if secret:
        shown = ""
        placeholder = config.mask(value, key) or "not set"
        secret_attr = " data-secret='1'"
        typ = "password"
    else:
        shown = value
        placeholder = ""
        secret_attr = ""
    ro = " readonly" if readonly else ""
    bounds = ""
    if minimum is not None:
        bounds += f" min='{minimum}'"
    if maximum is not None:
        bounds += f" max='{maximum}'"
    if step is not None:
        bounds += f" step='{_esc(step)}'"
    return (f"<div class='field'><label for='{key}'>{_esc(label)}</label>"
            f"<input id='{key}' type='{typ}' data-key='{key}' value='{_esc(shown)}' "
            f"placeholder='{_esc(placeholder)}'{secret_attr}{ro}{bounds} spellcheck='false' "
            "autocomplete='off'></div>")


def _choice(key: str, label: str, choices: tuple[tuple[str, str], ...]) -> str:
    current = config.pending(key)
    options = "".join(
        f"<option value='{_esc(value)}'"
        f"{' selected' if value == current else ''}>{_esc(text)}</option>"
        for value, text in choices)
    return (f"<div class='field'><label for='{key}'>{_esc(label)}</label>"
            f"<select id='{key}' data-key='{key}'>{options}</select></div>")


def _release_policy_editor() -> str:
    definitions = {
        "episode": (
            "Individual episode",
            "Smallest download and usually the quickest route to play."),
        "season": (
            "Single-season pack",
            "One season together; useful when you plan to keep watching."),
        "series": (
            "Whole-series or multi-season pack",
            "The full collection in one torrent for long-term seeding."),
    }
    selected = [
        value.strip().lower()
        for value in config.pending(
            "PRIVATE_TRACKER_RELEASE_ORDER").split(",")
        if value.strip().lower() in definitions
    ]
    order = selected + [kind for kind in definitions if kind not in selected]
    cards = []
    for kind in order:
        name, description = definitions[kind]
        checked = " checked" if kind in selected else ""
        cards.append(
            f"<div class='policyblock{' disabled' if not checked else ''}' "
            f"draggable='true' data-kind='{kind}'>"
            "<span class='grab' title='Drag to reorder' aria-hidden='true'>⠿</span>"
            f"<div><div class='policyname'>{_esc(name)}</div>"
            f"<div class='policydesc'>{_esc(description)}</div></div>"
            "<div class='policytools'>"
            f"<label class='include'><input type='checkbox'{checked}>Include</label>"
            "<button class='move' type='button' data-move='up' "
            "aria-label='Move up'>↑</button>"
            "<button class='move' type='button' data-move='down' "
            "aria-label='Move down'>↓</button></div></div>")
    return (
        "<input type='hidden' id='PRIVATE_TRACKER_RELEASE_ORDER' "
        "data-key='PRIVATE_TRACKER_RELEASE_ORDER'>"
        f"<div class='policyboard' id='release_policy'>{''.join(cards)}</div>")


def render_setup() -> str:
    compose = """cp deploy/rqbit-pia.env.example rqbit-pia.env
chmod 600 rqbit-pia.env
install -d rqbit/db rqbit/cache gluetun

docker compose \\
  --env-file /secure/path/pia.env \\
  --env-file ./rqbit-pia.env \\
  -f deploy/rqbit-pia.compose.yml config

docker compose \\
  --env-file /secure/path/pia.env \\
  --env-file ./rqbit-pia.env \\
  -f deploy/rqbit-pia.compose.yml up -d"""
    settings = """PRIVATE_STREAM_ENGINE=rqbit
PRIVATE_RQBIT_URL=http://<NAS-LAN-IP>:3030
PRIVATE_RQBIT_USERNAME=<RQBIT_HTTP_USER>
PRIVATE_RQBIT_PASSWORD=<RQBIT_HTTP_PASSWORD>
PRIVATE_RQBIT_OUTPUT_PATH=/data/nuviodownloads
PRIVATE_RQBIT_VPN_URL=http://<NAS-LAN-IP>:8000
PRIVATE_RQBIT_VPN_API_KEY=<RQBIT_VPN_CONTROL_API_KEY>"""
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='robots' content='noindex,nofollow'>
<title>{_esc(ADDON_NAME)} — private tracker setup</title>
<style>{_CSS}{adminui.NAV_CSS}</style></head><body><main class='wrap'>
{adminui.nav('private', ADDON_NAME)}
<p><a class='doclink' href='/private-trackers'>← Back to Private Trackers</a></p>
<h1>Private tracker progressive setup</h1>
<p class='sub'>This optional lane keeps local torrent downloads separate from
debrid. rqbit starts playback while downloading through a fail-closed PIA
tunnel; qBittorrent takes over the same files for completion and long-term
seeding.</p>

<section class='card steps'>
<h3>1. Prepare the services</h3>
<ul class='policy'>
 <li>A Prowlarr instance with at least one enabled <b>private torrent</b> indexer.</li>
 <li>A qBittorrent instance for permanent seeding, already routed through your VPN.</li>
 <li>A NAS-local download directory writable by rqbit and qBittorrent and
 readable by Stream Picker.</li>
 <li>PIA service credentials. Store them in a mode-<code>0600</code> env file,
 never in the Compose file or dashboard notes.</li>
</ul>

<h3>2. Deploy rqbit behind Gluetun on the storage host</h3>
<p>Run the companion stack on the NAS—not on a machine writing the destination
over NFS. rqbit shares Gluetun's network namespace, so it has no route that can
bypass the PIA firewall.</p>
<pre>{_esc(compose)}</pre>
<p>In <code>rqbit-pia.env</code>, set the NAS LAN bind address, the NAS-local
download directory, matching PUID/PGID, a generated rqbit HTTP password, and a
generated Gluetun control API key. Keep ports 3030 and 8000 LAN-only.</p>

<h3>3. Make all three paths resolve to the same files</h3>
<ul class='policy'>
 <li>rqbit writes to its configured output path, commonly
 <code>/data/nuviodownloads</code>.</li>
 <li>qBittorrent's save path may have a different container spelling, but it
 must be the same physical NAS directory.</li>
 <li>Stream Picker mounts that directory read-only at
 <code>PRIVATE_TRACKER_DOWNLOAD_ROOT</code>.</li>
</ul>

<h3>4. Configure Stream Picker</h3>
<p>Enter the values on the <a href='/private-trackers'>Private Trackers</a>
page. The progressive-specific values are:</p>
<pre>{_esc(settings)}</pre>
<p>Also enter the private Prowlarr connection, qBittorrent connection and save
path, Stream Picker read-only mount, and dedicated qBittorrent category.</p>

<h3>5. Verify before restarting</h3>
<ol class='policy'>
 <li>Press <b>Save private settings</b>.</li>
 <li>Press <b>Test connections</b> and require all five checks to pass:
 Prowlarr, PIA VPN, rqbit, qBittorrent, and storage.</li>
 <li>Restart Stream Picker so the saved settings become active.</li>
 <li>Open a private result. The first real media GET should create a stopped
 qBittorrent registration and start only the selected file in rqbit.</li>
</ol>

<h3>6. What happens during playback</h3>
<p>rqbit prioritizes the ranges the player is reading. When the selected file
finishes, Stream Picker pauses rqbit, asks qBittorrent to hash the same files in
place, starts the remainder according to your whole-torrent policy, and removes
the torrent from rqbit without deleting media.</p>

<div class='banner'><b>Fail-closed check:</b> if Gluetun reports anything other
than <code>running</code>, Stream Picker refuses activation. Gluetun's firewall
also blocks rqbit traffic at the network layer if the tunnel drops.</div>
</section>
</main></body></html>"""


def render(metrics: dict) -> str:
    on = config.pending("PRIVATE_TRACKERS_ENABLED").lower() not in (
        "", "0", "false", "no", "off")
    whole_on = config.pending("PRIVATE_TRACKER_WHOLE_TORRENT").lower() not in (
        "", "0", "false", "no", "off")
    scores_json = config.pending("PRIVATE_TRACKER_INDEXER_SCORES") or "{}"
    events = metrics.get("events") or {}
    data = json.dumps({"metrics": metrics}, separators=(",", ":")) \
        .replace("<", "\\u003c")
    restart = config.restart_pending()
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='robots' content='noindex,nofollow'>
<title>{_esc(ADDON_NAME)} — private trackers</title>
<style>{_CSS}{adminui.NAV_CSS}</style></head><body><main class='wrap'>
{adminui.nav('private', ADDON_NAME)}
<h1>Private Trackers</h1>
<section class='card master'><div class='mastercopy'>
 <div class='mastertitle'>Private tracker downloads</div>
 <div class='mut'>Master on/off control for your local torrent lane.</div>
 </div><span class='masterstate' id='master_result'></span>
 <input class='swi' id='private_master' type='checkbox'
 data-key='PRIVATE_TRACKERS_ENABLED' data-saved='{'1' if on else '0'}'
 {'checked' if on else ''} aria-label='Enable private tracker downloads'></section>
<p class='sub'>A deliberately isolated home for local downloads. Nothing in this
lane is ever sent to debrid, and it is a great fit for private trackers. Browse
freely—nothing starts downloading until you press play.</p>
<div class='banner'><b>Progressive mode streams the selected file through rqbit.</b>
 Once that file is complete, qBittorrent rechecks the same files, finishes the
 release, and seeds indefinitely.</div>

<div class='grid'>
 <section class='card'><h3>Apply and verify</h3>
 <div class='mut'>Tune this lane to match how you like to collect and seed.</div>
 <div class='actions'><button class='btn' id='save'>Save private settings</button>
 <button class='btn ghost' id='test'>Test connections</button>
 <button class='btn warn restart' id='restart' style='display:{'inline-block' if restart else 'none'}'>Restart addon</button></div>
 <div class='result' id='result'></div></section>
 <section class='card'><h3>Live status</h3>
 <div class='statusline'><span class='dot' id='pdot'></span><b>Prowlarr</b> <span class='mut' id='pstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='vdot'></span><b>PIA VPN</b> <span class='mut' id='vstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='rdot'></span><b>rqbit</b> <span class='mut' id='rstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='qdot'></span><b>qBittorrent seeder</b> <span class='mut' id='qstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='sdot'></span><b>Storage</b> <span class='mut' id='sstate'>Checking…</span></div>
 </section>
</div>

<h2>Download limits</h2><section class='card'>
 <div class='switch'><div class='mastercopy'>
  <div class='mastertitle'>Download the whole torrent (100%)</div>
  <div class='mut'>On (default): the complete release downloads and seeds — no
  hit-and-run. Off: only your clicked episode downloads from a season pack; the
  rest is skipped and the torrent stays partial.</div>
 </div><input class='swi' type='checkbox' data-key='PRIVATE_TRACKER_WHOLE_TORRENT'
  {'checked' if whole_on else ''} aria-label='Download the whole torrent'></div>
 {_field('PRIVATE_TRACKER_MAX_TORRENT_GB','Maximum download size (GB) — skip torrents bigger than this',
         typ='number',minimum=0,maximum=100000,step='0.1')}
 <div class='mut'>Private results larger than this are never offered, so a clicked
 torrent can't fill the disk with a huge UHD remux or multi-season pack. A clicked
  torrent is handed to qBittorrent after the watched file completes; qBittorrent
  then downloads the rest and seeds it. <b>0 = no limit.</b></div>
 {_field('PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS',
         'Maximum simultaneous downloads (0 = unlimited)',typ='number',
         minimum=0,maximum=1000)}
 <div class='mut'>How many private torrents may download at once. Default: 3.</div>
</section>

<h2>Setup</h2><section class='card'>
 <div class='mastertitle'>Optional advanced setup</div>
 <div class='mut'>The progressive path uses Prowlarr, rqbit behind a PIA/Gluetun
 kill switch, shared NAS storage, and qBittorrent for permanent seeding. Most
 users only do this once.</div>
 <a class='doclink' href='/private-trackers/setup'>Open the complete private-tracker setup guide →</a>
</section>

<h2>Connections</h2><div class='grid'>
 <section class='card'><h3>Private Prowlarr</h3>
 {_field('PRIVATE_PROWLARR_URL','Internal URL')}
 {_field('PRIVATE_PROWLARR_API_KEY','API key',secret=True)}</section>
 <section class='card'><h3>Private qBittorrent</h3>
 <div class='mut'>Long-term completion and permanent seeding.</div>
 {_field('PRIVATE_QBITTORRENT_URL','Internal URL')}
 {_field('PRIVATE_QBITTORRENT_USERNAME','Username')}
 {_field('PRIVATE_QBITTORRENT_PASSWORD','Password',secret=True)}</section>
 <section class='card'><h3>Progressive downloader</h3>
 {_choice('PRIVATE_STREAM_ENGINE','Playback engine',
          (('rqbit','rqbit — progressive streaming'),
           ('qbittorrent','qBittorrent — compatibility')))}
 {_field('PRIVATE_RQBIT_URL','rqbit internal URL')}
 {_field('PRIVATE_RQBIT_USERNAME','rqbit username (optional)')}
 {_field('PRIVATE_RQBIT_PASSWORD','rqbit password (optional)',secret=True)}
 {_field('PRIVATE_RQBIT_OUTPUT_PATH','rqbit output path')}
 {_field('PRIVATE_RQBIT_VPN_URL','Gluetun control URL')}
 {_field('PRIVATE_RQBIT_VPN_API_KEY','Gluetun control API key',secret=True)}
 <div class='mut'>Use the supplied rqbit + Gluetun stack on the NAS. Stream
 Picker requires an authenticated “VPN running” response before activation.
 rqbit's output path
 and qBittorrent's save path must point at the same physical directory.</div></section>
 <section class='card'><h3>Storage isolation</h3>
 {_field('PRIVATE_QBITTORRENT_SAVE_PATH','qBittorrent save path')}
 {_field('PRIVATE_TRACKER_DOWNLOAD_ROOT','Read-only Stream Picker mount')}
 {_field('PRIVATE_QBITTORRENT_CATEGORY','Dedicated category')}</section>
 <section class='card'><h3>Search tuning</h3>
 {_field('PRIVATE_TRACKER_CANDIDATES','Candidates shown',typ='number',
         minimum=1,maximum=1000)}
 <div class='mut'>Up to this many eligible results are shown using your release
 preference below. Each row names its private tracker. Default: 20.</div>
 {_field('PRIVATE_TRACKER_MIN_SEEDERS','Minimum seeders (hard eligibility floor)',
         typ='number',minimum=0,maximum=10000)}
 <div class='mut'>Results below this value are excluded before episode, season-pack,
 or whole-series preference is considered. Default: 5.</div>
 {_field('PRIVATE_TRACKER_SEARCH_TIMEOUT','Search timeout (seconds)',typ='number')}
 {_field('PRIVATE_TRACKER_START_TIMEOUT','Opening-piece wait (seconds)',typ='number')}
 {_field('PRIVATE_TRACKER_SEARCH_TTL','Search cache (seconds)',typ='number')}</section>
</div>

<h2>Your download policy</h2><section class='card'>
 <div class='mastertitle'>Release preference</div>
 <div class='mut'>Drag these blocks into your preferred order. Turn off a type
 to exclude it from private search results. Movies are unaffected.</div>
 {_release_policy_editor()}
 <div class='mastertitle'>Fixed safety guarantees</div><ul class='policy'>
 <li>rqbit exclusively downloads and progressively serves the clicked file.</li>
 <li>rqbit has no independent network interface. Gluetun's firewall blocks
 traffic if PIA drops, and Stream Picker also refuses activation unless the
 authenticated VPN health check reports <b>running</b>.</li>
 <li>After that file completes, qBittorrent rechecks it without copying, enables
 the rest of the pack, and takes sole ownership of the download.</li>
 <li>Stream Picker never deletes the media files; unlimited seeding is enforced
 per torrent after qBittorrent takes ownership.</li>
 <li>HEAD/preflight requests are inert; the first media GET is the activation boundary.</li>
</ul></section>

<h2>Tracker preferences</h2><section class='card'>
 <div class='mastertitle'>Favorite trackers first</div>
 <div class='mut'>Score each tracker Prowlarr searches — <b>1 = most preferred,
 100 = least, 50 = neutral</b>. Within a release type, results from
 higher-preference trackers are shown first. Leave everything at 50 to treat
 every tracker equally.</div>
 <div class='actions'><button class='btn ghost' type='button' id='pref_reset'>Reset all to 50</button>
 <span class='result' id='pref_state'>Loading trackers…</span></div>
 <div class='prefboard' id='indexers'></div>
 <input type='hidden' id='PRIVATE_TRACKER_INDEXER_SCORES'
  data-key='PRIVATE_TRACKER_INDEXER_SCORES' value='{_esc(scores_json)}'>
 <div class='mut'>Trackers are read live from Prowlarr. Save private settings and
 restart to apply new scores.</div>
</section>

<h2>Metrics</h2><div class='tiles'>
 <div class='tile'><div class='n' id='enabled'>—</div><div class='k'>runtime state</div></div>
 <div class='tile'><div class='n' id='ready'>—</div><div class='k'>setup readiness</div></div>
 <div class='tile'><div class='n' id='active'>—</div><div class='k'>active downloads</div></div>
 <div class='tile'><div class='n' id='complete'>—</div><div class='k'>completed torrents</div></div>
 <div class='tile'><div class='n'>{int(events.get('candidates',0))}</div><div class='k'>search candidates</div></div>
 <div class='tile'><div class='n'>{int(events.get('clicked',0))}</div><div class='k'>click activations</div></div>
 <div class='tile'><div class='n'>{int(events.get('start_failed',0))}</div><div class='k'>start failures</div></div>
</div>

<h2>Private downloads</h2><section class='card scroll'><table>
<thead><tr><th>Engine</th><th>Release</th><th>Progress</th><th>State</th><th>Download</th><th>Upload</th><th>Ratio</th></tr></thead>
<tbody id='downloads'><tr><td colspan='7' class='mut'>Checking torrent engines…</td></tr></tbody>
</table></section>
</main><script id='data' type='application/json'>{data}</script><script>{_JS}</script></body></html>"""
