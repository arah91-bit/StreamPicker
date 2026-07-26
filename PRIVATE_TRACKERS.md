# Self-hosted torrents — the private-tracker lane

**This is one of the three source lanes.** It's optional and completely
self-contained: instead of a paid service streaming to you, *you* download the
release to your own storage, start watching while it downloads, and then seed it
forever. See [SETUP.md](SETUP.md) for the other lanes and the base install.

Private releases are **never** sent to a debrid service, and browsing results
never starts a download — a download begins only when you press play.

> **Is this lane for you?**
> Pick it if you already have private-tracker accounts and want your own copies.
> Skip it if you just want something to play — a debrid or usenet lane is far
> less setup. You do **not** need this to use Stream Picker.

**What it costs:** a VPN subscription + whatever your trackers require.
**Difficulty:** the hardest lane — you're running three services.
**Time:** about an hour.

---
## How the lane works

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

## What you need first

| Piece | Why | Notes |
|-------|-----|-------|
| A **storage host** with Docker | rqbit + qBittorrent write here | A NAS or any Linux box. The downloaders must write to a **local** disk (see the NFS warning below). |
| A **VPN** subscription | The tunnel rqbit and qBittorrent ride | Gluetun supports ~40 providers — use whichever you subscribe to. Set `VPN_SERVICE_PROVIDER` to yours (this stack was tested with PIA). |
| **Prowlarr** + ≥1 private-tracker indexer | Finds private releases | You supply the tracker accounts. |
| **qBittorrent**, VPN-routed | Permanent seeding | You probably already run one; a known-good example is below. |
| **rqbit + Gluetun** companion stack | Progressive download behind the kill switch | Shipped in this repo as `deploy/rqbit-pia.compose.yml`. |
| A **shared download directory** | All three clients see the same bytes | The single most important detail — see **"Lay out the shared directory"** below. |

> **Critical rule — where each thing runs.** Run rqbit and qBittorrent **on the
> storage host itself**, writing to a *local* path. Do **not** run a downloader
> on a machine that reaches the directory over NFS — torrent writes over NFS
> hit `ESTALE` and corrupt state. Stream Picker is the exception: it only
> *reads* the finished files, so it may mount the directory read-only over NFS
> from a different host.

## Lay out the shared directory (do this first)

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

## Deploy Prowlarr (skip if you already run it)

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

## Deploy a VPN-routed qBittorrent (skip if you already run it)

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

## Deploy rqbit behind Gluetun (the VPN kill switch)

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
      VPN_SERVICE_PROVIDER: private internet access   # set to YOUR provider — Gluetun supports ~40 (tested with PIA)
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

## Wire it into Stream Picker

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
| `PRIVATE_TRACKER_RELEASE_ORDER` | `episode,season,series` | Preference order; set from the draggable cards (see **"Choose your release preference"**). |
| `PRIVATE_TRACKER_WHOLE_TORRENT` | `1` | After playback, finish the rest of a pack (not just the clicked file). |
| `PRIVATE_TRACKER_CANDIDATES` | `20` | Max results shown per title. |
| `PRIVATE_TRACKER_MIN_SEEDERS` | `5` | Hard eligibility floor. |
| `PRIVATE_TRACKER_MAX_TORRENT_GB` | `0` | Size cap in GB; `0` = unlimited. |
| `PRIVATE_TRACKER_MAX_ACTIVE_DOWNLOADS` | `3` | Concurrent rqbit downloads. |
| `PRIVATE_TRACKER_SEARCH_TIMEOUT` | `45` | Prowlarr search timeout (seconds). |
| `PRIVATE_TRACKER_START_TIMEOUT` | `90` | Max wait for playback to start (seconds). |
| `PRIVATE_TRACKER_SEARCH_TTL` | `10800` | How long a search result is cached (seconds). |

## Verify, then go live

On the **Private Trackers** tab:

1. Press **Save private settings**.
2. Press **Test connections** and require **all five** dots green:
   **Prowlarr, PIA VPN, rqbit, qBittorrent, storage**. (Storage failing almost
   always means the read-only mount from **"Wire it into Stream Picker"** step (a) is missing or points at the
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

## Choose your release preference

The **Private Trackers** tab shows three draggable cards — **individual
episode**, **single-season pack**, **whole-series / multi-season pack**. Drag
them into the order you like and toggle **Include** off to drop a type from
results entirely. This writes `PRIVATE_TRACKER_RELEASE_ORDER` (e.g.
`episode,season,series` or `season,episode`). Movies are unaffected. The
separate `PRIVATE_TRACKER_WHOLE_TORRENT` switch decides whether pack files
beyond the clicked episode are completed after playback. Save and restart to
apply.

## Tips, tricks & gotchas

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

## Lane-specific troubleshooting

- **Only the splash screen, or the stream 502s** — run **Test connections**;
  a red dot names the culprit. Most often rqbit isn't reachable on the LAN IP,
  or Gluetun isn't `running`.
- **Gluetun unhealthy, `AUTH_FAILED`** — stale/incorrect PIA credentials; see
  the password-reset tip in **"Tips, tricks & gotchas"**. Confirm the username is `pXXXXXXX` format.
- **Gluetun unhealthy, handshake never completes** — ensure
  `OPENVPN_PROTOCOL: tcp` and a pinned `SERVER_REGIONS`; try a different region.
- **Storage check fails** — the read-only mount from **"Wire it into Stream Picker"** step (a) is missing or points
  at the wrong directory; `PRIVATE_TRACKER_DOWNLOAD_ROOT` must be the container
  path of that mount.
- **qBittorrent shows 0% after handoff** — its save path and rqbit's output
  path aren't the same physical directory. Re-check **"Lay out the shared directory"**.
- **rqbit's egress equals your real WAN IP** — the tunnel is down; do not use
  the lane until the kill-switch test passes.
- **A download seems stuck after a Stream Picker restart** — startup
  reconciliation resumes an in-flight handoff automatically; watch
  `docker logs -f stream-picker` for the recovery worker.
## Handoff behavior

- Merely viewing private results does not start a torrent.
- The first media `GET` selects one file in rqbit and starts progressive HTTP
  range streaming.
- qBittorrent registers the metainfo in a stopped, flat-layout state while
  rqbit owns the shared files, preventing concurrent writes and preserving
  everything needed for a restart-safe handoff.
- When the selected file completes, rqbit pauses; qBittorrent adds the same
  metainfo stopped, rechecks the shared files, starts, and takes permanent
  ownership.
- New playback requests switch to qBittorrent only after its recheck succeeds.
- Startup reconciliation resumes a handoff if Stream Picker restarted during
  the download.
