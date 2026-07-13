# Setup guide — from zero to a working stream

This guide takes you from nothing to a working install, whether you're a person
following along or an AI agent doing the setup. Read the first two sections for
the mental model, then follow the numbered steps.

---

## 1. What this is (and isn't)

This project is a **picker and proxy** — the brain that sits between your video
player and a set of streaming *sources*. For each title you open, it:

1. asks every source you've connected for candidate streams,
2. **probes** the best ones to confirm they actually play,
3. hands your player a single high-quality, correct-audio stream that works,
4. (optionally) proxies the bytes so it can fail over mid-stream and read ahead.

```
   your player  ──►  THIS APP (picker + proxy)  ──►  sources you connect
  (Stremio /                   │                      (Comet, usenet, …)
   Nuvio / …)                  ▼                              │
                        verifies playback  ◄──────────────────┘
```

**It does not include the sources or any accounts.** Those are independent
services and subscriptions you bring. That's the part the rest of this guide is
about — the app itself is the easy 10 minutes; wiring up at least one real
source is the actual work.

---

## 2. The pieces you connect

You do **not** need all of these. Start with the "essential" row and one search
source; add the rest later from the dashboard.

| Piece | Role | Needed? | What it is |
|-------|------|---------|------------|
| **A debrid account** | Turns torrent/usenet hashes into instant HTTPS streams | **Essential** | A paid service — TorBox or Real-Debrid. This is what makes "torrents" stream instantly. |
| **A search addon** | Finds candidate releases for a title | **Essential** (≥1) | A Stremio-protocol addon that returns `/stream` results — e.g. **Comet** (`g0ldyy/comet`), configured with your debrid key. Self-host or use a hosted instance. |
| **TMDB API key** | Titles, original language, release dates | Strongly recommended | Free from themoviedb.org. Powers the audio-language gate and release-date logic. |
| More search addons | Wider coverage | Optional | **StremThru Torz** (`MunifTanjim/stremthru`), **MediaFusion** (`mhdzumair/MediaFusion`), or *any* addon via the **Custom addons** panel (AIOStreams, etc.). |
| **Usenet lane** | Direct usenet as a source | Optional (advanced) | Needs Newznab **indexers** (paid), a usenet **provider** (paid), and **nzbdav** to mount NZBs as a streamable filesystem. The most complex piece — skip it for your first run. |
| Jellyfin + Jellio | Serve titles you already own first | Optional | Jellio is a Jellyfin→player addon; its URL goes in the dashboard. |
| Radarr / Sonarr / Jellyseerr | Auto-request titles nothing can stream yet | Optional | Pointed at from the dashboard. |
| A reverse proxy | HTTPS + reach the addon from outside your LAN | Needed for remote use | Caddy / Traefik / nginx. Not needed if you only watch on your LAN. |
| A player | Plays the streams | **Essential** | Stremio, or a Stremio-compatible player (Nuvio, Vidi, Fusion). |

---

## 3. Minimal viable setup (fastest path to a working stream)

The smallest thing that streams something:

1. Get a **debrid** account (TorBox or Real-Debrid).
2. Get one **search addon** configured with that debrid key — a **Comet** base
   URL is the usual choice.
3. Get a free **TMDB** API key.
4. Run this app (steps below), paste those two values + the key into the
   dashboard, and install the addon in your player.

Everything else — more sources, usenet, the library, auto-acquire, a public
domain — is additive and can wait.

---

## 4. Step by step

### Step 0 — prerequisites
- A machine with **Docker** and the Compose plugin (`docker compose version`).
- At least the two "essential" items from §2 (a debrid account and one search
  addon's URL). You can start the app without them, but it won't return streams
  until at least one source is connected.

### Step 1 — get the code
```bash
git clone https://github.com/arah91-bit/StreamPicker.git
cd StreamPicker
```

### Step 2 — create your `.env`
```bash
cp .env.example .env
```
Edit `.env` and set the only two values that matter up front:
- `ADDON_SECRET` — an unguessable string; generate with `openssl rand -hex 24`.
  It gates the addon URLs (treat it like a password).
- `ADDON_PUBLIC_URL` — how a player reaches this box. For a first LAN test that's
  `http://<this-host-LAN-IP>:8011`; behind a reverse proxy it's your
  `https://…` domain. (Full list of every setting: see `.env.reference`.)

### Step 3 — start it
```bash
docker compose up -d --build
```
Check it's healthy: `docker compose ps` (should say `healthy` after ~15s).

### Step 4 — open the dashboard
In a browser on your LAN:
```
http://<this-host-LAN-IP>:8011/
```
No secret in this URL — the dashboard is LAN-only by default. You'll land on
**Overview** (empty until you've watched something). Go to the **Settings** tab.

### Step 5 — connect at least one source
On **Settings → Connections**:
1. Paste your **Comet** base URL into the Comet field (the URL that embeds your
   debrid key — it looks like `https://comet…/<long-config-string>`).
2. Paste your **TMDB** API key.
3. Click each service's **Test** button — you want a green dot before moving on.
4. Optionally add StremThru / MediaFusion, or any other addon under **Custom
   addons** (paste its `…/manifest.json` URL and Test it).
5. Click **Save**, then **Restart addon** to apply.

### Step 6 — choose how streams are handled
On **Settings → Stream path**, pick one:
- **Cache on disk** — best experience (read-ahead + mid-stream failover); needs
  disk and bandwidth.
- **Pass through** — proxied but nothing stored.
- **Direct links** — lightest; no failover/stats (and usenet results are
  dropped). See the on-page descriptions.

### Step 7 — (optional) expose it for remote use
Only if you want to watch away from home. Put a reverse proxy in front and point
your domain at the container. Minimal **Caddy** example:
```
autostream.example.com {
    reverse_proxy localhost:8011
}
```
Set `ADDON_PUBLIC_URL=https://autostream.example.com` in `.env` and restart.
The dashboard stays 404 to the public (the local-only guard blocks proxied
requests); only the secret-gated addon URLs are reachable from outside.

### Step 8 — install in your player
Add these in your player → Addons (swap in your base URL and `ADDON_SECRET`):

| What | URL |
|------|-----|
| Fast picker | `<base>/<secret>/manifest.json` |
| Best quality | `<base>/<secret>/slow/manifest.json` |
| Fast, mobile | `<base>/<secret>/mobile/manifest.json` |
| Best quality, mobile | `<base>/<secret>/slow/mobile/manifest.json` |

`<base>` is `http://<LAN-IP>:8011` for local use, or your `https://` domain.
Install the fast and best-quality addons side by side — they share one search.

### Step 9 — verify it works
1. Open a popular movie in your player → the addon should return a stream within
   a few seconds; play it.
2. Back in the dashboard, **Source health** fills in with probe results and the
   **Overview** ledger starts counting.
3. If you get no streams: re-check the **Test** buttons in Settings, and make
   sure your debrid account is active.

### Optional add-ons (later)
- **Usenet lane** — add Newznab indexers (`NZB_INDEXERS`) + nzbdav creds in
  Settings. Expect it to be fiddly; usenet is ~40% reliable by nature and the
  probe correctly drops the misses.
- **Library** — add your Jellio (Jellyfin) URL so titles you own play first.
- **Auto-acquire** — point Radarr/Sonarr/Jellyseerr so titles nothing can stream
  get requested automatically. If those run in another Compose project, see the
  `networks:` note in `docker-compose.yml`.

---

## 5. For an AI agent doing the setup

Everything is file- and API-driven, so no clicking is required:

- **Config via file:** every setting is an environment variable. `.env.reference`
  is the complete annotated list (defaults + one-line descriptions). Write the
  keys you want into `.env` and `docker compose up -d`. Required to boot:
  `ADDON_SECRET`. Strongly recommended: `ADDON_PUBLIC_URL`, one search source
  (`FAST_BASE_URL`), and `TMDB_API_KEY`.
- **Config via API** (dashboard endpoints, LAN/loopback only): `POST
  /api/settings/save` with `{"values": {KEY: VALUE, …}}`; test a service with
  `POST /api/settings/test/<service>`; snapshot current config with `GET
  /api/settings/export.env`; apply with `POST /api/settings/restart`. Unknown
  keys are rejected — it's not a generic env-setter.
- **Precedence:** stored config (`data/config.json`) overrides `.env`, which
  overrides code defaults. Changes apply on **restart**.
- **Verify programmatically:** `GET /health` → `{"ok":true}`; open a title's
  stream endpoint `GET /<secret>/stream/movie/<imdb-id>.json` and confirm the
  `streams` array is non-empty.

---

## 6. Troubleshooting

- **No streams returned** — no source connected, a failed **Test**, or an
  inactive debrid account. Start with Comet + debrid; confirm the Test is green.
- **Dashboard 404 from your domain** — expected; it's LAN-only. Use
  `http://<LAN-IP>:8011/`, or set `DASHBOARD_LOCAL_ONLY=0` only behind your own
  auth.
- **Player can't play the stream** — check `ADDON_PUBLIC_URL` is reachable from
  the player's device (it's baked into the proxied playback URLs).
- **Settings didn't take effect** — they apply on **restart**; use the dashboard's
  Restart button or `docker compose restart`.
- **Health check** — `docker compose ps`, `docker compose logs -f`, and the
  **Source health** page.
