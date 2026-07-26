"""The telemetry blocks Home renders: ``now_playing`` and ``ledger``.

Where /health/sources is the worst-first diagnostic view (which source to
blame), these are the opposite mood: the headline totals and the satisfying
superlatives — gigabytes streamed, hours watched, how often failover quietly
saved a stream, what the direct-usenet lane is really hitting and why it whiffs
the rest. ``now_playing`` is the live strip above them.

Every number is measured, not modelled: it comes from the same playback and
probe telemetry the diagnostics use (bytes and seconds are recorded per play in
telemetry.record_play), so nothing here is invented. On a fresh install the
sections say so rather than showing zeros dressed up as achievements.

Built on the Signal Room design system (app/uitheme.py): all chrome, palette,
and shared components come from there. The page-specific CSS these blocks need
(hero number, gauges, sparkline, now-playing cards) is exported as ``CSS`` for
the page that hosts them to inline — see app/home_ui.py.
"""

import os
import re
import time
from collections import defaultdict

from app import proxy, uitheme
from app.uitheme import esc as _esc

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

# Page-specific CSS only — everything palette/chrome-related is BASE_CSS.
# Public: the page hosting these blocks inlines it (home_ui).
CSS = """
.lhero{margin:0 0 26px}
.lhero .head{color:var(--mut);font-size:14px;margin-bottom:2px}
.lhero .big{font-size:clamp(46px,9vw,80px);line-height:1.05;font-weight:700;
letter-spacing:-.02em;color:var(--accent);font-variant-numeric:tabular-nums}
.lhero .big .u{font-size:.4em;font-weight:600;color:var(--mut);margin-left:8px}
.lhero .tail{font-size:15px;margin-top:8px;max-width:66ch}
.lhero .tail b{font-variant-numeric:tabular-nums}

.pad{padding:16px 18px}
.pt{font:600 10.5px var(--mono);letter-spacing:.14em;text-transform:uppercase;
color:var(--mut);margin:0 0 12px}

.hbar{margin:10px 0}
.hbar:last-of-type{margin-bottom:2px}
.hbar .lab{display:flex;justify-content:space-between;align-items:baseline;
gap:12px;font-size:13px;margin-bottom:5px}
.hbar .lab .n{color:var(--mut);font:12px var(--mono);white-space:nowrap;
font-variant-numeric:tabular-nums}

.gauge{display:flex;align-items:baseline;gap:10px;margin-bottom:4px}
.gauge .pct{font-size:38px;font-weight:700;color:var(--accent);
font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.gauge .of{color:var(--mut);font-size:13px}
.note{color:var(--mut);font-size:12.5px;margin:8px 0 0}

.npc{padding:14px 16px}
.npc .t{font-weight:650;font-size:14.5px;display:flex;align-items:center;
gap:9px;margin-bottom:9px}
.npc .t .tt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.npc .meta{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;
font-size:12.5px;color:var(--mut);margin-bottom:12px}
.plab{font:11px var(--mono);color:var(--mut);margin-top:6px;display:flex;
justify-content:space-between;gap:10px}

.spark{display:flex;align-items:flex-end;gap:6px;height:108px;padding-top:8px}
.spark .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
align-items:center;gap:6px;min-width:0}
.spark .stk{width:100%;max-width:34px;border-radius:4px 4px 0 0;
background:var(--accent);min-height:2px}
.spark .col.zero .stk{background:var(--track)}
.spark .d{font:10px var(--mono);color:var(--mut);white-space:nowrap}
"""


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
    """Render the 'Now Playing' section — one card per active buffered stream.
    Returns empty string when nothing is playing (no visual footprint)."""
    if not playing:
        return ""
    cards = []
    for s in playing:
        mid = s.get("media_id", "")
        title = _esc(names.get(mid, mid) or "Unknown")
        lbl = _esc(s.get("label", ""))
        dbr = (s.get("debrid") or "").strip()
        res = s.get("res", 0)
        node = s.get("node", "")
        avail = s.get("avail") or 0
        total = s.get("total")
        consumers = s.get("consumers", 1)

        meta = [f"<span>{lbl}</span>"]
        if res:
            meta.append(uitheme.badge(f"{res}p", "teal"))
        if dbr:
            meta.append(uitheme.badge(dbr, "info"))
        # node — shorten to first segment (e.g. nexus-190)
        node_short = node.split(".")[0] if node else ""
        if node_short:
            meta.append(f'<span class="tag">{_esc(node_short)}</span>')
        meta.append(f"<span>{consumers} viewer{'s' if consumers != 1 else ''}"
                    f"</span>")

        # buffered-progress meter
        if total and total > 0:
            pct = min(100.0, 100.0 * avail / total)
            avail_gb = avail / (1024 ** 3)
            total_gb = total / (1024 ** 3)
            prog = (uitheme.meter(pct, "teal")
                    + f"<div class='plab'><span>{avail_gb:.2f} / "
                      f"{total_gb:.2f} GB buffered</span>"
                      f"<span>{pct:.0f}%</span></div>")
        else:
            prog = (uitheme.meter(0, "teal")
                    + "<div class='plab'><span>buffering…</span></div>")

        cards.append(
            f"<div class='card npc'>"
            f"<div class='t'>{uitheme.status_dot('run')}"
            f"<span class='tt'>{title}</span></div>"
            f"<div class='meta'>{''.join(meta)}</div>{prog}</div>")

    return (uitheme.section("ON AIR", "Now Playing",
                           "live buffered streams · page reloads every 30 s")
            + f"<div class='cards'>{''.join(cards)}</div>")


# legacy fill-class → meter tone (kept so the call sites read the same)
_TONE = {"a2": "teal", "g": "ok", "w": "warn", "b": "bad"}


def _bar(label: str, n: int, total: int, cls: str = "", note: str = "") -> str:
    pct = (100 * n / total) if total else 0
    right = note or f"{n:,} · {pct:.0f}%"
    return (f"<div class='hbar'><div class='lab'><span>{_esc(label)}</span>"
            f"<span class='n'>{_esc(right)}</span></div>"
            f"{uitheme.meter(pct, _TONE.get(cls, ''))}</div>")


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
    return f"<div class='card pad'><h3 class='pt'>{_esc(title)}</h3>{bars}</div>"


# ── friendly names for the machine-y usenet failure reasons ──────────────────
_REASON = {
    "missing-articles": "Missing articles (incomplete on usenet)",
    "broken-archive": "Broken/unsupported archive",
    "encrypted": "Password-protected archive",
    "nzbdav-backend": "nzbdav backend fault (not the release)",
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


def now_playing(recs: list[dict]) -> str:
    """The live strip: one card per stream currently moving through the proxy.
    Empty string when nothing is playing, so Home has no dead section."""
    return _now_playing(proxy.active_stream_details(), _title_map(recs))


def ledger(recs: list[dict]) -> str:
    """The measured history block Home renders below the fold: totals, where
    the streams came from, the direct-usenet report card, records, and a
    two-week sparkline. Every number is telemetry, never modelled."""
    plays = [r for r in recs if r.get("kind") == "play"]
    probes = [r for r in recs if r.get("kind") == "probe"]
    nzb_probes = [r for r in probes if r.get("lane") == "nzb"]
    nzb_fails = [r for r in recs if r.get("kind") == "nzb_failure"]
    buffers = [r for r in recs if r.get("kind") == "buffer"]
    names = _title_map(recs)

    total_mb = sum(r.get("mb") or 0 for r in plays)
    total_secs = sum(r.get("secs") or 0 for r in plays)
    n_titles = len({r.get("id") for r in plays if r.get("id")})
    n_switch = sum(1 for r in plays if r.get("switched"))
    n_recon = sum(r.get("reconnects") or 0 for r in plays)
    n_twin = sum(1 for r in buffers if r.get("event") == "twin")
    n_reject = sum(1 for r in buffers if r.get("event") == "player_rejected")
    n_recover = sum(1 for r in buffers if r.get("event") == "recovery_ok")
    avg_mbps = (total_mb * 8 / total_secs) if total_secs else 0

    dv, du = _data(total_mb)
    hv, hu = _dur(total_secs)

    # ── the ledger's opening statement ──
    if plays:
        hero = (
            f"<div class='lhero'><div class='head'>{_esc(ADDON_NAME)} has "
            f"streamed</div><div class='big'>{dv}<span class='u'>{du}</span>"
            f"</div><div class='tail'>of video to your screens — across "
            f"<b>{hv} {hu}</b> of watching and <b>{_num(len(plays))}</b> "
            f"stream{'s' if len(plays) != 1 else ''} of "
            f"<b>{_num(n_titles)}</b> title{'s' if n_titles != 1 else ''}."
            f"</div></div>")
    else:
        hero = ("<div class='lhero'><div class='head'>"
                f"{_esc(ADDON_NAME)}</div><div class='big'>—</div>"
                "<div class='tail'>Nothing streamed through the proxy yet. "
                "Watch something and this fills in.</div></div>")

    # ── headline tiles ──
    tiles = []
    if plays:
        tiles.append(uitheme.tile(_num(len(plays)), "streams played"))
        tiles.append(uitheme.tile(f"{avg_mbps:.1f}<small>Mbps</small>",
                                  "avg bitrate", raw=True))
        tiles.append(uitheme.tile(_num(n_switch), "failover saves",
                                  "times a dying source was swapped at the "
                                  "start"))
        tiles.append(uitheme.tile(_num(n_recon), "reconnects ridden out",
                                  "mid-stream drops the buffer recovered from "
                                  "invisibly"))
        if n_twin:
            tiles.append(uitheme.tile(_num(n_twin), "twin splices",
                                      "jumped to an identical copy on another "
                                      "debrid"))
        if n_reject:
            tiles.append(uitheme.tile(_num(n_recover), "spinners auto-fixed",
                                      f"of {_num(n_reject)} files your player "
                                      f"couldn't open, caught and swapped"))
    tiles_section = (f"<div class='tiles'>{''.join(tiles)}</div>"
                     if tiles else "")

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
            "<div class='card pad'><h3 class='pt'>Cache hits</h3>"
            f"<div class='gauge'><span class='pct'>"
            f"{100 * cached / np:.0f}%</span>"
            "<span class='of'>of plays came straight from a cached copy</span>"
            "</div><p class='note'>Cached streams start fastest and rarely "
            "buffer.</p></div>")
        provenance = (
            uitheme.section("PROVENANCE", "Where your streams came from",
                            "measured on the bytes that reached the device")
            + f"<div class='cards'>{res_panel}{dbr_panel}{hdr_panel}"
              f"{cache_panel}</div>")

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
            f"<tr><td class='mono'>{_esc(ix)}</td><td>{g['n']}</td>"
            f"<td>{100 * g['ok'] / g['n']:.0f}%</td></tr>"
            for ix, g in ix_rows)
        ix_table = ("<div class='card pad'><h3 class='pt'>Which usenet "
                    "indexers land</h3><table><thead><tr><th>indexer</th>"
                    f"<th>tries</th><th>worked</th></tr></thead>"
                    f"<tbody>{body}</tbody></table></div>")

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
        why_panel = (f"<div class='card pad'><h3 class='pt'>Why usenet links "
                     f"fall through</h3>{bars}</div>")

    usenet_section = ""
    if nzb_n or total_reasons:
        rate = (100 * nzb_ok / nzb_n) if nzb_n else 0
        gauge = (
            "<div class='card pad'><h3 class='pt'>Usenet strike rate</h3>"
            f"<div class='gauge'><span class='pct'>{rate:.0f}%</span>"
            f"<span class='of'>of {_num(nzb_n)} verified attempts played</span>"
            "</div><p class='note'>Usenet is a coin-flip by nature — releases "
            "go missing and indexers lie about what they have. The probe is "
            "the gate that keeps the misses off your screen.</p></div>")
        usenet_section = (
            uitheme.section("DIRECT USENET", "Report card",
                            "what the self-hosted lane is actually hitting")
            + f"<div class='cards'>{gauge}{ix_table}{why_panel}</div>")

    # ── superlatives ──
    recs_html = ""
    if plays:
        def _who(r):
            return names.get(r.get("id"), r.get("id") or "?")
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
        recs_html = (uitheme.section("SUPERLATIVES", "Records",
                                     "best single plays since recording began")
                     + "<div class='tiles'>" + "".join(
                         uitheme.tile(v, c, w) for c, v, w in cards)
                     + "</div>")

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
        spark = (uitheme.section("ACTIVITY", "Last two weeks",
                                 "gigabytes streamed per day")
                 + "<div class='card pad'><div class='spark'>"
                 + "".join(cols) + "</div></div>")

    since = ""
    if recs:
        t0 = min(r.get("ts", 0) for r in recs)
        since = (f"since {time.strftime('%b %-d, %Y', time.localtime(t0))}, "
                 "measured through the addon's proxy")

    empty = ("" if plays else uitheme.empty(
        "No playback has been recorded yet. This fills in once streams are "
        "watched through the addon (it needs the proxy — the 'Direct links' "
        "stream path records nothing)."))

    return (uitheme.section("LEDGER", "What it has delivered",
                            since or "every number here is measured, not "
                                     "estimated")
            + hero + empty + tiles_section + provenance + usenet_section
            + recs_html + spark)
