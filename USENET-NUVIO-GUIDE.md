# Usenet streaming with stream-picker, nzbdav, and Caddy

This guide takes you from "I have a Stremio-compatible player with a debrid
provider and another addon" to a fully self-hosted stack that adds **direct
Usenet streaming**. By the end you'll have:

- **Caddy** — automatic HTTPS reverse proxy so your phone can reach the addon
  from anywhere.
- **stream-picker** — the Stremio-compatible picker/proxy addon (shows up in
  your player as "Auto Stream").
- **nzbdav** — WebDAV mount server that turns NZBs into streamable files
  without downloading them.

Your debrid key, indexer URLs, and NNTP provider credentials are wired in
through the stream-picker dashboard. stream-picker never speaks NNTP; your
NNTP logins live only in nzbdav.

## Words you'll see in this guide

New to self-hosting? These are the only terms you really need:

- **Docker / container** — a way to run an app in a self-contained box. Each
  service here runs as one container; you start them all with a single command.
- **Compose** — the file (`docker-compose.yml`) that describes those
  containers, and the `docker compose` command that runs them.
- **Debrid provider** — a paid service that turns torrent/usenet links into
  fast, direct downloads. You bring an account; the addon does the rest.
- **Usenet / NNTP provider** — a paid service you stream usenet articles from.
  "NNTP" is just the protocol it speaks.
- **Indexer (Newznab)** — a search engine for usenet. It returns an **NZB**: a
  small file that says where a release lives on usenet.
- **Reverse proxy** — a front door (here, **Caddy**) that adds HTTPS so your
  phone can reach the addon from outside your home.
- **WebDAV** — how stream-picker hands NZBs to nzbdav and reads the mounted
  files back. You'll pick a username/password for it inside nzbdav.

## How the pieces fit together

```
        phone / TV
            │
            ▼
    https://streams.example.com  (Caddy)
            │
            ▼
    ┌───────────────┐
    │ stream-picker │──► debrid torrent lanes
    └───────────────┘
            │
            └─► direct usenet lane:
                search your indexers,
                PUT the NZB into nzbdav,
                nzbdav mounts and streams
                from your NNTP provider(s)
```

For each title you open, stream-picker races all lanes at once:

- **Torrent lanes** — the setup wizard mints these automatically from just your
  debrid provider's API key.
- **Direct usenet lane** — stream-picker searches your Newznab indexers itself,
  PUTs the chosen NZB into nzbdav's WebDAV watch folder (`/nzbs/{movies|tv}/`),
  and nzbdav mounts it. It streams articles on demand from your NNTP provider(s)
  instead of downloading the whole release. stream-picker probes the mount for
  real playability and serves it to your player through its own proxy.

Another Stremio-compatible addon already installed in your player coexists
fine: leave it as-is, or optionally plug its manifest URL into stream-picker as
a custom addon so its results get re-verified by the same pipeline.

## What you need

- A machine (always-on is best) with Docker and the Compose plugin. If Docker
  isn't installed yet, follow the official install guide for your OS —
  <https://docs.docker.com/engine/install/> — which includes the Compose
  plugin. Check it works with `docker compose version`.
- **Only if you'll watch away from home:** a public domain or subdomain you can
  point at the machine (for Caddy's automatic HTTPS). A cheap domain from any
  registrar works; no domain? a free dynamic-DNS hostname (Duck DNS is a good
  one) works too. If your connection won't let you open ports 80/443 — common
  on home internet behind carrier-grade NAT — skip Caddy and use a tunnel
  instead (Cloudflare Tunnel or Tailscale Funnel both hand you a public URL).
  Watching only on your home network for now? You can skip all of this.
- An **API key from your debrid provider** (from its settings/account page).
  The setup wizard has a switch for each supported debrid service — you turn on
  the one you have and paste its key, nothing more to know.
- **API keys and Newznab URLs** for each indexer you use (each indexer's
  profile/API page shows both).
- **Username/password** for one or more NNTP/Usenet providers (NNTP access).
- A Stremio-compatible player installed on your phone/TV. Your existing addon
  setup stays untouched.

Expect to pay for two things: the debrid plan and the Usenet provider
(a few dollars a month each). Most indexers are free or ask for a small
one-time/donation fee for API access.

## File layout

Create one folder for the whole stack, then move into it — every command and
file below happens inside this folder:

```bash
mkdir stream-picker && cd stream-picker
```

Three of these you create by hand with a text editor: `docker-compose.yml`,
`Caddyfile`, and `.env`. On a plain Linux server, `nano docker-compose.yml`
opens an editor — paste the contents, then save with Ctrl-O, Enter, Ctrl-X.
The rest are created for you. You'll end up with:

```
stream-picker/
├── docker-compose.yml
├── .env
├── Caddyfile
├── secrets/
│   └── stream-picker-config.key
├── data/                  (created by stream-picker)
├── nzbdav-config/         (created by nzbdav)
├── caddy-data/            (Caddy certs)
└── caddy-config/
```

## 1. Combined `docker-compose.yml`

This single file runs everything on one Docker bridge network (`media`) so the
containers can reach each other by name.

```yaml
services:
  caddy:
    image: caddy:2
    container_name: caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy-data:/data
      - ./caddy-config:/config
    networks:
      - media

  stream-picker:
    image: ghcr.io/arah91-bit/streampicker:latest
    container_name: stream-picker
    restart: unless-stopped
    init: true
    stop_grace_period: 45s
    read_only: true
    cap_drop:
      - ALL
    # Used only by the entrypoint to repair bind-mount ownership and drop to
    # UID/GID 1000. The serving process retains no capabilities.
    cap_add:
      - CHOWN
      - DAC_OVERRIDE
      - SETGID
      - SETUID
    security_opt:
      - no-new-privileges:true
    pids_limit: 512
    tmpfs:
      - /tmp:rw,nosuid,noexec,size=256m
    ports:
      # LAN fallback for admin/dashboard setup. You can remove this after setup
      # if you only want to administer over LAN.
      - "8011:8000"
    environment:
      ADDON_SECRET: "${ADDON_SECRET:?Set ADDON_SECRET in .env — generate one with openssl rand -hex 24}"
      CONFIG_ENCRYPTION_KEY_FILE: /run/secrets/stream_picker_config_key
    env_file:
      - .env
    volumes:
      - ./data:/data
      - ./secrets/stream-picker-config.key:/run/secrets/stream_picker_config_key:ro
    healthcheck:
      test: ["CMD", "python3", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:8000/health/ready', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    networks:
      - media

  nzbdav:
    image: nzbdav/nzbdav:latest
    container_name: nzbdav
    restart: unless-stopped
    environment:
      PUID: "1000"
      PGID: "1000"
    ports:
      - "3000:3000"
    volumes:
      # nzbdav only needs its settings — it streams without storing anything.
      - ./nzbdav-config:/config
    networks:
      - media

networks:
  media:
    driver: bridge
```

> **Security:** always pull a fresh nzbdav image before you start
> (`docker pull nzbdav/nzbdav:latest`) so you're on the current release, and
> re-pull whenever you update. Keep nzbdav private: its admin UI is guarded only
> by the WebDAV login you set in step 5, so never forward port `3000` to the
> internet. In this stack only Caddy (ports 80/443) faces the internet, and it
> proxies stream-picker — not nzbdav.

## 2. `Caddyfile`

Create `Caddyfile` in the same directory. Replace `streams.example.com` with
your real domain or subdomain.

```
streams.example.com {
    reverse_proxy stream-picker:8000
}
```

Caddy will automatically request and renew a Let's Encrypt certificate as long
as port 80 and 443 are reachable from the internet and your DNS A record points
to this host's public IP.

If you cannot expose ports 80/443 or don't have a domain, skip Caddy and use a
tunnel (Cloudflare Tunnel, Tailscale Funnel) instead. Point
`ADDON_PUBLIC_URL` at the tunnel URL.

## 3. `.env`

`.env` holds two settings. First generate your addon secret — run this and copy
the output:

```bash
openssl rand -hex 24
```

Now create `.env` in the same directory and paste that value after
`ADDON_SECRET=` (no quotes, no spaces):

```bash
ADDON_SECRET=paste-the-openssl-output-here
ADDON_PUBLIC_URL=https://streams.example.com
```

`ADDON_SECRET` becomes a secret path segment in every addon URL — keep it
private; it's what stops someone who guesses your domain from using your addon.
`ADDON_PUBLIC_URL` is the address your player uses for manifest fetches and
playback, so it must be reachable from your phone. Watching only on your home
network for now? Leave the `ADDON_PUBLIC_URL` line out — you can add it later in
the dashboard.

> **This guide has you create three separate secrets — don't mix them up:**
> `ADDON_SECRET` (here); the **config encryption key** file in step 4 (it
> encrypts your saved keys on disk); and a **dashboard login** you choose on
> first visit in step 6 (a username and a password of at least 12 characters).
> Different jobs, different places.

## 4. Start the stack

```bash
install -d -m 700 secrets
openssl rand -out secrets/stream-picker-config.key 32
chmod 400 secrets/stream-picker-config.key

docker compose pull
docker compose up -d
```

Those first three commands create the **config encryption key** (the second of
your three secrets) *before* the containers start — the file has to exist first,
or Docker would create an empty folder in its place. It's the master key that
encrypts dashboard secrets at rest; back it up separately from `./data`.

Check that everything came up:

```bash
docker compose ps
```

All three containers should read `running`. **Caddy will log certificate errors
until you finish the DNS step (§7)** — that's expected and harmless; nothing
else depends on it. You can do all of the setup below over your home network
first and leave turning on outside-the-home access for last.

## 5. Configure nzbdav

Open `http://<host>:3000` and complete the initial admin account.

1. **Settings → Usenet** — add your NNTP provider(s): host, port, SSL setting,
   username, password, and max connections per your provider's documentation.
   Adding more than one provider improves article completion.
2. **Settings → WebDAV** — set a WebDAV username and password. These become
   stream-picker's `NZBDAV_USER` / `NZBDAV_PASS`.
3. **Settings → SABnzbd** — copy the API key shown here and keep it handy for
   step 6. Don't let the name fool you: this is the key for nzbdav's *own*
   SABnzbd-compatible API — nzbdav emulates SABnzbd, and the real SABnzbd plays
   no part in this setup. Step 6's nzbdav card has an optional **API key** field
   for it; filling it just adds queue/history visibility to the dashboard, so
   skip it if you don't care about that.

nzbdav also supports an rclone sidecar for the *arr "infinite library" flow.
You don't need that for this streaming setup — skip it.

## 6. Configure stream-picker

1. Open `http://<host>:8011/` in a browser on your LAN. First visit shows a
   one-time admin account creation (any username, password ≥12 characters);
   later visits use the browser's normal Basic-auth prompt.
2. You land on the guided setup at `/setup` (auto-shown until at least one
   source is configured). It has more cards than the list below — other source
   types, automation, extra metadata; leave anything you don't have switched
   off. For this usenet setup, switch on:
   - **Debrid services** — switch on the card for the debrid service you have
     and paste its API key. It's validated against the provider and live-tested
     with a real stream search; that one key mints both torrent search lanes
     automatically.
   - **Usenet indexers** — one per line, format `name | api-url | apikey`.
     Paste the URL and key from each indexer's profile/API page. The URL must
     be the full Newznab `/api` endpoint (for example:
     `https://api.example-indexer.com/api`).
   - **nzbdav** — URL `http://nzbdav:3000` (works because both containers
     share the `media` network), plus the WebDAV username/password you created
     in nzbdav's UI. The **API key** field is the optional one from step 5 —
     leave it blank if you skipped it.
   - **TMDB** (optional, recommended) — free API key for titles/languages.
   - **Watch away from home** — the public address from your `.env`
     (`https://streams.example.com`). Leave it blank if you're home-only.
3. The wizard live-tests everything before saving: each indexer with a
   Newznab `t=caps` query, nzbdav with an authenticated WebDAV PROPFIND to
   `/nzbs/`. Then **Set up my streams** → **Finish** restarts the addon.

Two rules that bite people:

- **The usenet lane needs both halves.** It activates only when the indexers
  *and* all three nzbdav fields (URL, user, password) are set. Indexers alone
  silently return nothing.
- **Stream path must not be "Direct links".** In stream-picker's Settings,
  keep the stream path on **Cache on disk** (best) or **Pass through** —
  usenet URLs carry WebDAV credentials and are always proxied, so Direct
  links mode drops usenet results entirely.

Every config change — wizard or Settings page — applies only after the
**Restart** button (or `docker compose restart`). Nothing applies live.

## 7. Turn on access from outside your home (DNS — one-time)

This is the last phase, and it's optional: do it once you've confirmed things
work on your home network, or skip it entirely if you only ever watch at home.

Point your domain or subdomain to your server's public IP (search "what is my
IP" in a browser to see it) at your registrar or DNS provider:

```
streams.example.com  A  <your-public-ip>
```

If the machine sits at home behind a router, also log into the router and
forward ports **80** and **443** to the machine's LAN IP, and make sure the
machine's own firewall allows them (e.g. `sudo ufw allow 80,443/tcp` on
Ubuntu/Debian).

Caddy will issue the certificate the first time a request hits port 443. If
you're behind a residential NAT or CGNAT, use a tunnel instead of Caddy.

## 8. Install in your player

After setup, the dashboard's **Overview** tab shows install links with Copy
buttons. The URL shapes (swap in your base and secret):

| Variant | URL |
|---------|-----|
| Fast picker | `<base>/<secret>/manifest.json` |
| Best quality (slower) | `<base>/<secret>/slow/manifest.json` |
| Fast, mobile (1080p cap) | `<base>/<secret>/mobile/manifest.json` |
| Best quality, mobile | `<base>/<secret>/slow/mobile/manifest.json` |

For a phone, start with the **mobile** variant. Fast and slow can be
installed side by side — they share one search, so it won't double your API
calls.

In your Stremio-compatible player: open the addon manager (usually **Addons →
Add Addon**), paste the manifest URL, and install. There is no `stremio://`
deep link; install is paste-the-URL. Your existing addon install keeps working
untouched — the player shows results from all installed addons together.

## 9. Verify it works

1. Play a popular title in your player. Usenet results show up as `NZB` entries
   in the stream list alongside the debrid ones.
2. First play of a title can take a little while — nzbdav is article-checking
   the release over NNTP (up to `NZB_MOUNT_WAIT=600` seconds worst case).
   Mounts persist in nzbdav, so re-opening the same title later is instant.
3. In the dashboard: the **Overview** tab has a "Direct usenet report card",
   and **Source health** shows your learned indexer order, per-indexer clear
   buttons, and failure evidence.

## What to expect from Usenet

Only roughly **~40% of usenet releases actually play** — missing or
incomplete articles are a fact of life. stream-picker probes every candidate
and drops the duds, so what you see should actually start; the cost is that
the usenet lane answers slower than the debrid lanes. All configured indexers
are always searched in parallel — the health scores only affect which indexer's
mount is tried first.

## Speeding up first play

The slow part of a *brand-new* usenet title is nzbdav pulling and checking the
release's articles over your provider — everything downstream is fast once that
finishes. Two levers help, one on each side:

**In nzbdav (the bigger lever for a cold title):**

1. **Settings → Usenet → Max connections** — raise this toward the maximum your
   provider allows (it's on their plan page). nzbdav checks and assembles a
   release faster with more connections, so the first play of a fresh title
   comes sooner.
2. **Add a second provider** on a different network/backbone. It fills the gaps
   when one provider is missing a release's articles — fewer dead releases, and
   the check runs across both at once.
3. Keep nzbdav's article/health checks **on**. They're what make ~40% of
   releases that actually play the ones you get, instead of ones that stall.

**In stream-picker (helps automatically, no change needed):**

- When you start an episode, it quietly prepares the *next* one in the
  background, so the following episode usually opens instantly on a binge.
- The moment a release is ready it warms up its opening, and it reserves one
  slot for a smaller, quicker-to-ready copy — so a usenet-only title has
  something playable while the best-quality copy is still coming down.

If a cold, obscure title still takes a bit to start the very first time, that's
your provider feeding nzbdav — the levers above are where to gain time. The
defaults are set for a good balance; you only need the Settings page if you want
to tune further (it explains each option inline).

## Keeping it up to date

Update the whole stack to the latest images:

```bash
docker compose pull
docker compose up -d
```

Back these up and you can rebuild everything from scratch:

- `secrets/stream-picker-config.key` — without it, the encrypted dashboard
  secrets in `data/config.json` are unreadable.
- `data/` — your stream-picker settings and learned source health.
- `nzbdav-config/` — your provider credentials and mount settings.

## Troubleshooting

- **No `NZB` results at all** — check that all three nzbdav fields *and* the
  indexers are saved, that the addon was restarted after saving, and that
  the stream path isn't "Direct links".
- **A source fails its wizard Test** — indexers: confirm the URL is the full
  Newznab `/api` endpoint and the key is current. nzbdav: confirm the WebDAV
  user/pass and that `http://nzbdav:3000` resolves (all containers on the
  same network).
- **An indexer suddenly stops contributing** — an indexer whose NZB-fetch
  endpoint fails repeatedly gets suppressed. Clear it from its button on the
  **Source health** tab.
- **First mount of a title is slow** — normal; nzbdav is checking articles
  over NNTP. Later plays of the same title reuse the mount and start fast.
- **Playback stalls or mounts crawl** — nzbdav and playback share your
  provider's NNTP connection cap. Keep connection counts sane in nzbdav's
  settings rather than maxing them out.
- **Caddy shows a certificate error** — your DNS A record isn't pointing at
  this host, or ports 80/443 aren't reachable from the internet (check router
  port-forwarding and the host firewall). Use `docker compose logs caddy` and
  confirm the public IP.
- **The dashboard doesn't load at `https://streams.example.com`** — that's by
  design: the dashboard is only served to LAN clients
  (`DASHBOARD_LOCAL_ONLY=1`), even though Caddy proxies the addon itself.
  Administer it at `http://<host>:8011` from your network. Manifest and
  stream URLs still work from anywhere.
- **A settings change did nothing** — changes never apply live. Hit
  **Restart** in the dashboard or run `docker compose restart`.
- **General health** — `docker compose ps`, `docker compose logs -f`, and the
  Source health tab.

## FAQ

**Does my debrid provider need to support Usenet?**
No. stream-picker's Usenet lane is independent: it searches your indexers and
streams through nzbdav using your NNTP provider(s). Most debrid plans do not
include Usenet anyway; check your provider's docs if you're unsure.

**Why doesn't this setup include SABnzbd (or another downloader)?**
It isn't needed — nzbdav talks to your provider directly and only *emulates*
the SABnzbd API. If you later want permanent, download-and-keep Usenet
downloads, you can add a classic downloader alongside this stack; it shares
nothing with the streaming path.

**Do I need to keep my other Stremio addon?**
Optional. Keep it installed as-is, uninstall it, or plug its manifest URL into
stream-picker's **Custom addons** panel so its results get re-verified by the
same probe pipeline. Whatever you choose, nothing in this guide breaks it.

**Can I use a provider-specific addon instead of nzbdav?**
Some NNTP providers offer their own player addon that streams directly from
their search engine. That's a zero-server alternative, but it's usually limited
to that provider's own index. The setup in this guide searches all your
indexers, can pull from multiple providers, verifies playability, and races
Usenet against your debrid lanes.

**Where do my provider credentials live?**
Only in nzbdav. stream-picker talks WebDAV/HTTP to nzbdav and never speaks
NNTP, so NNTP logins never leave that container.

**Do config changes need a restart?**
Yes — always. Settings are read at process start; use the dashboard Restart
button or `docker compose restart`.
