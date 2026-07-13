"""Renders /{secret}/overview — the fun, at-a-glance ledger of what the addon
has actually delivered.

Where /stats is the worst-first diagnostic view (which source to blame), this
page is the opposite mood: the headline totals and the satisfying superlatives —
gigabytes streamed, hours watched, how often failover quietly saved a stream,
what the direct-usenet lane is really hitting and why it whiffs the rest.

Every number is measured, not modelled: it comes from the same playback and
probe telemetry the diagnostics use (bytes and seconds are recorded per play in
telemetry.record_play), so nothing here is invented. On a fresh install the
sections say so rather than showing zeros dressed up as achievements.
"""

import html
import os
import re
import time
from collections import defaultdict

from app import adminui, proxy, telemetry, usenet_health

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--mut:#6b6b66;
--line:#e6e6e2;--bad:#c0392b;--warn:#b8860b;--good:#2e7d5b;--accent:#3b6ea5;
--accent2:#8e5bd0;--track:#eceae4;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--fg:#e9e9e6;
--mut:#9a9a94;--line:#2c2f34;--bad:#ff6b5e;--warn:#e0b74a;--good:#5cc99a;
--accent:#6ea3d8;--accent2:#b490e6;--track:#2a2d33;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
padding:24px 16px 72px}
.wrap{max-width:1000px;margin:0 auto}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.nav{display:flex;gap:8px;flex-wrap:wrap}
.navlink{font-size:13px;color:var(--accent);text-decoration:none;
border:1px solid var(--line);border-radius:20px;padding:5px 12px;
background:var(--card);white-space:nowrap}
.navlink:hover{border-color:var(--accent)}
.eyebrow{font:12px var(--mono);letter-spacing:.14em;text-transform:uppercase;
color:var(--mut);margin:0 0 6px}

.hero{margin:6px 0 30px}
.hero .head{font-size:15px;color:var(--mut);margin-bottom:4px}
.hero .big{font-size:clamp(44px,11vw,88px);line-height:1;font-weight:700;
letter-spacing:-.02em;background:linear-gradient(95deg,var(--accent),var(--accent2));
-webkit-background-clip:text;background-clip:text;color:transparent;
font-variant-numeric:tabular-nums}
.hero .big .u{font-size:.42em;font-weight:600;color:var(--mut);
-webkit-text-fill-color:var(--mut);margin-left:8px}
.hero .tail{font-size:16px;color:var(--fg);margin-top:8px}
.hero .tail b{font-variant-numeric:tabular-nums}

h2{font-size:16px;margin:34px 0 12px;display:flex;align-items:baseline;gap:10px}
h2 .hint{font-size:12.5px;color:var(--mut);font-weight:400}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px}
.tile .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;
letter-spacing:-.01em}
.tile .v small{font-size:14px;color:var(--mut);font-weight:500;margin-left:3px}
.tile .k{color:var(--mut);font-size:12.5px;margin-top:2px}
.tile .sub{color:var(--mut);font-size:11.5px;margin-top:6px}

.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px}
.panel h3{margin:0 0 12px;font-size:13px;color:var(--mut);font-weight:600;
text-transform:uppercase;letter-spacing:.04em}
.bar{margin:9px 0}
.bar .lab{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px}
.bar .lab .n{color:var(--mut);font-variant-numeric:tabular-nums;font-family:var(--mono);
font-size:12px}
.track{height:8px;border-radius:6px;background:var(--track);overflow:hidden}
.fill{height:100%;border-radius:6px;background:var(--accent)}
.fill.g{background:var(--good)}.fill.w{background:var(--warn)}.fill.b{background:var(--bad)}
.fill.a2{background:var(--accent2)}

.gauge{display:flex;align-items:baseline;gap:10px;margin-bottom:6px}
.gauge .pct{font-size:40px;font-weight:700;font-variant-numeric:tabular-nums;
letter-spacing:-.02em}
.gauge .of{color:var(--mut);font-size:13px}
.note{color:var(--mut);font-size:12.5px;margin:2px 0 0}

.recs{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.rec{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.rec .cap{font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.rec .v{font-size:22px;font-weight:650;margin:3px 0;font-variant-numeric:tabular-nums}
.rec .who{font-size:12.5px;color:var(--mut);overflow-wrap:anywhere}

.spark{display:flex;align-items:flex-end;gap:5px;height:96px;padding-top:8px}
.spark .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
align-items:center;gap:5px;min-width:0}
.spark .stk{width:100%;max-width:34px;border-radius:4px 4px 0 0;
background:linear-gradient(180deg,var(--accent),var(--accent2));min-height:2px}
.spark .d{font:10px var(--mono);color:var(--mut);white-space:nowrap}
.spark .col.zero .stk{background:var(--track)}

table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:0}
td.name{font-family:var(--mono);font-size:12.5px}
.empty{color:var(--mut);font-size:13px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:18px 16px;text-align:center}
.sub{color:var(--mut);font-size:13px;margin:0 0 22px}

.np{margin:0 0 28px}
.np h2{margin:0 0 10px;display:flex;align-items:center;gap:8px}
.np .dot{width:8px;height:8px;border-radius:50%;background:var(--good);
 display:inline-block;animation:pulse 1.8s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.np-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.np-card{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:16px;position:relative}
.np-card .title{font-size:15px;font-weight:600;margin-bottom:6px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.np-card .meta{font-size:12.5px;color:var(--mut);margin-bottom:10px;
 display:flex;flex-wrap:wrap;gap:6px 14px}
.np-card .meta .tag{background:var(--track);padding:2px 7px;border-radius:6px;
 font-family:var(--mono);font-size:11.5px;white-space:nowrap}
.np-card .prog{height:6px;border-radius:4px;background:var(--track);overflow:hidden}
.np-card .prog .bar{height:100%;border-radius:4px;
 background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .6s ease}
.np-card .prog-label{font:11px var(--mono);color:var(--mut);margin-top:5px;
 display:flex;justify-content:space-between}

@media (prefers-reduced-motion:reduce){*{transition:none!important}
.np .dot{animation:none}}
"""


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def _num(n) -> str:
    return f"{n:,}"


def _dur(secs: float) -> tuple[str, str]:
    """Human duration → (value, unit) for a big-number display."""
    if secs >= 3600:
        return f"{secs / 3600:.1f}", "hours"
    if secs >= 60:
        return f"{secs / 60:.0f}", "min"
    return f"{secs:.0f}", "sec"


def _data(mb: float) -> tuple[str, str]:
    """MB total → (value, unit), scaling MB → GB → TB."""
    gb = mb / 1000.0
    if gb >= 1000:
        return f"{gb / 1000:.2f}", "TB"
    if gb >= 1:
        return f"{gb:.1f}", "GB"
    return f"{mb:.0f}", "MB"


def _title_map(recs: list[dict]) -> dict[str, str]:
    """id → a human-ish name, taken from the most recent 'served' label for
    that id (play records only carry the tt-id)."""
    out: dict[str, str] = {}
    for r in recs:
        if r.get("kind") == "served" and r.get("id") and r.get("label"):
            name = re.sub(r"\s+", " ", r["label"]).strip()
            name = re.sub(r"^[^\w(]+", "", name)          # drop leading emoji
            out[r["id"]] = name[:48] or r["id"]
    return out


def _now_playing(playing: list[dict], names: dict[str, str]) -> str:
    """Render a 'Now Playing' section — one card per active buffered stream.
    Returns empty string when nothing is playing (no visual footprint)."""
    if not playing:
        return ""
    cards = []
    for s in playing:
        mid = s.get("media_id", "")
        title = _esc(names.get(mid, mid) or "Unknown")
        lbl = _esc(s.get("label", ""))
        dbr = s.get("debrid", "")
        res = s.get("res", 0)
        node = s.get("node", "")
        avail = s.get("avail") or 0
        total = s.get("total")
        consumers = s.get("consumers", 1)

        # resolution badge
        res_tag = f"<span class='tag'>{res}p</span>" if res else ""

        # debrid badge
        dbr_tag = f"<span class='tag'>{_esc(dbr)}</span>" if dbr else ""

        # node — shorten to first segment (e.g. nexus-190)
        node_short = node.split(".")[0] if node else ""
        node_tag = f"<span class='tag'>{_esc(node_short)}</span>" if node_short else ""

        # viewers
        viewers_txt = (f"{consumers} viewer{'s' if consumers != 1 else ''}")

        # progress bar
        if total and total > 0:
            pct = min(100.0, 100.0 * avail / total)
            avail_gb = avail / (1024 ** 3)
            total_gb = total / (1024 ** 3)
            prog = (f"<div class='prog'><div class='bar' style='width:{pct:.1f}%'></div></div>"
                    f"<div class='prog-label'><span>{avail_gb:.2f} / {total_gb:.2f} GB buffered</span>"
                    f"<span>{pct:.0f}%</span></div>")
        else:
            prog = ("<div class='prog'><div class='bar' style='width:0%'></div></div>"
                    "<div class='prog-label'><span>buffering…</span></div>")

        cards.append(
            f"<div class='np-card'>"
            f"<div class='title'>{title}</div>"
            f"<div class='meta'>"
            f"<span>{lbl}</span>"
            f"{res_tag}{dbr_tag}{node_tag}"
            f"<span>{_esc(viewers_txt)}</span>"
            f"</div>{prog}</div>")

    return ("<div class='np'>"
            "<h2><span class='dot'></span> Now Playing</h2>"
            f"<div class='np-cards'>{''.join(cards)}</div></div>")


def _bar(label: str, n: int, total: int, cls: str = "", note: str = "") -> str:
    pct = (100 * n / total) if total else 0
    right = note or f"{n:,} · {pct:.0f}%"
    return (f"<div class='bar'><div class='lab'><span>{_esc(label)}</span>"
            f"<span class='n'>{_esc(right)}</span></div>"
            f"<div class='track'><div class='fill {cls}' "
            f"style='width:{pct:.1f}%'></div></div></div>")


def _mix_panel(title: str, counts: dict, total: int, order=None,
               labels=None, classes=None) -> str:
    if not total:
        return ""
    labels = labels or {}
    classes = classes or {}
    keys = order or sorted(counts, key=lambda k: counts[k], reverse=True)
    bars = "".join(
        _bar(labels.get(k, k), counts.get(k, 0), total, classes.get(k, ""))
        for k in keys if counts.get(k))
    return f"<div class='panel'><h3>{_esc(title)}</h3>{bars}</div>"


# ── friendly names for the machine-y usenet failure reasons ──────────────────
_REASON = {
    "missing-articles": "Missing articles (incomplete on usenet)",
    "http-404": "Not on the indexer (404)",
    "http-410": "Gone from the indexer (410)",
    "short-body": "Truncated download",
    "empty-body": "Empty download",
    "wrong-episode": "Wrong episode returned",
    "not-video": "Not a video file",
    "timeout": "Timed out fetching",
    "mount-timeout": "Took too long to mount",
    "slow": "Too slow to stream",
    "transport": "Connection error",
}


def _reason_label(code: str) -> str:
    if code in _REASON:
        return _REASON[code]
    if code.startswith("http-"):
        return f"Indexer error ({code[5:]})"
    return code.replace("-", " ").capitalize() if code else "Unknown"


def render(recs: list[dict]) -> str:
    plays = [r for r in recs if r.get("kind") == "play"]
    probes = [r for r in recs if r.get("kind") == "probe"]
    nzb_probes = [r for r in probes if r.get("lane") == "nzb"]
    nzb_fails = [r for r in recs if r.get("kind") == "nzb_failure"]
    buffers = [r for r in recs if r.get("kind") == "buffer"]
    names = _title_map(recs)

    # ── now playing (live from proxy buffer cache) ──
    playing = proxy.active_stream_details()
    now_playing_html = _now_playing(playing, names)

    total_mb = sum(r.get("mb") or 0 for r in plays)
    total_secs = sum(r.get("secs") or 0 for r in plays)
    n_titles = len({r.get("id") for r in plays if r.get("id")})
    n_switch = sum(1 for r in plays if r.get("switched"))
    n_recon = sum(r.get("reconnects") or 0 for r in plays)
    n_twin = sum(1 for r in buffers if r.get("event") == "twin")
    avg_mbps = (total_mb * 8 / total_secs) if total_secs else 0

    dv, du = _data(total_mb)
    hv, hu = _dur(total_secs)

    # ── hero ──
    if plays:
        hero = (
            f"<div class='hero'><div class='head'>{_esc(ADDON_NAME)} has "
            f"streamed</div><div class='big'>{dv}<span class='u'>{du}</span>"
            f"</div><div class='tail'>of video to your screens — across "
            f"<b>{hv} {hu}</b> of watching and <b>{_num(len(plays))}</b> "
            f"stream{'s' if len(plays) != 1 else ''} of "
            f"<b>{_num(n_titles)}</b> title{'s' if n_titles != 1 else ''}."
            f"</div></div>")
    else:
        hero = ("<div class='hero'><div class='head'>"
                f"{_esc(ADDON_NAME)}</div><div class='big'>—</div>"
                "<div class='tail'>Nothing streamed through the proxy yet. "
                "Watch something and this fills in.</div></div>")

    # ── headline tiles ──
    tiles = []
    if plays:
        tiles.append(("streams played", _num(len(plays)), ""))
        tiles.append(("avg bitrate", f"{avg_mbps:.1f}<small>Mbps</small>", ""))
        tiles.append(("failover saves", _num(n_switch),
                      "times a dying source was swapped at the start"))
        tiles.append(("reconnects ridden out", _num(n_recon),
                      "mid-stream drops the buffer recovered from invisibly"))
        if n_twin:
            tiles.append(("twin splices", _num(n_twin),
                          "jumped to an identical copy on another debrid"))
    tile_html = "".join(
        f"<div class='tile'><div class='v'>{v}</div><div class='k'>{_esc(k)}</div>"
        + (f"<div class='sub'>{_esc(s)}</div>" if s else "") + "</div>"
        for k, v, s in tiles)
    tiles_section = (f"<div class='tiles'>{tile_html}</div>" if tiles else "")

    # ── where it came from (from real plays) ──
    res_counts, dbr_counts, hdr_counts, codec_counts = (
        defaultdict(int) for _ in range(4))
    cached = 0
    for r in plays:
        res = r.get("res") or 0
        res_counts[(">=2160" if res >= 2160 else "1080" if res >= 1080
                    else "720" if res >= 720 else "SD/other")] += 1
        dbr_counts[(r.get("debrid") or "").strip() or "direct / usenet"] += 1
        hdr_counts[(r.get("hdr") or "sdr")] += 1
        codec_counts[(r.get("codec") or "other")] += 1
        if r.get("cached"):
            cached += 1
    np = len(plays)
    provenance = ""
    if plays:
        res_panel = _mix_panel(
            "Resolution", res_counts, np,
            order=[">=2160", "1080", "720", "SD/other"],
            labels={">=2160": "4K (2160p)", "1080": "1080p", "720": "720p"},
            classes={">=2160": "a2", "1080": "", "720": "w", "SD/other": "b"})
        dbr_panel = _mix_panel("Debrid service", dbr_counts, np)
        hdr_panel = _mix_panel(
            "Dynamic range", hdr_counts, np,
            labels={"sdr": "SDR", "hdr10": "HDR10", "hdr10+": "HDR10+",
                    "dv": "Dolby Vision", "hlg": "HLG", "hdr": "HDR"},
            classes={"dv": "a2", "hdr10": "g", "hdr10+": "g", "sdr": ""})
        cache_panel = (
            "<div class='panel'><h3>Cache hits</h3>"
            f"<div class='gauge'><span class='pct'>"
            f"{100 * cached / np:.0f}%</span>"
            "<span class='of'>of plays came straight from a cached copy</span>"
            "</div><p class='note'>Cached streams start fastest and rarely "
            "buffer.</p></div>")
        provenance = (
            "<h2>Where your streams came from <span class='hint'>measured on "
            "the bytes that reached the device</span></h2>"
            f"<div class='split'>{res_panel}{dbr_panel}{hdr_panel}{cache_panel}"
            "</div>")

    # ── direct usenet report card ──
    nzb_ok = sum(1 for r in nzb_probes if r.get("ok"))
    nzb_n = len(nzb_probes)
    by_ix: dict[str, dict] = {}
    for r in nzb_probes:
        ix = (r.get("fetch_indexer") or "").strip() or "(unknown)"
        g = by_ix.setdefault(ix, {"n": 0, "ok": 0})
        g["n"] += 1
        g["ok"] += 1 if r.get("ok") else 0
    ix_rows = sorted(by_ix.items(), key=lambda kv: (kv[1]["ok"], kv[1]["n"]),
                     reverse=True)
    ix_table = ""
    if ix_rows:
        body = "".join(
            f"<tr><td class='name'>{_esc(ix)}</td><td>{g['n']}</td>"
            f"<td>{100 * g['ok'] / g['n']:.0f}%</td></tr>"
            for ix, g in ix_rows)
        ix_table = ("<div class='panel'><h3>Which usenet indexers land</h3>"
                    "<table><thead><tr><th>indexer</th><th>tries</th>"
                    f"<th>worked</th></tr></thead><tbody>{body}</tbody></table>"
                    "</div>")

    reason_counts: dict[str, int] = defaultdict(int)
    for r in nzb_fails:
        reason_counts[r.get("reason") or ""] += 1
    total_reasons = sum(reason_counts.values())
    why_panel = ""
    if total_reasons:
        top = sorted(reason_counts.items(), key=lambda kv: kv[1],
                     reverse=True)[:8]
        bars = "".join(_bar(_reason_label(code), n, total_reasons, "b")
                       for code, n in top)
        why_panel = (f"<div class='panel'><h3>Why usenet links fall through"
                     f"</h3>{bars}</div>")

    usenet_section = ""
    if nzb_n or total_reasons:
        rate = (100 * nzb_ok / nzb_n) if nzb_n else 0
        gauge = (
            "<div class='panel'><h3>Usenet strike rate</h3>"
            f"<div class='gauge'><span class='pct'>{rate:.0f}%</span>"
            f"<span class='of'>of {_num(nzb_n)} verified attempts played</span>"
            "</div><p class='note'>Usenet is a coin-flip by nature — releases "
            "go missing and indexers lie about what they have. The probe is "
            "the gate that keeps the misses off your screen.</p></div>")
        usenet_section = (
            "<h2>Direct usenet report card <span class='hint'>what the "
            "self-hosted lane is actually hitting</span></h2>"
            f"<div class='split'>{gauge}{ix_table}{why_panel}</div>")

    # ── superlatives ──
    recs_html = ""
    if plays:
        def _who(r):
            return _esc(names.get(r.get("id"), r.get("id") or "?"))
        biggest = max(plays, key=lambda r: r.get("mb") or 0)
        longest = max(plays, key=lambda r: r.get("secs") or 0)
        fastest = max(plays, key=lambda r: r.get("mbps") or 0)
        bv, bu = _data(biggest.get("mb") or 0)
        lv, lu = _dur(longest.get("secs") or 0)
        cards = [
            ("Biggest single stream", f"{bv} {bu}", _who(biggest)),
            ("Longest sitting", f"{lv} {lu}", _who(longest)),
            ("Fastest delivery", f"{(fastest.get('mbps') or 0) * 8:.0f} Mbps",
             _who(fastest)),
        ]
        recs_html = ("<h2>Records</h2><div class='recs'>" + "".join(
            f"<div class='rec'><div class='cap'>{_esc(c)}</div>"
            f"<div class='v'>{_esc(v)}</div><div class='who'>{w}</div></div>"
            for c, v, w in cards) + "</div>")

    # ── activity sparkline (GB/day, last 14 active-window days) ──
    spark = ""
    if plays:
        DAY = 86400
        now = time.time()
        buckets = defaultdict(float)
        for r in plays:
            day = int((r.get("ts") or now) // DAY)
            buckets[day] += (r.get("mb") or 0) / 1000.0
        today = int(now // DAY)
        span = [today - i for i in range(13, -1, -1)]
        peak = max((buckets[d] for d in span), default=0) or 1
        cols = []
        for d in span:
            gb = buckets.get(d, 0)
            h = max(2, round(90 * gb / peak))
            label = time.strftime("%-m/%-d", time.localtime(d * DAY))
            cls = "col zero" if gb == 0 else "col"
            title = f"{gb:.1f} GB" if gb else "no plays"
            cols.append(f"<div class='{cls}' title='{label}: {title}'>"
                        f"<div class='stk' style='height:{h}px'></div>"
                        f"<div class='d'>{label}</div></div>")
        spark = ("<h2>Last two weeks <span class='hint'>gigabytes streamed per "
                 "day</span></h2><div class='panel'><div class='spark'>"
                 + "".join(cols) + "</div></div>")

    span_txt = ""
    if recs:
        t0 = min(r.get("ts", 0) for r in recs)
        span_txt = (f"Since {time.strftime('%b %-d, %Y', time.localtime(t0))} · "
                    "measured through the addon's proxy")

    empty = ("" if plays else
             "<div class='empty'>No playback has been recorded yet. This page "
             "comes alive once streams are watched through the addon (it needs "
             "the proxy — the 'Direct links' stream path records nothing).</div>")

    refresh = ('<meta http-equiv="refresh" content="30">' if playing else '')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
{refresh}
<title>{_esc(ADDON_NAME)} — overview</title>
<style>{_CSS}{adminui.NAV_CSS}</style></head>
<body><div class="wrap">
{adminui.nav('overview', ADDON_NAME)}
{now_playing_html}
<p class="eyebrow">Stream ledger</p>
{hero}
<p class="sub">{_esc(span_txt)}</p>
{empty}
{tiles_section}
{provenance}
{usenet_section}
{recs_html}
{spark}
</div></body></html>"""
