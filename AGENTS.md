# Configuring stream-picker as an agent

There are **two equivalent ways** to configure this addon. They use the same
setting names, but the dashboard encrypts sensitive values before persisting
them:

1. **The dashboard / setup wizard** — for a human. Open the site, switch things
   on, paste keys, hit *Set up my streams* (or use **Settings** for the full
   list). This is documented in [README.md](README.md) and [SETUP.md](SETUP.md).
2. **Config files** — for automation, including an AI. Everything the dashboard
   can set, you can set by editing a file and restarting. **This document is
   the guide for that path.**

Neither path is "underneath" the other. Anything you set here shows up in the
dashboard, and vice-versa; only the on-disk representation of a secret differs.

---

## The model in one paragraph

There is a single catalog of every setting the addon understands (defined in
`app/config.py` + `app/knobs.py`). It is fed by two file surfaces that are
merged at process start by `config.apply_env()`:

- **`.env`** (mounted as the container's env, or a compose `environment:` block)
  — best for the required addon secret and non-sensitive deployment seeds. It
  is a plaintext format; do not put the Jellyfin password there in the normal
  deployment.
- **`data/config.json`** — a JSON document `{"env": {"KEY": "value", …}}`. This
  is what the dashboard writes. Sensitive fields are stored as independently
  authenticated AES-256-GCM ciphertext. **When a key is in both, `config.json`
  wins.**

The AES master key is separate from `config.json`. Production mounts it
read-only at `/run/secrets/stream_picker_config_key` and sets
`CONFIG_ENCRYPTION_KEY_FILE` to that path. Keep the host key outside the Git
repository, mode `0600`, and back it up with the encrypted config; losing or
replacing it makes existing ciphertext intentionally unreadable.

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
- **Secrets**: do not hand-write plaintext secrets into `config.json`. Pass the
  real value through `config.save(...)` or the authenticated dashboard API so
  it is encrypted before the file is replaced. A blank secret submitted through
  the dashboard means "keep the stored one"; omit it when no change is needed.

### Editing `.env`

Same keys, `KEY=value` per line. Use this for `ADDON_SECRET` (required) and
non-sensitive deployment seeds. `.env` is ignored by this repository but is
still plaintext on disk, so save `JELLYFIN_PASSWORD` through the dashboard/API
instead. See `.env.reference` for the annotated menu.

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

**Anime episode matching** — on by default (`ANIME_ENABLED`). Reconciles
absolute / per-cour / seasonal numbering for anime so the right episode is
confirmed and a wrong-season file isn't auto-played, using cached anime-lists +
Kitsu (+ Jikan as a best-effort secondary, `ANIME_JIKAN`). No keys required; it
needs only outbound network. Set `ANIME_ENABLED=0` to fall back to plain
filename `S×E` parsing.

**Public address** (reverse proxy / tunnel / blank for LAN):
`"ADDON_PUBLIC_URL": "https://streams.example.com"`.

**Native Jellyfin library** — set `JELLYFIN_URL` to the internal API base the
container can reach, then save `JELLYFIN_USERNAME` and `JELLYFIN_PASSWORD`
through the encrypted dashboard/API path. Use a dedicated non-admin Jellyfin
user restricted to the needed libraries and playback. There is no Jellio URL
and no player-facing Jellyfin URL: Stream Picker returns its own signed proxy
URL, injects the user token only on the server-side upstream request, and
preserves byte ranges for seeking.

Player-decodable codecs (H.264/HEVC/VP9/AV1) are direct-played untouched. A
codec the player can't decode (MPEG‑2, XviD/DivX, VC‑1, WMV) is transcoded by
Jellyfin and proxied back as token-safe HLS — but only if that Jellyfin user
has the *allow video/audio transcoding* and *allow remuxing* permissions
enabled in its Jellyfin policy (Stream Picker cannot grant those; an admin sets
them). `JELLYFIN_TRANSCODE=0` disables the fallback and drops such titles from
library results instead of handing over an audio-only file.

**Jellyfin, *arr, Jellyseerr, nzbdav** — see the `connections` section of
`.env.reference` for all exact key names (`JELLYFIN_URL`, `RADARR_URL` +
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
`NZB_INDEXERS`, a complete native Jellyfin connection, `MEDIAFUSION_BASE_URL`,
or `EXTRA_ADDONS`) should be set for the addon to return anything.

---

## Testing a credential before you commit to it

Every connection has the same live check the dashboard's **Test** button uses,
exposed as `app.connections.test(service, {KEY: value, …})` (async). Service
ids: `comet`, `stremthru`, `mediafusion`, `jellyfin`, `addon`, `tmdb`, `omdb`,
`tvdb`, `jellyseerr`, `radarr`, `sonarr`, `nzbdav`, `indexers`. It returns
`{"ok": bool, "detail": "…"}`. Use it to verify a key is real before writing it
to `config.json` and restarting.

---

## The HTTP path (running instance, no filesystem)

If you can only reach a running instance over HTTP and have the admin
credentials, the same operations are:

1. `GET /api/admin/csrf` → `{csrf_token}` (send it as `X-CSRF-Token` on writes).
2. `POST /api/settings/save` with `{"values": {KEY: value, …}}` — validates and
   writes to `config.json`, encrypting sensitive fields first, and returns
   `{changed, restart_needed}`. Never log or echo the submitted secrets.
3. `POST /api/settings/restart` — validates the pending config, then restarts.

The dashboard is admin-authenticated and LAN-only by default, so this path is
for an agent already trusted on that network.
