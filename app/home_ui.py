"""Renders / — Home, the "is it working?" page.

Opens with a single status hero (streaming / attention / restart / ready),
then the Add-to-Nuvio/Stremio install card with copyable manifest URLs, the
lane switches, an at-a-glance service summary deep-linking into /connect,
and the telemetry tiles that show what the addon has been doing.

Everything actionable here is the same machinery the other pages use — lane
switches save immediately via /api/settings/save, and the save bar handles
restart — so the page never drifts from config.json / .env parity.
"""

import os
import time
from urllib.parse import quote

from app import (adminui, config, overview, proxy, settings_ui, source_health,
                 telemetry, uitheme, usenet_health)

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

_esc = uitheme.esc

_CSS = """
/* home page — tokens & shared components live in uitheme.BASE_CSS */
.hero{display:flex;align-items:center;gap:20px;padding:24px 24px;
margin-bottom:16px;flex-wrap:wrap}
.hero .hdot{width:16px;height:16px;border-radius:50%;flex-shrink:0}
.hero .hstate{font-size:22px;font-weight:700;letter-spacing:-.01em}
.hero .hsub{color:var(--mut);font-size:13.5px;margin-top:3px;max-width:60ch}
.hero .hact{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap}
.install{margin-bottom:16px;padding:16px 18px}
.install .irow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
margin-top:10px}
.install .murl{flex:1;min-width:200px;font:12px var(--mono);color:var(--mut);
background:var(--inset);border:1px solid var(--line);border-radius:var(--r-s);
padding:9px 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.install details{margin-top:12px}
.install summary{cursor:pointer;font-size:12.5px;color:var(--mut)}
.install summary:hover{color:var(--accent)}
.install .variant{display:flex;align-items:center;gap:10px;margin-top:8px;
flex-wrap:wrap}
.install .vname{font-size:12.5px;min-width:150px;font-weight:600}
.glance{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
gap:10px;margin-bottom:8px}
.gitem{display:flex;align-items:center;gap:11px;padding:12px 14px;
background:var(--card);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--shadow);text-decoration:none;color:var(--fg);
transition:border-color .12s}
.gitem:hover{border-color:var(--line2);text-decoration:none}
.gitem .gn{font-weight:600;font-size:13.5px}
.gitem .gr{color:var(--mut);font-size:11.5px;margin-top:1px}
.gitem .gstate{margin-left:auto;font:11px var(--mono);color:var(--mut)}
.gitem .gstate.ok{color:var(--ok)}
/* unconfigured services: one closed row per category */
.notset{display:flex;flex-direction:column;gap:8px;margin-top:10px}
details.ngroup{overflow:hidden}
details.ngroup>summary{cursor:pointer;list-style:none;display:flex;
align-items:center;gap:10px;padding:11px 14px}
details.ngroup>summary::-webkit-details-marker{display:none}
details.ngroup>summary:hover .ngname{color:var(--accent)}
.ngname{font-weight:600;font-size:13px;transition:color .12s}
.ngcount{margin-left:auto;font:11px var(--mono);color:var(--mut)}
details.ngroup>summary .chev{color:var(--mut);transition:transform .15s;
flex-shrink:0}
details.ngroup[open]>summary .chev{transform:rotate(90deg)}
.ngbody{padding:2px 14px 12px;border-top:1px solid var(--line)}
.ngbody .glance{margin:10px 0 0}
.ngbody .gitem{background:var(--inset)}
"""


# Probes are cheap and plentiful, so a source has to fail a real run of them
# before we call it dead rather than unlucky.
_DEAD_SOURCE_PROBES = 8
# ...and it has to have failed them *lately*. Telemetry is retained for weeks,
# so without this a source that was removed or repaired days ago keeps raising
# the alarm until its records age out — the same permanent warning this hero
# exists to get rid of, arriving by a different route. Worse, a dead source
# with zero successes can never dilute its own 100% failure rate, so it would
# sit there until retention expired.
_RECENT_HOURS = 48.0


def _broken_services(recs: list[dict]) -> list[str]:
    """Services that are genuinely down *now* — not releases we screened out.

    Blocked releases are the product, not a problem: finding the bad ones and
    routing around them is the entire job, and a healthy instance accumulates
    them forever. Surfacing that pile as an alert trains you to ignore the
    hero. What does want a human is a service that never works at all — a
    usenet indexer whose NZB downloads are all rejected (expired plan, stale
    API key), or a source whose recent probes fail every single time.

    Both bars are the ones the engines already use to give up on an endpoint,
    so the hero can't disagree with Health about what is actually broken.
    Sources the operator has already answered for — dismissed, or blocked
    outright — are not raised again; that decision lives in source_health.
    """
    names: list[str] = []
    try:
        names += [r["name"] for r in usenet_health.indexer_listing()
                  if not r.get("fetch_allowed", True)]
    except Exception:                                # health db absent/locked
        pass
    cutoff = time.time() - _RECENT_HOURS * 3600
    probes = [r for r in recs if r.get("kind") == "probe"
              and (r.get("ts") or 0) >= cutoff]
    names += [r["key"] for r in
              telemetry.aggregate(probes, "src", min_n=_DEAD_SOURCE_PROBES)
              if r["fail_pct"] >= 100.0 and r["key"] != "(none)"]
    return sorted({n for n in names if not source_health.state(n)})


def _screened(blocks: list[dict]) -> str:
    """Blocked releases as evidence the picker is working, not as an alarm."""
    n = sum(1 for b in blocks if b.get("blocked"))
    if not n:
        return ""
    return (f" {overview._num(n)} bad release{'s' if n != 1 else ''} screened "
            "out along the way.")


def _hero(recs: list[dict], blocks: list[dict], names: dict[str, str]) -> str:
    """The one-glance answer. Priority: restart > now playing > broken service >
    streaming history > fresh-and-ready."""
    restart = config.restart_pending()
    playing = proxy.active_stream_details()
    plays = [r for r in recs if r.get("kind") == "play"]
    stalled = usenet_health.transport_stalled()
    # only when it can reach the screen — scanning probes costs a pass over
    # the whole telemetry window
    broken = [] if restart or playing or stalled else _broken_services(recs)

    if restart:
        state, dot, sub, act = (
            "Restart to apply changes", "warn",
            "Saved settings only take effect on the way back up. The save bar "
            "below has the restart button.",
            "")
    elif stalled:
        # Outranks "Streaming now" deliberately. When the usenet pipe wedges,
        # whatever is still on screen is playing out of the read-ahead buffer
        # and is about to stop — so the person looking at this page is looking
        # at a spinner, and "all good, streaming" is the least useful sentence
        # we could show them. It also outranks the per-source alarm, because
        # naming three innocent releases sends them hunting the wrong thing.
        mins = max(1, stalled["for_secs"] // 60)
        state, dot, sub, act = (
            "Usenet delivery has stalled", "warn",
            f"{stalled['releases']} different releases have failed to deliver "
            f"a byte in the last {mins} minute{'s' if mins != 1 else ''}, and "
            "nothing has succeeded in between — that is the connection to your "
            "news provider, not the releases. Restarting nzbdav clears it; its "
            "connection pool fills with dead sockets over a long uptime. "
            "Torrent and debrid sources are unaffected.",
            f"<a class='btn ghost' href='/health/sources'>"
            f"{uitheme.icon('activity')}See the failures</a>")
    elif playing:
        first = playing[0]
        title = names.get(first.get("media_id", ""),
                          first.get("media_id", "")) or "Unknown"
        more = f" and {len(playing) - 1} more" if len(playing) > 1 else ""
        state, dot, sub, act = (
            f"Streaming now", "run",
            f"Serving <b>{_esc(title)}</b>{more} through the proxy — "
            "failover and read-ahead are live.",
            "")
    elif broken:
        # Each name is its own link: the answer to "what do I do about this"
        # is per source — its errors, whose addon it is, and the buttons that
        # end the warning — so the click has to land there, not on the dump.
        shown = ", ".join(
            f"<a href='/health/source/{quote(n)}'>{_esc(n)}</a>"
            for n in broken[:3])
        more = f" +{len(broken) - 3} more" if len(broken) > 3 else ""
        first = quote(broken[0])
        state, dot, sub, act = (
            f"{len(broken)} source{'s' if len(broken) != 1 else ''} failing "
            "every time", "warn",
            f"<b>{shown}</b>{more} returned nothing usable in the last "
            f"{int(_RECENT_HOURS)}h — usually an expired plan or a stale API "
            "key. Everything else is picking normally.",
            f"<a class='btn ghost' href='/health/source/{first}'>"
            f"{uitheme.icon('activity')}Open {_esc(broken[0])}</a>")
    elif plays:
        total_mb = sum(r.get("mb") or 0 for r in plays)
        dv, du = overview._data(total_mb)
        state, dot, sub, act = (
            "All systems go", "ok",
            f"{overview._num(len(plays))} stream"
            f"{'s' if len(plays) != 1 else ''} served — {dv} {du} through the "
            f"proxy since telemetry reset.{_screened(blocks)}",
            "")
    else:
        state, dot, sub, act = (
            "Ready", "ok",
            "Everything is plugged in. Play something in Nuvio/Stremio and "
            "live stats show up here.",
            "")
    return (f"<section class='card hero'>"
            f"<span class='hdot dot {dot}' aria-hidden='true'></span>"
            f"<div><div class='hstate'>{_esc(state)}</div>"
            f"<div class='hsub'>{sub}</div></div>"
            f"<div class='hact'>{act}</div></section>")


def _install(addons: list[tuple[str, str]]) -> str:
    """The manifest card: primary URL with copy + deep link, variants folded."""
    if not addons:
        return ""
    _name, primary = addons[0]
    deep = "stremio://" + primary.split("://", 1)[-1]
    variants = "".join(
        f"<div class='variant'><span class='vname'>{_esc(n)}</span>"
        f"<span class='murl'>{_esc(u)}</span>"
        f"{uitheme.copybtn(u)}</div>"
        for n, u in addons[1:])
    folded = (f"<details><summary>Mobile &amp; slow-picker variants</summary>"
              f"{variants}</details>") if variants else ""
    return (
        uitheme.section("INSTALL", "Add to Nuvio/Stremio",
                        "paste this manifest URL into Nuvio/Stremio — or open the "
                        "deep link on a device with the app")
        + f"<div class='card install'>"
          f"<div class='irow'><span class='murl'>{_esc(primary)}</span>"
          f"{uitheme.copybtn(primary, 'Copy manifest URL', cls='btn sm')}"
          f"<a class='btn ghost sm' href='{_esc(deep)}'>"
          f"{uitheme.icon('external', size=14)}Open in Nuvio/Stremio</a></div>"
          f"{folded}"
          f"<p class='blurb' style='margin:10px 0 0'>First time here, or want "
          f"to redo the basics? <a href='/setup'>Re-run the setup guide</a>."
          f"</p></div>")


def _gitem(conn: dict, *, configured: bool) -> str:
    """One compact dot + name tile, deep-linked to its card on /connect."""
    state = "ok" if configured else "idle"
    label = "configured" if configured else "not set"
    return (f"<a class='gitem' href='/connect#conn-{_esc(conn['id'])}'>"
            f"{uitheme.status_dot(state)}"
            f"<span><span class='gn'>{_esc(conn['name'])}</span>"
            f"<div class='gr'>{_esc(conn['role'])}</div></span>"
            f"<span class='gstate {state}'>{label}</span></a>")


def _glance() -> str:
    """What's plugged in, on top; what isn't, folded away by category.

    The answer this section owes you is "what is actually running", so the
    configured services are the only thing on screen by default. Everything
    unconfigured is optional by definition — it collapses into one row per
    category that you open when you want to add something."""
    on, off = [], []
    for c in config.CONNECTIONS:
        (on if settings_ui._conn_configured(c) else off).append(c)
    if not (on or off):
        return ""

    live = (f"<div class='glance'>"
            f"{''.join(_gitem(c, configured=True) for c in on)}</div>"
            if on else uitheme.empty(
                "Nothing is connected yet. Open a category below, or run the "
                "<a href='/setup'>setup guide</a>."))

    # unconfigured, grouped by category and closed; categories keep
    # config.CONNECTION_GROUPS order, with anything uncategorized last
    known = [gid for gid, _t, _b in config.CONNECTION_GROUPS]
    titles = {gid: t for gid, t, _b in config.CONNECTION_GROUPS}
    buckets: dict[str, list[dict]] = {}
    for c in off:
        buckets.setdefault(c.get("cat") or "other", []).append(c)
    order = [g for g in known if g in buckets]
    order += [g for g in buckets if g not in known]
    folds = "".join(
        f"<details class='card ngroup'><summary>"
        f"<span class='ngname'>{_esc(titles.get(gid, 'Other'))}</span>"
        f"<span class='ngcount'>{len(cs)} available</span>"
        f"{uitheme.icon('arrow-right', size=14, cls='chev')}</summary>"
        f"<div class='ngbody'><div class='glance'>"
        f"{''.join(_gitem(c, configured=False) for c in cs)}"
        f"</div></div></details>"
        for gid, cs in ((g, buckets[g]) for g in order))

    return (uitheme.section("AT A GLANCE", "Services",
                            "everything this instance talks to — click one to "
                            "configure it",
                            tally=f"{len(on)} of {len(on) + len(off)} connected",
                            tone="ok" if on else "")
            + live
            + (f"<div class='notset'>{folds}</div>" if folds else ""))


def render(recs: list[dict], addons: list[tuple[str, str]],
           blocks: list[dict]) -> str:
    """/ — status hero, what's playing, install, lanes, glance, then the ledger.

    Ordered by urgency: act now (hero, on air), set up (install, lanes,
    services), look back (ledger). The page auto-refreshes only while
    something is actually streaming."""
    names = overview._title_map(recs)
    playing = overview.now_playing(recs)
    restart = "1" if config.restart_pending() else "0"
    body = f"""
{_hero(recs, blocks, names)}
{settings_ui._savebar(restart)}
{playing}
{_install(addons)}
{uitheme.section("LANES", "Stream sources on/off",
                 "flip a whole lane without touching its credentials — saves "
                 "immediately")}
{settings_ui._lane_masters()}
{_glance()}
{overview.ledger(recs)}"""
    return uitheme.shell(
        title="Home", name=ADDON_NAME, active="home",
        csrf=adminui.csrf_token(), body=body,
        head=f"<style>{settings_ui._CSS}{overview.CSS}{_CSS}</style>",
        scripts=f"<script>{settings_ui._JS}</script>",
        search=settings_ui.search_index(),
        refresh=30 if playing else None)
