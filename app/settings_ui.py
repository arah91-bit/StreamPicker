"""Renders /{secret}/settings — the operator dashboard.

One page, two jobs: plug in every upstream service this instance depends on
(with a live Test per service, so a pasted key is verified before it's
trusted), and set the handful of behavior knobs worth adjusting — led by the
stream-path choice: cache on disk, pass through, or hand players direct
links. Values are written to config.json via app.config and take effect on
restart; the save bar walks the operator through save → restart.

Visual language matches the /stats page (same palette, same card grammar).
One rule carried throughout: machine truth — env keys, URLs, latencies,
values — is set in monospace; human copy is in the system sans.
"""

import html
import os

from app import adminui, config, knobs

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--mut:#6b6b66;
--line:#e6e6e2;--bad:#c0392b;--warn:#b8860b;--good:#2e7d5b;--accent:#3b6ea5;
--accent-soft:#eef3f9;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--fg:#e9e9e6;
--mut:#9a9a94;--line:#2c2f34;--bad:#ff6b5e;--warn:#e0b74a;--good:#5cc99a;
--accent:#6ea3d8;--accent-soft:#232c37;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
padding:24px 16px 120px}
.wrap{max-width:1000px;margin:0 auto}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 20px;font-size:13px}
.sub code{font:12px var(--mono)}
.navlink{font-size:13px;color:var(--accent);text-decoration:none;
border:1px solid var(--line);border-radius:20px;padding:5px 12px;
background:var(--card);white-space:nowrap}
.navlink:hover{border-color:var(--accent)}
h2{font-size:16px;margin:30px 0 4px}
.blurb{color:var(--mut);font-size:13px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px}

.row{display:flex;justify-content:space-between;align-items:center;gap:24px;
padding:14px 16px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row.off{opacity:.45;pointer-events:none}
.lbl{font-weight:600}
.desc{color:var(--mut);font-size:12.5px;max-width:520px}
.envk{font:10.5px var(--mono);color:var(--mut);opacity:.65;margin-left:8px}
.ctl{display:flex;align-items:center;gap:10px;flex-shrink:0}
output{font:13px var(--mono);min-width:64px;text-align:right}

.swi{appearance:none;-webkit-appearance:none;width:42px;height:24px;margin:0;
border-radius:99px;background:var(--line);position:relative;cursor:pointer;
transition:background .15s;flex-shrink:0}
.swi::before{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;
border-radius:50%;background:var(--card);box-shadow:0 1px 2px rgba(0,0,0,.25);
transition:transform .15s}
.swi:checked{background:var(--accent)}
.swi:checked::before{transform:translateX(18px)}
input[type=range]{accent-color:var(--accent);width:190px}
select,input[type=text],input[type=password],input[type=url],textarea{
background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font:13px var(--mono);width:100%}
select{width:auto;font:14px inherit}
textarea{resize:vertical;min-height:74px;white-space:pre;overflow-x:auto}
input::placeholder{color:var(--mut);opacity:.8}

.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;
overflow:hidden;background:var(--bg)}
.seg label{cursor:pointer}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg span{display:block;padding:8px 16px;font-size:13.5px;color:var(--mut);
border-right:1px solid var(--line);transition:background .15s,color .15s}
.seg label:last-child span{border-right:0}
.seg input:checked+span{background:var(--accent);color:#fff}
.seg input:focus-visible+span{outline:2px solid var(--accent);outline-offset:-2px}

.pathcard{padding:18px 16px 4px}
.schema{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
font:12.5px var(--mono);margin:16px 2px 10px;min-height:44px}
.node{border:1px solid var(--line);border-radius:8px;padding:7px 13px;
background:var(--bg);transition:opacity .2s}
.node small{display:block;font-size:10px;color:var(--mut);letter-spacing:.04em;
text-transform:uppercase}
.node.hot{border-color:var(--accent);background:var(--accent-soft)}
.schema .arrow{color:var(--mut)}
.schema.direct .mid{display:none}
.modecap{color:var(--mut);font-size:12.5px;margin:0 2px 14px;max-width:640px}
.modecap b{color:var(--warn);font-weight:600}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));
gap:12px}
.conn{padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.chead{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.cname{font-weight:600}
.crole{color:var(--mut);font-size:12px;margin-top:1px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--line);
flex-shrink:0;margin-top:6px}
.dot.run{background:var(--accent);animation:pulse 1s infinite}
.dot.ok{background:var(--good)}
.dot.bad{background:var(--bad)}
@keyframes pulse{50%{opacity:.35}}
.f label{display:block;font-size:11.5px;color:var(--mut);margin:0 0 3px}
.hint{font-size:11px;color:var(--mut);opacity:.8;margin-top:3px}
.cfoot{display:flex;align-items:center;gap:10px;margin-top:2px}
.tres{font:11.5px var(--mono);color:var(--mut);overflow-wrap:anywhere}
.tres.ok{color:var(--good)}.tres.bad{color:var(--bad)}

.btn{font:600 13.5px inherit;color:#fff;background:var(--accent);border:0;
border-radius:8px;padding:8px 16px;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent);
border:1px solid var(--line)}
.btn:disabled{opacity:.5;cursor:default}
.btn.warn{background:var(--bad)}

.savebar[hidden]{display:none}
.savebar{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;
display:flex;align-items:center;gap:14px;background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:10px 16px;
box-shadow:0 6px 24px rgba(0,0,0,.14);z-index:10;max-width:92vw}
.savebar .msg{font-size:13.5px}
.savebar .msg b{font-weight:600}
.savebar .err{color:var(--bad);font-size:12.5px;max-width:340px}

.mono{font-family:var(--mono)}
.lbl.mono{font-size:12.5px}
details.adv{margin-top:36px;border-top:1px solid var(--line);padding-top:6px}
details.adv>summary{cursor:pointer;padding:12px 2px;list-style:none;
display:flex;align-items:baseline;gap:12px}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary::before{content:'▸';color:var(--mut);font-size:11px}
details.adv[open]>summary::before{content:'▾'}
.advtitle{font-size:16px;font-weight:600}
.advhint{color:var(--mut);font-size:12.5px}
.advtools{display:flex;gap:10px;align-items:center;margin:4px 2px 16px;
flex-wrap:wrap}
#advsearch{flex:1;min-width:220px;max-width:380px;font:13px inherit;
padding:8px 10px}
.advgroup{margin-bottom:18px}
.advgroup[hidden]{display:none}
.advh{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--mut);margin:0 2px 8px;font-weight:600}
.advin{width:150px;text-align:right}
.unit{font:12px var(--mono);color:var(--mut);min-width:26px}
.adv-row[hidden]{display:none}
.nomatch{color:var(--mut);font-size:13px;padding:8px 2px}
.addonrow{display:flex;align-items:center;gap:10px;padding:11px 0;
border-bottom:1px solid var(--line)}
.addonrow:last-of-type{border-bottom:0}
.addoninfo{flex:1;min-width:0}
.addonname{font-weight:600;font-size:14px}
.addonurl{display:block;font:11.5px var(--mono);color:var(--mut);overflow-wrap:anywhere}
.addon-del{background:none;border:0;color:var(--mut);font-size:21px;line-height:1;
cursor:pointer;padding:0 4px}
.addon-del:hover{color:var(--bad)}
.addonadd{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px;
padding-top:14px;border-top:1px solid var(--line)}
.addonadd input{flex:1;min-width:150px}
.addonempty{color:var(--mut);font-size:13px;padding:2px 0 4px}

:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
@media (max-width:640px){
 .row{flex-direction:column;align-items:flex-start;gap:8px}
 .ctl{width:100%;justify-content:space-between}
 input[type=range]{flex:1}
}
"""

_JS = """
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const CAP={
 cache:"Streams are pulled through the addon and read ahead onto local NVMe. "+
  "Seeking back is instant, twins share one download, and a dying source is "+
  "swapped mid-stream without the player noticing. The cache is wiped on restart.",
 proxy:"Streams pass through the addon byte-for-byte. Start-of-play failover "+
  "and playback stats still work; nothing is stored on disk.",
 direct:"Players get source URLs and fetch them directly. Lightest on the "+
  "server, but <b>no failover, no playback stats, and direct-usenet results "+
  "are dropped</b> (their URLs carry credentials and only work through the addon)."};

function ctlValue(el){return el.type==='checkbox'?(el.checked?'1':'0'):el.value}
function dirtyControls(){
 return $$('[data-key]').filter(el=>{
  if(el.dataset.secret)return el.value!=='';
  return ctlValue(el)!==el.dataset.init});
}
function refreshBar(){
 const n=dirtyControls().length,bar=$('#savebar');
 if(n){bar.hidden=false;$('#barmsg').innerHTML=
   `<b>${n} unsaved change${n>1?'s':''}</b>`;
  $('#savebtn').hidden=false;$('#restartbtn').hidden=true;}
 else if(bar.dataset.restart==='1'){bar.hidden=false;
  $('#barmsg').innerHTML='Saved — <b>restart to apply</b>';
  $('#savebtn').hidden=true;$('#restartbtn').hidden=false;}
 else bar.hidden=true;
 $('#barerr').textContent='';
}

/* stream-path mode: one control writing two stored keys */
function setMode(m,init){
 $('#h_PROXY_PLAYBACK').value=(m==='direct')?'0':'1';
 $('#h_PROXY_BUFFER').value=(m==='cache')?'1':'0';
 $('#modecap').innerHTML=CAP[m];
 const sch=$('#schema');sch.classList.toggle('direct',m==='direct');
 $('#nodesub').textContent=(m==='cache')?'nvme read-ahead':'pass-through';
 $('#nodeaddon').classList.toggle('hot',m==='cache');
 $$('.row.cacheonly').forEach(r=>{r.classList.toggle('off',m!=='cache');
  r.querySelectorAll('input').forEach(i=>i.disabled=(m!=='cache'))});
 if(!init)refreshBar();
}
$$('input[name=streammode]').forEach(r=>r.addEventListener('change',
 ()=>setMode(r.value,false)));

document.addEventListener('input',e=>{
 if(e.target.matches('input[type=range]')){
  const o=e.target.closest('.ctl').querySelector('output');
  if(o)o.textContent=e.target.value+(e.target.dataset.unit||'');}
 if(e.target.dataset.key!==undefined||e.target.name==='streammode')refreshBar();
});

async function post(url,body){
 const r=await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||('HTTP '+r.status));
 return r.json();
}

$('#savebtn').addEventListener('click',async()=>{
 const values={};dirtyControls().forEach(el=>values[el.dataset.key]=ctlValue(el));
 $('#savebtn').disabled=true;
 try{
  const res=await post('/api/settings/save',{values});
  dirtyControls().forEach(el=>{
   if(el.dataset.secret){el.dataset.init='';el.placeholder='kept · just saved';el.value='';}
   else el.dataset.init=ctlValue(el);});
  $('#savebar').dataset.restart=res.restart_needed?'1':'0';
 }catch(e){$('#barerr').textContent=e.message;}
 $('#savebtn').disabled=false;refreshBar();
});

$('#restartbtn').addEventListener('click',async()=>{
 let playing=0;
 try{playing=(await(await fetch('/api/settings/status.json')).json()).playing}catch(e){}
 const q=playing>0
  ?`${playing} stream${playing>1?'s':''} playing right now will be cut off. Restart anyway?`
  :'Restart the addon now? It comes back in a few seconds.';
 if(!confirm(q))return;
 $('#restartbtn').disabled=true;$('#barmsg').textContent='Restarting…';
 try{await post('/api/settings/restart')}catch(e){}
 const t0=Date.now();
 (async function poll(){
  if(Date.now()-t0>45000){$('#barmsg').textContent=
   'Still down — check the container logs.';return;}
  await new Promise(r=>setTimeout(r,1200));
  try{const r=await fetch('/health',{cache:'no-store'});
   if(r.ok)return location.reload();}catch(e){}
  poll();})();
});

$$('.test').forEach(btn=>btn.addEventListener('click',async()=>{
 const card=btn.closest('.conn'),svc=btn.dataset.service;
 const dot=card.querySelector('.dot'),res=card.querySelector('.tres');
 const values={};card.querySelectorAll('[data-key]').forEach(el=>{
  if(el.dataset.secret&&el.value==='')return;values[el.dataset.key]=el.value;});
 dot.className='dot run';res.className='tres';res.textContent='testing…';
 btn.disabled=true;
 try{const r=await post('/api/settings/test/'+svc,{values});
  dot.className='dot '+(r.ok?'ok':'bad');
  res.className='tres '+(r.ok?'ok':'bad');
  res.textContent=`${r.ms} ms · ${r.detail}`;
 }catch(e){dot.className='dot bad';res.className='tres bad';
  res.textContent=e.message;}
 btn.disabled=false;
}));

/* custom addons: a small editable list writing one hidden EXTRA_ADDONS field */
const addonval=$('#addonval');
function hesc(s){return (s||'').replace(/[&<>"']/g,c=>(
 {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
let ADDONS=[];try{ADDONS=JSON.parse(addonval.value||'[]')||[]}catch(e){ADDONS=[]}
function renderAddons(){
 const list=$('#addonlist');
 if(!ADDONS.length){list.innerHTML=
   "<div class='addonempty'>No custom addons yet — add one below.</div>";return;}
 list.innerHTML=ADDONS.map((a,i)=>
  `<div class='addonrow' data-i='${i}'><div class='addoninfo'>`+
  `<span class='addonname'>${hesc(a.name)}</span>`+
  `<span class='addonurl'>${hesc(a.url)}</span></div>`+
  `<span class='dot'></span>`+
  `<button type='button' class='btn ghost addon-test' data-i='${i}'>Test</button>`+
  `<button type='button' class='addon-del' data-i='${i}' title='Remove'>×</button>`+
  `<span class='tres'></span></div>`).join('');
}
function addonsSync(){
 addonval.value=JSON.stringify(ADDONS);
 addonval.dispatchEvent(new Event('input',{bubbles:true}));
 renderAddons();
}
$('#addon_add').addEventListener('click',()=>{
 const name=$('#addon_name').value.trim(),url=$('#addon_url').value.trim();
 if(!url)return;
 ADDONS.push({name:name||url,url});
 $('#addon_name').value='';$('#addon_url').value='';addonsSync();
});
$('#addon_url').addEventListener('keydown',e=>{if(e.key==='Enter')$('#addon_add').click();});
$('#addonlist').addEventListener('click',async e=>{
 const del=e.target.closest('.addon-del');
 if(del){ADDONS.splice(+del.dataset.i,1);addonsSync();return;}
 const t=e.target.closest('.addon-test');
 if(!t)return;
 const row=t.closest('.addonrow'),dot=row.querySelector('.dot'),
   res=row.querySelector('.tres');
 dot.className='dot run';res.className='tres';res.textContent='testing…';t.disabled=true;
 try{const r=await post('/api/settings/test/addon',{values:{url:ADDONS[+t.dataset.i].url}});
  dot.className='dot '+(r.ok?'ok':'bad');res.className='tres '+(r.ok?'ok':'bad');
  res.textContent=`${r.ms} ms · ${r.detail}`;
 }catch(err){dot.className='dot bad';res.className='tres bad';res.textContent=err.message;}
 t.disabled=false;
});
renderAddons();

const advsearch=$('#advsearch');
if(advsearch)advsearch.addEventListener('input',()=>{
 const q=advsearch.value.trim().toLowerCase();
 $$('.adv-row').forEach(r=>{r.hidden=!!q&&!r.dataset.hay.includes(q);});
 let anyGroup=false;
 $$('.advgroup').forEach(g=>{
  const shown=[...g.querySelectorAll('.adv-row')].some(r=>!r.hidden);
  g.hidden=!shown;anyGroup=anyGroup||shown;});
 $('#advnomatch').hidden=anyGroup;
});

setMode(document.querySelector('input[name=streammode]:checked').value,true);
refreshBar();
"""


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def _row(spec: dict) -> str:
    key = spec["key"]
    val = config.pending(key)
    extra = " cacheonly" if spec.get("mode") == "cache" else ""
    left = (f"<div><span class='lbl'>{_esc(spec['label'])}</span>"
            f"<span class='envk'>{_esc(key)}</span>"
            f"<div class='desc'>{_esc(spec.get('desc', ''))}</div></div>")
    t = spec["type"]
    if t == "bool":
        on = val.strip().lower() not in ("", "0", "false", "no", "off")
        ctl = (f"<input type='checkbox' class='swi' data-key='{key}' "
               f"data-init='{'1' if on else '0'}' {'checked' if on else ''}>")
    elif t == "number":
        ctl = (f"<input type='range' min='{spec['min']}' max='{spec['max']}' "
               f"step='{spec['step']}' value='{_esc(val)}' data-key='{key}' "
               f"data-init='{_esc(val)}' data-unit='{spec['unit']}'>"
               f"<output>{_esc(val)}{spec['unit']}</output>")
    elif t == "choice":
        opts = "".join(
            f"<option value='{_esc(v)}' {'selected' if v == val else ''}>"
            f"{_esc(lbl)}</option>" for v, lbl in spec["choices"])
        ctl = (f"<select data-key='{key}' data-init='{_esc(val)}'>{opts}"
               f"</select>")
    else:
        ctl = (f"<input type='text' value='{_esc(val)}' data-key='{key}' "
               f"data-init='{_esc(val)}' style='width:280px'>")
    return (f"<div class='row{extra}'>{left}"
            f"<div class='ctl'>{ctl}</div></div>")


def _settings_section(group: str, title: str, blurb: str) -> str:
    rows = "".join(_row(s) for s in config.SETTINGS
                   if s["group"] == group and not s.get("hidden"))
    if not rows:
        return ""
    return (f"<h2>{_esc(title)}</h2><p class='blurb'>{_esc(blurb)}</p>"
            f"<div class='card'>{rows}</div>")


def _stream_mode() -> str:
    playback = config.pending("PROXY_PLAYBACK").strip().lower() \
        not in ("", "0", "false", "no", "off")
    buffer_on = config.pending("PROXY_BUFFER").strip().lower() \
        not in ("", "0", "false", "no", "off")
    mode = "direct" if not playback else ("cache" if buffer_on else "proxy")

    def seg(value, label):
        chk = "checked" if value == mode else ""
        return (f"<label><input type='radio' name='streammode' "
                f"value='{value}' {chk}><span>{label}</span></label>")

    init_pb = "1" if playback else "0"
    init_buf = "1" if buffer_on else "0"
    rows = "".join(_row(s) for s in config.SETTINGS
                   if s["group"] == "stream" and not s.get("hidden"))
    return f"""
<h2>Stream path</h2><p class='blurb'>How bytes get from a source to the player.
<span class='envk'>PROXY_PLAYBACK · PROXY_BUFFER</span></p>
<div class='card'><div class='pathcard'>
<div class='seg' role='radiogroup' aria-label='Stream path'>
{seg('cache', 'Cache on disk')}{seg('proxy', 'Pass through')}{seg('direct', 'Direct links')}
</div>
<input type='hidden' id='h_PROXY_PLAYBACK' data-key='PROXY_PLAYBACK'
 data-init='{init_pb}' value='{init_pb}'>
<input type='hidden' id='h_PROXY_BUFFER' data-key='PROXY_BUFFER'
 data-init='{init_buf}' value='{init_buf}'>
<div class='schema' id='schema'>
 <span class='node'>source</span>
 <span class='arrow mid'>─▶</span>
 <span class='node mid' id='nodeaddon'>addon<small id='nodesub'></small></span>
 <span class='arrow'>─▶</span>
 <span class='node'>player</span>
</div>
<p class='modecap' id='modecap'></p>
</div>{rows}</div>"""


def _adv_row(spec: dict) -> str:
    """A tuning knob in the Advanced section. The env key is the label (this is
    the developer-facing surface); numeric/text fields show the override you've
    set, with the code default as placeholder so an unset field reads as 'on
    default'."""
    key, t, unit = spec["key"], spec["type"], spec["unit"]
    hay = _esc(f"{key} {spec['blurb']}".lower())
    left = (f"<div><span class='lbl mono'>{_esc(key)}</span>"
            f"<div class='desc'>{_esc(spec['blurb'])}</div></div>")
    if t == "bool":
        on = config.pending(key).strip().lower() not in (
            "", "0", "false", "no", "off")
        ctl = (f"<input type='checkbox' class='swi' data-key='{key}' "
               f"data-init='{'1' if on else '0'}' {'checked' if on else ''}>")
    else:
        override = config.stored(key)
        dflt = spec["default"]
        ph = f"default {dflt}" if dflt else "default: unset"
        u = f"<span class='unit'>{_esc(unit)}</span>" if unit else ""
        ctl = (f"<input type='text' inputmode='decimal' class='advin' "
               f"data-key='{key}' data-init='{_esc(override)}' "
               f"value='{_esc(override)}' placeholder='{_esc(ph)}' "
               f"spellcheck='false' autocomplete='off'>{u}")
    return (f"<div class='row adv-row' data-hay='{hay}'>{left}"
            f"<div class='ctl'>{ctl}</div></div>")


def _custom_addons() -> str:
    val = config.pending("EXTRA_ADDONS")     # JSON string, or ""
    return (
        "<h2>Custom addons <span class='advhint'>AIOStreams, a usenet addon, "
        "any player stream source</span></h2>"
        "<p class='blurb'>Paste any player addon's manifest URL. Its results "
        "join the same search, run through the same playback verification, and "
        "only streams that actually play reach you — everything is ranked "
        "together by quality, so order doesn't matter.</p>"
        "<div class='card' style='padding:14px 16px'>"
        "<div id='addonlist'></div>"
        "<div class='addonadd'>"
        "<input id='addon_name' type='text' autocomplete='off' "
        "placeholder='Name (e.g. AIOStreams)'>"
        "<input id='addon_url' type='text' spellcheck='false' autocomplete='off' "
        "placeholder='https://…/manifest.json'>"
        "<button type='button' class='btn ghost' id='addon_add'>Add</button>"
        "</div>"
        f"<input type='hidden' data-key='EXTRA_ADDONS' data-init='{_esc(val)}' "
        f"value='{_esc(val)}' id='addonval'>"
        "</div>")


def _advanced_section() -> str:
    groups = []
    for gid, title in knobs.GROUPS:
        rows = [_adv_row(s) for s in knobs.by_group(gid)]
        if not rows:
            continue
        groups.append(f"<div class='advgroup' data-group='{gid}'>"
                      f"<div class='advh'>{_esc(title)}</div>"
                      f"<div class='card'>{''.join(rows)}</div></div>")
    return (
        "<details class='adv' id='adv'><summary>"
        "<span class='advtitle'>Advanced tuning</span>"
        "<span class='advhint'>every remaining knob — timeouts, budgets, "
        "thresholds. You don't need these to get started.</span></summary>"
        "<div class='advtools'>"
        "<input id='advsearch' type='search' "
        "placeholder='Filter by name or description…' aria-label='Filter knobs'>"
        "<a class='navlink' href='/api/settings/export.env'>"
        "Download current .env</a></div>"
        f"{''.join(groups)}"
        "<div class='nomatch' id='advnomatch' hidden>No knob matches that.</div>"
        "</details>")


def _conn_card(conn: dict) -> str:
    fields = []
    for f in conn["fields"]:
        key, kind = f["key"], f.get("kind", "text")
        val = config.pending(key)
        hint = (f"<div class='hint'>{_esc(f['hint'])}</div>"
                if f.get("hint") else "")
        if kind == "secret":
            ph = config.mask(val) or "not set"
            inp = (f"<input type='password' data-key='{key}' data-secret='1' "
                   f"data-init='' placeholder='{_esc(ph)}' "
                   f"autocomplete='new-password'>")
        elif kind == "multiline":
            shown = val.replace(";", "\n")
            inp = (f"<textarea data-key='{key}' data-init='{_esc(shown)}' "
                   f"rows='{max(2, shown.count(chr(10)) + 1)}' "
                   f"spellcheck='false'>{_esc(shown)}</textarea>")
        else:
            inp = (f"<input type='text' data-key='{key}' "
                   f"data-init='{_esc(val)}' value='{_esc(val)}' "
                   f"spellcheck='false' autocomplete='off'>")
        fields.append(f"<div class='f'><label>{_esc(f['label'])}"
                      f"<span class='envk'>{_esc(key)}</span></label>"
                      f"{inp}{hint}</div>")
    return (f"<div class='card conn'><div class='chead'>"
            f"<div><span class='cname'>{_esc(conn['name'])}</span>"
            f"<div class='crole'>{_esc(conn['role'])}</div></div>"
            f"<span class='dot'></span></div>"
            f"{''.join(fields)}"
            f"<div class='cfoot'><button class='btn ghost test' "
            f"data-service='{conn['id']}'>Test</button>"
            f"<span class='tres'></span></div></div>")


def render() -> str:
    restart = "1" if config.restart_pending() else "0"
    sections = _stream_mode()
    for gid, title, blurb in config.GROUPS:
        if gid == "stream":
            continue
        sections += _settings_section(gid, title, blurb)
    cards = "".join(_conn_card(c) for c in config.CONNECTIONS)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{_esc(ADDON_NAME)} — settings</title>
<style>{_CSS}{adminui.NAV_CSS}</style></head>
<body><div class="wrap">
{adminui.nav('settings', ADDON_NAME)}
<h1>Settings</h1>
<p class="sub">Connect your services and choose how streams are handled.
Saved to <code>/data/config.json</code> on the addon's data volume; changes
apply on restart. Anyone deploying their own instance starts here.</p>
{sections}
<h2>Connections</h2>
<p class="blurb">Every upstream service this instance uses. Test verifies the
values in the form — including keys you haven't saved yet. Leave a masked
field blank to keep the stored key.</p>
<div class="cards">{cards}</div>
{_custom_addons()}
{_advanced_section()}
</div>
<div class="savebar" id="savebar" hidden data-restart="{restart}">
<span class="msg" id="barmsg"></span><span class="err" id="barerr"></span>
<button class="btn" id="savebtn">Save changes</button>
<button class="btn warn" id="restartbtn" hidden>Restart addon</button>
</div>
<script>{_JS}</script></body></html>"""
