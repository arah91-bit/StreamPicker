# Auto Stream — a self-hosted stream picker for your player

[![Test & publish image](https://github.com/arah91-bit/StreamPicker/actions/workflows/publish.yml/badge.svg)](https://github.com/arah91-bit/StreamPicker/actions/workflows/publish.yml)
[![Image](https://img.shields.io/badge/ghcr.io-arah91--bit%2Fstreampicker-blue)](https://github.com/arah91-bit/StreamPicker/pkgs/container/streampicker)

Races several debrid and direct-usenet sources for a title, verifies that the
top result actually plays, and hands your player a high-quality, correct-audio
stream first. Ships two addons from one container — a **fast** picker (answers
in a couple of seconds) and a **best-quality** picker (waits for everything and
ranks harder) — plus an optional on-disk read-ahead buffer that smooths over
flaky sources mid-playback.

Everything is configured from a browser dashboard, so deploying is: set one
secret, start the container, open the dashboard, plug in your services.

> **New here / starting from scratch?** Read **[SETUP.md](SETUP.md)** — a
> from-zero, step-by-step guide (for a human or an AI agent) covering what the
> external pieces are, a minimal viable path, and how to verify it works. This
> README is the quick reference once you know the shape of things.

## Requirements

- Docker with the Compose plugin (`docker compose`).
- Your own accounts/keys for whichever sources you want (a debrid service via
  Comet, TMDB, usenet indexers, etc.). None are bundled — you connect your own.
- To use it from outside your LAN, a reverse proxy terminating HTTPS in front
  of the container (any of Caddy / Traefik / nginx works).

## Quick start

Two files are the whole install — no clone, no build; the image is pulled
prebuilt from GitHub's registry (amd64 + arm64):

```bash
mkdir stream-picker && cd stream-picker
curl -O https://raw.githubusercontent.com/arah91-bit/StreamPicker/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/arah91-bit/StreamPicker/main/.env.example
install -d -m 700 secrets
openssl rand -out secrets/stream-picker-config.key 32
chmod 400 secrets/stream-picker-config.key

# edit .env: set ADDON_SECRET (run: openssl rand -hex 24)
#            set ADDON_PUBLIC_URL to how your devices reach this host
docker compose up -d
```

(Prefer building from source? Clone the repo, swap the compose file's `image:`
line for `build: .`, and run `docker compose up -d --build`.)

Then open the dashboard in a browser — no secret in the URL, just the port,
like any other self-hosted service's web UI:

```
http://<host>:8011/
```

On the first visit, the dashboard shows a one-time account-creation page. Choose
your own username and a password of at least 12 characters. Later visits use
that account through the browser's normal sign-in prompt. For unattended deployment, preseed
`ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` to skip enrollment.

After signing in you land on the **guided setup** — a plain "do you have
this?" checklist. Every kind of source, mount, automation and metadata
provider is a card you switch on *only if you have it*; leave the rest off.
Switch on a debrid (TorBox, Real-Debrid, AllDebrid, Premiumize) and it builds
and live-tests the two torrent search lanes from just the API key — but a
debrid isn't required: usenet indexers, a Jellyfin library, MediaFusion, or
another Stremio addon can each stand alone as your source of streams. Optional
helpers (nzbdav, Radarr/Sonarr, Jellyseerr) and metadata keys (TMDB, OMDb,
TVDB) are on the same page. Everything you switch on is tested before it's
saved (keys against the service itself; debrid lanes must actually return
streams), then it restarts and hands you the install links. The
**Watch away from home** card takes whatever public address your setup already
uses — a reverse proxy (Caddy/Nginx/Traefik) hostname, a tunnel (Cloudflare
Tunnel/Tailscale) URL, or nothing for home-network use. Revisitable any time
at `/setup`.

It's one site with three tabs you click between — **Overview**, **Settings**,
**Source health**. On Settings, each service has a **Test** button that checks
your URL/key or login before you save; hit **Save**, then **Restart addon** to
apply. Settings live in `./data/config.json`; sensitive values saved through
the dashboard are AES-256-GCM ciphertext rather than plaintext. They survive
rebuilds with the install's separate encryption key.
Everything the wizard offers — and the long-tail tuning knobs — can also be
changed there.

Prefer to configure it from files instead of the UI (for scripting, or to hand
the job to an AI)? Every setting is equally reachable by editing `.env` or
`data/config.json` — the same keys the dashboard writes. See
[AGENTS.md](AGENTS.md) for the guide and [`.env.reference`](.env.reference) for
the full annotated key list.

The dashboard is **authenticated and local-only by default** — it answers to
loopback/LAN/Docker clients but not to requests coming through a public reverse
proxy, so keep the port on your LAN (like Radarr/Sonarr). First-run enrollment
always remains local even if you later expose the authenticated dashboard. To
use a reverse proxy, set `DASHBOARD_LOCAL_ONLY=0` and use HTTPS. Forwarding
headers are ignored unless the immediate
peer is listed narrowly in `TRUSTED_PROXIES` (IP/CIDR); never trust
`0.0.0.0/0`.

## Install in your player

The *addon* (unlike the dashboard) is meant to be reached publicly, so it keeps
an unguessable secret in its URL. Once at least one source is connected, add
these in your player → Addons (swap in your public base and `ADDON_SECRET`):

| What | URL |
|------|-----|
| Fast picker | `https://your-domain/<secret>/manifest.json` |
| Best quality (slower) | `https://your-domain/<secret>/slow/manifest.json` |
| Fast, phone/tablet | `https://your-domain/<secret>/mobile/manifest.json` |
| Best quality, phone/tablet | `https://your-domain/<secret>/slow/mobile/manifest.json` |

Install the fast and best-quality addons side by side — they share one search,
so it won't double your API calls.

## Configuring: dashboard or files

There are two equivalent ways to set everything up — use whichever you prefer,
or mix them.

**From the dashboard** (`http://<host>:8011/settings`) — the whole configuration is
here. Connect each upstream (with a live **Test** button), pick how streams are
handled, and open **Advanced tuning** for the full set of timeouts, budgets,
and thresholds (searchable). **Save**, then **Restart addon** to apply.
Values land in `./data/config.json`; secret fields are encrypted there at rest
and exports always redact them.

**From files** (good for scripting or handing to an AI) — every setting is an
environment variable. `.env.reference` is the complete, self-describing menu:
each key with its default and a one-line description, all commented out. Copy
the ones you want into `.env` and `docker compose up -d`. You can also
**Download current .env** from the dashboard to snapshot your live config
(secrets redacted) for backup or migration.

Values in `.env` and values saved in the dashboard both feed the same settings;
dashboard edits (in `config.json`) win, and both apply on restart.

### Custom addons

Beyond the built-in sources, the Settings page has a **Custom addons** panel:
paste any player addon's manifest URL — AIOStreams, a usenet addon, a debrid
catalog, anything that serves `/stream` — and it joins the same search. Its
results are folded into one quality-ranked list with every other source, run
through the **same playback verification**, and only streams that actually play
reach the player. Each addon has a **Test** button that checks the manifest and
confirms it serves streams. (Stored as JSON in `EXTRA_ADDONS`.)

Stack as many as you like: addons that mirror each other's catalogs are
recognized — the picker identifies the same file across addons (filename,
exact size, or listing text) and verifies each release once, so extra addons
widen coverage instead of multiplying probe work. Duplicate copies are kept
as instant failover targets.

HLS streams (many of these addons serve `.m3u8`) are proxied with rewritten
playlists: the host only ever sees this server — with the addon's declared
headers, from one IP — so referer-gated and IP-locked streams that would die
on the player (especially away from home) play reliably, with per-segment
retries and read-ahead. `PROXY_HLS=0` restores raw pass-through.

## Choosing how streams are handled

The dashboard's **Stream path** switch is the main decision:

- **Cache on disk** — streams are pulled through the addon and read ahead onto
  local disk. Seeking back is instant, a dying source is swapped mid-stream
  without the player noticing, and identical copies share one download. The
  cache is a buffer, not a library — it's wiped on restart. Best experience;
  needs the most disk and bandwidth.
- **Pass through** — streams flow through the addon byte-for-byte. You keep
  start-of-play failover and playback stats; nothing is stored.
- **Direct links** — players fetch source URLs themselves. Lightest on the
  server, but no failover, no stats, and direct-usenet results are dropped
  (their URLs carry credentials that only work through the addon).

Everything else — cache size, read-ahead depth, how hard the pickers verify,
the audio-language gate, auto-adding missing titles, and so on — is a switch or
a slider on the same page.

## Updating

```bash
docker compose pull
docker compose up -d
```

Your `.env` and `./data` are untouched. (Building from source instead:
`git pull && docker compose up -d --build`.) To pin a version, use an
immutable tag — every commit on `main` is published as
`ghcr.io/arah91-bit/streampicker:<commit-sha>`.

## Connecting to an existing *arr / Jellyfin stack

The "add missing titles" fallback (Radarr / Sonarr / Jellyseerr) and the native
Jellyfin library check are optional. Jellio is not required. For Jellyfin, set:

- `JELLYFIN_URL` to the base URL this container can reach, such as
  `http://jellyfin:8096` on a shared Docker network.
- `JELLYFIN_USERNAME` and `JELLYFIN_PASSWORD` to a dedicated Jellyfin user with
  access only to the libraries and playback permissions this addon needs.

The addon logs in through Jellyfin's native API, resolves library items itself,
and returns a signed Auto Stream URL. Auto Stream then Range-proxies the media
to the player with server-side authentication, preserving seeking and direct
play without putting the Jellyfin password, access token, or internal address
in the player URL.

Most files play by **direct play** — the original bytes, untouched, no server
load. A file whose video codec the player can't decode (MPEG‑2, XviD/DivX,
VC‑1, WMV) would otherwise arrive as *audio with no picture*; for those, Auto
Stream falls back to a **Jellyfin transcode** to H.264, served back as seekable
HLS with the token still kept server-side. That fallback needs the Jellyfin
user to be **allowed to transcode**: in Jellyfin's **Dashboard → Users → (this
user)**, enable *Allow video playback that requires transcoding*, *Allow audio
playback that requires transcoding*, and *Allow media playback that requires
conversion by the server (remuxing)*. Set `JELLYFIN_TRANSCODE=0` to drop such
titles from library results instead of transcoding them.

If Jellyfin or the *arr services run in another Compose project on the same
host, uncomment the `networks:` blocks in `docker-compose.yml` and set the name
to that project's network (`docker network ls`). Otherwise give the dashboard
a reachable URL for each service.

## Notes

- `ADDON_SECRET` gates addon installation and is not accepted as the dashboard
  password. Dashboard credentials are stored as a salted scrypt verifier in
  `./data/admin-auth.json`, never as the chosen plaintext password.
- Connection secrets entered through the dashboard, including
  `JELLYFIN_PASSWORD`, are encrypted in `config.json` with AES-256-GCM and
  redacted from settings exports. The master key is mounted read-only from the
  host at `/run/secrets/stream_picker_config_key` (configured with
  `CONFIG_ENCRYPTION_KEY_FILE`) and must stay outside the repository. The `.env`
  format itself is plaintext, so enter the Jellyfin password through the
  dashboard rather than putting it in `.env`. Encryption limits damage from an
  accidental file copy or Git commit; it cannot protect against root or another
  process with access to both the ciphertext and the master key.
- To reset a forgotten dashboard account, stop the container, delete
  `data/admin-auth.json` and `data/admin-auth.initialized`, then start it and
  enroll again from the LAN.
- The supplied Compose service uses a read-only root filesystem, drops Linux
  capabilities, and confines writable persistent state to `./data` (plus a
  bounded temporary filesystem at `/tmp`).
- Already running this via a larger Compose file? This standalone
  `docker-compose.yml` uses the same container name and port, so don't
  `docker compose up` it on that same host — it's the copy you hand to someone
  else to run on theirs.
