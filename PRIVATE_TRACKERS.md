# Local private-tracker downloads

This optional lane is a deliberately isolated home for local torrent downloads.
It never sends private releases to debrid, and browsing results never starts a
download. Stream Picker can split the job between two clients:

1. **rqbit** downloads only the selected video and serves it progressively.
2. **qBittorrent** takes over the same on-disk files after that video finishes,
   rechecks them, downloads the rest of the release, and seeds indefinitely.

No media file is copied during the handoff. The rqbit output path and
qBittorrent save path must resolve to the same physical NAS directory.

## Choose your release preference

The **Private Trackers** dashboard includes draggable release-type cards:

- individual episode;
- single-season pack;
- whole-series or multi-season pack.

Drag them into any order and disable types you do not want offered. This writes
`PRIVATE_TRACKER_RELEASE_ORDER` as a comma-separated list such as
`episode,season,series` (the default) or `season,episode`. Movies are unaffected.
The separate whole-torrent switch controls whether pack files beyond the
clicked episode are completed after playback.

## PIA VPN boundary

Use [`deploy/rqbit-pia.compose.yml`](deploy/rqbit-pia.compose.yml) on the NAS.
rqbit uses `network_mode: service:rqbit-vpn`, so it has no independent network
interface or fallback route. Gluetun's firewall is the kill switch: if PIA is
down, rqbit's tracker and peer traffic is blocked.

Stream Picker adds a second, independent check. Before adding or refocusing a
private torrent, it calls Gluetun's authenticated `/v1/vpn/status` endpoint and
requires `{"status":"running"}`. A failed, stopped, unreachable, or
unauthenticated response prevents activation.

The companion stack uses Gluetun's PIA OpenVPN support. It reuses the same PIA
service credentials as the existing qBittorrent container; the rqbit and
Gluetun API passwords are separate local credentials. The supplied deployment
uses PIA's TCP OpenVPN endpoints because they are more tolerant of networks
where a UDP tunnel stalls during setup. It defaults to the `US New York`
region; change `PIA_SERVER_REGIONS` in the deployment env file if another PIA
region is closer.

## Deploy on the NAS

Do not run this downloader on a host that writes the destination over NFS.
Running it beside qBittorrent on the NAS keeps torrent writes local and lets
both clients see the same files for the zero-copy handoff.

Copy the template files to a private directory on the NAS:

```bash
cp deploy/rqbit-pia.env.example rqbit-pia.env
chmod 600 rqbit-pia.env
install -d rqbit/db rqbit/cache gluetun
```

Edit `rqbit-pia.env`:

- `RQBIT_BIND_IP` is the NAS's LAN IP. Never bind or port-forward these APIs to
  the public Internet.
- `PRIVATE_DOWNLOAD_HOST_PATH` is the existing NAS-local qBittorrent download
  directory.
- `PUID` and `PGID` match the owner used by qBittorrent.
- Generate the rqbit password with `openssl rand -hex 24`.
- Generate the Gluetun API key with
  `docker run --rm qmcgaw/gluetun:v3.41.1 genkey`.

Make the existing qBittorrent PIA credential file available on the NAS with
mode `0600`. It must contain the existing `VPN_USER` and `VPN_PASS` keys.
Compose interpolation happens before a service-level `env_file`, so load both
files explicitly, with the PIA file first:

```bash
docker compose \
  --env-file /secure/path/qbittorrentvpn.env \
  --env-file ./rqbit-pia.env \
  -f deploy/rqbit-pia.compose.yml config >/dev/null

docker compose \
  --env-file /secure/path/qbittorrentvpn.env \
  --env-file ./rqbit-pia.env \
  -f deploy/rqbit-pia.compose.yml up -d
```

The first command validates interpolation without printing secrets to the
terminal. Check both containers:

```bash
docker compose \
  --env-file /secure/path/qbittorrentvpn.env \
  --env-file ./rqbit-pia.env \
  -f deploy/rqbit-pia.compose.yml ps
```

## Configure Stream Picker

Open **Private Trackers** in the dashboard. Its **complete setup guide** link
walks through the optional deployment from inside the app. Use:

```text
PRIVATE_STREAM_ENGINE=rqbit
PRIVATE_RQBIT_URL=http://<NAS-LAN-IP>:3030
PRIVATE_RQBIT_USERNAME=<RQBIT_HTTP_USER>
PRIVATE_RQBIT_PASSWORD=<RQBIT_HTTP_PASSWORD>
PRIVATE_RQBIT_OUTPUT_PATH=/data/nuviodownloads
PRIVATE_RQBIT_VPN_URL=http://<NAS-LAN-IP>:8000
PRIVATE_RQBIT_VPN_API_KEY=<RQBIT_VPN_CONTROL_API_KEY>
```

Keep the existing qBittorrent connection for long-term seeding. Its
`PRIVATE_QBITTORRENT_SAVE_PATH` must map to the same NAS directory as rqbit's
`/data/nuviodownloads`. `PRIVATE_TRACKER_DOWNLOAD_ROOT` remains Stream Picker's
read-only mount of that directory.

Save the settings, run **Test connections**, and require all five checks to be
green: Prowlarr, PIA VPN, rqbit, qBittorrent, and storage. Only then restart
Stream Picker to apply the new engine.

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
