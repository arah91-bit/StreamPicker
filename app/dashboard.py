"""Renders the /health HTML page from the probe telemetry log.

The tables answer one question: which *sources* deliver badly? A source that
fails often, starts slowly (high TTFB), or streams slowly to our probe is the
one most likely to buffer on the viewer's device — the blacklist candidates.
The 'recent problem picks' list is the other direction: the actual #1 answers we
served that had poor delivery, i.e. the ones that probably buffered, so a report
of 'X buffered last night' can be checked against what we auto-picked for it.

Rendered on the shared Signal Room design system (app/uitheme): palette tokens,
the table shell, tiles, badges, dots and section heads all come from there.
Only genuinely page-specific cell styling lives in _CSS below — never palette
values, so both color schemes stay automatic.
"""

import os
from urllib.parse import quote

from app import (adminui, reputation, settings_ui, telemetry, uitheme,
                 usenet_health)

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

# Match the picker's soft gate so 'slow start' means the same thing everywhere.
GOOD_TTFB = float(os.environ.get("GOOD_TTFB", "4.0"))
SLOW_MBPS = float(os.environ.get("STATS_SLOW_MBPS", "4.0"))

# Page-specific cell styling only. The scrolling table shell (.tblwrap), row
# tints (tr.warn/tr.bad), tone utilities (.warn/.bad/.mut) and every component
# come from uitheme.BASE_CSS.
_CSS = """
.tblwrap table{min-width:660px}
td.id{font-family:var(--mono);font-size:12.5px;font-weight:600;overflow-wrap:anywhere}
td.v{font-family:var(--mono);font-size:12.5px;white-space:nowrap}
td.lbl{color:var(--mut);font-size:12.5px}
td.detail{white-space:pre-wrap!important;overflow-wrap:anywhere;max-width:460px;
font:12px/1.5 var(--mono);color:var(--mut)}
.legend{margin:30px 0 0}
"""


def _cls(row: dict) -> str:
    if row["fail_pct"] >= 25:
        return "bad"
    if row["ttfb_p90"] >= GOOD_TTFB * 1.5 or row["fail_pct"] >= 10:
        return "warn"
    return ""


def _num(v, unit="", warn=False, bad=False) -> str:
    c = "bad" if bad else ("warn" if warn else "")
    return f'<span class="{c}">{v}{unit}</span>'


def _tbl(head: list[str], body: list[str]) -> str:
    """Scrolling table shell. ``head`` entries are trusted literals (they may
    carry &nbsp;); ``body`` entries are pre-built <tr> markup."""
    cols = "".join(f"<th>{h}</th>" for h in head)
    return (f'<div class="tblwrap"><table><thead><tr>{cols}</tr></thead>'
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _probe_table(title: str, rows: list[dict], keyname: str) -> str:
    out = uitheme.section("PROBES", title)
    if not rows:
        return out + uitheme.empty("No data yet.")
    body = []
    for r in rows:
        body.append(
            f"<tr class='{_cls(r)}'>"
            f"<td class='id'>{uitheme.esc(r['key'])}</td>"
            f"<td class='v'>{r['n']}</td>"
            f"<td class='v'>{_num(r['fail_pct'], '%', warn=r['fail_pct']>=10, bad=r['fail_pct']>=25)}</td>"
            f"<td class='v'>{r['ttfb_med']}s</td>"
            f"<td class='v'>{_num(r['ttfb_p90'], 's', warn=r['ttfb_p90']>=GOOD_TTFB, bad=r['ttfb_p90']>=GOOD_TTFB*1.5)}</td>"
            f"<td class='v'>{r['mbps_med']}</td></tr>")
    return out + _tbl([uitheme.esc(keyname), "probes", "fail", "ttfb&nbsp;med",
                       "ttfb&nbsp;p90", "MB/s&nbsp;med"], body)


def _problem_picks(recs: list[dict], limit: int = 25) -> str:
    served = [r for r in recs if r.get("kind") == "served"]
    flagged = [r for r in served
               if (r.get("ttfb") or 0) > GOOD_TTFB
               or (r.get("mbps") is not None and r["mbps"] < SLOW_MBPS)]
    flagged = flagged[::-1][:limit]
    out = uitheme.section(
        "PLAYBACK", "Recent problem picks",
        "The #1 stream we actually served for these titles started slowly or "
        "streamed slowly to our probe — the likeliest to have buffered. "
        "Cross-check against anything that stuttered.")
    if not flagged:
        return out + uitheme.empty(
            "None — every auto-picked stream lately started promptly and "
            "streamed fast.")
    rows = []
    for r in flagged:
        ttfb = r.get("ttfb") or 0
        mbps = r.get("mbps")
        rows.append(
            f"<tr class='warn'>"
            f"<td class='id'>{uitheme.esc(r.get('id') or '?')}</td>"
            f"<td class='lbl'>{uitheme.esc((r.get('label') or '')[:48])}</td>"
            f"<td class='v'>{uitheme.esc(r.get('src') or '—')}</td>"
            f"<td class='v'>{uitheme.esc(r.get('debrid') or '—')}</td>"
            f"<td class='v'>{_num(round(ttfb,1),'s', bad=ttfb>GOOD_TTFB)}</td>"
            f"<td class='v'>{'' if mbps is None else _num(mbps,'', bad=(mbps<SLOW_MBPS))}</td></tr>")
    return out + _tbl(["title id", "stream", "source", "debrid", "ttfb",
                       "MB/s"], rows)


def _buffer_incidents(recs: list[dict], limit: int = 30) -> str:
    """The buffering proxy's producer-side trouble log: which feeding source
    dropped/slowed/died, where in the file, and what it switched to. A clean
    stream leaves only start/complete records, which aren't shown."""
    import datetime as _dt
    bad = [r for r in recs if r.get("kind") == "buffer"
           and r.get("event") in ("drop", "slow", "failed", "twin", "reconnect")]
    bad = bad[::-1][:limit]
    out = uitheme.section(
        "PLAYBACK", "Buffer incidents",
        "Producer-side events behind the read-ahead buffer: 'drop' = the "
        "feeding connection died (followed by 'reconnect' if it recovered), "
        "'slow' = it fell below the file's bitrate at the write head ('twin' = "
        "jumped to a byte-identical copy on another debrid), 'failed' = every "
        "source exhausted. The viewer only notices when runway ran out.")
    if not bad:
        return out + uitheme.empty(
            "None — every buffered stream filled without a source drop, "
            "slowdown, or switch.")
    # event -> (row tint, badge tone): recoveries stay untinted but read teal.
    sev = {"failed": ("bad", "bad"), "drop": ("warn", "warn"),
           "slow": ("warn", "warn"), "twin": ("", "teal"),
           "reconnect": ("", "teal")}
    rows = []
    for r in bad:
        ev = r.get("event", "?")
        row_cls, tone = sev.get(ev, ("", ""))
        off = r.get("offset")
        off_s = f"{off / 1e9:.2f} GB" if off else "—"
        mbps = r.get("mbps")
        rows.append(
            f"<tr class='{row_cls}'>"
            f"<td class='v'>{_dt.datetime.fromtimestamp(r.get('ts', 0)):%b %d %H:%M}</td>"
            f"<td>{uitheme.badge(ev, tone)}</td>"
            f"<td class='id'>{uitheme.esc(r.get('id') or '?')}</td>"
            f"<td class='lbl'>{uitheme.esc((r.get('src') or '—')[:34])}</td>"
            f"<td class='lbl'>{uitheme.esc((r.get('node') or '—')[:30])}</td>"
            f"<td class='v'>{off_s}</td>"
            f"<td class='v'>{'' if mbps is None else mbps}</td>"
            f"<td class='lbl'>{uitheme.esc((r.get('reason') or '')[:40])}</td></tr>")
    return out + _tbl(["when", "event", "title id", "source", "node",
                       "at&nbsp;byte", "MB/s", "reason"], rows)


def _play_table(recs: list[dict], key: str, title: str, keyname: str,
                min_n: int) -> str:
    rows = telemetry.aggregate_play(recs, key, min_n=min_n)
    out = uitheme.section(
        "PLAYBACK", title,
        "Measured on the actual bytes reaching the device — the ground truth. "
        "'died' = failed mid-stream; 'buffered' = the source couldn't keep up "
        "mid-stream (→ 15-min cooldown); 'switched-away' = auto-failed-over "
        "from it at the start.")
    if not rows:
        return out + uitheme.empty(
            "No playback logged yet — fills in once streams are watched "
            "through the proxy.")
    body = []
    for r in rows:
        bad = r["dead_pct"] >= 20 or r["slow_pct"] >= 20
        warn = r["dead_pct"] >= 5 or r["slow_pct"] >= 10
        cls = "bad" if bad else ("warn" if warn else "")
        body.append(
            f"<tr class='{cls}'><td class='id'>{uitheme.esc(r['key'])}</td>"
            f"<td class='v'>{r['n']}</td>"
            f"<td class='v'>{_num(r['dead_pct'], '%', warn=r['dead_pct']>=5, bad=r['dead_pct']>=20)}</td>"
            f"<td class='v'>{_num(r['slow_pct'], '%', warn=r['slow_pct']>=10, bad=r['slow_pct']>=20)}</td>"
            f"<td class='v'>{r['switch_pct']}%</td>"
            f"<td class='v'>{r['mbps_med']}</td>"
            f"<td class='v'>{r['watched_med']}%</td></tr>")
    return out + _tbl([uitheme.esc(keyname), "plays", "died", "buffered",
                       "switched-away", "MB/s&nbsp;med",
                       "watched&nbsp;med"], body)


def _blocklist_table(blocklist: list[dict]) -> str:
    out = uitheme.section(
        "REPUTATION", "Auto-blocked releases",
        f"Torrent/debrid releases block after several separate bad plays "
        f"(≥{reputation.MIN_BLOCK_SESSIONS}); direct NZB releases cool down "
        f"after one decisive failure and permanently block after two separated "
        f"failures. Transient provider/network failures only create a retry "
        f"cooldown.")
    if not blocklist:
        return out + uitheme.empty(
            "None yet. Debrid releases require repeated bad plays; direct NZB "
            "releases require two separated decisive failures. "
            "Network/provider errors only create a temporary retry cooldown.")
    body = []
    for b in blocklist:
        state = (uitheme.badge("BLOCKED", "bad") if b["blocked"]
                 else uitheme.badge("watching", "warn"))
        if b.get("kind") == "nzb":
            state += " " + uitheme.badge("NZB", "teal")
        link = f"/api/unblock?sig={uitheme.esc(b['sig'])}"
        body.append(
            f"<tr class='{'bad' if b['blocked'] else ''}'>"
            f"<td class='id'>{uitheme.esc(b['label'])}</td>"
            f"<td>{state}</td><td class='v'>{b['sessions']}</td>"
            f"<td class='v'>{b['nodes']}</td>"
            f"<td class='lbl'>{uitheme.esc(b['reason'])}</td>"
            f"<td class='v'>{b['age_h']}h</td>"
            f"<td><a href='{link}'>clear</a></td></tr>")
    return out + _tbl(["release", "state", "bad&nbsp;evidence", "nodes",
                       "last&nbsp;reason", "age", ""], body)


def _decode_table() -> str:
    from app import decode_health
    rows = decode_health.listing()
    if not rows:
        return ""
    body = []
    for r in rows:
        state = (uitheme.badge("UNDECODABLE", "bad") if r["bad"]
                 else uitheme.badge("watching", "warn") if r["rejects"]
                 else uitheme.badge("plays fine", "ok"))
        link = f"/api/decode/clear?key={uitheme.esc(r['key'])}"
        body.append(
            f"<tr class='{'bad' if r['bad'] else ''}'>"
            f"<td class='id'>{uitheme.esc(r['key'])}</td><td>{state}</td>"
            f"<td class='v'>{r['rejects']}</td><td class='v'>{r['plays']}</td>"
            f"<td class='lbl'>{uitheme.esc('; '.join(r['labels']))}</td>"
            f"<td><a href='{link}'>clear</a></td></tr>")
    return uitheme.section(
        "DECODE", "Player decode compatibility (learned)",
        "Codec attributes struck by player-rejected streams and credited by "
        "real playback. An UNDECODABLE attribute demotes matching releases "
        "below every clean candidate (never removes them). Clear an attribute "
        "after upgrading a player.") + _tbl(
            ["attribute", "state", "rejects", "plays", "example releases", ""],
            body)


def _nzb_indexer_table() -> str:
    rows = usenet_health.indexer_listing()
    if not rows:
        return ""
    body = []
    for r in rows:
        allowed = r.get("fetch_allowed", True)
        state = (uitheme.status_dot("ok", "ready") if allowed
                 else uitheme.badge("FETCH BLOCKED", "bad"))
        clear = ("" if allowed else
                 f"<a href='/api/nzb-indexer/clear?name="
                 f"{quote(r['name'], safe='')}'>retry</a>")
        body.append(
            f"<tr class='{'' if allowed else 'bad'}'>"
            f"<td class='id'>{uitheme.esc(r['name'])}</td>"
            f"<td class='v'>{r['score']:.3f}</td>"
            f"<td class='v'>{r['samples']}</td>"
            f"<td class='v'>{r.get('fetch_ok', 0):g}/{r.get('fetch_fail', 0):g}</td>"
            f"<td>{state}</td><td>{clear}</td></tr>")
    return uitheme.section(
        "USENET", "Direct usenet — learned indexer order",
        "Bayesian-smoothed, time-decayed evidence from search, NZB fetch, "
        "probe, and playback outcomes. Higher-scoring indexers supply the "
        "first mount candidates; all indexers are still searched in parallel. "
        "An endpoint with zero successful NZB downloads after sustained "
        "failures is persistently suppressed; use retry after repairing its "
        "account/plan.") + _tbl(
            ["indexer", "score", "evidence", "NZB fetch ok/fail",
             "fetch state", ""], body)


def _nzb_failure_table(recs: list[dict]) -> str:
    import datetime as _dt
    rows = telemetry.aggregate_usenet_failures(recs, limit=100)
    out = uitheme.section(
        "USENET", "Direct usenet — failure evidence",
        "Credential-redacted, exact error samples grouped by message shape. "
        "Decision enums drive today’s cooldown/block policy; the sample text "
        "is retained to improve the checker later.")
    if not rows:
        return out + uitheme.empty("No detailed failure samples yet.")
    body = []
    for row in rows:
        cls = "bad" if row["decision"] == "hard" else "warn"
        when = _dt.datetime.fromtimestamp(row["last_ts"]).strftime("%b %d %H:%M")
        body.append(
            f"<tr class='{cls}'><td class='v'>{uitheme.esc(when)}</td>"
            f"<td class='v'>{row['count']}</td>"
            f"<td class='v'>{uitheme.esc(row['stage'])}</td>"
            f"<td>{uitheme.badge(row['decision'], 'bad' if row['decision'] == 'hard' else 'warn')}</td>"
            f"<td class='v'>{uitheme.esc(row['reason'])}</td>"
            f"<td class='detail'>{uitheme.esc(row['detail'])}</td>"
            f"<td class='lbl'>{uitheme.esc(row['label'])}</td>"
            f"<td class='lbl'>{uitheme.esc(', '.join(row['indexers']))}</td></tr>")
    return out + _tbl(["last seen", "count", "stage", "decision", "reason",
                       "sample", "release", "indexers"], body)


def render(recs: list[dict], blocklist: list[dict],
           min_n: int = 3) -> str:
    probes = [r for r in recs if r.get("kind") == "probe"]
    plays = [r for r in recs if r.get("kind") == "play"]
    n_fail = sum(1 for r in probes if not r.get("ok"))
    fail_pct = round(100 * n_fail / len(probes), 1) if probes else 0.0
    switched = sum(1 for r in plays if r.get("switched"))
    switch_pct = round(100 * switched / len(plays), 1) if plays else 0.0
    n_blocked = sum(1 for b in blocklist if b["blocked"])
    cache = telemetry.aggregate_cache(recs)
    # Headline first, cache internals in their own group below — eleven flat
    # tiles read as a wall with nothing to look at first.
    headline = [
        ("probes logged", len(probes)),
        ("probe fail rate", f"{fail_pct}%"),
        ("streams played", len(plays)),
        ("auto-switched", f"{switch_pct}%"),
        ("blocked sources", n_blocked),
    ]
    cache_tiles = [
        ("E+1 ready", cache["prewarm_ready"]),
        ("E+1 ready median", f"{cache['prewarm_seconds_med']}s"),
        ("stale links revived", f"{cache['stale_success_pct']}%"),
        ("dead probes avoided", cache["probes_avoided"]),
        ("pack members reused", cache["pack_members_reused"]),
        ("playable identity rejects", cache["identity_rejected"]),
    ]
    tile_html = "".join(uitheme.tile(str(v), k) for k, v in headline)
    cache_html = "".join(uitheme.tile(str(v), k) for k, v in cache_tiles)
    span = ""
    if recs:
        import datetime as _dt
        t0 = min(r.get("ts", 0) for r in recs)
        t1 = max(r.get("ts", 0) for r in recs)
        span = (f"{_dt.datetime.fromtimestamp(t0):%b %d %H:%M} – "
                f"{_dt.datetime.fromtimestamp(t1):%b %d %H:%M}")
    subtitle = (
        (f"Window <code>{uitheme.esc(span)}</code> · " if span else "")
        + "Playback numbers are ground truth (bytes reaching the device via "
          "the proxy); probe numbers are our server's estimate. Worst first.")
    cache_note = (
        f'<div class="callout">Next-episode readiness: '
        f'<span class="mono">{cache["prewarm_cache_hits"]}</span> requests were '
        f'already warm; completed prewarms took p90 '
        f'<span class="mono">{cache["prewarm_seconds_p90"]}s</span>. '
        f'Stale-link revival is a live re-probe, never trust carried across '
        f'the three-hour URL window '
        f'(<span class="mono">{cache["stale_revalidated"]}/'
        f'{cache["stale_attempts"]}</span> passed).</div>')
    rare = ("" if min_n <= 1 else
            f' — <a href="?min_n=1">show rarely-seen sources</a>')
    legend = (
        f'<p class="sub legend">A row is <span class="warn">amber</span> at '
        f'≥10% fails or p90 first-byte ≥{GOOD_TTFB*1.5:.0f}s, '
        f'<span class="bad">red</span> at ≥25% fails. High p90 TTFB = the '
        f'source often starts slow. Sources seen fewer than '
        f'<code>{min_n}</code> times are hidden{rare}.</p>')

    if not recs:
        # A fresh install has nothing to diagnose. Nine "no data yet" cards
        # say that nine times; one card says it once and explains why.
        return uitheme.shell(
            title="Health", name=ADDON_NAME, active="health",
            csrf=adminui.csrf_token(),
            body=(f"<div class='pagehead'><p class='eyebrow'>TELEMETRY</p>"
                  f"<h1>Health</h1><p class='sub'>Which sources deliver "
                  f"badly — the ones worth blacklisting.</p></div>"
                  + uitheme.empty(
                      "Nothing measured yet. This page fills in as streams "
                      "are probed and watched through the addon — it needs "
                      "the proxy, so the 'Direct links' stream path records "
                      "nothing. Come back after a few plays.")),
            head=f"<style>{_CSS}</style>",
            search=settings_ui.search_index())

    body = (
        f'<div class="tiles">{tile_html}</div>'
        + uitheme.section("CACHE", "Prefetch & reuse",
                          "work the caches saved, and what it cost")
        + f'<div class="tiles">{cache_html}</div>'
        + cache_note
        + _blocklist_table(blocklist)
        + _decode_table()
        + _nzb_indexer_table()
        + _nzb_failure_table(recs)
        + _play_table(recs, 'src',
                      'Real playback delivery — by source (indexer)',
                      'source', min_n)
        + _play_table(recs, 'node',
                      'Real playback delivery — by delivery node',
                      'node', min_n)
        + _buffer_incidents(recs)
        + _problem_picks(recs)
        + _probe_table('Probe: by source (indexer)',
                       telemetry.aggregate(probes, 'src', min_n=min_n),
                       'source')
        + _probe_table('Probe: by debrid / cache tag',
                       telemetry.aggregate(probes, 'debrid', min_n=min_n),
                       'tag')
        + _probe_table('Probe: by release group',
                       telemetry.aggregate(probes, 'grp', min_n=min_n),
                       'group')
        + legend)
    head_block = (
        f"<div class='pagehead'><p class='eyebrow'>TELEMETRY</p>"
        f"<h1>Health</h1><p class='sub'>{subtitle}</p></div>")
    return uitheme.shell(
        title="Health", name=ADDON_NAME, active="health",
        csrf=adminui.csrf_token(),
        body=head_block + body, head=f"<style>{_CSS}</style>",
        search=settings_ui.search_index())
