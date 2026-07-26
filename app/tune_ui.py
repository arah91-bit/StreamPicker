"""Renders /tune — every behavior knob, grouped by intent.

One section at a time behind an in-page section nav (all sections stay in
the DOM, so dirty-tracking sees every control and the no-JS fallback reads
top to bottom), led by the stream-path choice, with Advanced tuning — the
full remaining knob catalog — always last. The Ctrl/⌘K palette in the rail
jumps straight to any setting by name or env key.

Sections deep-link as /tune#sec-<group>; the palette index and any prose
links rely on those ids, so they are stable API. All writes go through the
shared settings_ui machinery (data-key inputs → /api/settings/save),
keeping the page at exact parity with /data/config.json and .env edits.
"""

import os

from app import adminui, config, knobs, settings_ui, uitheme

ADDON_NAME = os.environ.get("ADDON_NAME", "Auto Stream")

_esc = uitheme.esc

_CSS = """
/* tune page — second tier of the section nav; layout in settings_ui._CSS */
.sidenav a.sn-sub{padding-left:24px;font-size:12.5px}
.sidenav a.sn-sub:first-of-type{margin-top:2px}
@media (max-width:840px){.sidenav a.sn-sub{padding-left:12px;opacity:.8}}
"""


def render() -> str:
    """/tune — the decisions, in small click-through sections."""
    restart = "1" if config.restart_pending() else "0"
    parts: list[tuple[str, str, str]] = []
    for gid, title, blurb in config.GROUPS:
        if gid == "stream":
            parts.append(("sec-stream", "Stream path",
                          settings_ui._stream_mode()))
        else:
            section_html = settings_ui._settings_section(gid, title, blurb)
            if section_html:
                parts.append((f"sec-{gid}", title, section_html))
    parts.append(("sec-advanced", "Advanced tuning",
                  settings_ui._advanced_section()))
    links = "".join(f'<a data-sec="{sid}" href="#{sid}">{_esc(t)}</a>'
                    for sid, t, _h in parts)
    # Second tier: the advanced catalog is 100+ knobs, so its sub-groups get
    # their own nav rows rather than hiding behind one link.
    links += "".join(
        f'<a class="sn-sub" data-sec="sec-advanced" href="#adv-{gid}">'
        f'{_esc(title)}</a>'
        for gid, title in knobs.GROUPS if knobs.by_group(gid))
    content = "".join(f'<section class="bsec" id="{sid}">{h}</section>'
                      for sid, _t, h in parts)
    body = f"""
<div class="pagehead"><p class="eyebrow">DECISIONS</p><h1>Tune</h1>
<p class="sub">How the addon behaves once your services are plugged in —
small sections, one at a time. Every value here is also a key in
<code>/data/config.json</code> (menu: <code>.env.reference</code>), so a
file or an AI can drive the exact same settings. Tip: press
<kbd>Ctrl/⌘+K</kbd> to jump to any setting by name or env key.</p></div>
{settings_ui._savebar(restart, top=True)}
<div class="slayout">
<aside class="sidenav" aria-label="Settings sections">
<p class="sn-cap">Sections</p>{links}</aside>
<div class="scontent">{content}</div>
</div>
{settings_ui._savebar(restart)}"""
    return uitheme.shell(
        title="Tune", name=ADDON_NAME, active="tune",
        csrf=adminui.csrf_token(), body=body,
        head=f"<style>{settings_ui._CSS}{_CSS}</style>",
        scripts=f"<script>{settings_ui._JS}</script>",
        search=settings_ui.search_index())
