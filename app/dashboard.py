"""Renders the /stats HTML page from the probe telemetry log.

The tables answer one question: which *sources* deliver badly? A source that
fails often, starts slowly (high TTFB), or streams slowly to our probe is the
one most likely to buffer on the viewer's device — the blacklist candidates.
The 'recent problem picks' list is the other direction: the actual #1 answers we
served that had poor delivery, i.e. the ones that probably buffered, so a report
of 'X buffered last night' can be checked against what we auto-picked for it.
"""

import html
import os

from app import adminui, reputation, telemetry, usenet_health

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

# Match the picker's soft gate so 'slow start' means the same thing everywhere.
GOOD_TTFB = float(os.environ.get("GOOD_TTFB", "4.0"))
SLOW_MBPS = float(os.environ.get("STATS_SLOW_MBPS", "4.0"))

_CSS = """
:root{color-scheme:light dark;--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--mut:#6b6b66;
--line:#e6e6e2;--bad:#c0392b;--warn:#b8860b;--good:#2e7d5b;--accent:#3b6ea5;
--badbg:#fdecea;--warnbg:#fcf6e3;}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--fg:#e9e9e6;
--mut:#9a9a94;--line:#2c2f34;--bad:#ff6b5e;--warn:#e0b74a;--good:#5cc99a;
--accent:#6ea3d8;--badbg:#3a1f1c;--warnbg:#332c17;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
padding:24px 16px 64px}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px;font-size:13px}
h2{font-size:16px;margin:32px 0 10px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 16px;min-width:120px}
.tile .v{font-size:24px;font-weight:600}.tile .k{color:var(--mut);font-size:12px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:14px;min-width:560px}
th,td{padding:9px 12px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:0}
tr.bad td{background:var(--badbg)}tr.warn td{background:var(--warnbg)}
.k1{font-weight:600}.mut{color:var(--mut)}
.detail{white-space:pre-wrap!important;overflow-wrap:anywhere;max-width:480px}
.pill{font-size:11px;padding:1px 7px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
.note{color:var(--mut);font-size:12.5px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px 14px;margin:8px 0 4px}
.bad{color:var(--bad)}.warn{color:var(--warn)}.good{color:var(--good)}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.navlink{font-size:13px;color:var(--accent);text-decoration:none;border:1px solid var(--line);
border-radius:20px;padding:5px 12px;background:var(--card);white-space:nowrap}
.navlink:hover{border-color:var(--accent)}
"""


def _esc(x) -> str:
    return html.escape(str(x))


def _cls(row: dict) -> str:
    if row["fail_pct"] >= 25:
        return "bad"
    if row["ttfb_p90"] >= GOOD_TTFB * 1.5 or row["fail_pct"] >= 10:
        return "warn"
    return ""


def _num(v, unit="", warn=False, bad=False) -> str:
    c = "bad" if bad else ("warn" if warn else "")
    return f'<span class="{c}">{v}{unit}</span>'


def _table(title: str, rows: list[dict], keyname: str) -> str:
    if not rows:
        return f"<h2>{_esc(title)}</h2><div class='note'>No data yet.</div>"
    body = []
    for r in rows:
        cls = _cls(r)
        body.append(
            f"<tr class='{cls}'>"
            f"<td class='k1'>{_esc(r['key'])}</td>"
            f"<td>{r['n']}</td>"
            f"<td>{_num(r['fail_pct'], '%', warn=r['fail_pct']>=10, bad=r['fail_pct']>=25)}</td>"
            f"<td>{r['ttfb_med']}s</td>"
            f"<td>{_num(r['ttfb_p90'], 's', warn=r['ttfb_p90']>=GOOD_TTFB, bad=r['ttfb_p90']>=GOOD_TTFB*1.5)}</td>"
            f"<td>{r['mbps_med']}</td></tr>")
    return (
        f"<h2>{_esc(title)}</h2><div class='scroll'><table>"
        f"<thead><tr><th>{_esc(keyname)}</th><th>probes</th><th>fail</th>"
        f"<th>ttfb&nbsp;med</th><th>ttfb&nbsp;p90</th><th>MB/s&nbsp;med</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>")


def _problem_picks(recs: list[dict], limit: int = 25) -> str:
    served = [r for r in recs if r.get("kind") == "served"]
    flagged = [r for r in served
               if (r.get("ttfb") or 0) > GOOD_TTFB
               or (r.get("mbps") is not None and r["mbps"] < SLOW_MBPS)]
    flagged = flagged[::-1][:limit]
    if not flagged:
        return ("<h2>Recent problem picks</h2><div class='note'>None — every "
                "auto-picked stream lately started promptly and streamed fast.</div>")
    rows = []
    for r in flagged:
        ttfb = r.get("ttfb") or 0
        mbps = r.get("mbps")
        rows.append(
            f"<tr class='warn'>"
            f"<td class='k1'>{_esc(r.get('id') or '?')}</td>"
            f"<td class='mut'>{_esc((r.get('label') or '')[:48])}</td>"
            f"<td>{_esc(r.get('src') or '—')}</td>"
            f"<td>{_esc(r.get('debrid') or '—')}</td>"
            f"<td>{_num(round(ttfb,1),'s', bad=ttfb>GOOD_TTFB)}</td>"
            f"<td>{'' if mbps is None else _num(mbps,'', bad=(mbps<SLOW_MBPS))}</td></tr>")
    return (
        "<h2>Recent problem picks</h2>"
        "<div class='note'>The #1 stream we actually served for these titles "
        "started slowly or streamed slowly to our probe — the likeliest to have "
        "buffered. Cross-check against anything that stuttered.</div>"
        "<div class='scroll'><table><thead><tr><th>title id</th><th>stream</th>"
        "<th>source</th><th>debrid</th><th>ttfb</th><th>MB/s</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")


def _buffer_incidents(recs: list[dict], limit: int = 30) -> str:
    """The buffering proxy's producer-side trouble log: which feeding source
    dropped/slowed/died, where in the file, and what it switched to. A clean
    stream leaves only start/complete records, which aren't shown."""
    import datetime as _dt
    bad = [r for r in recs if r.get("kind") == "buffer"
           and r.get("event") in ("drop", "slow", "failed", "twin", "reconnect")]
    bad = bad[::-1][:limit]
    if not bad:
        return ("<h2>Buffer incidents</h2><div class='note'>None — every buffered "
                "stream filled without a source drop, slowdown, or switch.</div>")
    sev = {"failed": "bad", "drop": "warn", "slow": "warn",
           "twin": "", "reconnect": ""}
    rows = []
    for r in bad:
        ev = r.get("event", "?")
        off = r.get("offset")
        off_s = f"{off / 1e9:.2f} GB" if off else "—"
        mbps = r.get("mbps")
        rows.append(
            f"<tr class='{sev.get(ev, '')}'>"
            f"<td class='k1'>{_dt.datetime.fromtimestamp(r.get('ts', 0)):%b %d %H:%M}</td>"
            f"<td>{_esc(ev)}</td>"
            f"<td>{_esc(r.get('id') or '?')}</td>"
            f"<td class='mut'>{_esc((r.get('src') or '—')[:34])}</td>"
            f"<td class='mut'>{_esc((r.get('node') or '—')[:30])}</td>"
            f"<td>{off_s}</td>"
            f"<td>{'' if mbps is None else mbps}</td>"
            f"<td class='mut'>{_esc((r.get('reason') or '')[:40])}</td></tr>")
    return (
        "<h2>Buffer incidents</h2>"
        "<div class='note'>Producer-side events behind the read-ahead buffer: "
        "'drop' = the feeding connection died (followed by 'reconnect' if it "
        "recovered), 'slow' = it fell below the file's bitrate at the write head "
        "('twin' = jumped to a byte-identical copy on another debrid), 'failed' = "
        "every source exhausted. The viewer only notices when runway ran out.</div>"
        "<div class='scroll'><table><thead><tr><th>when</th><th>event</th>"
        "<th>title id</th><th>source</th><th>node</th><th>at&nbsp;byte</th>"
        "<th>MB/s</th><th>reason</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")


def _play_table(recs: list[dict], key: str, title: str, keyname: str,
                min_n: int) -> str:
    rows = telemetry.aggregate_play(recs, key, min_n=min_n)
    if not rows:
        return (f"<h2>{_esc(title)}</h2><div class='note'>No playback logged yet — "
                "fills in once streams are watched through the proxy.</div>")
    body = []
    for r in rows:
        bad = r["dead_pct"] >= 20 or r["slow_pct"] >= 20
        warn = r["dead_pct"] >= 5 or r["slow_pct"] >= 10
        cls = "bad" if bad else ("warn" if warn else "")
        body.append(
            f"<tr class='{cls}'><td class='k1'>{_esc(r['key'])}</td>"
            f"<td>{r['n']}</td>"
            f"<td>{_num(r['dead_pct'], '%', warn=r['dead_pct']>=5, bad=r['dead_pct']>=20)}</td>"
            f"<td>{_num(r['slow_pct'], '%', warn=r['slow_pct']>=10, bad=r['slow_pct']>=20)}</td>"
            f"<td>{r['switch_pct']}%</td>"
            f"<td>{r['mbps_med']}</td>"
            f"<td>{r['watched_med']}%</td></tr>")
    return (
        f"<h2>{_esc(title)}</h2>"
        "<div class='note'>Measured on the actual bytes reaching the device — the "
        "ground truth. 'died' = failed mid-stream; 'buffered' = the source couldn't "
        "keep up mid-stream (→ 15-min cooldown); 'switched-away' = auto-failed-over "
        "from it at the start.</div>"
        f"<div class='scroll'><table><thead><tr><th>{_esc(keyname)}</th><th>plays</th>"
        "<th>died</th><th>buffered</th><th>switched-away</th><th>MB/s&nbsp;med</th>"
        "<th>watched&nbsp;med</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>")


def _blocklist_table(blocklist: list[dict]) -> str:
    if not blocklist:
        return ("<h2>Auto-blocked releases</h2><div class='note'>None yet. "
                "Debrid releases require repeated bad plays; direct NZB releases "
                "require two separated decisive failures. Network/provider errors "
                "only create a temporary retry cooldown.</div>")
    body = []
    for b in blocklist:
        state = ("<span class='bad'>BLOCKED</span>" if b["blocked"]
                 else "<span class='warn'>watching</span>")
        if b.get("kind") == "nzb":
            state += " <span class='pill'>NZB</span>"
        link = f"/api/unblock?sig={_esc(b['sig'])}"
        body.append(
            f"<tr class='{'bad' if b['blocked'] else ''}'>"
            f"<td class='k1'>{_esc(b['label'])}</td>"
            f"<td>{state}</td><td>{b['sessions']}</td><td>{b['nodes']}</td>"
            f"<td>{_esc(b['reason'])}</td><td>{b['age_h']}h</td>"
            f"<td><a href='{link}'>clear</a></td></tr>")
    return (
        "<h2>Auto-blocked releases</h2>"
        "<div class='note'>Torrent/debrid releases block after several separate "
        f"bad plays (≥{reputation.MIN_BLOCK_SESSIONS}); direct NZB releases cool "
        "down after one decisive failure and permanently block after two separated "
        "failures. Transient provider/network failures only create a retry cooldown.</div>"
        "<div class='scroll'><table><thead><tr><th>release</th><th>state</th>"
        "<th>bad&nbsp;evidence</th><th>nodes</th><th>last&nbsp;reason</th>"
        "<th>age</th><th></th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>")


def _nzb_indexer_table() -> str:
    rows = usenet_health.indexer_listing()
    if not rows:
        return ""
    body = "".join(
        f"<tr><td class='k1'>{_esc(r['name'])}</td>"
        f"<td>{r['score']:.3f}</td><td>{r['samples']}</td></tr>" for r in rows)
    return (
        "<h2>Direct usenet — learned indexer order</h2>"
        "<div class='note'>Bayesian-smoothed, time-decayed evidence from search, "
        "NZB fetch, probe, and playback outcomes. Higher-scoring indexers supply "
        "the first mount candidates; all indexers are still searched in parallel.</div>"
        "<div class='scroll'><table><thead><tr><th>indexer</th><th>score</th>"
        f"<th>evidence</th></tr></thead><tbody>{body}</tbody></table></div>")


def _nzb_failure_table(recs: list[dict]) -> str:
    import datetime as _dt
    rows = telemetry.aggregate_usenet_failures(recs, limit=100)
    if not rows:
        return ("<h2>Direct usenet — failure evidence</h2>"
                "<div class='note'>No detailed failure samples yet.</div>")
    body = []
    for row in rows:
        cls = "bad" if row["decision"] == "hard" else "warn"
        when = _dt.datetime.fromtimestamp(row["last_ts"]).strftime("%b %d %H:%M")
        body.append(
            f"<tr class='{cls}'><td>{_esc(when)}</td>"
            f"<td>{row['count']}</td><td>{_esc(row['stage'])}</td>"
            f"<td>{_esc(row['decision'])}</td><td>{_esc(row['reason'])}</td>"
            f"<td class='detail'>{_esc(row['detail'])}</td>"
            f"<td class='mut'>{_esc(row['label'])}</td>"
            f"<td class='mut'>{_esc(', '.join(row['indexers']))}</td></tr>")
    return (
        "<h2>Direct usenet — failure evidence</h2>"
        "<div class='note'>Credential-redacted, exact error samples grouped by "
        "message shape. Decision enums drive today’s cooldown/block policy; the "
        "sample text is retained to improve the checker later.</div>"
        "<div class='scroll'><table><thead><tr><th>last seen</th><th>count</th>"
        "<th>stage</th><th>decision</th><th>reason</th><th>sample</th>"
        "<th>release</th><th>indexers</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>")


def render(recs: list[dict], blocklist: list[dict],
           min_n: int = 3) -> str:
    probes = [r for r in recs if r.get("kind") == "probe"]
    served = [r for r in recs if r.get("kind") == "served"]
    plays = [r for r in recs if r.get("kind") == "play"]
    n_fail = sum(1 for r in probes if not r.get("ok"))
    fail_pct = round(100 * n_fail / len(probes), 1) if probes else 0.0
    switched = sum(1 for r in plays if r.get("switched"))
    switch_pct = round(100 * switched / len(plays), 1) if plays else 0.0
    n_blocked = sum(1 for b in blocklist if b["blocked"])
    tiles = [
        ("probes logged", len(probes)),
        ("probe fail rate", f"{fail_pct}%"),
        ("streams played", len(plays)),
        ("auto-switched", f"{switch_pct}%"),
        ("blocked sources", n_blocked),
    ]
    tile_html = "".join(
        f"<div class='tile'><div class='v'>{_esc(v)}</div>"
        f"<div class='k'>{_esc(k)}</div></div>" for k, v in tiles)
    span = ""
    if recs:
        import datetime as _dt
        t0 = min(r.get("ts", 0) for r in recs)
        t1 = max(r.get("ts", 0) for r in recs)
        span = (f"{_dt.datetime.fromtimestamp(t0):%b %d %H:%M} – "
                f"{_dt.datetime.fromtimestamp(t1):%b %d %H:%M}")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(ADDON_NAME)} — source health</title>
<style>{_CSS}{adminui.NAV_CSS}</style></head>
<body><div class="wrap">
{adminui.nav('stats', ADDON_NAME)}
<h1>Source health</h1>
<p class="sub">{_esc(span)} · playback numbers are ground truth (bytes reaching the
device via the proxy); probe numbers are our server's estimate. Worst first.</p>
<div class="tiles">{tile_html}</div>
{_blocklist_table(blocklist)}
{_nzb_indexer_table()}
{_nzb_failure_table(recs)}
{_play_table(recs, 'src', 'Real playback delivery — by source (indexer)', 'source', min_n)}
{_play_table(recs, 'node', 'Real playback delivery — by delivery node', 'node', min_n)}
{_buffer_incidents(recs)}
{_problem_picks(recs)}
{_table('Probe: by source (indexer)', telemetry.aggregate(probes, 'src', min_n=min_n), 'source')}
{_table('Probe: by debrid / cache tag', telemetry.aggregate(probes, 'debrid', min_n=min_n), 'tag')}
{_table('Probe: by release group', telemetry.aggregate(probes, 'grp', min_n=min_n), 'group')}
<p class="sub" style="margin-top:28px">A row is <span class="warn">amber</span> at
≥10% fails or p90 first-byte ≥{GOOD_TTFB*1.5:.0f}s, <span class="bad">red</span> at
≥25% fails. High p90 TTFB = the source often starts slow. Add
<code>?min_n=1</code> to include rarely-seen sources.</p>
</div></body></html>"""
