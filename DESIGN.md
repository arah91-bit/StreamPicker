# Signal Room — design system contract

The dashboard UI is being rebuilt on **`app/uitheme.py`**. It is the only
place palette values and shared components live. Stdlib-only, imports
nothing from other app modules; pages import it, never the reverse.

Creative direction: a clean, neutral control room — **off-white** surfaces
in light mode, **true dark** in dark mode, an **emerald** primary accent,
and a **sky** secondary for eyebrows/healthy highlights. Scheme follows
`prefers-color-scheme` by default; a nav toggle button (sun/moon) stores an
explicit choice in `localStorage('sp-theme')` and applies it via
`<html data-theme="light|dark">`, which beats the media query. The toggle
is a browser-local preference — it is deliberately NOT a config.json key.

## Palette (never hardcode these — use the CSS variables)

| token | dark (default) | light | role |
|---|---|---|---|
| `--bg` | `#0e1013` | `#f7f7f4` | page backdrop (true dark / off-white) |
| `--card` | `#161a1f` | `#ffffff` | elevated surface |
| `--inset` | `#0a0c0f` | `#f0f0ec` | sunken surface (inputs, pre, seg track) |
| `--fg` | `#e8eaed` | `#1c2024` | primary text |
| `--mut` | `#9aa3ad` | `#5f6b76` | muted text (AA on all surfaces) |
| `--line` / `--line2` | `#262c34` / `#3a424d` | `#e2e4e6` / `#c9ced4` | hairline / stronger border |
| `--accent` | `#34d399` | `#047857` | emerald — links, active, primary |
| `--accent-ink` | `#04150d` | `#f0fdf9` | text **on** `--accent`/`--bad` fills |
| `--accent2` | `#7dd3fc` | `#0369a1` | sky — eyebrows, healthy, secondary |
| `--ok` / `--warn` / `--bad` | `#4ade80` / `#fbbf24` / `#f87171` | `#16a34a` / `#b45309` / `#dc2626` | semantic states |
| `--track` | `#1d232a` | `#e8eaec` | meter/slider/badge neutral track |
| `--accent-soft` `--accent2-soft` `--ok-soft` `--warn-soft` `--bad-soft` | color-mix tints | same | tinted backgrounds |
| `--ring` | color-mix | color-mix | input focus ring |
| `--shadow` / `--shadow2` | — | — | card / floating-bar elevation |
| `--sans` / `--mono` | — | — | type stacks · `--r` 11px, `--r-s` 8px radius |

Scheme comes from `prefers-color-scheme`, overridable per-browser via the
rail's theme picker (`data-theme` attribute). Both schemes are automatic if
you only use tokens.

## Themes

`uitheme.THEMES` drives the picker: `auto`, `light`, `dark`, plus three that
override the same token block —

| id | look |
|---|---|
| `shark` | 1994 airbrushed mall poster. Deep water, electric cyan, chrome bevels, italic Arial Black. Backdrop art + poppable bubbles + a shark that swims the page. |
| `heart` | The kdrama skin — hot pink, Georgia italic, falling petals, soft 16px radii. |
| `crt` | Amber phosphor VT220. Monospace everywhere, scanline overlay, 2px radii, blinking `h1` cursor. |

Rules that keep this from becoming a maintenance sink:

- **A theme is a token block first.** Anything you can express by overriding
  `--bg`/`--accent`/`--r`/`--sans` costs nothing per page. Decoration goes in
  `SKIN_CSS`, scoped to `:root[data-theme=<id>]`, never in a page's CSS.
- Backdrop art comes from `--skin` + `--skin-scrim` and is painted on the
  `.skinbg` element shell() emits. Art is served same-origin from
  `/skin/<name>.jpg` (allowlisted in main.py) — the CSP is `img-src 'self'
  data:`, so no CDN, ever.
- `?theme=<id>` applies and pins a theme, so a look is linkable and testable.
  The allowlist in `_THEME_RESTORE` must stay in sync with `THEMES` —
  there's a test for exactly that.
- Every animated extra sits behind `@media (prefers-reduced-motion:reduce)`
  and must never cover a control. The shark's bubbles are real `<button>`s
  with a 44px hit area; everything else is `pointer-events:none`.

### Feeding Frenzy

Poking the shark starts an endless survival round. **Click or tap where you
want him and he swims there, eating whatever he touches on the way** — the
prey themselves are never clicked.

He is steered, not dragged: a velocity eases toward the bearing of his goal
(`TURN` 0.085) at a fixed cruise speed (`SPEED` 6.1 px/frame), so he banks
through turns instead of sliding sideways, and a tail wag scales with how
fast he's actually going. On arrival he does not stop — he falls into a
wander, drifting his heading a little each frame, until you give him
somewhere new to be. All three inputs set the same goal:

- **mouse / pen / touch** — one `pointerdown` path on the shield, with
  `touch-action:none` so a tap swims instead of scrolling the page
- **arrow keys / WASD** — sends him 260 px that way, for playing without a
  pointer

`STANDOFF` (68 px) is the one concession to fingers: a touch aims him
*above* the contact point, because a fingertip covers roughly its own width
of screen and steering him under your own thumb hides the thing you're
aiming. Mouse clicks get no offset — nothing is covering the cursor.

Fish score with a combo multiplier, junk costs a tooth, three teeth and
you're out. A fish that escapes off the top breaks the combo but is never
fatal. Every 8 fish raises the level, and every 5th level hands a tooth back
if you're down.

**There is no clock — the difficulty ramp is the ending.** Level raises the
spawn rate (`pace`), floats prey up quicker (`rise`) and mixes in more junk
(`junkRate`); once those hit their floors the shoals get bigger (`shoal`)
instead, so pressure keeps climbing however good you are. Best score is
banked in `localStorage('sp-shark-hi')` on *any* ending, quitting included —
a run you bailed out of is still a run you played.

A hidden tab pauses the round. `requestAnimationFrame` stops in a background
tab but `setInterval` does not, so without it the shark freezes while the
sea keeps filling.

The game is tested for real, not by grepping its source: `tests/
shark_harness.mjs` runs the shipped script in a Node vm against a DOM stub
and a virtual clock with a seeded PRNG, and `tests/test_shark_game.py`
asserts on the resulting playthrough — that a good run outlives any timer,
that spawn pressure climbs, that teeth running out ends it, and that your
best survives a quit.

The rule that matters: **a game must not be able to change a setting.** While
a round runs, `#sharkshield` covers the page and swallows every input that
would otherwise land on it — a test asserts a click over a lane toggle hits
the shield. Prey are `pointer-events:none` so they can never intercept the
pointer that is steering. The rail stays above the shield so you can always
navigate away, and quit/Esc tears it all down immediately.

## Python API

```python
from app import uitheme
uitheme.esc(x)                      # html.escape(str(x), quote=True)
uitheme.icon(name, size=16, cls="") # inline SVG, stroke, currentColor
uitheme.pagehead(h, eyebrow=None, subtitle=None)   # subtitle = raw HTML
uitheme.section(eyebrow, title, hint="", *, tally="", tone="")
uitheme.copybtn(value, label="Copy", *, cls=..., raw=False)  # LAN-safe copy
uitheme.badge(text, tone="")        # tone: ok|warn|bad|info|teal
uitheme.status_dot(state="idle", label="")  # ok|warn|bad|run(pulse)|idle
uitheme.meter(pct, tone="")         # progressbar w/ ARIA; ok|warn|bad|teal
uitheme.kv(key, value)              # mono key/value row
uitheme.tile(value, label, sub="", raw=False)  # stat tile; raw for <small>
uitheme.empty(text)                 # dashed empty-state card
```

`icon()` names: `activity arrow-right bolt chart check copy external film
gear home info key link moon play plug plus refresh rss search server
sliders sun trash warn x`. Unknown → KeyError.

`shell()` assembles the whole document — pages stop hand-rolling chrome:

```python
def render() -> str:                       # keep existing signatures!
    body = (uitheme.section("SOURCES", "Debrid services", "one is enough",
                            tally="2/4 configured", tone="ok")
            + '<div class="cards"><div class="card">…</div></div>')
    return uitheme.shell(
        title="Connect", name=ADDON_NAME, active="connect",   # NAV id
        csrf=adminui.csrf_token(),          # adds data-csrf + link→POST JS
        body=body,
        head="<style>…page-only CSS…</style>",           # optional
        scripts="<script>…page JS…</script>",            # optional
        search=settings_ui.search_index(),  # enables the ⌘K palette
        refresh=30)                         # only when the page is live
```

- `active` is a `uitheme.NAV` id: `home connect tune health private`.
- `search=` is a list of `{t,k,s,href}` dicts, **pre-escaped** — it is
  injected as JSON and rendered with innerHTML.
- `uitheme.page()` is the chrome-less variant, for pre-login pages only
  (first-run account creation). Everything reachable by an operator uses
  `shell()`.
- `<style>{BASE_CSS}</style>` is always first; `head=` CSS comes after —
  so page CSS wins. **Namespace page classes**: two pages defining `.hero`
  differently and both loading on `/` is how the ledger hero broke once.

## Class inventory (from BASE_CSS)

- Shell: `.app` body · `.rail` fixed left column (`.rail-brand` `.rail-nav`
  `.rlink`+`.on` `.rail-search` `.rail-foot` `.rail-footrow`) · `.appmain`
  `.wrap` content column · `.pk-overlay .pk-s .pk-list .pk-item .pk-t
  .pk-empty` the ⌘K palette · `.themebtn` scheme toggle · `.mark` product
  mark · `.pagehead` · `.eyebrow` sky mono uppercase label · `.sub` muted
  subtitle.
- Structure: `.shead`/`section()` accent-rule section header
  (`.shead .eyebrow .hint .tally`) · `.card` · `.cards` grid ·
  `.tiles .tile .v .k .s` stat tiles.
- Buttons: `.btn` primary emerald (ink text) · `.btn.ghost` · `.btn.danger`
  (+`.ghost`) · `.btn.sm` · `.btnrow` footer row · `.ic` icon alignment.
- Forms: bare `input[type=text|password|url|number|search]`, `textarea`,
  `select` are styled · `.swi` toggle (`<input type=checkbox class=swi>`) ·
  bare `input[type=range]` thin track + emerald thumb · `output` mono value ·
  `.seg` segmented bus buttons (`<div class=seg><label><input type=radio
  name=…><span>Choice</span></label>…`) · `input[type=checkbox/radio]`
  get the emerald accent-color.
- Status: `.badge` (+tones) · `.tag` mono chip · `.dot` (+`ok warn bad run`)
  glow dots · `.stat`/`.stat-t` dot+label · `.meter>.fill` (+tones).
- Data: `.tblwrap` scrolling table shell (bare `table th td` styled,
  `tr.warn`/`tr.bad` row tints) · `.kv .k .v` · `.row .lbl .desc .envk .ctl`
  settings rows (`.row.off` dims).
- Disclosure/bars: `details.acc` accordion (`.acc-t .acc-hint .acc-n`) ·
  `.savebar` (+`.top` sticky variant, `[hidden]` respected, `.msg .err`) ·
  `.toasts .toast` (+`ok warn bad`, 2px accent left edge).
- Misc: `.callout` (+tones) banner · `.empty` · bare `code pre a h1-h3` ·
  utilities `.mut .mono .num .ok .warn .bad .small`.

## Hard rules for page agents

1. **Keep every existing `render()` signature and all fetch/API/JS data
   contracts.** You are re-skinning, not rewiring. `data-key`, ids, form
   field names, endpoint URLs, and the CSRF mechanism must not change.
2. **No external assets.** No CDN/icon fonts, no images, no network fetches.
   Icons only via `uitheme.icon()`. The dashboard works offline on a LAN.
3. **Never hardcode colors.** Use the tokens; both schemes then come free.
   Delete each page's old `:root`/palette/reset rules — BASE_CSS replaces
   them. Keep only genuinely page-specific CSS in `head=`.
4. **Type discipline:** human copy in `--sans`; machine truth (env keys,
   URLs, latencies, IDs, numbers) in `--mono`; stats use tabular figures
   (`.num`, already on `.tile .v` and `td`).
5. **Native semantics:** real `<button>`/`<label>`/`<input>`/`<details>`/
   `<table>`; `.swi` stays a checkbox; `.seg` stays radio inputs. Visible
   focus rings and `prefers-reduced-motion` are handled by BASE_CSS —
   don't override them.
6. **Motifs, tastefully:** the accent rule on `section()`; sky = eyebrows
   + healthy state; emerald = action/active. Don't invent new accent hues
   or glow effects.
7. Verify with the venv: `python -m pytest tests/ -x -q` and eyeball the
   page in both `prefers-color-scheme` modes before handing back.
