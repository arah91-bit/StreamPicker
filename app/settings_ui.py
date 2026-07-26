"""Shared settings machinery for the /connect, /tune, and / pages.

This module holds the control renderers and all of the client-side save /
test / restart logic the three pages share:

- ``_row`` / ``_settings_section`` / ``_stream_mode`` — the behavior-knob
  controls used by tune_ui (/tune)
- ``_advanced_section`` — the full remaining-knob catalog, also /tune
- ``_lane_masters`` — the four stream-lane master switches (home + connect)
- ``_scrapers`` — the unified Sources panel (debrid keys, Prowlarr, scraper
  engines, custom addons), on /connect
- ``_conn_fields`` / ``_conn_configured`` — connection field generation and
  state, on /connect
- ``_savebar`` — the save → restart bar all three pages share
- ``search_index`` — the Ctrl/⌘K palette index fed to uitheme.shell
- ``_CSS`` / ``_JS`` — the shared page styles and client logic (dirty
  tracking via [data-key], /api/settings/save, per-service Test, restart)

Everything writes to config.json via app.config and takes effect on restart;
every control is equally settable by editing the file (or asking an AI to).
"""

import json

from app import config, debrid, knobs, scrapers, uitheme

_CSS = """
/* page-specific layout — tokens & common components live in uitheme.BASE_CSS */

/* lane master switches */
.lane-masters{margin:0 0 18px}
.lane-master{display:flex;align-items:center;gap:14px;padding:13px 16px;
border-bottom:1px solid var(--line)}
.lane-master:last-child{border-bottom:0}
.lane-master .mastercopy{flex:1;min-width:0}
.lane-master .mastertitle{font-weight:650}
.lane-master .masterdesc{color:var(--mut);font-size:12.5px}
.lane-master .masterstate{font:11.5px var(--mono);color:var(--mut);
min-width:70px;text-align:right}
.lane-master .masterstate.ok{color:var(--ok)}
.lane-master .masterstate.bad{color:var(--bad)}

.blurb{color:var(--mut);font-size:13px;margin:-6px 0 12px;max-width:72ch}
.blurb .envk{margin-left:4px}

/* stream path diagram */
.pathcard{padding:18px 16px 6px}
.schema{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
font:12.5px var(--mono);margin:16px 2px 10px;min-height:44px}
.node{border:1px solid var(--line);border-radius:8px;padding:7px 13px;
background:var(--inset);transition:opacity .2s,border-color .2s,background .2s}
.node small{display:block;font-size:10px;color:var(--mut);letter-spacing:.04em;
text-transform:uppercase}
.node.hot{border-color:var(--accent);background:var(--accent-soft)}
.schema .arrow{color:var(--mut)}
.schema.direct .mid{display:none}
.modecap{color:var(--mut);font-size:12.5px;margin:0 2px 14px;max-width:640px}
.modecap b{color:var(--warn);font-weight:600}

/* slider+number combo (max bitrate) */
.brctl{display:flex;align-items:center;gap:10px}
.brctl .br-range{width:150px}
.br-num{width:64px;text-align:right}
.br-num::-webkit-outer-spin-button,.br-num::-webkit-inner-spin-button{margin:0}
.brctl output{min-width:72px;text-align:right}

/* connection cards */
details.acc>.cards{margin:2px 0 16px}
.conn{padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.chead{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.cname{font-weight:600}
.crole{color:var(--mut);font-size:12px;margin-top:1px}
.keylink{font-size:12px;white-space:nowrap}
.f label{display:block;font-size:11.5px;color:var(--mut);margin:0 0 3px}
.hint{font-size:11px;color:var(--mut);opacity:.85;margin-top:3px}
.cfoot{display:flex;align-items:center;gap:10px;margin-top:2px}
.tres{font:11.5px var(--mono);color:var(--mut);overflow-wrap:anywhere}
.tres.ok{color:var(--ok)}.tres.bad{color:var(--bad)}

/* sources panel: debrid rows, engine rows, custom addons */
.srcsub{font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.14em;
color:var(--accent2);margin:0 0 8px}
.srcsub2{margin-top:20px;padding-top:14px;border-top:1px solid var(--line)}
.srcsub .advhint{font:400 12px var(--sans);text-transform:none;letter-spacing:0;
color:var(--mut)}
.badge2{font:600 10px var(--mono);letter-spacing:.07em;text-transform:uppercase;
color:var(--accent);background:var(--accent-soft);border-radius:5px;
padding:4px 8px;flex-shrink:0;white-space:nowrap}
.debridrow{display:flex;align-items:center;gap:10px;padding:10px 0;
border-bottom:1px solid var(--line)}
.debridrow:last-of-type{border-bottom:0}
.debridname{font-weight:600;font-size:14px;min-width:92px}
.debridkey{flex:1;min-width:120px}
.debridadd{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px;
padding-top:14px;border-top:1px solid var(--line)}
.debridadd input{flex:1;min-width:140px}
.debridfoot{display:flex;align-items:center;gap:10px;margin-top:12px;flex-wrap:wrap}
.debridres{font:11.5px var(--mono);color:var(--mut);overflow-wrap:anywhere;flex:1}
.debridres.ok{color:var(--ok)}.debridres.bad{color:var(--bad)}
.engrow{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;
padding:12px 0;border-bottom:1px solid var(--line)}
.engrow:last-of-type{border-bottom:0}
.engrow .swi,.engrow .badge2{margin-top:2px}
.engrow .dot{margin-top:9px}
.enginfo{flex:1;min-width:120px}
.engname{font-weight:600;font-size:14px}
.engblurb{color:var(--mut);font-size:12.5px;margin-top:2px}
.engwarn{color:var(--warn);font-size:12px;margin-top:5px}
.engurl{margin-top:8px}
/* fill the info column — the default intrinsic input width truncates a
   manifest URL to about two words */
.engurl input{font-size:12px;width:100%;max-width:520px}

.engrow .tres{flex-basis:100%;text-align:right}
.addonrow{display:flex;align-items:center;gap:10px;padding:11px 0;
border-bottom:1px solid var(--line)}
.addonrow:last-of-type{border-bottom:0}
.addoninfo{flex:1;min-width:0}
.addonname{font-weight:600;font-size:14px}
.addonurl{display:block;font:11.5px var(--mono);color:var(--mut);
overflow-wrap:anywhere}
.addon-del{background:none;border:0;color:var(--mut);font-size:21px;line-height:1;
cursor:pointer;padding:0 4px}
.addon-del:hover{color:var(--bad)}
.addonadd{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px;
padding-top:14px;border-top:1px solid var(--line)}
.addonadd input{flex:1;min-width:150px}
.addonempty{color:var(--mut);font-size:13px;padding:2px 0 4px}

/* advanced tuning */
#adv{margin-top:36px}
.advtools{display:flex;gap:10px;align-items:center;margin:10px 2px 16px;
flex-wrap:wrap}
#advsearch{flex:1;min-width:220px;max-width:380px}
.advgroup{margin-bottom:18px}
.advgroup[hidden]{display:none}
.advh{font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.14em;
color:var(--accent2);margin:0 2px 8px}
.advin{width:150px;text-align:right}
.unit{font:12px var(--mono);color:var(--mut);min-width:26px}
.adv-row[hidden]{display:none}
.nomatch{color:var(--mut);font-size:13px;padding:8px 2px}
.lbl.mono{font-size:12.5px}

/* decisions page: left section-nav layout */
.slayout{display:grid;grid-template-columns:200px minmax(0,1fr);gap:28px;
align-items:start}
.sidenav{position:sticky;top:16px;display:flex;flex-direction:column;gap:2px}
.sidenav .sn-cap{font:600 10.5px var(--mono);letter-spacing:.14em;
text-transform:uppercase;color:var(--accent2);margin:0 0 8px 12px}
.sidenav a{display:block;padding:8px 12px;border-radius:8px;color:var(--mut);
font-size:13.5px;text-decoration:none;border-left:2px solid transparent}
.sidenav a:hover{color:var(--fg);background:var(--track)}
.sidenav a.on{color:var(--accent);background:var(--accent-soft);
border-left-color:var(--accent)}
.bsec[hidden]{display:none}

@media (max-width:840px){
 .slayout{grid-template-columns:1fr}
 .sidenav{position:static;flex-direction:row;flex-wrap:wrap;gap:4px;
 margin:0 0 16px}
 .sidenav .sn-cap{display:none}
}

@media (max-width:640px){
 .row{flex-direction:column;align-items:flex-start;gap:8px}
 .ctl{width:100%;justify-content:space-between}
 input[type=range]{flex:1}
}

"""

INDEXER_CSS = """
/* usenet indexer row editor — a name/url/key triple per row */
.ixrows{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
.ixrow{display:grid;grid-template-columns:minmax(110px,1fr) minmax(150px,2fr)
minmax(110px,1fr) auto;gap:8px;align-items:center}
.ixrow input{font-size:12.5px}
.ixdel{display:inline-flex;align-items:center;justify-content:center;
width:32px;height:32px;flex-shrink:0;border:1px solid var(--line);
border-radius:var(--r-s);background:var(--card);color:var(--mut);cursor:pointer;
transition:color .12s,border-color .12s}
.ixdel:hover{color:var(--bad);border-color:var(--bad)}
@media (max-width:640px){.ixrow{grid-template-columns:1fr auto;
grid-template-areas:'name del' 'url url' 'key key';gap:6px}
.ixrow .ixname{grid-area:name}.ixrow .ixurl{grid-area:url}
.ixrow .ixkey{grid-area:key}.ixrow .ixdel{grid-area:del}
.ixrow+.ixrow{border-top:1px solid var(--line);padding-top:10px}}
"""

_CSS = _CSS + INDEXER_CSS

INDEXER_JS = """
/* usenet indexer rows → the hidden [data-key] value the save path reads.
   An untouched saved row keeps its data-orig index and serialises to
   @keep:<i>, which the server resolves against the stored spec — that is how
   a key nobody retyped survives without ever being sent to the browser. */
function ixSync(ed){
 const parts=[...ed.querySelectorAll('.ixrow')].map(r=>{
  const name=r.querySelector('.ixname').value.trim();
  const url=r.querySelector('.ixurl').value.trim();
  const typed=r.querySelector('.ixkey').value.trim();
  const orig=r.dataset.orig;
  const key=typed||(orig!==undefined?'@keep:'+orig:'');
  return (name||url||key)?[name,url,key].join('|'):'';
 }).filter(Boolean);
 ed.querySelector('.ixvalue').value=parts.join(';');
 if(typeof refreshBar==='function')refreshBar();
}
document.querySelectorAll('.ixeditor').forEach(ed=>{
 ed.addEventListener('input',e=>{
  if(e.target.closest('.ixrow')){
   /* typing a replacement key drops the row's claim on the saved one */
   if(e.target.classList.contains('ixkey')&&e.target.value!=='')
    delete e.target.closest('.ixrow').dataset.orig;
   ixSync(ed);}
 });
 ed.addEventListener('click',e=>{
  const del=e.target.closest('.ixdel');
  if(del){const rows=ed.querySelectorAll('.ixrow');
   if(rows.length>1)del.closest('.ixrow').remove();
   else del.closest('.ixrow').querySelectorAll('input')
    .forEach(i=>{i.value='';});
   delete del.closest('.ixrow')?.dataset.orig;ixSync(ed);return;}
  if(e.target.closest('.ixadd')){
   const first=ed.querySelector('.ixrow');
   const row=first.cloneNode(true);
   delete row.dataset.orig;
   row.querySelectorAll('input').forEach(i=>{i.value='';
    if(i.classList.contains('ixkey'))i.placeholder='API key';});
   ed.querySelector('.ixrows').appendChild(row);
   row.querySelector('.ixname').focus();ixSync(ed);}
 });
});
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
 const n=dirtyControls().length;
 $$('.savebar').forEach(bar=>{
  const msg=bar.querySelector('.msg'),save=bar.querySelector('.savebtn'),
    restart=bar.querySelector('.restartbtn');
  if(n){bar.hidden=false;msg.innerHTML=`<b>${n} unsaved change${n>1?'s':''}</b>`;
   save.hidden=false;restart.hidden=true;}
  else if(bar.dataset.restart==='1'){bar.hidden=false;
   msg.innerHTML='Saved — <b>restart to apply</b>';
   save.hidden=true;restart.hidden=false;}
  else bar.hidden=true;
  bar.querySelector('.err').textContent='';});
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
 if(e.target.matches('input[type=range]')&&!e.target.closest('.brctl')){
  const o=e.target.closest('.ctl').querySelector('output');
  if(o)o.textContent=e.target.value+(e.target.dataset.unit||'');}
 if(e.target.dataset.key!==undefined||e.target.name==='streammode')refreshBar();
});

/* Max-bitrate style control: a slider and a number box drive one saved value,
   and its floor (0) reads as an off-switch label ("Unlimited") instead of "0". */
function brSync(box,from){
 const rng=box.querySelector('.br-range'),num=box.querySelector('.br-num'),
   out=box.querySelector('output');
 if(from==='range')num.value=rng.value; else rng.value=(num.value||0);
 const v=parseFloat(num.value||'0');
 out.textContent=(!v)?num.dataset.zero:(num.value+num.dataset.unit);
}
$$('.brctl').forEach(box=>{
 box.querySelector('.br-range').addEventListener('input',
  ()=>{brSync(box,'range');refreshBar();});
 box.querySelector('.br-num').addEventListener('input',()=>brSync(box,'num'));
});

async function post(url,body){
 const r=await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json','X-CSRF-Token':
   document.querySelector('[data-csrf]').dataset.csrf},body:JSON.stringify(body||{})});
 if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||('HTTP '+r.status));
 return r.json();
}

/* Stream-lane master switches save independently so toggling a lane cannot
   submit or clear unrelated credential fields. */
$$('.lane-master-toggle').forEach(master=>master.addEventListener('change',async()=>{
 const state=$('#'+master.dataset.state),before=master.dataset.init;
 master.disabled=true;state.className='masterstate';state.textContent='saving…';
 try{const values={};values[master.dataset.key]=master.checked?'1':'0';
  const res=await post('/api/settings/save',{values});
  master.dataset.init=master.checked?'1':'0';
  state.className='masterstate ok';state.textContent='saved · restart';
  $$('.savebar').forEach(b=>b.dataset.restart=res.restart_needed?'1':'0');
 }catch(e){master.checked=before==='1';state.className='masterstate bad';
  state.textContent=e.message;}
 master.disabled=false;refreshBar();
}));

async function doSave(){
 const values={};dirtyControls().forEach(el=>values[el.dataset.key]=ctlValue(el));
 const saves=$$('.savebar .savebtn');saves.forEach(b=>b.disabled=true);
 try{
  const res=await post('/api/settings/save',{values});
  dirtyControls().forEach(el=>{
   if(el.dataset.secret){el.dataset.init='';el.placeholder='kept · just saved';el.value='';}
   else el.dataset.init=ctlValue(el);});
  $$('.savebar').forEach(b=>b.dataset.restart=res.restart_needed?'1':'0');
 }catch(e){$$('.savebar .err').forEach(el=>el.textContent=e.message);}
 saves.forEach(b=>b.disabled=false);refreshBar();
}
$$('.savebar .savebtn').forEach(b=>b.addEventListener('click',doSave));

async function doRestart(){
 let playing=0;
 try{playing=(await(await fetch('/api/settings/status.json')).json()).playing}catch(e){}
 const q=playing>0
  ?`${playing} stream${playing>1?'s':''} playing right now will be cut off. Restart anyway?`
  :'Restart the addon now? It comes back in a few seconds.';
 if(!confirm(q))return;
 $$('.savebar .restartbtn').forEach(b=>b.disabled=true);
 $$('.savebar .msg').forEach(m=>m.textContent='Restarting…');
 try{await post('/api/settings/restart')}catch(e){}
 const t0=Date.now();
 (async function poll(){
  if(Date.now()-t0>45000){$$('.savebar .msg').forEach(m=>m.textContent=
   'Still down — check the container logs.');return;}
  await new Promise(r=>setTimeout(r,1200));
  try{const r=await fetch('/health',{cache:'no-store'});
   if(r.ok)return location.reload();}catch(e){}
  poll();})();
}
$$('.savebar .restartbtn').forEach(b=>b.addEventListener('click',doRestart));

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

/* Sources catalog: one panel POSTing {debrids, engines} to
   /api/settings/scrapers, which mints every enabled engine from the central
   debrid key and rewrites the runtime source keys server-side. Stored keys
   never reach the browser — a blank debrid key means "keep the stored one". */
function hesc(s){return (s||'').replace(/[&<>"']/g,c=>(
 {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function slug(s){return (s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-')
 .replace(/^-+|-+$/g,'')||'addon';}
const SBOX=$('#scrapers');
if(SBOX){
 const DATA=JSON.parse($('#scrapers-data').textContent);
 const PROV={};DATA.providers.forEach(p=>PROV[p.id]=p);
 const ENG={};DATA.engines.forEach(e=>ENG[e.id]=e);
 let DROWS=DATA.debrids.map(id=>({service:id,hasKey:true,key:''}));
 const ST={};DATA.engines.forEach(e=>ST[e.id]={on:false,url:''});
 let CUSTOM=[];
 DATA.enabled.forEach(en=>{
  if(ENG[en.id])ST[en.id]={on:true,url:en.url||''};
  else CUSTOM.push({name:en.name||'',url:en.url||''});});
 const PR={url:(DATA.prowlarr&&DATA.prowlarr.url)||'',
           hasKey:!!(DATA.prowlarr&&DATA.prowlarr.has_key),key:''};
 const havePr=()=>!!(PR.url&&(PR.hasKey||PR.key));
 const haveDebrid=()=>DROWS.length>0;
 // Why a toggle is grayed out: its Prowlarr backend or a debrid key is missing.
 const gateReason=e=>{
  if(e.needs_prowlarr&&!havePr())return 'prowlarr';
  if(e.needs_debrid&&!haveDebrid()&&!ST[e.id].url)return 'debrid';
  return '';};
 const gated=e=>!!gateReason(e);
 function setRes(msg,cls){const r=$('#scrapersres');
  r.className='debridres'+(cls?' '+cls:'');r.textContent=msg||'';}

 /* debrid key list */
 const used=()=>new Set(DROWS.map(r=>r.service));
 function renderPicker(){
  const u=used(),avail=DATA.providers.filter(p=>!u.has(p.id));
  $('#debrid_pick').innerHTML=avail.length
   ?avail.map(p=>`<option value='${p.id}'>${hesc(p.label)}</option>`).join('')
   :"<option value=''>all added</option>";
  $('#debrid_pick').disabled=$('#debrid_add').disabled=!avail.length;
  $('#debrid_newkey').disabled=!avail.length;syncKeyLink();}
 function syncKeyLink(){
  const p=PROV[$('#debrid_pick').value],link=$('#debrid_keylink');
  if(p){link.href=p.key_url;link.textContent='where is my '+p.label+' key?';
   link.hidden=false;$('#debrid_newkey').placeholder='paste your '+p.label+' API key';}
  else link.hidden=true;}
 function renderDebrids(){
  const list=$('#debridlist');
  if(!DROWS.length){list.innerHTML=
    "<div class='addonempty'>No debrid yet — add one below to power the scrapers.</div>";
   renderPicker();return;}
  list.innerHTML=DROWS.map((r,i)=>{const p=PROV[r.service];
   const ph=r.hasKey?'kept · hidden — blank keeps it':'paste API key';
   return `<div class='debridrow' data-i='${i}'>`+
    `<span class='badge2'>${hesc(p.badge)}</span>`+
    `<span class='debridname'>${hesc(p.label)}</span>`+
    `<input type='password' class='debridkey' data-i='${i}' `+
    `autocomplete='new-password' spellcheck='false' `+
    `placeholder='${ph}' value='${hesc(r.key)}'>`+
    `<button type='button' class='addon-del' data-i='${i}' `+
    `title='Remove'>&times;</button></div>`;}).join('');
  renderPicker();}
 $('#debrid_pick').addEventListener('change',syncKeyLink);
 $('#debrid_add').addEventListener('click',()=>{
  const id=$('#debrid_pick').value,key=$('#debrid_newkey').value.trim();
  if(!id)return;
  if(!key)return setRes('Paste the API key for '+PROV[id].label+'.','bad');
  DROWS.push({service:id,hasKey:false,key});
  $('#debrid_newkey').value='';setRes('');renderDebrids();renderEngines();});
 $('#debrid_newkey').addEventListener('keydown',
  e=>{if(e.key==='Enter')$('#debrid_add').click();});
 $('#debridlist').addEventListener('input',e=>{
  const k=e.target.closest('.debridkey');if(k)DROWS[+k.dataset.i].key=k.value;});
 $('#debridlist').addEventListener('click',e=>{
  const del=e.target.closest('.addon-del');if(!del)return;
  DROWS.splice(+del.dataset.i,1);setRes('');renderDebrids();renderEngines();});

 /* scraper engine toggles */
 function renderEngines(){
  $('#enginelist').innerHTML=DATA.engines.map(e=>{
   const st=ST[e.id],g=gated(e),on=st.on&&!g;
   const showUrl=!e.internal&&(e.custom_only||st.url!=='');
   const ph=e.custom_only?'paste your configured manifest URL'
     :'custom manifest URL (optional)';
   const warn=gateReason(e)==='prowlarr'
     ?'Add your Prowlarr above to switch this on.'
     :'Add a debrid key above to switch this on.';
   return `<div class='engrow' data-id='${e.id}'>`+
    `<input type='checkbox' class='swi engtoggle' data-id='${e.id}' `+
     `${on?'checked':''} ${g?'disabled':''}>`+
    `<span class='badge2'>${hesc(e.badge)}</span>`+
    `<div class='enginfo'>`+
     `<div class='engname'>${hesc(e.label)} `+
      `<a class='keylink' href='${hesc(e.docs)}' target='_blank' `+
      `rel='noopener noreferrer'>docs</a></div>`+
     `<div class='engblurb'>${hesc(e.blurb)}</div>`+
     (g?`<div class='engwarn'>${warn}</div>`:'')+
     (e.internal?'':`<div class='engurl' ${showUrl?'':'hidden'}>`+
      `<input class='enginput' data-id='${e.id}' spellcheck='false' `+
       `autocomplete='off' placeholder='${ph}' value='${hesc(st.url)}'></div>`)+
    `</div>`+
    `<span class='dot'></span>`+
    (e.internal?'':`<button type='button' class='btn ghost engadv' `+
      `data-id='${e.id}'>URL</button>`)+
    `<button type='button' class='btn ghost engtest' data-id='${e.id}'>Test</button>`+
    `<span class='tres'></span></div>`;}).join('');}
 $('#enginelist').addEventListener('change',e=>{
  const t=e.target.closest('.engtoggle');if(t)ST[t.dataset.id].on=t.checked;});
 $('#enginelist').addEventListener('input',e=>{
  const i=e.target.closest('.enginput');if(i)ST[i.dataset.id].url=i.value.trim();});
 $('#enginelist').addEventListener('click',e=>{
  const adv=e.target.closest('.engadv');
  if(adv){const u=adv.closest('.engrow').querySelector('.engurl');
   if(u){u.hidden=!u.hidden;if(!u.hidden)u.querySelector('input').focus();}return;}
  const t=e.target.closest('.engtest');if(!t)return;
  const row=t.closest('.engrow'),id=t.dataset.id;
  if(ENG[id]&&ENG[id].internal){        // Prowlarr source → test its backend
   testProwlarr(t,row.querySelector('.dot'),row.querySelector('.tres'));return;}
  testOne(id,ST[id].url,t,row.querySelector('.dot'),row.querySelector('.tres'));});

 /* custom addons (folded in) */
 function renderCustom(){
  const list=$('#customlist');
  list.innerHTML=CUSTOM.map((c,i)=>
   `<div class='addonrow' data-i='${i}'><div class='addoninfo'>`+
   `<span class='addonname'>${hesc(c.name||c.url)}</span>`+
   `<span class='addonurl'>${hesc(c.url)}</span></div>`+
   `<span class='dot'></span>`+
   `<button type='button' class='btn ghost custom-test' data-i='${i}'>Test</button>`+
   `<button type='button' class='addon-del' data-i='${i}' title='Remove'>&times;</button>`+
   `<span class='tres'></span></div>`).join('');}
 $('#custom_add').addEventListener('click',()=>{
  const name=$('#custom_name').value.trim(),url=$('#custom_url').value.trim();
  if(!url)return;
  CUSTOM.push({name:name||url,url});
  $('#custom_name').value='';$('#custom_url').value='';renderCustom();});
 $('#custom_url').addEventListener('keydown',
  e=>{if(e.key==='Enter')$('#custom_add').click();});
 $('#customlist').addEventListener('click',e=>{
  const del=e.target.closest('.addon-del');
  if(del){CUSTOM.splice(+del.dataset.i,1);renderCustom();return;}
  const t=e.target.closest('.custom-test');if(!t)return;
  const row=t.closest('.addonrow'),c=CUSTOM[+t.dataset.i];
  testOne('custom-'+slug(c.name||c.url),c.url,t,
   row.querySelector('.dot'),row.querySelector('.tres'));});

 /* test one engine/addon — the server mints it from the current debrid rows */
 async function testOne(id,url,btnEl,dotEl,resEl){
  const debrids=DROWS.map(r=>({service:r.service,key:(r.key||'').trim()}));
  dotEl.className='dot run';resEl.className='tres';resEl.textContent='testing…';
  btnEl.disabled=true;
  try{const r=await post('/api/settings/test/scraper',{values:{id,url,debrids}});
   dotEl.className='dot '+(r.ok?'ok':'bad');resEl.className='tres '+(r.ok?'ok':'bad');
   resEl.textContent=`${r.ms} ms · ${r.detail}`;
  }catch(err){dotEl.className='dot bad';resEl.className='tres bad';
   resEl.textContent=err.message;}
  btnEl.disabled=false;}

 /* Prowlarr backend: one URL + key, tested against its own indexer API. A blank
    key reuses the stored one. Shared by the Prowlarr block button and the
    Prowlarr source row's Test button. */
 async function testProwlarr(btnEl,dotEl,resEl){
  dotEl.className='dot run';resEl.className='tres';resEl.textContent='testing…';
  btnEl.disabled=true;
  try{const r=await post('/api/settings/test/prowlarr',
        {values:{PROWLARR_URL:PR.url,PROWLARR_API_KEY:PR.key}});
   dotEl.className='dot '+(r.ok?'ok':'bad');resEl.className='tres '+(r.ok?'ok':'bad');
   resEl.textContent=`${r.ms} ms · ${r.detail}`;
  }catch(err){dotEl.className='dot bad';resEl.className='tres bad';
   resEl.textContent=err.message;}
  btnEl.disabled=false;}
 function initProwlarr(){
  $('#prowlarr_url').value=PR.url;
  if(PR.hasKey)$('#prowlarr_key').placeholder='kept · hidden — blank keeps it';
  $('#prowlarr_url').addEventListener('input',ev=>{
   PR.url=ev.target.value.trim();renderEngines();});
  $('#prowlarr_key').addEventListener('input',ev=>{
   PR.key=ev.target.value.trim();renderEngines();});
  $('#prowlarr_test').addEventListener('click',()=>testProwlarr(
   $('#prowlarr_test'),$('#prowlarr_dot'),$('#prowlarr_res')));}

 /* collect enabled engines + custom addons, then save/test-keys together */
 function collectEngines(){
  const out=[];
  DATA.engines.forEach(e=>{const st=ST[e.id];
   if(st.on&&!gated(e))out.push(st.url?{id:e.id,url:st.url}:{id:e.id});});
  CUSTOM.forEach(c=>out.push({id:'custom-'+slug(c.name||c.url),
   name:c.name,url:c.url}));
  return out;}
 async function sendScrapers(dry){
  const debrids=DROWS.map(r=>({service:r.service,key:(r.key||'').trim()}));
  const btn=dry?$('#scrapers_test'):$('#scrapers_save');
  btn.disabled=true;setRes(dry?'testing…':'saving…');
  try{
   const res=await post('/api/settings/scrapers',
     {debrids,engines:collectEngines(),
      prowlarr:{url:PR.url,api_key:PR.key},dry_run:dry});
   const parts=Object.entries(res.results||{}).map(([k,v])=>
    (v.ok===false?'✗ ':v.ok===true?'✓ ':'• ')+(PROV[k]?PROV[k].label:k));
   if(dry)setRes(parts.join('   ')||'no checkable keys — save to apply',
     res.ok?'ok':'bad');
   else if(res.ok){
    DROWS=DROWS.map(r=>({service:r.service,hasKey:true,key:''}));renderDebrids();
    PR.hasKey=PR.url?(PR.hasKey||!!PR.key):false;PR.key='';
    $('#prowlarr_key').value='';
    $('#prowlarr_key').placeholder=PR.hasKey?'kept · hidden — blank keeps it'
     :'Prowlarr API key';
    renderEngines();
    setRes('Saved — restart to apply.','ok');
    $$('.savebar').forEach(b=>b.dataset.restart='1');refreshBar();}
   else setRes('Rejected: '+parts.join('   '),'bad');
  }catch(e){setRes(e.message,'bad');}
  btn.disabled=false;}
 $('#scrapers_test').addEventListener('click',()=>sendScrapers(true));
 $('#scrapers_save').addEventListener('click',()=>sendScrapers(false));
 renderDebrids();renderEngines();renderCustom();initProwlarr();
}

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

/* decisions page: one section at a time behind the left nav. All sections
   stay in the DOM (so dirty-tracking and save see every control); without
   JS nothing is hidden and the page reads top to bottom.
   Each link carries data-sec (the section to reveal) and an href anchor to
   scroll to — that is how the advanced sub-groups get their own nav rows. */
const snav=$('.sidenav');
if(snav){
 const links=[...snav.querySelectorAll('a[data-sec]')];
 const secs=$$('.bsec');
 function go(a){
  const sec=a.dataset.sec,tgt=a.getAttribute('href').slice(1);
  secs.forEach(s=>{s.hidden=(s.id!==sec)});
  links.forEach(x=>x.classList.toggle('on',x===a));
  if(sec==='sec-advanced'){const adv=document.getElementById('adv');
   if(adv)adv.open=true;}
  const el=tgt!==sec?document.getElementById(tgt):null;
  if(el)el.scrollIntoView({block:'start'});else window.scrollTo(0,0);
 }
 links.forEach(a=>a.addEventListener('click',e=>{e.preventDefault();
  history.replaceState(null,'',a.getAttribute('href'));go(a);}));
 const h=location.hash||'';
 go(links.find(a=>a.getAttribute('href')===h)||links[0]);
}

const sm0=document.querySelector('input[name=streammode]:checked');
if(sm0)setMode(sm0.value,true);
refreshBar();
""" + INDEXER_JS


_esc = uitheme.esc


def _row(spec: dict) -> str:
    key = spec["key"]
    val = config.pending(key)
    extra = " cacheonly" if spec.get("mode") == "cache" else ""
    left = (f"<div><span class='lbl'>{_esc(spec['label'])}</span>"
            f"<span class='envk'>{_esc(key)}</span>"
            f"<div class='desc'>{_esc(spec.get('desc', ''))}</div></div>")
    t = spec["type"]
    if config.is_secret(key):
        ph = config.mask(val, key) or (
            "uses ADDON_SECRET" if key == "ADMIN_PASSWORD" else "not set")
        ctl = (f"<input type='password' data-key='{key}' data-secret='1' "
               f"data-init='' placeholder='{_esc(ph)}' "
               f"autocomplete='new-password' style='width:280px'>")
    elif t == "bool":
        on = val.strip().lower() not in ("", "0", "false", "no", "off")
        ctl = (f"<input type='checkbox' class='swi' data-key='{key}' "
               f"data-init='{'1' if on else '0'}' {'checked' if on else ''}>")
    elif t == "number" and spec.get("zero_label"):
        # A slider you can also type into; its floor doubles as an off switch
        # ("Unlimited" at 0). The number box carries data-key (the saved value);
        # the range only mirrors it. See the .brctl handlers in _JS.
        zero, unit = spec["zero_label"], spec["unit"]
        disp = zero if val.strip() in ("", "0") else f"{_esc(val)}{_esc(unit)}"
        ctl = (f"<div class='brctl'>"
               f"<input type='range' class='br-range' min='{spec['min']}' "
               f"max='{spec['max']}' step='{spec['step']}' value='{_esc(val)}' "
               f"aria-label='{_esc(spec['label'])}'>"
               f"<input type='number' class='br-num' min='{spec['min']}' "
               f"max='{spec['max']}' step='{spec['step']}' value='{_esc(val)}' "
               f"data-key='{key}' data-init='{_esc(val)}' "
               f"data-unit='{_esc(unit)}' data-zero='{_esc(zero)}' "
               f"aria-label='{_esc(spec['label'])} value'>"
               f"<output>{disp}</output></div>")
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


# Short thematic eyebrows for the tally-bar section headers, keyed by
# config.GROUPS id (the "stream" group is rendered by _stream_mode instead).
_EYEBROW = {"picking": "RANKING", "acquire": "FALLBACK", "identity": "IDENTITY"}


def _settings_section(group: str, title: str, blurb: str) -> str:
    rows = "".join(_row(s) for s in config.SETTINGS
                   if s["group"] == group and not s.get("hidden"))
    if not rows:
        return ""
    return (uitheme.section(_EYEBROW.get(group, group.upper()), title, blurb)
            + f"<div class='card'>{rows}</div>")


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
{uitheme.section("STREAM PATH", "Stream path",
                 "How bytes get from a source to the player. "
                 "PROXY_PLAYBACK · PROXY_BUFFER")}
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


def _advanced_section() -> str:
    groups = []
    for gid, title in knobs.GROUPS:
        rows = [_adv_row(s) for s in knobs.by_group(gid)]
        if not rows:
            continue
        # id is stable API: /tune#adv-<gid> is what the section nav targets
        groups.append(f"<div class='advgroup' id='adv-{gid}' "
                      f"data-group='{gid}'>"
                      f"<div class='advh'>{_esc(title)}</div>"
                      f"<div class='card'>{''.join(rows)}</div></div>")
    return (
        "<details class='acc' id='adv'><summary>"
        "<span class='acc-t'>Advanced tuning</span>"
        "<span class='acc-hint'>every remaining knob — timeouts, budgets, "
        "thresholds. You don't need these to get started.</span></summary>"
        "<div class='advtools'>"
        "<input id='advsearch' type='search' "
        "placeholder='Filter by name or description…' aria-label='Filter knobs'>"
        "<a class='btn ghost sm' href='/api/settings/export.env'>"
        "Download current .env</a></div>"
        f"{''.join(groups)}"
        "<div class='nomatch' id='advnomatch' hidden>No knob matches that.</div>"
        "</details>")


def _indexer_row(orig: int | None, name: str = "", url: str = "",
                 masked: str = "") -> str:
    """One indexer: name, API URL, API key. A saved row's key box is empty
    with the stored key masked as its placeholder — leave it alone and the row
    serialises to @keep:<orig>, so nothing has to be retyped."""
    keep = "" if orig is None else f" data-orig='{orig}'"
    ph = _esc(masked) if masked else "API key"
    return (f"<div class='ixrow'{keep}>"
            f"<input class='ixname' type='text' value='{_esc(name)}' "
            f"placeholder='Name (e.g. NZBgeek)' spellcheck='false' "
            f"autocomplete='off' aria-label='Indexer name'>"
            f"<input class='ixurl' type='text' value='{_esc(url)}' "
            f"placeholder='https://api.example.com/api' spellcheck='false' "
            f"autocomplete='off' aria-label='Indexer API URL'>"
            f"<input class='ixkey' type='password' value='' "
            f"placeholder='{ph}' autocomplete='new-password' "
            f"aria-label='Indexer API key'>"
            f"<button type='button' class='ixdel' aria-label='Remove indexer'>"
            f"{uitheme.icon('trash', size=15)}</button></div>")


def _indexer_editor(key: str) -> str:
    """The NZB_INDEXERS editor: one row per indexer instead of a pipe-delimited
    blob. A hidden [data-key] input carries the serialised value, so save,
    dirty-tracking and Test all work exactly as they do for any other field."""
    rows, init = [], []
    for i, (name, url, api_key) in enumerate(
            config.parse_indexers(config.pending(key))):
        # Tail-only mask (not the whole-field "hidden") — the blob was
        # sensitive because it mixed a URL with a key; here the key stands
        # alone, and a four-character tail is what every other secret shows.
        rows.append(_indexer_row(i, name, url, config.mask(api_key)))
        init.append(f"{name}|{url}|@keep:{i}")
    if not rows:
        rows.append(_indexer_row(None))
    return (f"<div class='ixeditor'>"
            f"<div class='ixrows'>{''.join(rows)}</div>"
            f"<button type='button' class='btn ghost sm ixadd'>"
            f"{uitheme.icon('plus', size=14)}Add indexer</button>"
            f"<input type='hidden' class='ixvalue' data-key='{key}' "
            f"data-init='{_esc(';'.join(init))}' "
            f"value='{_esc(';'.join(init))}'></div>")


def _conn_fields(conn: dict) -> str:
    """The labeled inputs for one connection (secret-masked, dirty-tracked).
    Shared by the /connect catalog card and any future connection surface."""
    fields = []
    for f in conn["fields"]:
        key, kind = f["key"], f.get("kind", "text")
        val = config.pending(key)
        hint = (f"<div class='hint'>{_esc(f['hint'])}</div>"
                if f.get("hint") else "")
        if kind == "indexers":
            inp = _indexer_editor(key)
        elif config.is_secret(key):
            ph = config.mask(val, key) or "not set"
            tag = "textarea" if kind == "multiline" else "input"
            if tag == "textarea":
                inp = (f"<textarea data-key='{key}' data-secret='1' data-init='' "
                       f"placeholder='{_esc(ph)}' rows='2' spellcheck='false' "
                       f"autocomplete='off'></textarea>")
            else:
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
    return "".join(fields)


def _conn_configured(conn: dict) -> bool:
    """Whether any of a connection's fields currently holds a value."""
    return any(config.pending(f["key"]).strip() for f in conn["fields"])


def _scrapers() -> str:
    """The unified Sources catalog: a central debrid-key editor on top, then a
    toggle per scraper engine (each minted from that key), a custom-addon adder,
    and one Save that rewrites FAST_BASE_URL / STREMTHRU_BASE_URL /
    MEDIAFUSION_BASE_URL / EXTRA_ADDONS + SCRAPERS server-side. Stored keys are
    never emitted — only which providers/engines are configured (a custom URL a
    user pasted themselves is echoed back so they can edit it)."""
    fast = config.pending("FAST_BASE_URL")
    stremthru = config.pending("STREMTHRU_BASE_URL")
    mediafusion = config.pending("MEDIAFUSION_BASE_URL")
    ids = [d["service"] for d in debrid.current(fast)]
    have = set(ids)
    for d in debrid.stremthru_current(stremthru):
        if d["service"] not in have:
            ids.append(d["service"])
            have.add(d["service"])
    data = json.dumps({
        "providers": [{"id": p["id"], "label": p["label"], "badge": p["badge"],
                       "key_url": debrid.signup_url(p)}
                      for p in debrid.PROVIDERS],
        "debrids": ids,
        "engines": scrapers.engine_meta(),
        "enabled": scrapers.current(fast, stremthru, mediafusion,
                                    config.pending("EXTRA_ADDONS"),
                                    config.pending("SCRAPERS"),
                                    config.pending("PROWLARR_SOURCE")),
        # Prowlarr backend: the URL is safe to echo; the key is a secret, so the
        # panel only learns whether one is stored (blank submit keeps it).
        "prowlarr": {"url": config.pending("PROWLARR_URL"),
                     "has_key": bool(config.pending("PROWLARR_API_KEY"))}},
        separators=(",", ":"))
    return (
        uitheme.section("CATALOG", "Sources",
                        "debrid-powered scrapers, indexers, custom addons")
        + "<p class='blurb'>Your debrid key powers every scraper here — add it "
        "once, then switch on the ones you want. Each engine can take your own "
        "manifest URL instead of the default.</p>"
        "<div class='card' id='scrapers' style='padding:14px 16px'>"
        "<div class='srcsub'>Debrid services</div>"
        "<div id='debridlist'></div>"
        "<div class='debridadd'>"
        "<select id='debrid_pick' aria-label='Debrid provider'></select>"
        "<input id='debrid_newkey' type='password' autocomplete='new-password' "
        "spellcheck='false' placeholder='API key'>"
        "<button type='button' class='btn ghost' id='debrid_add'>Add</button>"
        "<a id='debrid_keylink' class='keylink' target='_blank' "
        "rel='noopener noreferrer' hidden></a></div>"
        "<div class='srcsub srcsub2'>Prowlarr "
        "<span class='advhint'>your own indexer backend — optional</span></div>"
        "<div class='debridadd' style='margin-top:8px'>"
        "<input id='prowlarr_url' type='text' spellcheck='false' "
        "autocomplete='off' placeholder='http://prowlarr:9696'>"
        "<input id='prowlarr_key' type='password' autocomplete='new-password' "
        "spellcheck='false' placeholder='Prowlarr API key'>"
        "<button type='button' class='btn ghost' id='prowlarr_test'>Test</button>"
        "<span class='dot' id='prowlarr_dot'></span>"
        "<span class='tres' id='prowlarr_res'></span></div>"
        "<p class='blurb' style='margin:6px 0 0'>Add it once — MediaFusion and "
        "the Prowlarr source below both use it. Comet reads Prowlarr from its "
        "own container environment, so point it at Prowlarr in your compose, "
        "not here.</p>"
        "<div class='srcsub srcsub2'>Scrapers</div>"
        "<div id='enginelist'></div>"
        "<div class='srcsub srcsub2'>Custom addon "
        "<span class='advhint'>any other player stream source</span></div>"
        "<div id='customlist'></div>"
        "<div class='addonadd'>"
        "<input id='custom_name' type='text' autocomplete='off' "
        "placeholder='Name (e.g. AIOStreams)'>"
        "<input id='custom_url' type='text' spellcheck='false' autocomplete='off' "
        "placeholder='https://…/manifest.json'>"
        "<button type='button' class='btn ghost' id='custom_add'>Add</button></div>"
        "<div class='debridfoot'>"
        "<button type='button' class='btn ghost' id='scrapers_test'>Test keys</button>"
        "<button type='button' class='btn' id='scrapers_save'>Save sources</button>"
        "<span class='debridres' id='scrapersres'></span></div>"
        f"<script type='application/json' id='scrapers-data'>{data}</script>"
        "</div>")


def _savebar(restart: str, *, top: bool = False) -> str:
    """The save → restart bar. One near the top of the page (sticky) and one
    at the bottom; _JS keeps all of them in sync."""
    cls = "savebar top" if top else "savebar"
    return (f'<div class="{cls}" hidden data-restart="{restart}">'
            '<span class="msg"></span><span class="err"></span>'
            '<button class="btn savebtn">Save changes</button>'
            '<button class="btn danger restartbtn" hidden>Restart addon</button>'
            '</div>')


def _lane_masters() -> str:
    """Every stream lane's master switch. Each saves itself immediately and
    independently (see the .lane-master-toggle handler in _JS) so flipping a
    lane never submits or clears unrelated credential fields.

    Private trackers are a lane like any other, so the switch lives here too —
    its trackers, ratios and safety rules stay on /private-trackers, but the
    on/off belongs with its siblings."""
    def lane_on(key: str) -> bool:
        return config.pending(key).lower() not in (
            "", "0", "false", "no", "off")

    public_on = lane_on("PUBLIC_TRACKERS_ENABLED")
    private_on = lane_on("PRIVATE_TRACKERS_ENABLED")
    https_on = lane_on("HTTPS_STREAMS_ENABLED")
    jellyfin_on = lane_on("JELLYFIN_ENABLED")
    usenet_on = lane_on("USENET_ENABLED")
    return f"""
<section class="card lane-masters">
 <div class="lane-master"><div class="mastercopy"><div class="mastertitle">Public tracker searches</div>
 <div class="masterdesc">One switch for Comet, StremThru, MediaFusion,
 native Prowlarr, and every custom online addon. Every other lane below
 stays independent of this one.</div></div>
 <span class="masterstate" id="public_trackers_state"></span>
 <input class="swi lane-master-toggle" id="public_trackers_master" type="checkbox"
 data-key="PUBLIC_TRACKERS_ENABLED" data-state="public_trackers_state"
 data-init="{'1' if public_on else '0'}" {'checked' if public_on else ''}
 aria-label="Enable all public tracker searches"></div>
 <div class="lane-master"><div class="mastercopy"><div class="mastertitle">Private tracker downloads</div>
 <div class="masterdesc">Your isolated local lane — downloads run on this box
 and never touch debrid. Trackers, release order and seeding rules live on
 <a href="/private-trackers">Trackers</a>.</div></div>
 <span class="masterstate" id="private_trackers_state"></span>
 <input class="swi lane-master-toggle" id="private_trackers_master" type="checkbox"
 data-key="PRIVATE_TRACKERS_ENABLED" data-state="private_trackers_state"
 data-init="{'1' if private_on else '0'}" {'checked' if private_on else ''}
 aria-label="Enable private tracker downloads"></div>
 <div class="lane-master"><div class="mastercopy"><div class="mastertitle">Direct HTTPS streams</div>
 <div class="masterdesc">Enable or disable direct HTTP(S) playback sources
 from custom addons independently of torrent/debrid results.</div></div>
 <span class="masterstate" id="https_master_state"></span>
 <input class="swi lane-master-toggle" id="https_master" type="checkbox"
 data-key="HTTPS_STREAMS_ENABLED" data-state="https_master_state"
 data-init="{'1' if https_on else '0'}" {'checked' if https_on else ''}
 aria-label="Enable direct HTTPS stream sources"></div>
 <div class="lane-master"><div class="mastercopy"><div class="mastertitle">Jellyfin library</div>
 <div class="masterdesc">Enable or disable Jellyfin as a local stream source
 without removing its saved connection.</div></div>
 <span class="masterstate" id="jellyfin_master_state"></span>
 <input class="swi lane-master-toggle" id="jellyfin_master" type="checkbox"
 data-key="JELLYFIN_ENABLED" data-state="jellyfin_master_state"
 data-init="{'1' if jellyfin_on else '0'}" {'checked' if jellyfin_on else ''}
 aria-label="Enable Jellyfin library source"></div>
 <div class="lane-master"><div class="mastercopy"><div class="mastertitle">Direct Usenet</div>
 <div class="masterdesc">Enable or disable Newznab search and nzbdav mounting
 without deleting indexer or nzbdav credentials.</div></div>
 <span class="masterstate" id="usenet_master_state"></span>
 <input class="swi lane-master-toggle" id="usenet_master" type="checkbox"
 data-key="USENET_ENABLED" data-state="usenet_master_state"
 data-init="{'1' if usenet_on else '0'}" {'checked' if usenet_on else ''}
 aria-label="Enable direct Usenet source"></div>
</section>"""


def search_index() -> list[dict]:
    """Ctrl/⌘K palette index for the app shell: every knob and every
    connection, deep-linking to its page anchor. Values are pre-escaped for
    the palette's innerHTML rendering."""
    titles = {gid: title for gid, title, _b in config.GROUPS}
    out: list[dict] = []
    for s in config.SETTINGS:
        if s.get("hidden"):
            continue
        out.append({
            "t": _esc(s["label"]), "k": _esc(s["key"]),
            "s": _esc(f"Tune · {titles.get(s['group'], s['group'])}"),
            "href": f"/tune#sec-{s['group']}"})
    for gid, title in knobs.GROUPS:
        for s in knobs.by_group(gid):
            out.append({
                "t": _esc(s["key"]), "k": _esc(s["key"]),
                "s": _esc(f"Advanced · {title}"),
                "href": "/tune#sec-advanced"})
    for c in config.CONNECTIONS:
        out.append({
            "t": _esc(c["name"]), "k": _esc(c["id"]),
            "s": "Connect", "href": f"/connect#conn-{c['id']}"})
    return out
