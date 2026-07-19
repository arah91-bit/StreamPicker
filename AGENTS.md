# Configuring stream-picker as an agent

There are **two equivalent ways** to configure this addon, and they write the
same keys to the same place:

1. **The dashboard / setup wizard** — for a human. Open the site, switch things
   on, paste keys, hit *Set up my streams* (or use **Settings** for the full
   list). This is documented in [README.md](README.md) and [SETUP.md](SETUP.md).
2. **Config files** — for automation, including an AI. Everything the dashboard
   can set, you can set by editing a file and restarting. **This document is
   the guide for that path.**

Neither path is "underneath" the other: the dashboard just edits the same file
surfaces described below. Anything you set here shows up in the dashboard, and
vice-versa.

---

## The model in one paragraph

There is a single catalog of every setting the addon understands (defined in
`app/config.py` + `app/knobs.py`). It is fed by two file surfaces that are
merged at process start by `config.apply_env()`:

- **`.env`** (mounted as the container's env, or a compose `environment:` block)
  — best for the required secret and any deployment seeds.
- **`data/config.json`** — a JSON document `{"env": {"KEY": "value", …}}`. This
  is what the dashboard writes. **When a key is in both, `config.json` wins.**

Both are read **only at startup**, so a config change takes effect on the
**next restart**, never live. That is by design and is the one rule you must
not forget.

The **authoritative, always-current list of every key** — with its default and
a one-line description — is the committed file **[`.env.reference`](.env.reference)**.
Read it first. Regenerate it after any code change with:

```
python -m tools.gen_env_reference
```

---

## The one authoritative reference

Do **not** hardcode a key list from this document — it will drift. Instead:

- **`.env.reference`** — every key, its default, grouped and commented. The menu.
- **`GET /api/settings/export.env`** (admin-authenticated) — the *current*
  effective config of a running instance as a ready-to-edit `.env`, with
  secrets redacted. Good for "show me what's set right now."

If you only remember one thing: **`.env.reference` is the source of truth for
what keys exist.**

---

## How to make a change (file path)

Pick **one** surface. For most agent tasks, editing `data/config.json` is
simplest because it is exactly what the dashboard uses and it is per-install
(not baked into compose).

### Editing `data/config.json`

The shape is strict:

```json
{
  "env": {
    "TMDB_API_KEY": "abc123",
    "NZB_INDEXERS": "myindexer|https://api.myindexer.com/api|deadbeef",
    "ACQUIRE_ENABLED": "1"
  }
}
```

Rules that will bite you if ignored:

- **Every value is a scalar** — a string, or a number/bool *written as a
  string*. Never a nested object or array. A list-shaped setting (see
  `EXTRA_ADDONS` below) is stored as a **JSON string**, not a JSON array.
- **Only known keys are accepted.** An unknown key makes the whole file invalid.
  Check the name against `.env.reference`.
- **Booleans** are `"1"`/`"0"` (the loader also accepts `true/false/yes/no/on/off`).
- **Blank a key to revert it** to its `.env`/built-in default (just remove the
  key, or set it to `""`).
- **Secrets**: put the real value. (Through the dashboard API a *blank* secret
  means "keep the stored one"; when hand-editing the file, just write the value
  or omit the key.)

### Editing `.env`

Same keys, `KEY=value` per line. Use this for `ADDON_SECRET` (required) and
anything you want to live in version-controlled deployment config. See
`.env.reference` for the annotated menu; copy the lines you want and fill them in.

### Then: restart

```
docker compose restart stream-picker      # or: docker compose up -d
```

The change lands on the way back up. To confirm a running instance has a
change *pending* a restart, `GET /api/settings/status.json` returns
`{"restart_pending": true|false}`.

### Validate *before* you restart — don't rely on the safety net

At boot, an invalid `config.json` (a bad value, an out-of-range number, or a
broken cross-field invariant) is **quarantined**: the process moves it aside as
`config.json.corrupt-<ts>` and boots on `.env`/defaults instead of
crash-looping. That protects the install — but it means a botched edit is
**silently reverted**, and because a bad file is quarantined *on read*,
`config.validate_pending()` will report "OK" (it re-read the now-empty store).
So do not use the file + restart loop to find out whether your change is valid.

To check a prospective change **without** the silent-revert surprise, validate
it the way the dashboard does — it rejects a bad combination up front with a
clear message instead of quarantining:

- In-process: `config.save({KEY: value, …})` — validates the merged config and
  raises `ValueError("…")` on any violation, otherwise writes it.
- Over HTTP: `POST /api/settings/save` returns **400** with the reason.

Only after that succeeds do you restart to apply.

---

## Common recipes

Keys below are the important ones; consult `.env.reference` for the rest.

**A debrid + its two search lanes.** The wizard mints these from a debrid key,
but you can set the finished URLs directly:

```json
"FAST_BASE_URL": "https://comet.example/<base64-config>",
"STREMTHRU_BASE_URL": "https://stremthru.example/stremio/torz/<base64-config>"
```

If you want the wizard's minting logic instead of a hand-built URL, call
`app.wizard.comet_url([("torbox", "<key>")])` /
`app.wizard.stremthru_url([...])` — they encode Comet's padded-base64 and
StremThru's urlsafe-unpadded dialects correctly.

**Usenet indexers** — `name|api-url|apikey`, `;`-separated (one per line in the
UI becomes `;`-joined in storage):

```json
"NZB_INDEXERS": "nzbgeek|https://api.nzbgeek.info/api|KEY;nzbfinder|https://nzbfinder.ws/api|KEY2"
```

**A custom Stremio addon** — stored as a **JSON string** (note the escaping):

```json
"EXTRA_ADDONS": "[{\"name\":\"My Addon\",\"url\":\"https://addon.example/manifest.json\"}]"
```

**Metadata** — `"TMDB_API_KEY": "…"`, `"OMDB_API_KEY": "…"`, `"TVDB_API_KEY": "…"`.

**Public address** (reverse proxy / tunnel / blank for LAN):
`"ADDON_PUBLIC_URL": "https://streams.example.com"`.

**Jellyfin library, *arr, Jellyseerr, nzbdav** — see the `connections` section
of `.env.reference` for the exact key names (`JELLIO_URL`, `RADARR_URL` +
`RADARR_API_KEY`, `NZBDAV_URL` + `NZBDAV_USER` + `NZBDAV_PASS`, etc.).

---

## Invariants you must not violate

These are cross-field constraints checked at startup (`_validate_effective` in
`app/config.py`). Break one and the config is rejected (and, for `config.json`,
quarantined):

- `BUFFER_AHEAD_GB` ≤ `BUFFER_CACHE_GB`
- `VERIFIED_WANT` ≤ `MAX_PROBES`
- `SLOW_PROBE_RESERVE` < `SLOW_TOTAL_DEADLINE`
- `FAST_RACE_DEADLINE` ≤ `TOTAL_DEADLINE`

Also: numeric keys must parse as numbers **and stay within their own bounds**
(e.g. `BUFFER_AHEAD_GB` ≤ 32) — see the units/ranges in `.env.reference`; URLs
must be absolute `http(s)`; and at least one **stream source** (a debrid lane,
`NZB_INDEXERS`, `JELLIO_URL`, `MEDIAFUSION_BASE_URL`, or `EXTRA_ADDONS`) should
be set for the addon to return anything.

---

## Testing a credential before you commit to it

Every connection has the same live check the dashboard's **Test** button uses,
exposed as `app.connections.test(service, {KEY: value, …})` (async). Service
ids: `comet`, `stremthru`, `mediafusion`, `jellio`, `addon`, `tmdb`, `omdb`,
`tvdb`, `jellyseerr`, `radarr`, `sonarr`, `nzbdav`, `indexers`. It returns
`{"ok": bool, "detail": "…"}`. Use it to verify a key is real before writing it
to `config.json` and restarting.

---

## The HTTP path (running instance, no filesystem)

If you can only reach a running instance over HTTP and have the admin
credentials, the same operations are:

1. `GET /api/admin/csrf` → `{csrf_token}` (send it as `X-CSRF-Token` on writes).
2. `POST /api/settings/save` with `{"values": {KEY: value, …}}` — validates and
   writes to `config.json`, returns `{changed, restart_needed}`.
3. `POST /api/settings/restart` — validates the pending config, then restarts.

The dashboard is admin-authenticated and LAN-only by default, so this path is
for an agent already trusted on that network.
