"""Dedicated admin page for the opt-in private-tracker subsystem."""

from __future__ import annotations

import html
import json
import os

from app import adminui, config, uitheme

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
    "PRIVATE_TRACKER_WHOLE_TORRENT", "PRIVATE_TRACKER_MIN_SOURCES",
)

# Page-specific CSS only — palette, cards, buttons, switches, dots, meters,
# tables and type come from uitheme.BASE_CSS via uitheme.page().
_CSS = """
.pad{padding:16px 18px}
.card h3{font-size:15px;margin:0 0 10px}
.note{color:var(--mut);font-size:12.5px;margin:9px 0 0;max-width:72ch}
.field{margin:11px 0}
.field label{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
.field select{width:100%}
.switch{display:flex;justify-content:space-between;align-items:center;gap:18px}
.switchcopy{flex:1;min-width:0}
.switchtitle{font-weight:700}
.masterstate{font:12px var(--mono);color:var(--mut);white-space:nowrap}
.masterstate.ok{color:var(--ok)}.masterstate.bad{color:var(--bad)}
.trigger{display:flex;justify-content:space-between;align-items:center;gap:18px;
margin-top:15px;padding-top:15px;border-top:1px solid var(--line)}
.trigger input.thin{width:88px;text-align:center;font:600 15px var(--mono)}
.result{font:12px var(--mono);color:var(--mut);margin-top:10px;overflow-wrap:anywhere}
.result.ok{color:var(--ok)}.result.bad{color:var(--bad)}
.statusline{display:flex;gap:9px;align-items:center;margin:9px 0}
.statusline b{font-size:13.5px}
.statusline .mut{font-size:12.5px}
.doclink{display:inline-flex;align-items:center;gap:6px;margin-top:12px;font-weight:650}
.policy{margin:9px 0 0;padding-left:20px}.policy li{margin:7px 0}
.policyboard{display:grid;gap:9px;margin:12px 0}
.policyblock{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:12px;
border:1px solid var(--line);border-radius:10px;padding:12px;background:var(--inset);
transition:opacity .15s}
.policyblock.dragging{opacity:.45}
.policyblock.disabled{opacity:.58}
.grab{cursor:grab;color:var(--mut);font-size:20px;line-height:1;user-select:none}
.policyname{font-weight:700}.policydesc{font-size:12px;color:var(--mut)}
.policytools{display:flex;align-items:center;gap:6px}
.move{border:1px solid var(--line);background:var(--card);color:var(--fg);
border-radius:7px;width:29px;height:29px;cursor:pointer}
.move:hover{border-color:var(--accent);color:var(--accent)}
.include{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--mut)}
.prefboard{display:grid;gap:8px;margin:12px 0}
.prefrow{display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px;
border:1px solid var(--line);border-radius:10px;padding:9px 12px;background:var(--inset)}
.prefname{font-weight:600;overflow-wrap:anywhere}
.prefscore{width:82px;text-align:center}
.steps{display:grid;gap:12px}
.backlink{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;margin:0 0 18px}
"""

_JS = r"""
const DATA=JSON.parse(document.getElementById('data').textContent),$=q=>document.querySelector(q);
const csrf=()=>document.querySelector('[data-csrf]').dataset.csrf;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)});let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.detail||`HTTP ${r.status}`);return d}
function values(){const out={};document.querySelectorAll('[data-key]').forEach(el=>{out[el.dataset.key]=el.type==='checkbox'?(el.checked?'1':'0'):el.value.trim()});return out}
const policy=$('#release_policy'),policyValue=$('#PRIVATE_TRACKER_RELEASE_ORDER');
function syncPolicy(){if(!policy||!policyValue)return;const enabled=[];policy.querySelectorAll('.policyblock').forEach(block=>{const box=block.querySelector('.include input');block.classList.toggle('disabled',!box.checked);if(box.checked)enabled.push(block.dataset.kind)});policyValue.value=enabled.join(',')}
if(policy){let dragged=null;policy.querySelectorAll('.policyblock').forEach(block=>{block.addEventListener('dragstart',()=>{dragged=block;block.classList.add('dragging')});block.addEventListener('dragend',()=>{block.classList.remove('dragging');dragged=null;syncPolicy()});block.addEventListener('dragover',e=>{e.preventDefault();if(!dragged||dragged===block)return;const rect=block.getBoundingClientRect();policy.insertBefore(dragged,e.clientY<rect.top+rect.height/2?block:block.nextSibling)});block.querySelector('.include input').addEventListener('change',syncPolicy);block.querySelector('[data-move=\"up\"]').onclick=()=>{const prev=block.previousElementSibling;if(prev)policy.insertBefore(block,prev);syncPolicy()};block.querySelector('[data-move=\"down\"]').onclick=()=>{const next=block.nextElementSibling;if(next)policy.insertBefore(next,block);syncPolicy()}});syncPolicy()}
async function saveOne(key,value,el,r){el.disabled=true;r.className='masterstate';r.textContent='saving…';try{await post('/api/private-trackers/save',{values:{[key]:value}});el.dataset.saved=value;r.className='masterstate ok';r.textContent='Saved · restart to apply';$('#restart').style.display='inline-block';return true}catch(e){r.className='masterstate bad';r.textContent=e.message;return false}finally{el.disabled=false}}
$('#private_master').onchange=async()=>{const el=$('#private_master'),before=el.dataset.saved;if(!await saveOne('PRIVATE_TRACKERS_ENABLED',el.checked?'1':'0',el,$('#master_result')))el.checked=before==='1'};
const thin=$('#private_min_sources');
function thinCopy(v){v=Number(v);if(!Number.isFinite(v)||!Number.isInteger(v))return'A whole number: −1 always, 0 last resort, otherwise the thin-results threshold.';if(v<0)return'Always on — every slow pick searches your trackers alongside the public sources.';if(v===0)return'Last resort — searched only when the public path found nothing playable at all.';return`Searched as well whenever a pick turns up fewer than ${v} distinct public release${v===1?'':'s'}.`}
function thinNote(){$('#thin_note').textContent=thinCopy(thin.value)}
thin.oninput=thinNote;
thin.onchange=async()=>{let v=Math.round(Number(thin.value));if(!Number.isFinite(v))v=Number(thin.dataset.saved)||0;v=Math.max(-1,Math.min(1000,v));thin.value=String(v);thinNote();if(String(v)===thin.dataset.saved)return;if(!await saveOne('PRIVATE_TRACKER_MIN_SOURCES',String(v),thin,$('#thin_result'))){thin.value=thin.dataset.saved;thinNote()}};
thinNote();
$('#save').onclick=async()=>{const b=$('#save'),r=$('#result');b.disabled=true;r.className='result';r.textContent='saving…';try{const d=await post('/api/private-trackers/save',{values:values()});r.className='result ok';r.textContent='Saved. Restart to apply.';$('#restart').style.display='inline-block'}catch(e){r.className='result bad';r.textContent=e.message}b.disabled=false};
$('#test').onclick=async()=>{const b=$('#test'),r=$('#result');b.disabled=true;r.className='result';r.textContent='testing private connections…';try{const d=await post('/api/private-trackers/test',{values:values()});const parts=Object.entries(d).filter(([k,v])=>v&&typeof v==='object'&&'ok'in v).map(([k,v])=>`${v.ok?'✓':'✗'} ${k}: ${v.detail}`);r.className='result '+(d.ok?'ok':'bad');r.textContent=parts.join(' · ')}catch(e){r.className='result bad';r.textContent=e.message}b.disabled=false};
$('#restart').onclick=async()=>{const b=$('#restart');b.disabled=true;try{await post('/api/settings/restart',{});b.textContent='Restarting…'}catch(e){b.disabled=false;alert(e.message)}};
function speed(n){n=Number(n)||0;return n?`${(n/1e6).toFixed(1)} MB/s`:'—'}
async function refresh(){try{const r=await fetch('/api/private-trackers/status.json',{cache:'no-store'}),d=await r.json();$('#enabled').textContent=d.enabled?'Enabled':'Disabled';$('#enabled').className='v '+(d.enabled?'ok':'');const ready=!!(d.configured&&d.prowlarr&&d.qbittorrent&&d.vpn&&d.rqbit&&d.storage);$('#ready').textContent=ready?'Ready':'Incomplete';$('#ready').className='v '+(ready?'ok':'bad');$('#pdot').className='dot '+(d.prowlarr?'ok':'bad');$('#pstate').textContent=d.prowlarr?`${d.private_torrent_indexers||0} private torrent indexers`:'Unavailable';$('#qdot').className='dot '+(d.qbittorrent?'ok':'bad');$('#qstate').textContent=d.qbittorrent?'Authenticated for seeding':'Unavailable';$('#vdot').className='dot '+(d.vpn?'ok':'bad');$('#vstate').textContent=d.stream_engine==='rqbit'?(d.vpn?'PIA tunnel running':'Tunnel unavailable'):'Not required';$('#rdot').className='dot '+(d.rqbit?'ok':'bad');$('#rstate').textContent=d.stream_engine==='rqbit'?(d.rqbit?'Progressive engine ready':'Unavailable'):'Compatibility mode';$('#sdot').className='dot '+(d.storage?'ok':'bad');$('#sstate').textContent=d.storage?'Shared download folder readable':'Read-only mount missing';const rows=d.downloads||[];$('#active').textContent=rows.filter(x=>x.progress<100).length;$('#complete').textContent=rows.filter(x=>x.progress>=100).length;$('#downloads').innerHTML=rows.length?rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.engine||'—')}</td><td><div class='meter'><span class='fill' style='width:${Math.max(0,Math.min(100,x.progress))}%'></span></div>${x.progress.toFixed(1)}%</td><td>${esc(x.state)}</td><td>${speed(x.download_speed)}</td><td>${speed(x.upload_speed)}</td><td>${Number(x.ratio).toFixed(2)}</td></tr>`).join(''):`<tr><td colspan='7' class='mut'>No private-tracker torrents yet.</td></tr>`}catch(e){$('#pstate').textContent='Status unavailable';$('#qstate').textContent='Status unavailable';$('#vstate').textContent='Status unavailable';$('#rstate').textContent='Status unavailable';$('#sstate').textContent='Status unavailable'}}
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
    steps = [
        ("Prepare the services", """<ul class='policy'>
 <li>A Prowlarr instance with at least one enabled <b>private torrent</b> indexer.</li>
 <li>A qBittorrent instance for permanent seeding, already routed through your VPN.</li>
 <li>A NAS-local download directory writable by rqbit and qBittorrent and
 readable by Stream Picker.</li>
 <li>PIA service credentials. Store them in a mode-<code>0600</code> env file,
 never in the Compose file or dashboard notes.</li>
</ul>"""),
        ("Deploy rqbit behind Gluetun on the storage host",
         """<p class='note'>Run the companion stack on the NAS—not on a machine
writing the destination over NFS. rqbit shares Gluetun's network namespace, so
it has no route that can bypass the PIA firewall.</p>
"""
         + f"<pre>{_esc(compose)}</pre>"
         + """<p class='note'>In <code>rqbit-pia.env</code>, set the NAS LAN bind
address, the NAS-local download directory, matching PUID/PGID, a generated rqbit
HTTP password, and a generated Gluetun control API key. Keep ports 3030 and 8000
LAN-only.</p>"""),
        ("Make all three paths resolve to the same files", """<ul class='policy'>
 <li>rqbit writes to its configured output path, commonly
 <code>/data/nuviodownloads</code>.</li>
 <li>qBittorrent's save path may have a different container spelling, but it
 must be the same physical NAS directory.</li>
 <li>Stream Picker mounts that directory read-only at
 <code>PRIVATE_TRACKER_DOWNLOAD_ROOT</code>.</li>
</ul>"""),
        ("Configure Stream Picker",
         """<p class='note'>Enter the values on the
<a href='/private-trackers'>Private Trackers</a> page. The progressive-specific
values are:</p>
"""
         + f"<pre>{_esc(settings)}</pre>"
         + """<p class='note'>Also enter the private Prowlarr connection,
qBittorrent connection and save path, Stream Picker read-only mount, and
dedicated qBittorrent category.</p>"""),
        ("Verify before restarting", """<ol class='policy'>
 <li>Press <b>Save private settings</b>.</li>
 <li>Press <b>Test connections</b> and require all five checks to pass:
 Prowlarr, PIA VPN, rqbit, qBittorrent, and storage.</li>
 <li>Restart Stream Picker so the saved settings become active.</li>
 <li>Open a private result. The first real media GET should create a stopped
 qBittorrent registration and start only the selected file in rqbit.</li>
</ol>"""),
        ("What happens during playback",
         """<p class='note'>rqbit prioritizes the ranges the player is reading.
When the selected file finishes, Stream Picker pauses rqbit, asks qBittorrent to
hash the same files in place, starts the remainder according to your
whole-torrent policy, and removes the torrent from rqbit without deleting
media.</p>"""),
    ]
    body = ["<p class='backlink'><a href='/private-trackers'>← Back to "
            "Private Trackers</a></p>", "<div class='steps'>"]
    for i, (title, content) in enumerate(steps, 1):
        body.append(
            f"<section class='card pad'><p class='eyebrow'>Step {i}</p>"
            f"<h3>{_esc(title)}</h3>{content}</section>")
    body.append("</div>")
    body.append(
        "<div class='callout warn' style='margin-top:12px'>"
        "<b>Fail-closed check:</b> if Gluetun reports anything other than "
        "<code>running</code>, Stream Picker refuses activation. Gluetun's "
        "firewall also blocks rqbit traffic at the network layer if the "
        "tunnel drops.</div>")
    head_block = (
        "<div class='pagehead'><p class='eyebrow'>SETUP GUIDE</p>"
        "<h1>Private tracker progressive setup</h1>"
        "<p class='sub'>This optional lane keeps local torrent downloads "
        "separate from debrid. rqbit starts playback while downloading "
        "through a fail-closed PIA tunnel; qBittorrent takes over the same "
        "files for completion and long-term seeding.</p></div>")
    return uitheme.shell(
        title="Private tracker setup", name=ADDON_NAME, active="private",
        csrf=adminui.csrf_token(),
        body=head_block + "".join(body), head=f"<style>{_CSS}</style>",
        robots="noindex,nofollow")


def render(metrics: dict) -> str:
    on = config.pending("PRIVATE_TRACKERS_ENABLED").lower() not in (
        "", "0", "false", "no", "off")
    whole_on = config.pending("PRIVATE_TRACKER_WHOLE_TORRENT").lower() not in (
        "", "0", "false", "no", "off")
    scores_json = config.pending("PRIVATE_TRACKER_INDEXER_SCORES") or "{}"
    thin = config.pending("PRIVATE_TRACKER_MIN_SOURCES") or config.default(
        "PRIVATE_TRACKER_MIN_SOURCES")
    events = metrics.get("events") or {}
    data = json.dumps({"metrics": metrics}, separators=(",", ":")) \
        .replace("<", "\\u003c")
    restart = config.restart_pending()
    body = f"""
<section class='card hot pad'><div class='switch'><div class='switchcopy'>
 <div class='switchtitle'>Private tracker downloads
 <span class='envk'>PRIVATE_TRACKERS_ENABLED</span></div>
 <div class='note'>Master on/off control for your local torrent lane.</div>
 </div><span class='masterstate' id='master_result'></span>
 <input class='swi' id='private_master' type='checkbox'
 data-key='PRIVATE_TRACKERS_ENABLED' data-saved='{'1' if on else '0'}'
 {'checked' if on else ''} aria-label='Enable private tracker downloads'></div>
 <div class='trigger'><div class='switchcopy'>
 <div class='switchtitle'>Search when public sources are thin
 <span class='envk'>PRIVATE_TRACKER_MIN_SOURCES</span></div>
 <div class='note' id='thin_note'></div>
 </div><span class='masterstate' id='thin_result'></span>
 <input class='thin' id='private_min_sources' type='number' min='-1' max='1000'
 step='1' data-key='PRIVATE_TRACKER_MIN_SOURCES' value='{_esc(thin)}'
 data-saved='{_esc(thin)}'
 aria-label='Public releases below which private trackers are also searched'>
 </div></section>
<div class='callout' style='margin-top:12px'><b>Progressive mode streams the
selected file through rqbit.</b> Once that file is complete, qBittorrent
rechecks the same files, finishes the release, and seeds indefinitely.</div>

<div class='cards' style='margin-top:12px'>
 <section class='card pad'><h3>Apply and verify</h3>
 <p class='note'>Tune this lane to match how you like to collect and seed.</p>
 <div class='btnrow' style='margin-top:12px'><button class='btn' id='save'>Save private settings</button>
 <button class='btn ghost' id='test'>Test connections</button>
 <button class='btn danger' id='restart' style='display:{'inline-block' if restart else 'none'}'>Restart addon</button></div>
 <div class='result' id='result'></div></section>
 <section class='card pad'><h3>Live status</h3>
 <div class='statusline'><span class='dot' id='pdot'></span><b>Prowlarr</b> <span class='mut' id='pstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='vdot'></span><b>PIA VPN</b> <span class='mut' id='vstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='rdot'></span><b>rqbit</b> <span class='mut' id='rstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='qdot'></span><b>qBittorrent seeder</b> <span class='mut' id='qstate'>Checking…</span></div>
 <div class='statusline'><span class='dot' id='sdot'></span><b>Storage</b> <span class='mut' id='sstate'>Checking…</span></div>
 </section>
</div>

{uitheme.section("GOVERNOR", "Download limits",
                 "caps applied before a torrent is ever offered")}
<section class='card'>
 <div class='row'><div><div class='lbl'>Download the whole torrent (100%)
  <span class='envk'>PRIVATE_TRACKER_WHOLE_TORRENT</span></div>
  <div class='desc'>Your clicked episode always downloads first and alone, so it
  streams in order. On (default): once it finishes, the rest of the pack
  resumes so the release completes and seeds — no hit-and-run. Off: the rest
  stays skipped and the torrent stays partial.</div></div>
  <div class='ctl'><input class='swi' type='checkbox' data-key='PRIVATE_TRACKER_WHOLE_TORRENT'
  {'checked' if whole_on else ''} aria-label='Download the whole torrent'></div></div>
 <div class='pad'>
 {_field('PRIVATE_TRACKER_MAX_TORRENT_GB','Maximum download size (GB) — skip torrents bigger than this',
         typ='number',minimum=0,maximum=100000,step='0.1')}
 <p class='note'>Private results larger than this are never offered, so a clicked
 torrent can't fill the disk with a huge UHD remux or multi-season pack. A clicked
 torrent is handed to qBittorrent after the watched file completes; qBittorrent
 then downloads the rest and seeds it. <b>0 = no limit.</b></p>
 {_field('PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS',
         'Maximum simultaneous downloads (0 = unlimited)',typ='number',
         minimum=0,maximum=1000)}
 <p class='note'>How many private torrents may download at once. Default: 3.</p>
 </div>
</section>

{uitheme.section("GUIDE", "Setup", "one-time progressive stack")}
<section class='card pad'>
 <div class='switchtitle'>Optional advanced setup</div>
 <p class='note'>The progressive path uses Prowlarr, rqbit behind a PIA/Gluetun
 kill switch, shared NAS storage, and qBittorrent for permanent seeding. Most
 users only do this once.</p>
 <a class='doclink' href='/private-trackers/setup'>Open the complete
 private-tracker setup guide {uitheme.icon('arrow-right')}</a>
</section>

{uitheme.section("CONNECTIONS", "Connections",
                 "Prowlarr, qBittorrent, rqbit + VPN, storage, search tuning")}
<div class='cards'>
 <section class='card pad'><h3>Private Prowlarr</h3>
 {_field('PRIVATE_PROWLARR_URL','Internal URL')}
 {_field('PRIVATE_PROWLARR_API_KEY','API key',secret=True)}</section>
 <section class='card pad'><h3>Private qBittorrent</h3>
 <p class='note'>Long-term completion and permanent seeding.</p>
 {_field('PRIVATE_QBITTORRENT_URL','Internal URL')}
 {_field('PRIVATE_QBITTORRENT_USERNAME','Username')}
 {_field('PRIVATE_QBITTORRENT_PASSWORD','Password',secret=True)}</section>
 <section class='card pad'><h3>Progressive downloader</h3>
 {_choice('PRIVATE_STREAM_ENGINE','Playback engine',
          (('rqbit','rqbit — progressive streaming'),
           ('qbittorrent','qBittorrent — compatibility')))}
 {_field('PRIVATE_RQBIT_URL','rqbit internal URL')}
 {_field('PRIVATE_RQBIT_USERNAME','rqbit username (optional)')}
 {_field('PRIVATE_RQBIT_PASSWORD','rqbit password (optional)',secret=True)}
 {_field('PRIVATE_RQBIT_OUTPUT_PATH','rqbit output path')}
 {_field('PRIVATE_RQBIT_VPN_URL','Gluetun control URL')}
 {_field('PRIVATE_RQBIT_VPN_API_KEY','Gluetun control API key',secret=True)}
 <p class='note'>Use the supplied rqbit + Gluetun stack on the NAS. Stream
 Picker requires an authenticated “VPN running” response before activation.
 rqbit's output path
 and qBittorrent's save path must point at the same physical directory.</p></section>
 <section class='card pad'><h3>Storage isolation</h3>
 {_field('PRIVATE_QBITTORRENT_SAVE_PATH','qBittorrent save path')}
 {_field('PRIVATE_TRACKER_DOWNLOAD_ROOT','Read-only Stream Picker mount')}
 {_field('PRIVATE_QBITTORRENT_CATEGORY','Dedicated category')}</section>
 <section class='card pad'><h3>Search tuning</h3>
 {_field('PRIVATE_TRACKER_CANDIDATES','Candidates shown',typ='number',
         minimum=1,maximum=1000)}
 <p class='note'>Up to this many eligible results are shown using your release
 preference below. Each row names its private tracker. Default: 20.</p>
 {_field('PRIVATE_TRACKER_MIN_SEEDERS','Minimum seeders (hard eligibility floor)',
         typ='number',minimum=0,maximum=10000)}
 <p class='note'>Results below this value are excluded before episode, season-pack,
 or whole-series preference is considered. Default: 5.</p>
 {_field('PRIVATE_TRACKER_SEARCH_TIMEOUT','Search timeout (seconds)',typ='number')}
 {_field('PRIVATE_TRACKER_START_TIMEOUT','Opening-piece wait (seconds)',typ='number')}
 {_field('PRIVATE_TRACKER_SEARCH_TTL','Search cache (seconds)',typ='number')}</section>
</div>

{uitheme.section("POLICY", "Your download policy",
                 "release-type order and fixed safety guarantees")}
<section class='card pad'>
 <div class='switchtitle'>Release preference</div>
 <p class='note'>Drag these blocks into your preferred order. Turn off a type
 to exclude it from private search results. Movies are unaffected.</p>
 {_release_policy_editor()}
 <div class='switchtitle'>Fixed safety guarantees</div><ul class='policy'>
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

{uitheme.section("TRACKERS", "Tracker preferences",
                 "scores read live from Prowlarr")}
<section class='card pad'>
 <div class='switchtitle'>Favorite trackers first</div>
 <p class='note'>Score each tracker Prowlarr searches — <b>1 = most preferred,
 100 = least, 50 = neutral</b>. Within a release type, results from
 higher-preference trackers are shown first. Leave everything at 50 to treat
 every tracker equally.</p>
 <div class='btnrow' style='margin-top:12px'><button class='btn ghost sm' type='button' id='pref_reset'>Reset all to 50</button>
 <span class='result' id='pref_state' style='margin-top:0'>Loading trackers…</span></div>
 <div class='prefboard' id='indexers'></div>
 <input type='hidden' id='PRIVATE_TRACKER_INDEXER_SCORES'
  data-key='PRIVATE_TRACKER_INDEXER_SCORES' value='{_esc(scores_json)}'>
 <p class='note'>Trackers are read live from Prowlarr. Save private settings and
 restart to apply new scores.</p>
</section>

{uitheme.section("TELEMETRY", "Metrics", "live, refreshes every 10 s")}
<div class='tiles'>
 <div class='tile'><div class='v' id='enabled'>—</div><div class='k'>runtime state</div></div>
 <div class='tile'><div class='v' id='ready'>—</div><div class='k'>setup readiness</div></div>
 <div class='tile'><div class='v' id='active'>—</div><div class='k'>active downloads</div></div>
 <div class='tile'><div class='v' id='complete'>—</div><div class='k'>completed torrents</div></div>
 {uitheme.tile(str(int(events.get('candidates', 0))), 'search candidates')}
 {uitheme.tile(str(int(events.get('clicked', 0))), 'click activations')}
 {uitheme.tile(str(int(events.get('start_failed', 0))), 'start failures')}
</div>

{uitheme.section("ACTIVITY", "Private downloads", "both engines, live")}
<div class='tblwrap'><table>
<thead><tr><th>Release</th><th>Engine</th><th>Progress</th><th>State</th><th>Download</th><th>Upload</th><th>Ratio</th></tr></thead>
<tbody id='downloads'><tr><td colspan='7' class='mut'>Checking torrent engines…</td></tr></tbody>
</table></div>"""
    scripts = (f"<script id='data' type='application/json'>{data}</script>"
               f"<script>{_JS}</script>")
    head_block = (
        "<div class='pagehead'><p class='eyebrow'>LOCAL LANE</p>"
        "<h1>Private Trackers</h1><p class='sub'>A deliberately isolated home "
        "for local downloads. Nothing in this lane is ever sent to debrid, and "
        "it is a great fit for private trackers. Browse freely—nothing "
        "starts downloading until you press play.</p></div>")
    return uitheme.shell(
        title="Private Trackers", name=ADDON_NAME, active="private",
        csrf=adminui.csrf_token(),
        body=head_block + body, head=f"<style>{_CSS}</style>",
        scripts=scripts, robots="noindex,nofollow")
