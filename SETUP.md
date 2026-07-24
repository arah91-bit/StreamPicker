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
| **OMDb API key** | Independent title identity and runtime check | Optional | The app persists exact-IMDb results and caps uncached calls at 750/day, leaving headroom under OMDb's 1,000-call plan. |
| More search addons | Wider coverage | Optional | **StremThru Torz** (`MunifTanjim/stremthru`), **MediaFusion** (`mhdzumair/MediaFusion`), or *any* addon via the **Custom addons** panel (AIOStreams, etc.). |
| **Usenet lane** | Direct usenet as a source | Optional (advanced) | Needs Newznab **indexers** (paid), a usenet **provider** (paid), and **nzbdav** to mount NZBs as a streamable filesystem. The most complex piece — skip it for your first run. |
| Jellyfin library | Serve titles you already own first | Optional | Native Jellyfin API integration; no Jellio plugin is required. Give it an internal URL and a restricted playback user's login. |
| Radarr / Sonarr / Jellyseerr | Auto-request titles nothing can stream yet | Optional | Pointed at from the dashboard. |
| A reverse proxy | HTTPS + reach the addon from outside your LAN | Needed for remote use | Caddy / Traefik / nginx. Not needed if you only watch on your LAN. |
| A player | Plays the streams | **Essential** | Stremio, or a Stremio-compatible player (Nuvio, Vidi, Fusion). |

---

## 3. Minimal viable setup (fastest path to a working stream)

The smallest thing that streams something:

1. Have at least one **source of streams**. The easy one is a **debrid**
   account (TorBox or Real-Debrid) — but usenet indexers, a Jellyfin library,
   MediaFusion, or any other Stremio addon each count on their own too.
2. Run this app (steps below) and open the dashboard: the **guided setup** is a
   "do you have this?" checklist. Switch on what you actually have, paste its
   details, and it live-tests each one. For a debrid it builds the search lanes
   from just the API key — no addon URLs to assemble.
3. Optionally switch on a free **TMDB** key and the other extras on the same page.
4. Install the addon links it hands you in your player.

Everything on that page is optional except *one* stream source; helpers
(usenet mount, *arr, requests), metadata and a public domain are additive and
can wait.

---

## 4. Step by step

### Step 0 — prerequisites
- A machine with **Docker** and the Compose plugin (`docker compose version`).
- At least the two "essential" items from §2 (a debrid account and one search
  addon's URL). You can start the app without them, but it won't return streams
  until at least one source is connected.

### Step 1 — get the two files
No clone or build needed — the image is prebuilt. Make a directory and grab
the compose file and the env template:
```bash
mkdir stream-picker && cd stream-picker
curl -O https://raw.githubusercontent.com/arah91-bit/StreamPicker/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/arah91-bit/StreamPicker/main/.env.example
install -d -m 700 secrets
openssl rand -out secrets/stream-picker-config.key 32
chmod 400 secrets/stream-picker-config.key
```
The last three commands create the separate 32-byte key that encrypts dashboard
secrets. Keep it out of Git and back it up separately from `./data`; neither a
lost key nor a ciphertext file by itself can recover the credentials. (Working
from a clone of the repo instead? `cp .env.example .env` and swap the compose
file's `image:` line for `build: .`.)

### Step 2 — fill in your `.env`
Edit `.env` and set the only two values that matter up front:
- `ADDON_SECRET` — an unguessable string; generate with `openssl rand -hex 24`.
  It gates the addon URLs (treat it like a password).
- `ADDON_PUBLIC_URL` — how a player reaches this box. For a first LAN test that's
  `http://<this-host-LAN-IP>:8011`; behind a reverse proxy it's your
  `https://…` domain. (Full list of every setting: see `.env.reference`.)

### Step 3 — start it
```bash
docker compose up -d
```
Check it's healthy: `docker compose ps` (should say `healthy` after ~15s).

### Step 4 — open the dashboard
In a browser on your LAN:
```
http://<this-host-LAN-IP>:8011/
```
No secret in this URL — the dashboard is LAN-only by default. After creating
your administrator account you land on the **guided setup** — a checklist
grouped into debrid services, more stream sources (usenet indexers, Jellyfin
library, MediaFusion, another addon), usenet mount & automation
(nzbdav, Radarr/Sonarr, Jellyseerr), metadata (TMDB/OMDb/TVDB), and a public
address. Switch on whatever you have, fill it in, and hit **Set up my
streams** — you don't need a debrid if another source is switched on. Keys are
verified against each service, debrid lanes must return real streams, and only
what passes is saved; one restart later your install links are on the Overview
tab. This one page covers everything Step 5 does; Step 5 is the manual
equivalent (and where these same connections live, any time later).

### Step 5 — connect sources manually (optional)
On **Settings → Connections**:
1. Paste your **Comet** base URL into the Comet field (the URL that embeds your
   debrid key — it looks like `https://comet…/<long-config-string>`).
2. Paste your **TMDB** API key.
3. Click each service's **Test** button — you want a green dot before moving on.
4. Optionally add StremThru / MediaFusion, or any other addon under **Custom
   addons** (paste its `…/manifest.json` URL and Test it).
5. Optionally enable **Jellyfin library**. Enter the internal base URL the
   container can reach (for example `http://jellyfin:8096`) and the username
   and password for a dedicated Jellyfin user. Give that user only the library
   access and playback permissions it needs, then use **Test** to verify login.
6. Click **Save**, then **Restart addon** to apply. Sensitive values entered in
   the dashboard are encrypted at rest and redacted from exports.

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
requests); only the secret-gated addon URLs are reachable from outside. A
connected Jellyfin server does not need its own public URL: Auto Stream serves
library playback through its authenticated, Range-capable proxy.

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
- **OMDb identity check** — add an OMDb API key for a persistent independent
  title/year/type/runtime cross-check. The default hard limit is 750 calls per
  UTC day; cached lookups do not consume the budget.
- **Usenet lane** — add Newznab indexers (`NZB_INDEXERS`) + nzbdav creds in
  Settings. Expect it to be fiddly; usenet is ~40% reliable by nature and the
  probe correctly drops the misses.
- **Library** — add `JELLYFIN_URL`, `JELLYFIN_USERNAME`, and
  `JELLYFIN_PASSWORD` so titles you own play first. The addon uses Jellyfin's
  native API and its own signed playback proxy; Jellio is not required and the
  Jellyfin token never appears in the player URL. Enter the password through
  the dashboard so it is encrypted rather than storing it in plaintext `.env`.
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
- **Config via API** (dashboard endpoints, LAN/loopback only): first open `/`
  locally and create the administrator account. Use that account with HTTP
  Basic auth. Automated deployments can
  preseed `ADMIN_USERNAME` + `ADMIN_PASSWORD` instead. Fetch
  `GET /api/admin/csrf`, then send its `csrf_token` as
  `X-CSRF-Token` on every POST. Save with `POST /api/settings/save` and
  `{"values": {KEY: VALUE, …}}`; test with `POST
  /api/settings/test/<service>`; export with `GET /api/settings/export.env`;
  apply with `POST /api/settings/restart`. Unknown keys are rejected.
- **Secrets via API:** send secret values only over the authenticated dashboard
  API (and HTTPS if the dashboard is exposed). Sensitive values are sealed with
  AES-256-GCM before `config.json` is written and are redacted on export. Do not
  put `JELLYFIN_PASSWORD` in plaintext `.env` for a normal deployment.
- **Precedence:** stored config (`data/config.json`) overrides `.env`, which
  overrides code defaults. Changes apply on **restart**.
- **Verify programmatically:** `GET /health/ready` → `{"ok":true,…}`; open a title's
  stream endpoint `GET /<secret>/stream/movie/<imdb-id>.json` and confirm the
  `streams` array is non-empty.

---

## 6. Troubleshooting

- **No streams returned** — no source connected, a failed **Test**, or an
  inactive debrid account. Start with Comet + debrid; confirm the Test is green.
- **Dashboard 404 from your domain** — expected; it's LAN-only. Use
  `http://<LAN-IP>:8011/`. On the first visit, create an account; afterward
  sign in with that account. Set
  `DASHBOARD_LOCAL_ONLY=0` only over HTTPS. If
  a reverse proxy supplies client-IP headers, list only its IP/CIDR in
  `TRUSTED_PROXIES`.
- **Player can't play the stream** — check `ADDON_PUBLIC_URL` is reachable from
  the player's device (it's baked into the proxied playback URLs).
- **Jellyfin login passes but local playback fails** — confirm `JELLYFIN_URL`
  is reachable from inside the Stream Picker container and that the dedicated
  user has permission to play that library. The player itself does not need to
  reach Jellyfin.
- **Encrypted settings stop loading after a move** — restore the matching
  32-byte master key mounted at `/run/secrets/stream_picker_config_key`. The
  ciphertext deliberately cannot be decrypted with a replacement key.
- **Settings didn't take effect** — they apply on **restart**; use the dashboard's
  Restart button or `docker compose restart`.
- **Health check** — `docker compose ps`, `docker compose logs -f`, and the
  **Source health** page.

---

## 7. The private-tracker lane — progressive local downloads (optional)

Everything above gets you instant streaming from debrid + a search addon. This
section is a completely separate, self-contained lane for people who **like
private trackers** and want their own copies: it downloads the release to your
own storage, starts playing it while it downloads, and then seeds it forever.

It is a deliberately isolated home for local downloads. Private releases are
**never** sent to debrid, and browsing results never starts a download —
playback begins only when you press play. Think of it as a fun "collector"
angle you can switch on if pulling from your trackers is your thing, not a
last resort.

You do **not** need this to use Stream Picker. If you don't run private
trackers, skip the whole section.

### 7.1 How the lane works

Two torrent clients split one job, and neither one ever copies the file:

```
  you press play
        │
        ▼
   ┌─────────┐   downloads ONLY the file you clicked, opening bytes first,
   │  rqbit  │   and serves it over HTTP range requests as it arrives
   └────┬────┘   (playback starts in seconds, not at 100%)
        │  file reaches 100%
        ▼
 ┌──────────────┐  re-checks the exact same bytes in place, finishes the rest
 │ qBittorrent  │  of the release, and seeds it indefinitely
 └──────────────┘
        │
        ▼
   the SAME physical directory on your storage host (no copy, ever)
```

- **rqbit** is the fast progressive downloader/streamer. It runs behind a
  **PIA VPN** with a kill switch, so private-tracker traffic can never leak your
  real IP.
- **qBittorrent** is the long-term seedbox. It takes over the finished files,
  verifies them, downloads anything else in the pack, and keeps seeding.
- **Prowlarr** provides the private-tracker search indexers.
- **Stream Picker** orchestrates all three and serves the video to your player.

Two independent safety layers keep it fail-closed:

1. rqbit shares Gluetun's network namespace (`network_mode: service:rqbit-vpn`),
   so it has **no route** around the VPN. If PIA drops, Gluetun's firewall
   blocks rqbit's traffic at the network layer.
2. Before adding or refocusing any torrent, Stream Picker calls Gluetun's
   authenticated `/v1/vpn/status` and refuses to proceed unless it reports
   `running`.

**What playback feels like:** the clicked file downloads opening-bytes-first, so
you get a ~10–20 second "Finding Best Stream" splash (a startup buffer, *not* a
wait for the full download), then it plays. In a season pack the clicked episode
downloads first and in order so it's immediately streamable while the rest of
the pack fills in behind it.

### 7.2 What you need first

| Piece | Why | Notes |
|-------|-----|-------|
| A **storage host** with Docker | rqbit + qBittorrent write here | A NAS or any Linux box. The downloaders must write to a **local** disk (see the NFS warning below). |
| A **PIA** (Private Internet Access) subscription | The VPN rqbit and qBittorrent ride | Username looks like `p1234567`. |
| **Prowlarr** + ≥1 private-tracker indexer | Finds private releases | You supply the tracker accounts. |
| **qBittorrent**, VPN-routed | Permanent seeding | You probably already run one; a known-good example is below. |
| **rqbit + Gluetun** companion stack | Progressive download behind the kill switch | Shipped in this repo as `deploy/rqbit-pia.compose.yml`. |
| A **shared download directory** | All three clients see the same bytes | The single most important detail — see §7.3. |

> **Critical rule — where each thing runs.** Run rqbit and qBittorrent **on the
> storage host itself**, writing to a *local* path. Do **not** run a downloader
> on a machine that reaches the directory over NFS — torrent writes over NFS
> hit `ESTALE` and corrupt state. Stream Picker is the exception: it only
> *reads* the finished files, so it may mount the directory read-only over NFS
> from a different host.

### 7.3 Lay out the shared directory (do this first)

Pick one physical directory on the storage host, e.g. `/srv/nas/private-dl`.
All three containers must map to that **same** directory:

| Container | Its internal path | Set in Stream Picker as |
|-----------|-------------------|-------------------------|
| rqbit | `/data/nuviodownloads` | `PRIVATE_RQBIT_OUTPUT_PATH` |
| qBittorrent | `/data/nuviodownloads` | `PRIVATE_QBITTORRENT_SAVE_PATH` |
| Stream Picker (read-only) | `/private-downloads/nuviodownloads` | `PRIVATE_TRACKER_DOWNLOAD_ROOT` |

The container paths may differ; the **physical directory must be identical**.
rqbit writes the torrent's files *directly* into its output folder (a flat
layout with no per-release subfolder), and Stream Picker registers qBittorrent
with `NoSubfolder` automatically so the two agree — you don't configure that,
but it's why the paths must line up exactly.

### 7.4 Deploy Prowlarr (skip if you already run it)

```yaml
# prowlarr.compose.yml — on any host reachable by Stream Picker
services:
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: prowlarr
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
    volumes:
      - ./prowlarr/config:/config
    ports:
      - "9696:9696"
```

Then in the Prowlarr web UI:
1. **Settings → Indexers → Add** each of your private trackers (enter your
   tracker credentials there).
2. **Settings → General → Security** — copy the **API Key**; you'll paste it
   into Stream Picker.

### 7.5 Deploy a VPN-routed qBittorrent (skip if you already run it)

Private torrents must never egress your real IP, so qBittorrent needs its own
VPN. A known-good self-contained image is `binhex/arch-qbittorrentvpn`:

```yaml
# qbittorrent.compose.yml — on the STORAGE host (local writes)
services:
  qbittorrentvpn:
    image: binhex/arch-qbittorrentvpn:latest
    container_name: qbittorrentvpn
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - "8081:8081"            # WebUI
    environment:
      - VPN_ENABLED=yes
      - VPN_PROV=pia
      - VPN_CLIENT=openvpn
      - VPN_USER=${VPN_USER}   # your PIA username, e.g. p1234567
      - VPN_PASS=${VPN_PASS}   # your PIA password
      - STRICT_PORT_FORWARD=yes
      - LAN_NETWORK=192.168.0.0/24   # your LAN CIDR
      - WEBUI_PORT=8081
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
    volumes:
      - ./qbittorrent/config:/config
      - /srv/nas/private-dl:/data/nuviodownloads   # the SHARED directory
```

Load PIA credentials from a protected file rather than typing them inline:

```bash
install -d -m 700 /srv/docker/secrets
printf 'VPN_USER=p1234567\nVPN_PASS=your-pia-password\n' \
  > /srv/docker/secrets/pia.env
chmod 600 /srv/docker/secrets/pia.env

docker compose --env-file /srv/docker/secrets/pia.env \
  -f qbittorrent.compose.yml up -d
```

In the qBittorrent WebUI (`http://<storage-host>:8081`, default login
`admin` / `adminadmin`) set a real username/password under **Options → Web UI**
and, under **Options → Downloads**, set the default save path to
`/data/nuviodownloads`. Confirm it's on the VPN:

```bash
# The two must differ — qBittorrent must NOT show your real WAN IP.
curl -s https://ipinfo.io/ip
docker exec qbittorrentvpn curl -s https://ipinfo.io/ip
```

### 7.6 Deploy rqbit behind Gluetun/PIA

This repo ships the stack. On the **storage host**, put it in its own directory:

```bash
install -d -m 750 /srv/docker/rqbit-pia
cd /srv/docker/rqbit-pia
# copy deploy/rqbit-pia.compose.yml and deploy/rqbit-pia.env.example here
cp rqbit-pia.env.example rqbit-pia.env
chmod 600 rqbit-pia.env
install -d rqbit/db rqbit/cache gluetun
```

The compose file (also at `deploy/rqbit-pia.compose.yml`) is:

```yaml
name: stream-picker-rqbit

services:
  rqbit-vpn:
    image: qmcgaw/gluetun:v3.41.1
    container_name: rqbit-vpn
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    security_opt:
      - no-new-privileges:true
    ports:
      # Bind ONLY to the storage host's LAN IP. Never forward these on the router.
      - "${RQBIT_BIND_IP:?Set RQBIT_BIND_IP to the NAS LAN IP}:3030:3030/tcp"
      - "${RQBIT_BIND_IP:?Set RQBIT_BIND_IP to the NAS LAN IP}:8000:8000/tcp"
    environment:
      VPN_SERVICE_PROVIDER: private internet access
      VPN_TYPE: openvpn
      OPENVPN_PROTOCOL: tcp            # TCP survives flaky UDP paths; see tips
      SERVER_REGIONS: "${PIA_SERVER_REGIONS:-US New York}"
      OPENVPN_USER: "${VPN_USER:?Load the PIA credential env file}"
      OPENVPN_PASSWORD: "${VPN_PASS:?Load the PIA credential env file}"
      FIREWALL_INPUT_PORTS: "3030,8000"
      HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE: >-
        {"auth":"apikey","apikey":"${RQBIT_VPN_CONTROL_API_KEY:?Set a Gluetun control API key}"}
      TZ: "${TZ:-America/New_York}"
    volumes:
      - ./gluetun:/gluetun

  rqbit:
    image: ikatson/rqbit:8.1.1
    container_name: rqbit
    restart: unless-stopped
    # KILL SWITCH: rqbit owns no network namespace of its own, so it has no
    # route around Gluetun if PIA disconnects.
    network_mode: "service:rqbit-vpn"
    depends_on:
      rqbit-vpn:
        condition: service_healthy
    user: "${PUID:-1000}:${PGID:-1000}"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    environment:
      RQBIT_HTTP_API_LISTEN_ADDR: "0.0.0.0:3030"
      RQBIT_HTTP_BASIC_AUTH_USERPASS: "${RQBIT_HTTP_USER:?Set an rqbit API username}:${RQBIT_HTTP_PASSWORD:?Set an rqbit API password}"
      RQBIT_UPNP_PORT_FORWARD_DISABLE: "true"
      TZ: "${TZ:-America/New_York}"
    command: ["server", "start", "/data/nuviodownloads"]
    volumes:
      - ./rqbit/db:/home/rqbit/db
      - ./rqbit/cache:/home/rqbit/cache
      - "${PRIVATE_DOWNLOAD_HOST_PATH:?Set the NAS download directory}:/data/nuviodownloads"
```

Fill in `rqbit-pia.env` (keep it mode `0600`) — VPN_USER/VPN_PASS are
intentionally **absent** here; they come from the protected PIA file:

```bash
# rqbit-pia.env
RQBIT_BIND_IP=192.168.1.10                  # the storage host's LAN IP
PRIVATE_DOWNLOAD_HOST_PATH=/srv/nas/private-dl   # the SHARED directory
PUID=1000
PGID=1000
TZ=America/New_York
PIA_SERVER_REGIONS=US New York              # pick a region near you

RQBIT_HTTP_USER=stream-picker
RQBIT_HTTP_PASSWORD=REPLACE_ME              # openssl rand -hex 24
RQBIT_VPN_CONTROL_API_KEY=REPLACE_ME        # docker run --rm qmcgaw/gluetun:v3.41.1 genkey
```

Generate the two secrets (do **not** reuse your PIA password for either):

```bash
openssl rand -hex 24                        # -> RQBIT_HTTP_PASSWORD
docker run --rm qmcgaw/gluetun:v3.41.1 genkey   # -> RQBIT_VPN_CONTROL_API_KEY
```

Bring it up. Compose interpolation happens **before** any service-level
`env_file`, so pass the PIA file first with `--env-file` (this is why VPN_USER
lives on the command line, not in the compose `env_file:`):

```bash
# Validate interpolation without printing secrets:
docker compose \
  --env-file /srv/docker/secrets/pia.env \
  --env-file ./rqbit-pia.env \
  -f rqbit-pia.compose.yml config >/dev/null

# Start it (Gluetun must become healthy before rqbit starts):
docker compose \
  --env-file /srv/docker/secrets/pia.env \
  --env-file ./rqbit-pia.env \
  -f rqbit-pia.compose.yml up -d
```

Confirm the tunnel is up and rqbit only egresses through it:

```bash
set -a; . ./rqbit-pia.env; set +a

# Gluetun should report running:
curl -fsS -H "X-API-Key: $RQBIT_VPN_CONTROL_API_KEY" \
  http://$RQBIT_BIND_IP:8000/v1/vpn/status

# rqbit API answers (authenticated):
curl -fsS -u "$RQBIT_HTTP_USER:$RQBIT_HTTP_PASSWORD" http://$RQBIT_BIND_IP:3030/

# rqbit's egress IP must differ from your real WAN IP:
curl -s https://ipinfo.io/ip
docker exec rqbit-vpn wget -qO- https://ipinfo.io/ip
```

### 7.7 Wire it into Stream Picker

**a) Add the read-only mount (the one thing you must edit in the compose file).**
Everything else is set from the dashboard, but a bind mount can't be — so add
the shared directory to Stream Picker's `docker-compose.yml`, read-only. If
Stream Picker runs on a different host than storage, this mount is over NFS,
which is fine because it only reads:

```yaml
    volumes:
      - ./data:/data
      - ./secrets/stream-picker-config.key:/run/secrets/stream_picker_config_key:ro
      # Private-tracker lane: read-only view of the SAME physical directory
      # rqbit and qBittorrent write to on the storage host.
      - /mnt/nas/private-dl:/private-downloads/nuviodownloads:ro
```

Then `docker compose up -d` to recreate Stream Picker with the new mount.

**b) Enter the connection details in the dashboard.** Open **Private Trackers**
in the dashboard and fill these in (they're stored encrypted and applied on
restart). Use LAN IPs when Stream Picker and the storage host are different
machines; use Docker service names only if everything is on one Compose
network.

```ini
PRIVATE_TRACKERS_ENABLED=1
PRIVATE_STREAM_ENGINE=rqbit            # the whole point of this lane

# Search
PRIVATE_PROWLARR_URL=http://<prowlarr-host>:9696
PRIVATE_PROWLARR_API_KEY=<from Prowlarr → Settings → General>

# Long-term seeding client
PRIVATE_QBITTORRENT_URL=http://<storage-host>:8081
PRIVATE_QBITTORRENT_USERNAME=<qbit web ui user>
PRIVATE_QBITTORRENT_PASSWORD=<qbit web ui pass>
PRIVATE_QBITTORRENT_SAVE_PATH=/data/nuviodownloads   # qBittorrent's internal path
PRIVATE_QBITTORRENT_CATEGORY=stream-picker-private   # auto-created

# Progressive downloader
PRIVATE_RQBIT_URL=http://<storage-host>:3030
PRIVATE_RQBIT_USERNAME=<RQBIT_HTTP_USER>
PRIVATE_RQBIT_PASSWORD=<RQBIT_HTTP_PASSWORD>
PRIVATE_RQBIT_OUTPUT_PATH=/data/nuviodownloads       # rqbit's internal path
PRIVATE_RQBIT_VPN_URL=http://<storage-host>:8000
PRIVATE_RQBIT_VPN_API_KEY=<RQBIT_VPN_CONTROL_API_KEY>

# Stream Picker's read-only view of the shared directory
PRIVATE_TRACKER_DOWNLOAD_ROOT=/private-downloads/nuviodownloads
```

**c) Behavior knobs** (optional; all have sensible defaults):

| Key | Default | What it does |
|-----|---------|--------------|
| `PRIVATE_TRACKER_RELEASE_ORDER` | `episode,season,series` | Preference order; set from the draggable cards (§7.9). |
| `PRIVATE_TRACKER_WHOLE_TORRENT` | `1` | After playback, finish the rest of a pack (not just the clicked file). |
| `PRIVATE_TRACKER_CANDIDATES` | `20` | Max results shown per title. |
| `PRIVATE_TRACKER_MIN_SEEDERS` | `5` | Hard eligibility floor. |
| `PRIVATE_TRACKER_MAX_TORRENT_GB` | `0` | Size cap in GB; `0` = unlimited. |
| `PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS` | `3` | Concurrent rqbit downloads. |
| `PRIVATE_TRACKER_SEARCH_TIMEOUT` | `45` | Prowlarr search timeout (seconds). |
| `PRIVATE_TRACKER_START_TIMEOUT` | `90` | Max wait for playback to start (seconds). |
| `PRIVATE_TRACKER_SEARCH_TTL` | `10800` | How long a search result is cached (seconds). |

### 7.8 Verify, then go live

On the **Private Trackers** tab:

1. Press **Save private settings**.
2. Press **Test connections** and require **all five** dots green:
   **Prowlarr, PIA VPN, rqbit, qBittorrent, storage**. (Storage failing almost
   always means the read-only mount from §7.7a is missing or points at the
   wrong path.)
3. **Restart** Stream Picker so the `rqbit` engine takes effect.
4. Open a movie/episode that a private tracker has. Pick the row labelled
   `🔒 … Local · … · Click to Download & Stream`, press play, wait out the
   short splash, and it should play while downloading.

**Prove the kill switch** once (rqbit must have no route when PIA is down):

```bash
set -a; . ./rqbit-pia.env; set +a
NAS=$RQBIT_BIND_IP

# Stop the tunnel:
curl -fsS -X PUT -H "X-API-Key: $RQBIT_VPN_CONTROL_API_KEY" \
  -H 'Content-Type: application/json' -d '{"status":"stopped"}' \
  http://$NAS:8000/v1/vpn/status

# rqbit should now have NO internet (this should fail/time out):
docker exec rqbit-vpn wget -qO- --timeout=8 https://ipinfo.io/ip \
  && echo "LEAK — investigate" || echo "blocked (correct)"

# Restore:
curl -fsS -X PUT -H "X-API-Key: $RQBIT_VPN_CONTROL_API_KEY" \
  -H 'Content-Type: application/json' -d '{"status":"running"}' \
  http://$NAS:8000/v1/vpn/status
```

With PIA stopped, Stream Picker also refuses to start any download (its
independent `/v1/vpn/status` check), so you have both a network-level and an
application-level guard.

### 7.9 Choose your release preference

The **Private Trackers** tab shows three draggable cards — **individual
episode**, **single-season pack**, **whole-series / multi-season pack**. Drag
them into the order you like and toggle **Include** off to drop a type from
results entirely. This writes `PRIVATE_TRACKER_RELEASE_ORDER` (e.g.
`episode,season,series` or `season,episode`). Movies are unaffected. The
separate `PRIVATE_TRACKER_WHOLE_TORRENT` switch decides whether pack files
beyond the clicked episode are completed after playback. Save and restart to
apply.

### 7.10 Tips, tricks & gotchas

- **UDP tunnels can stall; use TCP.** The stack ships `OPENVPN_PROTOCOL: tcp`
  on purpose — a random PIA UDP endpoint sometimes never completes its
  handshake and Gluetun stays unhealthy (which correctly keeps rqbit stopped).
- **Pin a region.** `SERVER_REGIONS`/`PIA_SERVER_REGIONS` avoids Gluetun
  randomly picking a dead endpoint. Choose one near you.
- **A PIA password reset invalidates every existing session.** If Gluetun logs
  `AUTH_FAILED`, your stored credentials are stale. Update the protected PIA
  env file **and every container that reads it** (both this stack and
  qBittorrent), then recreate them — existing WireGuard sessions keep working
  on the old password until they restart, which masks the problem.
- **Order of `--env-file` matters.** The PIA file must come **first** so
  `${VPN_USER}`/`${VPN_PASS}` interpolate before the compose is parsed. A
  service-level `env_file:` is too late for interpolation.
- **Never forward ports 3030/8000.** They bind to the LAN IP only. The rqbit
  and Gluetun-control APIs are authenticated, but they should never be
  internet-reachable.
- **Match `PUID`/`PGID`** to whatever owns the shared directory (usually
  `1000:1000`) so rqbit and qBittorrent can both read/write the same files.
- **rqbit auto-starts an added torrent.** Don't be alarmed if a torrent is
  already downloading right after it's added — Stream Picker handles the
  start/pause idempotency.
- **Keep secrets out of Git and notes.** PIA creds live in a `0600` env file;
  the rqbit HTTP password and Gluetun control key are separate local secrets.
- **Back up `rqbit/db`.** It holds rqbit's session/torrent state; losing it
  mid-download means re-adding, though qBittorrent already has any handed-off
  releases.

### 7.11 Lane-specific troubleshooting

- **Only the splash screen, or the stream 502s** — run **Test connections**;
  a red dot names the culprit. Most often rqbit isn't reachable on the LAN IP,
  or Gluetun isn't `running`.
- **Gluetun unhealthy, `AUTH_FAILED`** — stale/incorrect PIA credentials; see
  the password-reset tip in §7.10. Confirm the username is `pXXXXXXX` format.
- **Gluetun unhealthy, handshake never completes** — ensure
  `OPENVPN_PROTOCOL: tcp` and a pinned `SERVER_REGIONS`; try a different region.
- **Storage check fails** — the read-only mount from §7.7a is missing or points
  at the wrong directory; `PRIVATE_TRACKER_DOWNLOAD_ROOT` must be the container
  path of that mount.
- **qBittorrent shows 0% after handoff** — its save path and rqbit's output
  path aren't the same physical directory. Re-check §7.3.
- **rqbit's egress equals your real WAN IP** — the tunnel is down; do not use
  the lane until the kill-switch test passes.
- **A download seems stuck after a Stream Picker restart** — startup
  reconciliation resumes an in-flight handoff automatically; watch
  `docker logs -f stream-picker` for the recovery worker.

For the reference version of this lane, see
[PRIVATE_TRACKERS.md](PRIVATE_TRACKERS.md); the same guide is also available
in-app via **Private Trackers → Open the complete private-tracker setup
guide**.
