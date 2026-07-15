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

It's one site with three tabs you click between — **Overview**, **Settings**,
**Source health**. On Settings, each service has a **Test** button that checks
your URL/key before you save; hit **Save**, then **Restart addon** to apply.
Settings live in `./data/config.json` and survive rebuilds.

The dashboard is **local-only by default** — it answers to loopback/LAN/Docker
clients but not to requests coming through a public reverse proxy, so keep the
port on your LAN (like Radarr/Sonarr). To lift that (only behind your own auth),
set `DASHBOARD_LOCAL_ONLY=0`.

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
and thresholds (searchable). **Save**, then **Restart addon** to apply. It all
lands in `./data/config.json`.

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

The "add missing titles" fallback (Radarr / Sonarr / Jellyseerr) and the local
library check (Jellyfin via Jellio) are optional. If those run in another
Compose project on the same host, uncomment the `networks:` blocks in
`docker-compose.yml` and set the name to that project's network
(`docker network ls`) — then you can point the dashboard at
`http://radarr:7878` instead of an IP. Otherwise just give the dashboard a
reachable URL for each.

## Notes

- `ADDON_SECRET` gates both the addon and the dashboard — anyone who has it can
  install your addon and change your settings. Keep it secret; rotate it by
  changing `.env` and restarting.
- The container writes `./data` as root (it runs as root, like most
  self-hosted media tooling). That directory holds your config and telemetry.
- Already running this via a larger Compose file? This standalone
  `docker-compose.yml` uses the same container name and port, so don't
  `docker compose up` it on that same host — it's the copy you hand to someone
  else to run on theirs.
