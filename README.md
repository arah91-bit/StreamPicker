# Auto Stream — a self-hosted Stremio stream picker

Races several debrid and direct-usenet sources for a title, verifies that the
top result actually plays, and hands Stremio a high-quality, correct-audio
stream first. Ships two addons from one container — a **fast** picker (answers
in a couple of seconds) and a **best-quality** picker (waits for everything and
ranks harder) — plus an optional on-disk read-ahead buffer that smooths over
flaky sources mid-playback.

Everything is configured from a browser dashboard, so deploying is: set one
secret, start the container, open the dashboard, plug in your services.

## Requirements

- Docker with the Compose plugin (`docker compose`).
- Your own accounts/keys for whichever sources you want (a debrid service via
  Comet, TMDB, usenet indexers, etc.). None are bundled — you connect your own.
- To use it from outside your LAN, a reverse proxy terminating HTTPS in front
  of the container (any of Caddy / Traefik / nginx works).

## Quick start

```bash
cp .env.example .env
# edit .env: set ADDON_SECRET (run: openssl rand -hex 24)
#            set ADDON_PUBLIC_URL to how your devices reach this host
docker compose up -d --build
```

Then open the dashboard and connect your services:

```
http://<host>:8011/<ADDON_SECRET>/settings
```

Each service has a **Test** button that checks your URL/key before you save.
Hit **Save**, then **Restart addon** to apply. Settings live in
`./data/config.json` and survive rebuilds.

## Install in Stremio

Once at least one source is connected, add these URLs in Stremio → Addons
(swap in your public base and secret):

| What | URL |
|------|-----|
| Fast picker | `https://your-domain/<secret>/manifest.json` |
| Best quality (slower) | `https://your-domain/<secret>/slow/manifest.json` |
| Fast, phone/tablet | `https://your-domain/<secret>/mobile/manifest.json` |
| Best quality, phone/tablet | `https://your-domain/<secret>/slow/mobile/manifest.json` |

Install the fast and best-quality addons side by side — they share one search,
so it won't double your API calls. Two more pages, same secret:

- **Overview** — `https://your-domain/<secret>/overview` (the fun ledger:
  gigabytes and hours streamed, resolution/HDR/debrid mix, the direct-usenet
  strike rate and why links fail, and your records)
- **Settings** — `https://your-domain/<secret>/settings`
- **Source health** — `https://your-domain/<secret>/stats` (per-source delivery
  stats and the auto-blocklist, filled in as you watch)

## Configuring: dashboard or files

There are two equivalent ways to set everything up — use whichever you prefer,
or mix them.

**From the dashboard** (`/<secret>/settings`) — the whole configuration is
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
git pull        # or drop in a new copy of the folder
docker compose up -d --build
```

Your `.env` and `./data` are untouched.

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
