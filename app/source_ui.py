"""One source, end to end: what it did, who serves it, and what you can do.

Reached from the home-page warning. The warning names a source; this answers
the three questions that immediately follow — *why* is it failing, *whose*
addon is it, and *how do I make this stop* — because a dead-end alert is one
you learn to ignore.
"""

from __future__ import annotations

import collections
import os
import time
from urllib.parse import quote

from app import source_health, sources, telemetry, uitheme, usenet_health

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

# Probe history is a diagnosis aid, not an archive: enough rows to see whether
# something is dead or merely flaky, few enough to read.
HISTORY_ROWS = 40

_CSS = """
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:12px;margin:14px 0 6px}
.fact{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:12px 14px}
.fact .k{color:var(--mut);font-size:11.5px;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:3px}
.fact .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
.fact .v.bad{color:var(--bad)}.fact .v.ok{color:var(--ok)}
.why{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:14px 16px;margin:14px 0}
.why h3{margin:0 0 8px;font-size:14px}
.why .err{display:flex;justify-content:space-between;gap:16px;padding:5px 0;
border-bottom:1px solid var(--line);font-size:13px}
.why .err:last-child{border-bottom:0}
.why .err .n{font:12px var(--mono);color:var(--mut)}
.why .err code{background:var(--inset);padding:1px 6px;border-radius:5px;
font:12.5px var(--mono)}
.acts{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 4px;align-items:center}
.acts form{margin:0}
.decided{background:var(--inset);border:1px solid var(--line2);
border-radius:var(--r);padding:11px 14px;margin:14px 0;font-size:13px}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r)}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.05em}
tr:last-child td{border-bottom:0}
td.t{font:12px var(--mono);color:var(--mut)}
td.bad{color:var(--bad)}td.ok{color:var(--ok)}
"""


def _ago(ts: float, now: float) -> str:
    if not ts:
        return "never"
    hours = (now - ts) / 3600
    if hours < 1:
        return f"{max(1, round(hours * 60))}m ago"
    if hours < 48:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


def addon_for(name: str, recs: list[dict]) -> dict:
    """Which lane carried this source's streams.

    Probe records do not carry the lane, but the identity records written for
    the same streams do, so the attribution is recovered by joining on the
    source name rather than guessed from configuration.
    """
    keys = collections.Counter(
        r.get("source_key") for r in recs
        if (r.get("src") or "") == name and r.get("source_key"))
    if not keys:
        return {"key": "", "label": "", "url": "", "configured": True}
    key = keys.most_common(1)[0][0]
    for meta in getattr(sources, "EXTRA_META", []):
        if meta.get("key") == key:
            return {"key": key, "label": meta.get("name") or key,
                    "url": meta.get("url") or "", "configured": True}
    if not str(key).startswith("x:"):
        # A built-in lane: it is configured as long as the build has it.
        return {"key": key, "label": str(key), "url": "", "configured": True}
    # An "x:" lane that no longer resolves is a custom addon that has since
    # been removed — worth saying outright, because it means the failure is
    # already historical and there is nothing left to repair.
    return {"key": key, "label": str(key)[2:], "url": "", "configured": False}


def summarize(name: str, recs: list[dict]) -> dict:
    """Everything the page states about a source, computed once."""
    probes = [r for r in recs
              if r.get("kind") == "probe" and (r.get("src") or "") == name]
    ok = [r for r in probes if r.get("ok")]
    stamps = [r.get("ts") or 0 for r in probes]
    reasons = collections.Counter(
        (r.get("reason") or r.get("err") or "unknown") for r in probes
        if not r.get("ok"))
    return {
        "name": name,
        "probes": len(probes),
        "ok": len(ok),
        "fail_pct": round(100 * (len(probes) - len(ok)) / len(probes), 1)
                    if probes else 0.0,
        "first_ts": min(stamps) if stamps else 0,
        "last_ts": max(stamps) if stamps else 0,
        "last_ok_ts": max((r.get("ts") or 0 for r in ok), default=0),
        "reasons": reasons.most_common(8),
        "history": sorted(probes, key=lambda r: r.get("ts") or 0,
                          reverse=True)[:HISTORY_ROWS],
        "addon": addon_for(name, recs),
        "indexer": next((r for r in usenet_health.indexer_listing()
                         if r.get("name") == name), None),
    }


def _facts(s: dict, now: float) -> str:
    fail_class = "bad" if s["fail_pct"] >= 50 else "ok" if s["probes"] else ""
    tiles = [
        ("probes", str(s["probes"]), ""),
        ("failed", f"{s['fail_pct']}%", fail_class),
        ("last tried", _ago(s["last_ts"], now), ""),
        ("last worked", _ago(s["last_ok_ts"], now),
         "" if s["last_ok_ts"] else "bad"),
    ]
    return '<div class="facts">' + "".join(
        f'<div class="fact"><div class="k">{uitheme.esc(k)}</div>'
        f'<div class="v {cls}">{uitheme.esc(v)}</div></div>'
        for k, v, cls in tiles) + "</div>"


def _why(s: dict) -> str:
    """The errors themselves, verbatim. Whether this is worth retrying is a
    judgement call, and it cannot be made from 'it failed'."""
    if not s["reasons"]:
        return ""
    rows = "".join(
        f'<div class="err"><code>{uitheme.esc(str(reason)[:120])}</code>'
        f'<span class="n">{count}&times;</span></div>'
        for reason, count in s["reasons"])
    return (f'<div class="why"><h3>What it returned</h3>{rows}</div>')


def _served_by(s: dict) -> str:
    addon = s["addon"]
    if not addon["key"]:
        return ""
    if not addon["configured"]:
        body = (f'Came from the custom addon <b>{uitheme.esc(addon["label"])}'
                f'</b>, which is <b>no longer configured</b>. Nothing here is '
                f'still being searched — clearing this source is enough.')
    else:
        where = (f' (<span class="mono">{uitheme.esc(addon["url"])}</span>)'
                 if addon["url"] else "")
        body = (f'Served through <b>{uitheme.esc(addon["label"])}</b>{where}. '
                f'To stop searching that addon entirely rather than this one '
                f'source, remove it under <a href="/connect">Connect</a>.')
    return f'<div class="why"><h3>Who serves it</h3><p>{body}</p></div>'


def _decision(s: dict, now: float) -> str:
    state = source_health.state(s["name"])
    if not state:
        return ""
    when = _ago(source_health.decided_at(s["name"]), now)
    if state == "blocked":
        text = (f"<b>Blocked {uitheme.esc(when)}.</b> Its releases are not "
                "offered as candidates. Clear below to start using it again.")
    else:
        text = (f"<b>Dismissed {uitheme.esc(when)}.</b> Still searched and "
                "still picked — only the home-page warning is silenced.")
    return f'<div class="decided">{text}</div>'


def _actions(s: dict) -> str:
    name = quote(s["name"])
    state = source_health.state(s["name"])
    buttons = []
    if state:
        buttons.append(
            f'<form method="post" action="/api/source/clear?name={name}">'
            f'<button class="btn" type="submit">Clear decision — use it '
            f'normally again</button></form>')
    else:
        buttons.append(
            f'<form method="post" action="/api/source/dismiss?name={name}">'
            f'<button class="btn" type="submit">Dismiss warning</button>'
            f'</form>')
    if state != "blocked":
        buttons.append(
            f'<form method="post" action="/api/source/block?name={name}">'
            f'<button class="btn ghost danger" type="submit">Block this '
            f'source</button></form>')
    if s["indexer"] is not None:
        buttons.append(
            f'<form method="post" action="/api/nzb-indexer/clear?name={name}">'
            f'<button class="btn ghost" type="submit">Reset indexer health — '
            f'retry it</button></form>')
    return '<div class="acts">' + "".join(buttons) + "</div>"


def _history(s: dict, now: float) -> str:
    if not s["history"]:
        return uitheme.section("HISTORY", "Recent probes") + uitheme.empty(
            "No probe records for this source in the retained window.")
    rows = []
    for r in s["history"]:
        ok = bool(r.get("ok"))
        detail = "" if ok else str(r.get("reason") or r.get("err") or "")
        rows.append(
            f"<tr><td class='t'>{uitheme.esc(_ago(r.get('ts') or 0, now))}</td>"
            f"<td class='{'ok' if ok else 'bad'}'>{'ok' if ok else 'failed'}</td>"
            f"<td>{uitheme.esc(detail[:70]) or '—'}</td>"
            f"<td class='t'>{uitheme.esc(str(r.get('id') or '—'))}</td>"
            f"<td>{uitheme.esc(str(r.get('res') or '—'))}</td></tr>")
    return (uitheme.section("HISTORY", "Recent probes",
                            "Newest first. Each row is one attempt to fetch "
                            "the start of a stream from this source.")
            + f'<div class="tblwrap"><table><thead><tr><th>when</th>'
              f'<th>result</th><th>detail</th><th>title</th><th>res</th>'
              f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render(name: str, recs: list[dict]) -> str:
    now = time.time()
    s = summarize(name, recs)
    head = uitheme.pagehead(
        name or "Unknown source", "SOURCE",
        "Everything this source has done in the retained telemetry window, "
        "who serves it, and what you can do about it.")
    body = (head + _decision(s, now) + _facts(s, now) + _why(s)
            + _served_by(s) + _actions(s) + _history(s, now))
    return uitheme.shell(title=f"Source · {name}", name=ADDON_NAME,
                         active="health", body=body,
                         head=f"<style>{_CSS}</style>",
                         robots="noindex,nofollow")
