"""Signal Room — the shared design system for the StreamPicker dashboard.

A clean, neutral control-room aesthetic: off-white surfaces in light mode,
true-dark surfaces in dark mode (OS preference by default, with a manual
nav toggle persisted in localStorage), and an emerald primary accent with a
sky secondary for eyebrows/highlights. Semantic ok/warn/bad are retuned to
harmonize.

This module is the ONLY place design tokens and shared components live.
Pages import it and never redefine palette values; it imports nothing from
other app modules (stdlib only) so any page can depend on it safely.

Public API:
    BASE_CSS      full token + component stylesheet (goes in <style> first)
    NAV           app-shell primary nav (id, href, label, icon)
    icon(name)    inline-SVG icon, 16px stroke, currentColor
    shell(...)    the app layout: fixed left rail + content column
    page(...)     chrome-less document, for pre-login pages only
    pagehead(...) eyebrow + h1 + subtitle block
    section(...)  section header with tally bar + eyebrow
    copybtn(value, label)        copy-to-clipboard button (LAN-safe)
    badge(text, tone)            status pill
    status_dot(state, label="")  soft-glow status dot
    meter(pct, tone="")          thin progress track, emerald fill
    kv(key, value)               key/value row
    tile(value, label, sub="")   stat tile (tabular figures)
    empty(text)                  empty-state card
    esc(x)                       html.escape(str(x), quote=True)

Hard rules for consumers: no external assets (no CDN fonts, no icon fonts,
no images — icons come from icon()); human copy in var(--sans), machine
truth (env keys, URLs, latencies, values) in var(--mono); both color
schemes come free from the tokens — never hardcode a hex.
"""

import html

__all__ = [
    "BASE_CSS", "NAV", "esc", "icon", "page", "shell", "pagehead", "section",
    "copybtn", "badge", "status_dot", "meter", "kv", "tile", "empty",
]

# (id, href, label, icon) — the app-shell primary nav, organized by user
# intent: watch status (Home), plug services in (Connect), change behavior
# (Tune), fix problems (Health), plus the Private Trackers sidecar.
NAV = [
    ("home", "/", "Home", "home"),
    ("connect", "/connect", "Connect", "plug"),
    ("tune", "/tune", "Tune", "sliders"),
    ("health", "/health/sources", "Health", "activity"),
    ("picks", "/picks", "Picks", "film"),
    ("private", "/private-trackers", "Trackers", "rss"),
]


def esc(x) -> str:
    return html.escape(str(x), quote=True)


BASE_CSS = """
/* ── Signal Room · tokens ─────────────────────────────────────────────── */
/* Neutral surfaces (off-white light / true dark) + emerald accent. The dark
   scheme is the default; light applies via OS preference, and an explicit
   user choice wins via <html data-theme="light|dark"> (see page()). */
:root,:root[data-theme=dark]{color-scheme:dark;
--bg:#0e1013;--bgimg:radial-gradient(1100px 480px at 50% -8%,rgba(52,211,153,.05),transparent 70%);
--card:#161a1f;--inset:#0a0c0f;--fg:#e8eaed;--mut:#9aa3ad;
--line:#262c34;--line2:#3a424d;--track:#1d232a;
--accent:#34d399;--accent-ink:#04150d;--accent2:#7dd3fc;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;
--accent-soft:color-mix(in srgb,var(--accent) 14%,transparent);
--accent2-soft:color-mix(in srgb,var(--accent2) 13%,transparent);
--ok-soft:color-mix(in srgb,var(--ok) 14%,transparent);
--warn-soft:color-mix(in srgb,var(--warn) 14%,transparent);
--bad-soft:color-mix(in srgb,var(--bad) 14%,transparent);
--ring:color-mix(in srgb,var(--accent) 55%,transparent);
--shadow:0 2px 10px rgba(0,0,0,.28);--shadow2:0 10px 30px rgba(0,0,0,.36);
--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--r:11px;--r-s:8px}
@media (prefers-color-scheme:light){:root:not([data-theme]){color-scheme:light;
--bg:#f7f7f4;--bgimg:radial-gradient(1100px 480px at 50% -8%,rgba(4,120,87,.05),transparent 70%);
--card:#ffffff;--inset:#f0f0ec;--fg:#1c2024;--mut:#5f6b76;
--line:#e2e4e6;--line2:#c9ced4;--track:#e8eaec;
--accent:#047857;--accent-ink:#f0fdf9;--accent2:#0369a1;
--ok:#16a34a;--warn:#b45309;--bad:#dc2626;
--shadow:0 2px 10px rgba(20,24,28,.07);--shadow2:0 10px 30px rgba(20,24,28,.12)}}
:root[data-theme=light]{color-scheme:light;
--bg:#f7f7f4;--bgimg:radial-gradient(1100px 480px at 50% -8%,rgba(4,120,87,.05),transparent 70%);
--card:#ffffff;--inset:#f0f0ec;--fg:#1c2024;--mut:#5f6b76;
--line:#e2e4e6;--line2:#c9ced4;--track:#e8eaec;
--accent:#047857;--accent-ink:#f0fdf9;--accent2:#0369a1;
--ok:#16a34a;--warn:#b45309;--bad:#dc2626;
--accent-soft:color-mix(in srgb,var(--accent) 14%,transparent);
--accent2-soft:color-mix(in srgb,var(--accent2) 13%,transparent);
--ok-soft:color-mix(in srgb,var(--ok) 14%,transparent);
--warn-soft:color-mix(in srgb,var(--warn) 14%,transparent);
--bad-soft:color-mix(in srgb,var(--bad) 14%,transparent);
--ring:color-mix(in srgb,var(--accent) 55%,transparent);
--shadow:0 2px 10px rgba(20,24,28,.07);--shadow2:0 10px 30px rgba(20,24,28,.12);
--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--r:11px;--r-s:8px}

/* ── Shark · 1994 airbrushed mall poster ──────────────────────────────── */
/* Deep water, electric cyan, chrome. The art is a real backdrop (see
   .skinbg) and the extras live in SKIN_CSS/SKIN_JS below. */
:root[data-theme=shark]{color-scheme:dark;
--bg:#03141f;--bgimg:none;
--card:rgba(6,32,48,.82);--inset:rgba(2,18,29,.72);--fg:#e9fbff;--mut:#8fc4da;
--line:#12506e;--line2:#1f86ad;--track:#0a3346;
--accent:#1fe0ff;--accent-ink:#00131c;--accent2:#7deeff;
--ok:#35f0c0;--warn:#ffc447;--bad:#ff5f7e;
--accent-soft:color-mix(in srgb,var(--accent) 16%,transparent);
--accent2-soft:color-mix(in srgb,var(--accent2) 14%,transparent);
--ok-soft:color-mix(in srgb,var(--ok) 15%,transparent);
--warn-soft:color-mix(in srgb,var(--warn) 15%,transparent);
--bad-soft:color-mix(in srgb,var(--bad) 15%,transparent);
--ring:color-mix(in srgb,var(--accent) 60%,transparent);
--shadow:0 2px 12px rgba(0,0,0,.5);--shadow2:0 12px 34px rgba(0,0,0,.6);
--sans:'Trebuchet MS',Verdana,-apple-system,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--r:6px;--r-s:4px;
--skin:url('/skin/shark.jpg');
--skin-scrim:linear-gradient(180deg,rgba(2,14,24,.62),rgba(2,12,20,.80) 50%,rgba(1,10,17,.93));
--glow:0 0 6px rgba(31,224,255,.75),0 0 18px rgba(31,224,255,.35)}

/* ── Neon Heart · the kdrama skin, all sparkle ────────────────────────── */
:root[data-theme=heart]{color-scheme:light;
--bg:#fff4fa;--bgimg:none;
--card:rgba(255,252,254,.94);--inset:#fdeaf4;--fg:#2c0a20;--mut:#8a5273;
--line:#f4cbe0;--line2:#e79ec4;--track:#fbdcec;
--accent:#e0187c;--accent-ink:#fff5fa;--accent2:#8b3fd6;
--ok:#0f9d6a;--warn:#c2660a;--bad:#d81b4a;
--accent-soft:color-mix(in srgb,var(--accent) 12%,transparent);
--accent2-soft:color-mix(in srgb,var(--accent2) 11%,transparent);
--ok-soft:color-mix(in srgb,var(--ok) 13%,transparent);
--warn-soft:color-mix(in srgb,var(--warn) 13%,transparent);
--bad-soft:color-mix(in srgb,var(--bad) 13%,transparent);
--ring:color-mix(in srgb,var(--accent) 50%,transparent);
--shadow:0 2px 12px rgba(190,60,130,.14);--shadow2:0 12px 30px rgba(190,60,130,.2);
--sans:Georgia,'Times New Roman',serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--r:16px;--r-s:11px;
--skin:url('/skin/kdrama.jpg');
--skin-scrim:linear-gradient(180deg,rgba(255,242,249,.84),rgba(255,238,247,.90) 45%,rgba(255,245,251,.96));
--glow:0 0 8px rgba(224,24,124,.35)}

/* ── Amber CRT · a VT220 that wandered into 2026 ──────────────────────── */
:root[data-theme=crt]{color-scheme:dark;
--bg:#0b0d07;--bgimg:radial-gradient(1200px 620px at 50% -10%,rgba(255,176,0,.07),transparent 72%);
--card:#12150c;--inset:#080a05;--fg:#ffcf70;--mut:#a3844a;
--line:#31290f;--line2:#5a4a1c;--track:#1c1809;
--accent:#ffb000;--accent-ink:#160f00;--accent2:#7dff9e;
--ok:#7dff9e;--warn:#ffd166;--bad:#ff6b53;
--accent-soft:color-mix(in srgb,var(--accent) 15%,transparent);
--accent2-soft:color-mix(in srgb,var(--accent2) 13%,transparent);
--ok-soft:color-mix(in srgb,var(--ok) 14%,transparent);
--warn-soft:color-mix(in srgb,var(--warn) 14%,transparent);
--bad-soft:color-mix(in srgb,var(--bad) 14%,transparent);
--ring:color-mix(in srgb,var(--accent) 60%,transparent);
--shadow:0 0 0 1px rgba(255,176,0,.06);--shadow2:0 6px 26px rgba(0,0,0,.6);
--sans:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--r:2px;--r-s:2px;
--glow:0 0 5px rgba(255,176,0,.55)}

/* ── base ─────────────────────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 var(--sans);
padding:24px 16px 96px}
body::before{content:'';position:fixed;inset:0 0 auto;height:460px;
background:var(--bgimg);pointer-events:none;z-index:-1}
::selection{background:var(--accent-soft)}
.wrap{max-width:1020px;margin:0 auto}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code{font:12.5px var(--mono)}
pre{background:var(--inset);border:1px solid var(--line);border-radius:var(--r-s);
padding:12px 14px;font:12.5px/1.55 var(--mono);overflow-x:auto;white-space:pre}
h1{font-size:24px;letter-spacing:-.01em;margin:0}
h2{font-size:16px;margin:0}
h3{font-size:13.5px;margin:0}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{
transition:none!important;animation:none!important}}

/* ── utilities ────────────────────────────────────────────────────────── */
.mut{color:var(--mut)}.mono{font-family:var(--mono)}
.num{font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.small{font-size:12.5px}
.ic{vertical-align:-.16em;flex-shrink:0}

/* ── chrome: brand mark, theme toggle, page head, section head ────────── */
.brand{font-weight:700;font-size:14.5px;letter-spacing:-.01em;display:flex;
gap:10px;align-items:center;white-space:nowrap}
.mark{width:10px;height:17px;border-radius:3px;background:var(--accent);
box-shadow:0 0 10px var(--ring);flex-shrink:0}
.themebtn{display:inline-flex;align-items:center;justify-content:center;
width:32px;height:32px;border-radius:8px;border:1px solid transparent;
background:transparent;color:var(--mut);cursor:pointer;flex-shrink:0;
transition:background .12s,color .12s,border-color .12s}
.themebtn:hover{color:var(--accent);background:var(--accent-soft);
border-color:var(--line)}
.pagehead{margin:2px 0 26px}
.pagehead h1{margin:0 0 6px}
.sub{color:var(--mut);font-size:13.5px;margin:0;max-width:72ch}
.eyebrow{font:600 10.5px var(--mono);letter-spacing:.18em;text-transform:uppercase;
color:var(--accent2);margin:0 0 7px}
.shead{display:flex;align-items:flex-start;gap:12px;margin:34px 0 14px}
.shead::before{content:'';width:2px;align-self:stretch;border-radius:2px;
background:var(--accent);flex-shrink:0}
.shead .st{flex:1;min-width:0}
.shead .eyebrow{margin-bottom:4px}
.shead .hint{color:var(--mut);font-size:12.5px;margin:3px 0 0}
.shead .tally{align-self:center;flex-shrink:0;font:11.5px var(--mono);
color:var(--mut);background:var(--inset);border:1px solid var(--line);
border-radius:999px;padding:3px 10px;white-space:nowrap}
.shead .tally.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 35%,var(--line))}

/* ── surfaces ─────────────────────────────────────────────────────────── */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--shadow)}
.card.hot{position:relative}
.card.hot::before{content:'';position:absolute;left:-1px;top:10px;bottom:10px;
width:2px;border-radius:2px;background:var(--accent);box-shadow:0 0 8px var(--ring)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:14px 16px;box-shadow:var(--shadow)}
.tile .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;
letter-spacing:-.01em}
.tile .v small{font-size:13px;color:var(--mut);font-weight:500;margin-left:3px}
.tile .k{color:var(--mut);font-size:12.5px;margin-top:2px}
.tile .s{color:var(--mut);font-size:11.5px;margin-top:6px}
.empty{color:var(--mut);font-size:13.5px;background:var(--card);
border:1px dashed var(--line2);border-radius:var(--r);padding:22px 18px;text-align:center}
.callout{border:1px solid var(--line);border-left:2px solid var(--accent);
background:var(--accent-soft);border-radius:var(--r-s);padding:12px 14px;font-size:13.5px}
.callout.ok{border-left-color:var(--ok);background:var(--ok-soft)}
.callout.warn{border-left-color:var(--warn);background:var(--warn-soft)}
.callout.bad{border-left-color:var(--bad);background:var(--bad-soft)}

/* ── buttons ──────────────────────────────────────────────────────────── */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;
font:600 13.5px var(--sans);color:var(--accent-ink);background:var(--accent);
border:1px solid transparent;border-radius:var(--r-s);padding:9px 18px;
cursor:pointer;white-space:nowrap;transition:filter .12s,background .12s,
color .12s,border-color .12s}
.btn:hover{filter:brightness(1.07);text-decoration:none}
.btn:active{filter:brightness(.95)}
.btn:disabled{opacity:.5;cursor:default;filter:none}
.btn.ghost{background:transparent;color:var(--accent);border-color:var(--line2)}
.btn.ghost:hover{border-color:var(--accent);background:var(--accent-soft);filter:none}
.btn.danger{background:var(--bad);color:var(--accent-ink)}
.btn.danger.ghost{background:transparent;color:var(--bad);border-color:var(--line2)}
.btn.danger.ghost:hover{border-color:var(--bad);background:var(--bad-soft)}
.btn.sm{padding:5px 12px;font-size:12.5px;border-radius:7px}
.btnrow{display:flex;gap:10px;flex-wrap:wrap;align-items:center}

/* ── form controls ────────────────────────────────────────────────────── */
input[type=text],input[type=password],input[type=url],input[type=number],
input[type=search],textarea,select{background:var(--inset);color:var(--fg);
border:1px solid var(--line);border-radius:var(--r-s);padding:8px 10px;
font:13px var(--mono);width:100%;transition:border-color .12s,box-shadow .12s}
select{width:auto;font:13.5px var(--sans)}
/* a count or a size never needs 1000px of field — cap it, but keep the
   100% so it still shrinks inside narrow cards */
input[type=number]{max-width:180px}
textarea{resize:vertical;min-height:74px}
input::placeholder,textarea::placeholder{color:var(--mut);opacity:.75}
input:focus-visible,textarea:focus-visible,select:focus-visible{
outline:2px solid var(--ring);outline-offset:0;border-color:var(--accent)}
input[type=checkbox]:not(.swi),input[type=radio]{accent-color:var(--accent)}
output{font:13px var(--mono);font-variant-numeric:tabular-nums}
.swi{appearance:none;-webkit-appearance:none;width:42px;height:24px;margin:0;
border-radius:99px;background:var(--track);border:1px solid var(--line2);
position:relative;cursor:pointer;flex-shrink:0;
transition:background .15s,border-color .15s}
.swi::before{content:'';position:absolute;top:2px;left:2px;width:18px;height:18px;
border-radius:50%;background:var(--mut);transition:transform .15s,background .15s}
.swi:checked{background:var(--accent);border-color:var(--accent)}
.swi:checked::before{transform:translateX(18px);background:var(--accent-ink)}
.swi:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input[type=range]{appearance:none;-webkit-appearance:none;width:190px;height:4px;
border-radius:3px;background:var(--track);cursor:pointer}
input[type=range]::-webkit-slider-thumb{appearance:none;-webkit-appearance:none;
width:16px;height:16px;border-radius:50%;background:var(--accent);border:0;
box-shadow:0 0 0 3px var(--accent-soft);cursor:pointer}
input[type=range]::-moz-range-thumb{width:16px;height:16px;border-radius:50%;
background:var(--accent);border:0;box-shadow:0 0 0 3px var(--accent-soft);cursor:pointer}
input[type=range]::-moz-range-progress{height:4px;border-radius:3px;background:var(--accent)}
input[type=range]:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.seg{display:inline-flex;gap:2px;border:1px solid var(--line);border-radius:10px;
padding:2px;background:var(--inset)}
.seg label{cursor:pointer;position:relative}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg span{display:block;padding:7px 15px;font-size:13.5px;color:var(--mut);
border-radius:8px;white-space:nowrap;
transition:background .12s,color .12s,box-shadow .12s}
.seg label:hover span{color:var(--fg)}
.seg input:checked+span{background:var(--card);color:var(--accent);
box-shadow:inset 0 -2px 0 var(--accent),var(--shadow)}
.seg input:focus-visible+span{outline:2px solid var(--accent);outline-offset:1px}

/* ── status: badges, tags, dots, meters ───────────────────────────────── */
.badge{display:inline-flex;align-items:center;gap:5px;font:600 10px/1.2 var(--mono);
letter-spacing:.07em;text-transform:uppercase;padding:4px 8px;border-radius:5px;
background:var(--track);color:var(--mut);white-space:nowrap}
.badge.ok{background:var(--ok-soft);color:var(--ok)}
.badge.warn{background:var(--warn-soft);color:var(--warn)}
.badge.bad{background:var(--bad-soft);color:var(--bad)}
.badge.info{background:var(--accent-soft);color:var(--accent)}
.badge.teal{background:var(--accent2-soft);color:var(--accent2)}
.tag{font:11px var(--mono);background:var(--track);color:var(--mut);
padding:2px 7px;border-radius:5px;white-space:nowrap}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
background:var(--line2);flex-shrink:0}
.dot.ok{background:var(--ok);box-shadow:0 0 7px color-mix(in srgb,var(--ok) 70%,transparent)}
.dot.warn{background:var(--warn);box-shadow:0 0 7px color-mix(in srgb,var(--warn) 70%,transparent)}
.dot.bad{background:var(--bad);box-shadow:0 0 7px color-mix(in srgb,var(--bad) 70%,transparent)}
.dot.run{background:var(--accent);
box-shadow:0 0 8px color-mix(in srgb,var(--accent) 75%,transparent);
animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.4}}
.stat{display:inline-flex;align-items:center;gap:7px}
.stat-t{font:11.5px var(--mono);color:var(--mut)}
.meter{height:5px;border-radius:4px;background:var(--track);overflow:hidden}
.meter .fill{display:block;height:100%;border-radius:4px;background:var(--accent);
transition:width .5s ease}
.meter .fill.ok{background:var(--ok)}.meter .fill.warn{background:var(--warn)}
.meter .fill.bad{background:var(--bad)}.meter .fill.teal{background:var(--accent2)}

/* ── data: tables, kv, rows ───────────────────────────────────────────── */
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r);
background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:8px 12px;text-align:right;white-space:nowrap;
border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--mut);font:600 10.5px var(--mono);text-transform:uppercase;
letter-spacing:.08em}
td{font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
tr.bad td{background:var(--bad-soft)}
tr.warn td{background:var(--warn-soft)}
.kv{display:flex;justify-content:space-between;align-items:baseline;gap:16px;
padding:9px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.kv:last-child{border-bottom:0}
.kv .k{font:12px var(--mono);color:var(--mut);flex-shrink:0}
.kv .v{font-family:var(--mono);font-size:12.5px;text-align:right;
overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.row{display:flex;justify-content:space-between;align-items:center;gap:24px;
padding:14px 16px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row.off{opacity:.45;pointer-events:none}
.lbl{font-weight:600}
.desc{color:var(--mut);font-size:12.5px;max-width:56ch}
.envk{font:10.5px var(--mono);color:var(--mut);opacity:.7;margin-left:8px}
.ctl{display:flex;align-items:center;gap:10px;flex-shrink:0}

/* ── disclosure ───────────────────────────────────────────────────────── */
details.acc{border-top:1px solid var(--line)}
details.acc>summary{cursor:pointer;padding:12px 2px;list-style:none;
display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
details.acc>summary::-webkit-details-marker{display:none}
details.acc>summary::before{content:'▸';color:var(--accent2);font-size:11px;align-self:center}
details.acc[open]>summary::before{content:'▾'}
details.acc>summary:hover .acc-t{color:var(--accent)}
.acc-t{font-weight:600;font-size:14.5px}
.acc-hint{color:var(--mut);font-size:12.5px;flex:1;min-width:120px}
.acc-n{color:var(--mut);font:11px var(--mono);white-space:nowrap}

/* ── bars: save bar, toasts ───────────────────────────────────────────── */
.savebar{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;
display:flex;align-items:center;gap:14px;background:var(--card);
border:1px solid var(--line);border-radius:14px;padding:10px 10px 10px 16px;
box-shadow:var(--shadow2);z-index:50;max-width:92vw}
.savebar[hidden]{display:none}
.savebar.top{position:sticky;top:16px;bottom:auto;left:auto;transform:none;
margin:0 0 22px;max-width:none;z-index:30}
.savebar .msg{font-size:13.5px}
.savebar .err{color:var(--bad);font-size:12.5px;max-width:340px}
/* Narrow screens: the nav owns the top edge, and the floating bottom save
   bar is already always on screen — so the inline one stops sticking
   rather than fighting the rail for the same 50px. */
@media (max-width:840px){.savebar.top{position:static;top:auto}}
.toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;
gap:8px;z-index:60}
.toast{display:flex;align-items:center;gap:9px;background:var(--card);
border:1px solid var(--line);border-left:2px solid var(--accent);
border-radius:10px;padding:10px 14px;box-shadow:var(--shadow2);
font-size:13.5px;max-width:min(420px,90vw)}
.toast.ok{border-left-color:var(--ok)}
.toast.warn{border-left-color:var(--warn)}
.toast.bad{border-left-color:var(--bad)}

/* ── app shell: left rail + content ───────────────────────────────────── */
body.app{display:flex;min-height:100vh;padding:0}
body.app::before{height:100vh}
/* The nav never scrolls away: pinned to the side on desktop, pinned to the
   top as a single compact row on narrow screens. overflow-y keeps a long
   rail usable on short viewports instead of clipping its footer. */
.rail{position:sticky;top:0;height:100vh;width:232px;flex-shrink:0;
display:flex;flex-direction:column;padding:20px 12px 14px;overflow-y:auto;
background:var(--card);border-right:1px solid var(--line);z-index:50}
.rail-brand{display:flex;gap:10px;align-items:center;font-weight:700;
font-size:14.5px;letter-spacing:-.01em;padding:2px 10px 16px;margin-bottom:12px;
border-bottom:1px solid var(--line);white-space:nowrap}
.rail-nav{display:flex;flex-direction:column;gap:2px}
.rlink{display:flex;align-items:center;gap:11px;padding:9px 11px;
border-radius:9px;color:var(--mut);font-size:13.5px;font-weight:550;
text-decoration:none;transition:background .12s,color .12s}
.rlink:hover{background:var(--track);color:var(--fg);text-decoration:none}
.rlink.on{background:var(--accent-soft);color:var(--accent)}
.rlink svg{opacity:.85}
.rail-search{margin-top:10px;display:flex;align-items:center;gap:9px;
width:100%;padding:8px 11px;border-radius:9px;border:1px solid var(--line);
background:var(--inset);color:var(--mut);font:13px var(--sans);cursor:text;
transition:border-color .12s,color .12s}
.rail-search:hover{border-color:var(--line2);color:var(--fg)}
.rail-search kbd{margin-left:auto;font:10.5px var(--mono);color:var(--mut);
border:1px solid var(--line2);border-radius:5px;padding:1px 5px;background:var(--card)}
.rail-foot{margin-top:auto;display:flex;flex-direction:column;gap:10px;
padding-top:12px;border-top:1px solid var(--line)}
.rail-footrow{display:flex;align-items:center;justify-content:space-between;gap:8px}
.appmain{flex:1;min-width:0;padding:28px 36px 110px}
.appmain>.wrap{max-width:1080px;margin:0}
@media (max-width:840px){
body.app{flex-direction:column}
.rail{position:sticky;top:0;width:auto;height:auto;flex-direction:row;
flex-wrap:nowrap;align-items:center;gap:4px;overflow:visible;
border-right:0;border-bottom:1px solid var(--line);padding:8px 12px;
box-shadow:var(--shadow)}
.rail-brand{border:0;margin:0;padding:2px 8px 2px 2px}
.rail-nav{flex-direction:row;flex-wrap:nowrap}
.rlink span.rl-t{display:none}
.rlink{padding:8px 10px}
.rail-search{margin:0 0 0 auto;width:auto;padding:8px 10px}
.rail-search .rs-t,.rail-search kbd{display:none}
.rail-foot{margin:0;border:0;padding:0;flex-direction:row;align-items:center}
.rail-foot .rf-t{display:none}
.appmain{padding:20px 16px 90px}}
/* phones: the product name goes too, so the bar is mark + icons + actions */
@media (max-width:560px){.rail-brand .rb-t{display:none}
.rail{gap:2px;padding:8px}}

/* ── copy button feedback ─────────────────────────────────────────────── */
.btn.ok{background:var(--ok);color:var(--accent-ink)}
.btn.ghost.ok{background:var(--ok-soft);color:var(--ok);border-color:var(--ok)}

/* ── command palette (Ctrl/⌘+K) ───────────────────────────────────────── */
.pk-overlay{position:fixed;inset:0;z-index:80;display:grid;
place-items:start center;padding:12vh 16px 16px;
background:color-mix(in srgb,var(--inset) 40%,transparent);
backdrop-filter:blur(3px)}
.pk-overlay[hidden]{display:none}
.pk{width:min(580px,100%);background:var(--card);border:1px solid var(--line2);
border-radius:14px;box-shadow:var(--shadow2);overflow:hidden}
.pk input{width:100%;border:0;border-radius:0;background:transparent;
border-bottom:1px solid var(--line);padding:15px 16px;font:14.5px var(--sans)}
.pk input:focus-visible{outline:none;border-bottom-color:var(--accent)}
.pk-list{max-height:48vh;overflow:auto;padding:6px}
.pk-item{display:flex;align-items:center;gap:10px;padding:9px 10px;
border-radius:8px;color:var(--fg);text-decoration:none}
.pk-item:hover{text-decoration:none;background:var(--track)}
.pk-item.sel{background:var(--accent-soft)}
.pk-item.sel .pk-t{color:var(--accent)}
.pk-t{font-size:13.5px;font-weight:550}
.pk-s{font:11px var(--mono);color:var(--mut)}
.pk-item .k{margin-left:auto;font:11px var(--mono);color:var(--mut)}
.pk-item.sel .k{color:var(--accent)}
.pk-empty{padding:20px;text-align:center;color:var(--mut);font-size:13px}
"""


# ── icons ────────────────────────────────────────────────────────────────
# 24×24 stroke-based glyphs (feather-style), rendered at 16px, currentColor.
_ICONS = {
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "bolt": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "play": '<path d="M6 4l14 8-14 8V4z"/>',
    "chart": '<path d="M18 20V10M12 20V4M6 20v-6"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "warn": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "refresh": '<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "arrow-right": '<path d="M5 12h14M12 5l7 7-7 7"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6h14zM10 11v6M14 11v6"/>',
    "server": '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
    "film": '<rect x="2" y="2" width="20" height="20" rx="2.18"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "home": '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>',
    "plug": '<path d="M9 2v5M15 2v5"/><path d="M7 7h10v3a5 5 0 0 1-10 0V7z"/><path d="M12 15v7"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><path d="M1 14h6M9 8h6M17 16h6"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "rss": '<path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14 21 3"/>',
}


def icon(name: str, *, size: int = 16, cls: str = "") -> str:
    """Inline-SVG icon. Stroke-based, currentColor, aria-hidden.

    Raises KeyError for unknown names; available: """ + ", ".join(_ICONS) + """.
    """
    try:
        inner = _ICONS[name]
    except KeyError:
        raise KeyError(
            f"uitheme.icon: unknown icon {name!r}; "
            f"available: {', '.join(sorted(_ICONS))}") from None
    c = f"ic {cls}".strip()
    return (f'<svg class="{c}" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{inner}</svg>')


# ── chrome ───────────────────────────────────────────────────────────────
# Theme choice lives in localStorage (browser-local UI preference, not an app
# setting — it is intentionally NOT part of config.json). _THEME_RESTORE runs
# in <head> before first paint; _THEME_JS wires the nav toggle button.
_THEME_RESTORE = (
    "<script>(function(){var ok=['light','dark','shark','heart','crt'];"
    "var t=null;try{t=localStorage.getItem('sp-theme')}catch(e){}"
    # ?theme=<id> both previews and pins a theme, so a look is linkable
    "try{var q=new URLSearchParams(location.search).get('theme');"
    "if(q!==null){t=ok.indexOf(q)>=0?q:null;"
    "t?localStorage.setItem('sp-theme',t):localStorage.removeItem('sp-theme')}"
    "}catch(e){}"
    "if(t&&ok.indexOf(t)>=0)document.documentElement.dataset.theme=t;"
    "})();</script>")

# (id, label, swatch) — the theme menu. "" is auto (follow the OS). Swatches
# are two stops of each palette so the menu shows the mood, not just a name.
THEMES = [
    ("", "Auto", "#0e1013,#f7f7f4"),
    ("light", "Signal Room · Light", "#f7f7f4,#047857"),
    ("dark", "Signal Room · Dark", "#0e1013,#34d399"),
    ("shark", "Shark 🦈", "#03141f,#1fe0ff"),
    ("heart", "Neon Heart 💘", "#fff4fa,#e0187c"),
    ("crt", "Amber CRT 🖥", "#0b0d07,#ffb000"),
]

_THEME_JS = ("<script>(function(){var d=document.documentElement,"
             "b=document.getElementById('themebtn'),"
             "m=document.getElementById('thememenu');if(!b||!m)return;"
             "function cur(){return d.dataset.theme||''}"
             "function paint(){var c=cur();"
             "[].forEach.call(m.querySelectorAll('[data-theme-id]'),function(x){"
             "x.setAttribute('aria-checked',x.dataset.themeId===c?'true':'false')})}"
             "function close(){m.hidden=true;b.setAttribute('aria-expanded','false')}"
             "b.addEventListener('click',function(e){e.stopPropagation();"
             "m.hidden=!m.hidden;b.setAttribute('aria-expanded',String(!m.hidden));"
             "if(!m.hidden)paint()});"
             "m.addEventListener('click',function(e){"
             "var x=e.target.closest('[data-theme-id]');if(!x)return;"
             "var id=x.dataset.themeId;"
             "if(id)d.dataset.theme=id;else delete d.dataset.theme;"
             "try{id?localStorage.setItem('sp-theme',id):"
             "localStorage.removeItem('sp-theme')}catch(_){}"
             "paint();close();"
             "document.dispatchEvent(new CustomEvent('sp-theme',{detail:id}))});"
             "document.addEventListener('click',function(e){"
             "if(!m.hidden&&!m.contains(e.target)&&e.target!==b)close()});"
             "document.addEventListener('keydown',function(e){"
             "if(e.key==='Escape'&&!m.hidden)close()});"
             "paint()})();</script>")

# ── skins ────────────────────────────────────────────────────────────────
# Everything past the palette: the backdrop art, the chrome, the 90s type,
# and the shark's toys. Only themes that opt in get any of it, so Signal
# Room stays exactly as clean as it was.
SKIN_CSS = """
/* full-bleed backdrop behind everything, art + scrim from the tokens */
.skinbg{display:none;position:fixed;inset:0;z-index:-1;pointer-events:none;
background-position:center top;background-size:cover;background-repeat:no-repeat}
:root[data-theme=shark] .skinbg,:root[data-theme=heart] .skinbg{display:block;
background-image:var(--skin-scrim),var(--skin)}

/* the theme menu */
.themewrap{position:relative}
.thememenu{position:absolute;bottom:calc(100% + 8px);right:0;z-index:80;
min-width:212px;padding:5px;border-radius:var(--r);background:var(--card);
border:1px solid var(--line2);box-shadow:var(--shadow2)}
.thememenu[hidden]{display:none}
.thememenu button{display:flex;align-items:center;gap:9px;width:100%;
padding:7px 9px;border:0;border-radius:var(--r-s);background:transparent;
color:var(--fg);font:13px var(--sans);text-align:left;cursor:pointer}
.thememenu button:hover{background:var(--track)}
.thememenu button[aria-checked=true]{color:var(--accent);
background:var(--accent-soft)}
.thememenu .sw{width:22px;height:14px;border-radius:3px;flex-shrink:0;
border:1px solid var(--line2)}
.thememenu .cap{font:600 10px var(--mono);letter-spacing:.16em;
text-transform:uppercase;color:var(--mut);padding:6px 9px 4px}
/* the narrow-screen rail sits at the top, so the menu has to drop down */
@media (max-width:840px){.thememenu{bottom:auto;top:calc(100% + 10px)}}

/* ── shark: chrome, neon and teeth ──────────────────────────────────── */
:root[data-theme=shark] .card,:root[data-theme=shark] .rail{
backdrop-filter:blur(7px);border-color:var(--line2);
box-shadow:inset 0 1px 0 rgba(180,240,255,.14),var(--shadow)}
:root[data-theme=shark] h1,:root[data-theme=shark] h2{
font-family:'Arial Black',Impact,sans-serif;font-style:italic;font-weight:900;
letter-spacing:.3px;text-shadow:var(--glow)}
:root[data-theme=shark] .rail-brand{font-family:'Arial Black',Impact,sans-serif;
font-style:italic;text-shadow:var(--glow)}
:root[data-theme=shark] .btn{border:2px ridge var(--line2);
text-shadow:0 1px 0 rgba(0,0,0,.4)}
:root[data-theme=shark] .rlink.on{box-shadow:inset 0 0 0 1px var(--accent),
0 0 12px rgba(31,224,255,.35)}
:root[data-theme=shark] .tile .v,:root[data-theme=shark] .lhero .big{
text-shadow:var(--glow)}
:root[data-theme=shark] .mark{box-shadow:0 0 14px var(--accent)}

/* ── neon heart: soft edges, petals ─────────────────────────────────── */
:root[data-theme=heart] .card{backdrop-filter:blur(6px)}
:root[data-theme=heart] h1,:root[data-theme=heart] h2{font-style:italic;
text-shadow:var(--glow)}
:root[data-theme=heart] .petals{display:block;position:fixed;left:0;right:0;
top:-60px;z-index:9;pointer-events:none;font-size:19px;word-spacing:31vw;
white-space:nowrap;opacity:.55;animation:sp-fall 13s linear infinite}
.petals{display:none}
@keyframes sp-fall{from{transform:translateY(-4vh) translateX(0) rotate(0)}
to{transform:translateY(108vh) translateX(-8vw) rotate(80deg)}}

/* ── amber CRT: scanlines and a phosphor glow ───────────────────────── */
:root[data-theme=crt] .scanlines{display:block;position:fixed;inset:0;z-index:9;
pointer-events:none;opacity:.5;
background:repeating-linear-gradient(180deg,rgba(0,0,0,.32) 0 1px,
transparent 1px 3px)}
.scanlines{display:none}
:root[data-theme=crt] body{text-shadow:var(--glow)}
:root[data-theme=crt] .card{border-color:var(--line2)}
:root[data-theme=crt] h1::after{content:'_';animation:sp-blink 1.1s step-end infinite}
@keyframes sp-blink{50%{opacity:0}}

/* ── shark toys: poppable bubbles + a shark that swims the rail ─────── */
#sharkfx{display:none;position:fixed;inset:0;z-index:40;pointer-events:none;
overflow:hidden}
:root[data-theme=shark] #sharkfx{display:block}
#sharkfx .bub{appearance:none;position:fixed;bottom:-56px;min-width:44px;
min-height:44px;display:grid;place-items:center;padding:5px;border:0;
background:transparent;line-height:1;opacity:.5;pointer-events:auto;
cursor:pointer;user-select:none;animation:sp-rise linear forwards}
#sharkfx .bub:focus-visible,#sharkpet:focus-visible{outline:2px solid var(--accent2);
outline-offset:2px}
@keyframes sp-rise{50%{transform:translateY(-55vh) translateX(var(--sway,20px))}
to{transform:translateY(-112vh) translateX(0)}}
.sp-pop{position:fixed;z-index:41;pointer-events:none;
transform:translate(-50%,-100%);
font:italic 900 20px 'Arial Black',Impact,sans-serif;color:var(--accent2);
text-shadow:0 0 8px rgba(31,224,255,.9),0 0 20px rgba(31,224,255,.5);
animation:sp-float 1.1s ease forwards}
@keyframes sp-float{to{transform:translate(-50%,-100%) translateY(-46px);
opacity:0}}
#sharkpet{display:none;position:fixed;bottom:10px;z-index:41;width:52px;
height:52px;place-items:center;background:none;border:0;padding:0;
cursor:pointer;font-size:40px;line-height:1;opacity:.9;user-select:none;
filter:drop-shadow(0 0 8px rgba(31,224,255,.8));
animation:sp-swim 17s linear infinite}
:root[data-theme=shark] #sharkpet{display:grid}
#sharkpet span{display:inline-block}
#sharkpet span.r-spin{animation:sp-spin .85s ease}
@keyframes sp-spin{to{transform:rotate(-360deg) scale(1.25)}}
#sharkpet span.r-chomp{animation:sp-chomp .8s ease}
@keyframes sp-chomp{0%,100%{transform:scale(1) rotate(0)}
18%{transform:scale(1.55) rotate(-18deg)}32%{transform:scale(1.5) rotate(14deg)}
46%{transform:scale(1.6) rotate(-16deg)}62%{transform:scale(1.5) rotate(10deg)}
80%{transform:scale(1.25) rotate(-4deg)}}
#sharkpet span.r-zap{animation:sp-zap .9s ease}
@keyframes sp-zap{0%,100%{filter:none}
25%{filter:brightness(2.6) drop-shadow(0 0 20px rgba(31,224,255,1))}
55%{filter:brightness(1.7) drop-shadow(0 0 12px #fff)}
75%{filter:brightness(2.2) drop-shadow(0 0 16px rgba(31,224,255,.9))}}
@keyframes sp-swim{0%{right:-14%;transform:scaleX(1)}
48%{right:100%;transform:scaleX(1)}50%{right:100%;transform:scaleX(-1)}
98%{right:-14%;transform:scaleX(-1)}100%{right:-14%;transform:scaleX(1)}}
/* The shark keeps cruising during a round — parking it centre-bottom would
   sit it right on top of the floating save bar. */

/* While a round runs, a shield covers the page: it is both the play surface
   that tracks your pointer/finger and the guard that stops a stray input
   reaching a real setting. touch-action:none so dragging swims instead of
   scrolling. Quit (or Esc) removes it instantly. */
#sharkshield{display:none;position:fixed;inset:0;z-index:39;
background:rgba(2,12,20,.34);backdrop-filter:blur(1.5px);cursor:crosshair;
touch-action:none;-webkit-user-select:none;user-select:none}
:root[data-theme=shark] #sharkshield.on{display:block}

/* hunting mode: the shark stops cruising and is driven frame by frame */
#sharkpet.hunting{animation:none;right:auto;left:0;top:0;bottom:auto;
will-change:transform;transition:none;pointer-events:none;z-index:41}

/* ── Feeding Frenzy ─────────────────────────────────────────────────────
   An endless round you start by poking the shark: it gets faster until
   you lose your three teeth. Deliberately penned into a corner panel:
   the targets drift in the gutters, everything else stays
   pointer-events:none, and one click quits. A settings page must never
   be harder to use because a game is running. */
#sharkhud{display:none;position:fixed;left:14px;bottom:14px;z-index:60;
min-width:184px;padding:10px 12px;border:2px ridge var(--line2);
border-radius:var(--r);background:rgba(3,20,31,.92);color:var(--fg);
box-shadow:var(--shadow2),inset 0 1px 0 rgba(180,240,255,.16);
font:12px var(--mono);backdrop-filter:blur(6px)}
:root[data-theme=shark] #sharkhud.on{display:block}
#sharkhud .hrow{display:flex;align-items:baseline;justify-content:space-between;
gap:12px;margin-bottom:3px}
#sharkhud .htitle{font:italic 900 13px 'Arial Black',Impact,sans-serif;
color:var(--accent);text-shadow:var(--glow);letter-spacing:.4px}
#sharkhud .hscore{font:900 22px var(--mono);color:var(--accent2);
text-shadow:var(--glow);font-variant-numeric:tabular-nums}
#sharkhud .hmeta{color:var(--mut)}
#sharkhud .hcombo{color:var(--warn)}
#sharkhud .hbar{height:5px;border-radius:3px;background:var(--track);
overflow:hidden;margin-top:7px}
#sharkhud .hbar i{display:block;height:100%;width:100%;background:var(--accent);
box-shadow:0 0 8px var(--accent);transition:width .25s linear}
#sharkhud .hhow{margin:8px 0 0;font:10.5px var(--mono);color:var(--mut);
line-height:1.5}
#sharkhud .hquit{margin-top:7px;width:100%;padding:5px;cursor:pointer;
border:1px solid var(--line2);border-radius:var(--r-s);
background:transparent;color:var(--mut);font:11px var(--mono)}
#sharkhud .hquit:hover{color:var(--bad);border-color:var(--bad)}
#sharkhud.paused .htitle::after{content:' · PAUSED';color:var(--warn)}
/* prey are scenery now — the shark eats by touching them, so they must not
   intercept the pointer that is steering him */
#sharkfx .prey{font-size:30px;opacity:.95;animation-name:sp-drift;
pointer-events:none;cursor:default}
#sharkfx .prey.junk{opacity:.9}
#sharkfx .prey.eaten{animation:sp-eaten .28s ease forwards}
@keyframes sp-eaten{to{transform:scale(.2) rotate(40deg);opacity:0}}
/* where you told him to go */
#sharkfx .sp-mark{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;
border:2px solid var(--accent2);border-radius:50%;pointer-events:none;
box-shadow:0 0 12px rgba(31,224,255,.7);animation:sp-mark .7s ease forwards}
@keyframes sp-mark{from{transform:scale(.3);opacity:.95}
to{transform:scale(1.5);opacity:0}}
@keyframes sp-drift{
50%{transform:translateY(-52vh) translateX(var(--sway,20px)) rotate(12deg)}
to{transform:translateY(-108vh) translateX(0) rotate(-8deg)}}
.sp-pop.big{font-size:30px}
.sp-pop.miss{color:var(--bad);
text-shadow:0 0 8px rgba(255,95,126,.9),0 0 20px rgba(255,95,126,.5)}

/* Nobody's settings page should fight an animation they didn't ask for. */
@media (prefers-reduced-motion:reduce){
#sharkfx,#sharkpet,.petals,#sharkhud,#sharkshield{display:none!important}
:root[data-theme=crt] h1::after{animation:none}}
"""

# Bubbles and the shark pet. Only runs while the shark theme is active, and
# stops the moment you pick another one.
SKIN_JS = """<script>(function(){
var d=document.documentElement,fx=document.getElementById('sharkfx');
var pet=document.getElementById('sharkpet');if(!fx||!pet)return;
if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
var BUBS=['\\u{1FAE7}','\\u25CB','\\u26AA','\\u{1F4A7}'];
var POPS=['POP!','CHOMP!','ZAP!','SPLASH!','GNARLY!'];
var TRICKS=['r-spin','r-chomp','r-zap'];
var timer=null;
function rnd(a){return a[Math.floor(Math.random()*a.length)]}
function pop(x,y,txt,cls){
 var s=document.createElement('div');s.className='sp-pop'+(cls?' '+cls:'');
 s.textContent=txt;s.style.left=x+'px';s.style.top=y+'px';
 document.body.appendChild(s);setTimeout(function(){s.remove()},1150);
}
function spawn(){
 /* idle ambience only — during a round the prey are the targets */
 if(d.dataset.theme!=='shark'||game)return;
 var b=document.createElement('button');b.className='bub';b.type='button';
 b.textContent=rnd(BUBS);b.setAttribute('aria-label','Pop the bubble');
 var size=16+Math.random()*26;
 b.style.left=(Math.random()*92+2)+'vw';
 b.style.fontSize=size+'px';
 b.style.setProperty('--sway',(Math.random()*70-35)+'px');
 b.style.animationDuration=(9+Math.random()*8)+'s';
 b.addEventListener('click',function(e){
  var r=b.getBoundingClientRect();
  pop(r.left+r.width/2,r.top,rnd(POPS));b.remove();
 });
 fx.appendChild(b);
 setTimeout(function(){b.remove()},18000);
}
function trick(name){
 var s=pet.querySelector('span');if(!s)return;
 s.className='';void s.offsetWidth;s.className=name||rnd(TRICKS);
}

/* ── Feeding Frenzy ──────────────────────────────────────────────────
   Poke the shark to start. Fish are worth points and build a combo;
   junk costs you a tooth and breaks it. No clock: the sea just keeps
   getting busier until your last tooth goes, and your best run is kept.
   The round only ever adds targets in the page gutters and can be quit
   with one click — the settings underneath stay fully usable. */
var hud=document.getElementById('sharkhud');
var shield=document.getElementById('sharkshield');
var FISH=['\\u{1F41F}','\\u{1F420}','\\u{1F421}','\\u{1F990}'];
var JUNK=['\\u{1F9F4}','\\u{1F97E}','\\u{1FAA3}','\\u26F5'];
var YUM=['YUM!','CHOMP!','NOM!','TASTY!','FEED ME!'];
var BLEH=['BLEH!','YUCK!','NOT FOOD!','PTOOEY!'];
var LIVES=3,PER_LEVEL=8,TOOTH_EVERY=5;
var game=null,spawner=null;

function hiscore(){try{return +localStorage.getItem('sp-shark-hi')||0}
 catch(e){return 0}}
function setHi(v){try{localStorage.setItem('sp-shark-hi',String(v))}catch(e){}}
/* the idle shark advertises what there is to beat */
function showBest(){
 var hi=hiscore(),s=hi?' \\u2014 best '+hi:'';
 pet.title='Feeding Frenzy'+(hi?' \\u00b7 best '+hi:'');
 pet.setAttribute('aria-label','Feed the shark \\u2014 starts a game'+s);
}

/* Difficulty is a pure function of level, and it never plateaus — that is
   the whole ending condition. Prey come faster and rise quicker until
   those hit their floors, after which the shoals get bigger instead, so
   the sea keeps filling however good you are. Level climbs every
   PER_LEVEL fish, so a run ends when your reflexes do, not on a clock. */
function pace(l){return Math.max(190,700-l*46)}
function rise(l){return Math.max(2.3,6.2-l*0.34)}
function junkRate(l){return Math.min(0.5,0.20+l*0.026)}
/* the shoal cap is a browser-sanity limit, not a difficulty one: six at
   the 190ms floor is ~31 prey a second, half of them junk */
function shoal(l){return l<9?1:Math.min(6,Math.floor((l-1)/4))}

function drawHud(){
 if(!game)return;
 hud.querySelector('.hscore').textContent=game.score;
 hud.querySelector('.hcombo').textContent=
  game.combo>1?('x'+game.combo+' combo'):'\\u00a0';
 hud.querySelector('.hbest').textContent='best '+Math.max(hiscore(),game.score);
 hud.querySelector('.hlives').textContent=
  new Array(game.lives+1).join('\\u{1F9B7}');
 hud.querySelector('.hlevel').textContent='lvl '+game.level;
 hud.querySelector('.hbar i').style.width=
  (100*(game.fed%PER_LEVEL)/PER_LEVEL)+'%';
}
function endGame(quiet){
 clearInterval(spawner);spawner=null;
 shield.classList.remove('on');
 [].forEach.call(fx.querySelectorAll('.prey'),function(p){p.remove()});
 var s=game?game.score:0,lv=game?game.level:1,best=hiscore();
 var hx=hunt?hunt.x:innerWidth/2,hy=hunt?hunt.y:innerHeight/2;
 game=null;hunt=null;hud.classList.remove('on');
 if(raf){cancelAnimationFrame(raf);raf=null}
 /* hand the shark back to his idle cruise */
 pet.classList.remove('hunting');pet.style.transform='';
 /* a best run is a best run: bank it even when you quit or switch theme
    mid-round, or a stray Esc costs you the score you actually got */
 var rec=s>best;if(rec)setHi(s);showBest();
 if(quiet)return;
 if(rec)pop(hx,hy-40,'NEW HIGH! '+s,'big');
 else pop(hx,hy-40,'GAME OVER \\u00b7 '+s+' \\u00b7 lvl '+lv+
  ' \\u00b7 best '+best,'big');
}
function retime(){
 clearInterval(spawner);
 spawner=setInterval(wave,pace(game.level));
}
/* one tick can be a whole shoal at high levels; the stagger keeps them
   from landing as a single wall of emoji */
function wave(){
 if(!game)return;
 for(var i=0,n=shoal(game.level);i<n;i++)setTimeout(spawnPrey,i*70);
}
function hit(el,isFish){
 if(!game||el.classList.contains('eaten'))return;
 var r=el.getBoundingClientRect(),x=r.left+r.width/2,y=r.top;
 el.classList.add('eaten');setTimeout(function(){el.remove()},300);
 if(isFish){
  game.combo++;game.fed++;
  var gain=10*Math.max(1,game.combo);
  game.score+=gain;trick('r-chomp');
  pop(x,y,'+'+gain+' '+rnd(YUM));
  if(game.fed%PER_LEVEL===0){
   game.level++;retime();
   /* a tooth back every few levels — it buys you a run, it can't save
      you, because the spawn rate keeps climbing regardless */
   var msg='LEVEL '+game.level+'!';
   if(game.level%TOOTH_EVERY===0&&game.lives<LIVES){
    game.lives++;msg='EXTRA TOOTH! \\u{1F9B7}';
   }
   pop(x,y-34,msg,'big');
  }
 }else{
  game.combo=0;game.lives--;trick('r-zap');
  pop(x,y,rnd(BLEH),'miss');
  if(game.lives<=0){drawHud();return endGame();}
 }
 drawHud();
}
/* A hidden tab freezes requestAnimationFrame but not setInterval, so
   without this the sea keeps filling while the shark stands still: you
   come back to a wall of fish and a combo you never had a chance to
   keep. Pause the round instead, and clear the water on the way out. */
document.addEventListener('visibilitychange',function(){
 if(!game)return;
 if(document.hidden){
  game.paused=true;clearInterval(spawner);spawner=null;
  if(raf){cancelAnimationFrame(raf);raf=null}
  [].forEach.call(fx.querySelectorAll('.prey'),function(p){p.remove()});
  hud.classList.add('paused');
 }else{
  game.paused=false;hud.classList.remove('paused');
  retime();if(!raf)raf=requestAnimationFrame(frame);
 }
});
function spawnPrey(){
 if(!game||game.paused)return;
 var junk=Math.random()<junkRate(game.level);
 var b=document.createElement('button');
 b.className='bub prey'+(junk?' junk':'');b.type='button';
 b.textContent=junk?rnd(JUNK):rnd(FISH);
 b.setAttribute('aria-label',junk?'Junk — do not feed':'Fish — feed the shark');
 /* the shield makes the whole width fair game — nothing underneath is
    clickable while a round is running */
 b.style.left=(3+Math.random()*92)+'vw';
 b.style.setProperty('--sway',(Math.random()*80-40)+'px');
 var secs=rise(game.level);
 b.style.animationDuration=secs+'s';
 fx.appendChild(b);
 /* a fish that swims off the top costs the combo, never a tooth — and
    never while the round is paused, which is not a miss you made */
 setTimeout(function(){
  if(b.isConnected&&!b.classList.contains('eaten')&&game&&!game.paused
     &&!junk&&game.combo){
   game.combo=0;drawHud();}
  b.remove();
 },secs*1000);
}

/* ── swimming ────────────────────────────────────────────────────────
   Click or tap and the shark swims there. He is steered, not dragged:
   a heading eases toward the bearing of his goal at a fixed cruise
   speed, which makes him bank through turns instead of sliding
   sideways. On arrival he doesn't stop — sharks don't — he falls into
   a lazy wander until you give him somewhere else to be. */
var hunt=null,raf=null,HALF=27,BITE=46,SPEED=6.1,TURN=0.085,EDGE=54;
var STANDOFF=68;
function goto_(x,y,touch){
 if(!hunt)return;
 ripple(x,y);
 /* A fingertip covers roughly its own width of screen, so a tap sends him
    to a spot above it — otherwise you steer him straight under your thumb
    and lose sight of the one thing you are aiming. */
 if(touch)y-=STANDOFF;
 hunt.goal={x:Math.max(EDGE,Math.min(innerWidth-EDGE,x)),
            y:Math.max(EDGE,Math.min(innerHeight-EDGE,y))};
}
function ripple(x,y){
 var r=document.createElement('div');r.className='sp-mark';
 r.style.left=x+'px';r.style.top=y+'px';
 fx.appendChild(r);setTimeout(function(){r.remove()},700);
}
function frame(){
 if(!game||!hunt){raf=null;return}
 hunt.t++;
 var gx,gy;
 if(hunt.goal){
  gx=hunt.goal.x;gy=hunt.goal.y;
  if(Math.abs(gx-hunt.x)<24&&Math.abs(gy-hunt.y)<24)hunt.goal=null;
 }
 if(!hunt.goal){
  /* wander: aim a little ahead of himself, drifting the bearing */
  hunt.wa+=(Math.random()-0.5)*0.34;
  var sp=Math.hypot(hunt.vx,hunt.vy)||1;
  gx=hunt.x+hunt.vx/sp*150+Math.cos(hunt.wa)*80;
  gy=hunt.y+hunt.vy/sp*150+Math.sin(hunt.wa)*80;
 }
 gx=Math.max(EDGE,Math.min(innerWidth-EDGE,gx));
 gy=Math.max(EDGE,Math.min(innerHeight-EDGE,gy));
 var ax=gx-hunt.x,ay=gy-hunt.y,m=Math.hypot(ax,ay)||1;
 hunt.vx+=(ax/m*SPEED-hunt.vx)*TURN;
 hunt.vy+=(ay/m*SPEED-hunt.vy)*TURN;
 hunt.x=Math.max(HALF,Math.min(innerWidth-HALF,hunt.x+hunt.vx));
 hunt.y=Math.max(HALF,Math.min(innerHeight-HALF,hunt.y+hunt.vy));
 /* 🦈 faces left, so swimming right needs the flip; the deadband keeps
    him from flickering when he turns through vertical */
 if(hunt.vx>0.4)hunt.dir=-1;else if(hunt.vx<-0.4)hunt.dir=1;
 var speed=Math.hypot(hunt.vx,hunt.vy);
 var wag=Math.sin(hunt.t*0.3)*Math.min(7,speed*1.5);
 pet.style.transform='translate3d('+(hunt.x-HALF)+'px,'+(hunt.y-HALF)+
  'px,0) scaleX('+hunt.dir+') rotate('+wag+'deg)';
 /* Eating the last junk ends the round from inside this scan, which
    tears down `hunt` — so re-check it each step instead of reading a
    dead shark's position for the rest of the shoal. */
 var prey=fx.querySelectorAll('.prey');
 for(var i=0;i<prey.length&&game&&hunt;i++){
  var p=prey[i];
  if(p.classList.contains('eaten'))continue;
  var r=p.getBoundingClientRect();
  var dx=r.left+r.width/2-hunt.x,dy=r.top+r.height/2-hunt.y;
  if(dx*dx+dy*dy<BITE*BITE)hit(p,!p.classList.contains('junk'));
 }
 if(!game||!hunt){raf=null;return}
 raf=requestAnimationFrame(frame);
}
/* pointerdown covers mouse, pen and touch in one path */
shield.addEventListener('pointerdown',function(e){
 e.preventDefault();goto_(e.clientX,e.clientY,e.pointerType==='touch');
});
var KEYS={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1],
 a:[-1,0],d:[1,0],w:[0,-1],s:[0,1]};
document.addEventListener('keydown',function(e){
 if(!game||!hunt)return;
 /* WASD is also just letters: never eat a keystroke meant for a field
    that had focus when the round started */
 var t=e.target,tn=t&&t.tagName;
 if(tn==='INPUT'||tn==='TEXTAREA'||tn==='SELECT'||(t&&t.isContentEditable))return;
 var k=KEYS[e.key];if(!k)return;
 e.preventDefault();
 /* send him a good way in that direction rather than a pixel nudge */
 goto_(hunt.x+k[0]*260,hunt.y+k[1]*260);
});
function startGame(){
 if(game)return;
 game={score:0,combo:0,fed:0,level:1,lives:LIVES,paused:false};
 shield.classList.add('on');hud.classList.add('on');
 var r=pet.getBoundingClientRect();
 hunt={x:r.left+r.width/2||innerWidth/2,y:r.top+r.height/2||innerHeight*0.7,
       vx:-SPEED,vy:0,dir:1,goal:null,wa:Math.random()*6.28,t:0};
 pet.classList.add('hunting');
 drawHud();
 pop(innerWidth/2,innerHeight*0.4,'FEEDING FRENZY!','big');
 if(!raf)raf=requestAnimationFrame(frame);
 retime();spawnPrey();spawnPrey();
}
pet.addEventListener('click',function(){
 if(game){trick('r-chomp');return}
 startGame();
});
hud.querySelector('.hquit').addEventListener('click',function(){endGame(true)});
document.addEventListener('keydown',function(e){
 if(e.key==='Escape'&&game)endGame(true);
});

function sync(){
 clearInterval(timer);timer=null;
 if(game)endGame(true);
 if(d.dataset.theme!=='shark'){fx.textContent='';return;}
 showBest();
 timer=setInterval(spawn,2600);spawn();
}
document.addEventListener('sp-theme',sync);
sync();
})();</script>"""

# Link-interceptor: upgrades plain <a href> unblock/clear actions on the
# Health page into CSRF-protected POSTs. Emitted by shell() whenever a csrf
# token is supplied.
_CSRF_JS = (
    "<script>document.addEventListener('click',async e=>{"
    "const a=e.target.closest('a[href]');if(!a)return;"
    "const u=new URL(a.href,location.href);"
    "if(!['/api/unblock','/api/decode/clear','/api/nzb-indexer/clear']"
    ".includes(u.pathname))return;"
    "e.preventDefault();const c=document.querySelector('[data-csrf]').dataset.csrf;"
    "const r=await fetch(u,{method:'POST',headers:{'X-CSRF-Token':c}});"
    "if(r.ok)location.href='/health/sources';else alert('Action failed: HTTP '+r.status);"
    "});</script>")


def pagehead(h: str, eyebrow: str | None = None,
             subtitle: str | None = None) -> str:
    """Eyebrow + <h1> + subtitle block. ``subtitle`` is raw (trusted) HTML;
    ``h`` and ``eyebrow`` are escaped."""
    bits = ['<div class="pagehead">']
    if eyebrow:
        bits.append(f'<p class="eyebrow">{esc(eyebrow)}</p>')
    bits.append(f'<h1>{esc(h)}</h1>')
    if subtitle:
        bits.append(f'<p class="sub">{subtitle}</p>')
    bits.append('</div>')
    return "".join(bits)


def page(*, title: str, body: str, name: str = "",
         h: str | None = None, eyebrow: str | None = None,
         subtitle: str | None = None, head: str = "", scripts: str = "",
         robots: str = "noindex") -> str:
    """A chrome-less document: doctype, metas, BASE_CSS, optional page head.

    For the handful of pages reached *before* there is anything to navigate
    to — first-run account creation. Everything an operator can navigate
    from uses shell() instead, which adds the app rail.

    title     document title (rendered as ``{name} — {title}``); escaped
    body      raw HTML placed inside <main class="wrap">
    name      product name for the title (usually ADDON_NAME)
    h         visible <h1>; if given (or eyebrow/subtitle), a .pagehead block
              is rendered first; h/eyebrow escaped, subtitle raw
    head      extra raw HTML appended after the BASE_CSS <style> (page CSS)
    scripts   raw HTML (usually <script> blocks) before </body>
    robots    robots meta content ("noindex" default, "noindex,nofollow" etc.)
    """
    doc_title = f"{name} — {title}" if name else title
    metas = ['<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<meta name="robots" content="{esc(robots)}">']
    head_html = pagehead(h if h is not None else title, eyebrow, subtitle) \
        if (h or eyebrow or subtitle) else ""
    return (f'<!doctype html><html><head>{"".join(metas)}'
            f'{_THEME_RESTORE}<title>{esc(doc_title)}</title>'
            f'<style>{BASE_CSS}</style>{head}</head>'
            f'<body><main class="wrap">{head_html}{body}</main>'
            f'{_THEME_JS}{scripts}</body></html>')


# ── small components ─────────────────────────────────────────────────────
_TONES = ("ok", "warn", "bad", "info", "teal")
_DOT_STATES = ("ok", "warn", "bad", "run", "idle")


def section(eyebrow: str, title: str, hint: str = "", *,
            tally: str = "", tone: str = "") -> str:
    """Section header: accent rule + eyebrow + title (+ hint), and an optional
    right-aligned tally pill ("2/5 configured"). Everything is escaped — pass
    counts through `tally`, never as markup inside `title`."""
    h = f'<p class="hint">{esc(hint)}</p>' if hint else ""
    t = (f'<span class="tally{" ok" if tone == "ok" else ""}">{esc(tally)}'
         f'</span>') if tally else ""
    return (f'<div class="shead"><div class="st">'
            f'<p class="eyebrow">{esc(eyebrow)}</p><h2>{esc(title)}</h2>'
            f'{h}</div>{t}</div>')


def badge(text: str, tone: str = "") -> str:
    """Uppercase mono status pill. tone: ok | warn | bad | info | teal.

    Empty text renders nothing rather than a naked pill — records missing an
    optional field should leave a blank cell, not a coloured smudge."""
    if not str(text).strip():
        return ""
    cls = f"badge {tone}" if tone in _TONES else "badge"
    return f'<span class="{cls}">{esc(text)}</span>'


def status_dot(state: str = "idle", label: str = "") -> str:
    """Soft-glow status dot. state: ok | warn | bad | run (pulsing) | idle.

    With ``label``, wraps the dot + mono label in a .stat chip.
    """
    st = state if state in _DOT_STATES else "idle"
    dot = f'<span class="dot {st}" aria-hidden="true"></span>'
    if not label:
        return dot
    return f'<span class="stat">{dot}<span class="stat-t">{esc(label)}</span></span>'


def meter(pct: float, tone: str = "") -> str:
    """Thin progress track with amber fill (tone: ok | warn | bad | teal)."""
    p = max(0.0, min(100.0, float(pct)))
    t = f" {tone}" if tone in ("ok", "warn", "bad", "teal") else ""
    return (f'<div class="meter" role="progressbar" aria-valuenow="{p:.0f}" '
            f'aria-valuemin="0" aria-valuemax="100">'
            f'<span class="fill{t}" style="width:{p:.1f}%"></span></div>')


def kv(key: str, value: str) -> str:
    """One key/value row (mono both sides, hairline-separated)."""
    return (f'<div class="kv"><span class="k">{esc(key)}</span>'
            f'<span class="v">{esc(value)}</span></div>')


def tile(value: str, label: str, sub: str = "", *, raw: bool = False) -> str:
    """Stat tile with tabular figures. raw=True lets ``value`` carry markup
    (e.g. a <small> unit suffix)."""
    v = value if raw else esc(value)
    s = f'<div class="s">{esc(sub)}</div>' if sub else ""
    return (f'<div class="tile"><div class="v">{v}</div>'
            f'<div class="k">{esc(label)}</div>{s}</div>')


def empty(text: str) -> str:
    """Empty-state card (dashed hairline, muted)."""
    return f'<div class="empty">{esc(text)}</div>'


# ── app shell ────────────────────────────────────────────────────────────
# Clipboard: the dashboard is usually reached over plain LAN http, where the
# async clipboard API is unavailable (non-secure context) — keep the
# textarea+execCommand fallback or copy buttons silently do nothing there.
_COPY_JS = """<script>document.addEventListener('click',function(e){
var b=e.target.closest('[data-copy]');if(!b)return;
var v=b.dataset.copy;
var done=function(){var t=b.innerHTML;b.textContent='Copied \\u2713';
b.classList.add('ok');setTimeout(function(){b.innerHTML=t;b.classList.remove('ok')},1500)};
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(v).then(done);return}
var ta=document.createElement('textarea');ta.value=v;ta.style.position='fixed';
ta.style.opacity='0';document.body.appendChild(ta);ta.select();
document.execCommand('copy');document.body.removeChild(ta);done();});</script>"""

# Command palette: Ctrl/⌘+K (or / outside an input) filters window.SP_SEARCH
# [{t,k,s,href}] and navigates. Emitted by shell() when search= is passed.
_PALETTE_JS = """<script>(function(){
var ov=document.getElementById('pk');if(!ov)return;
var inp=document.getElementById('pk-in'),list=document.getElementById('pk-list');
var data=window.SP_SEARCH||[],sel=0,view=[];
function open(){ov.hidden=false;inp.value='';draw('');inp.focus()}
function close(){ov.hidden=true}
function draw(q){q=q.trim().toLowerCase();view=[];
for(var i=0;i<data.length&&view.length<40;i++){var x=data[i];
if(!q||x.t.toLowerCase().indexOf(q)>=0||x.k.toLowerCase().indexOf(q)>=0||
x.s.toLowerCase().indexOf(q)>=0)view.push(x)}
sel=0;paint()}
function paint(){
if(!view.length){list.innerHTML='<div class="pk-empty">Nothing matches \\u2014 try an env key like BUFFER_CACHE_GB</div>';return}
var h='';for(var i=0;i<view.length;i++){var x=view[i];
h+='<a class="pk-item'+(i===sel?' sel':'')+'" href="'+x.href+
'"><span class="pk-t">'+x.t+'</span><span class="pk-s">'+x.s+'</span>'+
'<span class="k">'+x.k+'</span></a>'}
list.innerHTML=h;
var s=list.children[sel];if(s)s.scrollIntoView({block:'nearest'})}
inp.addEventListener('input',function(){draw(inp.value)});
inp.addEventListener('keydown',function(e){
if(e.key==='ArrowDown'){e.preventDefault();sel=Math.min(sel+1,view.length-1);paint()}
else if(e.key==='ArrowUp'){e.preventDefault();sel=Math.max(sel-1,0);paint()}
else if(e.key==='Enter'){e.preventDefault();if(view[sel])location.href=view[sel].href}});
document.addEventListener('keydown',function(e){
if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){
e.preventDefault();ov.hidden?open():close()}
else if(e.key==='Escape'&&!ov.hidden)close();
else if(e.key==='/'&&ov.hidden&&document.activeElement&&
!/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)){
e.preventDefault();open()}});
ov.addEventListener('click',function(e){if(e.target===ov)close()});
var b=document.getElementById('pkbtn');if(b)b.addEventListener('click',open);
})();</script>"""

_PALETTE_HTML = (
    '<div class="pk-overlay" id="pk" hidden>'
    '<div class="pk" role="dialog" aria-modal="true" aria-label="Search settings">'
    '<input id="pk-in" type="search" placeholder="Search settings or env keys…" '
    'autocomplete="off" spellcheck="false">'
    '<div class="pk-list" id="pk-list"></div></div></div>')


def copybtn(value: str, label: str = "Copy", *, cls: str = "btn ghost sm",
            raw: bool = False) -> str:
    """Copy-to-clipboard button. raw=True lets ``label`` carry icon markup."""
    lab = label if raw else esc(label)
    return (f'<button type="button" class="{esc(cls)}" '
            f'data-copy="{esc(value)}">{lab}</button>')


def shell(*, title: str, body: str, name: str = "", active: str = "",
          csrf: str | None = None, head: str = "", scripts: str = "",
          search: list[dict] | None = None, foot: str = "",
          refresh: int | None = None, robots: str = "noindex") -> str:
    """The app layout: fixed left rail (brand, primary nav, search, theme)
    plus a fluid content column. ``search`` is a list of
    {"t": title, "k": env-key, "s": section, "href": url} dicts feeding the
    Ctrl/⌘+K palette; values must already be HTML-escaped. ``foot`` is raw
    HTML for the rail footer (e.g. a restart-pending badge)."""
    import json as _json

    doc_title = f"{name} — {title}" if name else title
    metas = ['<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<meta name="robots" content="{esc(robots)}">']
    if refresh:
        metas.append(f'<meta http-equiv="refresh" content="{int(refresh)}">')
    links = "".join(
        f'<a class="rlink{" on" if tid == active else ""}" href="{esc(href)}">'
        f'{icon(ic, size=17)}<span class="rl-t">{esc(label)}</span></a>'
        for tid, href, label, ic in NAV)
    csrf_attr = f' data-csrf="{esc(csrf)}"' if csrf else ""
    palette = ""
    search_btn = ""
    if search is not None:
        idx = _json.dumps(search).replace("</", "<\\/")
        palette = (f"<script>window.SP_SEARCH={idx};</script>"
                   f"{_PALETTE_HTML}{_PALETTE_JS}")
        search_btn = ('<button class="rail-search" id="pkbtn" type="button" '
                      'aria-label="Search settings">'
                      f'{icon("search", size=15)}'
                      '<span class="rs-t">Search settings</span>'
                      '<kbd>⌘K</kbd></button>')
    swatches = "".join(
        f'<button type="button" role="menuitemradio" aria-checked="false" '
        f'data-theme-id="{esc(tid)}">'
        f'<span class="sw" style="background:linear-gradient(135deg,'
        f'{esc(sw.split(",")[0])} 50%,{esc(sw.split(",")[1])} 50%)"></span>'
        f'{esc(label)}</button>'
        for tid, label, sw in THEMES)
    theme_btn = (
        '<div class="themewrap">'
        '<button class="themebtn" id="themebtn" type="button" '
        'aria-haspopup="true" aria-expanded="false" title="Change theme" '
        f'aria-label="Change theme">{icon("sun", size=16)}</button>'
        '<div class="thememenu" id="thememenu" role="menu" hidden>'
        '<p class="cap">Theme</p>' + swatches + '</div></div>')
    # The brand name and page title are wrapped so the narrow-screen bar can
    # drop them and stay one row tall — see the max-width:840px block.
    rail = (f'<aside class="rail"{csrf_attr}>'
            f'<div class="rail-brand"><span class="mark" aria-hidden="true">'
            f'</span><span class="rb-t">{esc(name)}</span></div>'
            f'<nav class="rail-nav" aria-label="Primary">{links}</nav>'
            f'{search_btn}'
            f'<div class="rail-foot">{foot}'
            f'<div class="rail-footrow"><span class="small mut rf-t">'
            f'{esc(title)}</span>{theme_btn}</div></div></aside>')
    # Skin furniture: inert in Signal Room, the whole show in the loud themes.
    skin = ('<div class="skinbg" aria-hidden="true"></div>'
            '<div class="scanlines" aria-hidden="true"></div>'
            '<div class="petals" aria-hidden="true">🌸 ✨ 🌸</div>'
            '<div id="sharkshield" aria-hidden="true"></div>'
            '<div id="sharkfx" aria-hidden="true"></div>'
            '<button id="sharkpet" type="button" '
            'aria-label="Feed the shark — starts a game">'
            '<span>🦈</span></button>'
            '<div id="sharkhud" role="status" aria-live="polite">'
            '<div class="hrow"><span class="htitle">FEEDING FRENZY</span>'
            '<span class="hlives"></span></div>'
            '<div class="hrow"><span class="hscore">0</span>'
            '<span class="hmeta hlevel">lvl 1</span></div>'
            '<div class="hrow"><span class="hcombo">&nbsp;</span>'
            '<span class="hmeta hbest">best 0</span></div>'
            '<div class="hbar"><i></i></div>'
            '<p class="hhow">tap where to swim &middot; eat 🐟 &middot; dodge 🧴'
            '<br>lose 3 teeth and it&rsquo;s over &middot; it only gets faster</p>'
            '<button type="button" class="hquit">quit (esc)</button></div>')
    js = (_CSRF_JS if csrf else "") + _THEME_JS + _COPY_JS + SKIN_JS
    return (f'<!doctype html><html><head>{"".join(metas)}'
            f'{_THEME_RESTORE}<title>{esc(doc_title)}</title>'
            f'<style>{BASE_CSS}{SKIN_CSS}</style>{head}</head>'
            f'<body class="app">{skin}{rail}'
            f'<main class="appmain"><div class="wrap">{body}</div></main>'
            f'{palette}{js}{scripts}</body></html>')
