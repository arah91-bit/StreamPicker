# Setup guide — from nothing to watching something

> **Never used Docker before?** You can still do this. Every command below is
> copy-paste, and each one says what it does. You do not need to understand
> Docker to get this working.
>
> **Time:** about 15 minutes for the app itself, plus however long your chosen
> source takes. **Hardest part:** picking a source and signing up for it — the
> app is the easy bit.

---

## 1. What this is (30 seconds)

This app is a **picker**. It doesn't host any video. You connect it to one or
more *sources*, and for every title you open it asks them all, checks which
results actually play, and hands your player the single best working one.

```
   your player   ──►   THIS APP   ──►   the source you connect
  (Stremio /          picks + verifies       │
   Nuvio / …)                ▲               │
                             └── plays it to check ──┘
```

**It includes no sources and no accounts.** Bringing one source is the real
work, and that's what Step 3 is about.

---

## 2. Pick ONE lane

There are three ways to feed this app. **You only need one.** Pick the row that
matches what you're willing to pay for and how much setup you'll tolerate, then
follow only that lane in §4. You can always add the others later.

| | **A · Debrid** | **B · Usenet** | **C · Self-hosted torrents** |
|---|---|---|---|
| **What it is** | A paid service that already holds popular releases and streams them to you instantly over HTTPS | Paid providers that store files you download fast and directly | You download the release to your own disk and watch it as it arrives |
| **You pay for** | One subscription | One or two bills — see §4 | A VPN, plus whatever your trackers need |
| **You run** | Nothing extra | Nothing extra, *or* a mounting helper | Three services on your own storage box |
| **Setup effort** | 🟢 Easiest — paste one key | 🟢 Easy *or* 🟡 fiddly, depending which route | 🔴 Hardest — about an hour |
| **Starts playing in** | Seconds | Seconds | ~10–20 seconds, then streams as it downloads |
| **Reliability** | Very high | Mixed — misses are normal and get skipped | High once it's running |
| **You keep a copy** | No | No | **Yes** — and it seeds |
| **Best for** | "I just want it to work" | People already on usenet | Collectors / private-tracker members |

**Not sure? Choose A.** It's one signup and one copy-pasted key, and you can
have it streaming today.

> **If you're leaning toward usenet**, read §4 Lane B before you buy anything.
> There are two very different routes, and the simpler one is one subscription
> with nothing extra to run — closer to Lane A in effort than the table's
> "fiddly" suggests.

> **Bonus lane (free):** already have a **Jellyfin** server? You can point this
> app at it so films you *already own* play first, with no subscription at all.
> That counts as your one source — see §7.

---

## 3. Install the app (same for every lane)

### Step 1 — Check Docker is installed

Docker is the tool that runs this app. Paste this in a terminal:

```bash
docker compose version
```

- **You see a version number** → you're good, go to Step 2.
- **"command not found"** → install Docker Desktop (Windows/macOS) or Docker
  Engine (Linux) from docker.com, then run the command again.

### Step 2 — Create a folder and download two files

Nothing to build — the app is prebuilt. This makes a folder, downloads the two
files it needs, and creates the key that encrypts your saved passwords:

```bash
mkdir stream-picker && cd stream-picker
curl -O https://raw.githubusercontent.com/arah91-bit/StreamPicker/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/arah91-bit/StreamPicker/main/.env.example
install -d -m 700 secrets
openssl rand -out secrets/stream-picker-config.key 32
chmod 400 secrets/stream-picker-config.key
```

> **Back up `secrets/stream-picker-config.key`** somewhere separate from the
> `data` folder. It encrypts every password you'll type into the dashboard.
> Lose it and those saved passwords can't be recovered (you'd just re-enter
> them).

**✅ Check:** `ls` should show `docker-compose.yml`, `.env`, and `secrets`.

### Step 3 — Fill in two settings

Open `.env` in any text editor. Only two lines matter right now:

**`ADDON_SECRET`** — a random password that keeps your addon URLs private.
Generate one and paste it in:

```bash
openssl rand -hex 24
```

**`ADDON_PUBLIC_URL`** — how your player reaches this machine. For a first test
on your home network, use this computer's LAN IP with port `8011`, e.g.
`http://192.168.1.50:8011`.

<details>
<summary>How do I find my LAN IP?</summary>

```bash
hostname -I | awk '{print $1}'      # Linux
ipconfig getifaddr en0              # macOS (Wi-Fi)
ipconfig                            # Windows — use the IPv4 Address
```
It starts with `192.168.`, `10.`, or `172.`. Not `127.0.0.1`.
</details>

*(Every other setting has a sensible default. The full annotated list is in
`.env.reference` if you're curious.)*

### Step 4 — Start it

```bash
docker compose up -d
```

**✅ Check:** after ~15 seconds, `docker compose ps` shows **healthy**.

### Step 5 — Open the dashboard

In a browser **on the same network**, go to:

```
http://<your-LAN-IP>:8011/
```

There's no secret in this address — the dashboard is locked to your local
network by default and is invisible from the internet.

Create your administrator account, and you'll land on the **guided setup**: a
plain "do you have this?" checklist. That's where Step 3 (your lane) happens —
switch on only what you have, paste the details, and it live-tests each one
before saving.

---

## 4. Connect your lane

Do **only** the lane you picked in §2.

### Lane A · Debrid (easiest)

1. Sign up for a debrid service and copy your **API key** from its website.
2. In the guided setup, switch on the debrid card and paste the key.
3. Press **Set up my streams**.

That's it — the app builds and tests the search lanes for you from the key
alone. No addon URLs to assemble.

> **Known-good combinations.** This has been tested end-to-end with debrid
> services including **TorBox** and **Real-Debrid**, searched via **Comet** (a
> free, open-source search addon). It isn't limited to those — any debrid your
> search addon supports works the same way — but if you want a proven starting
> point, that's one.

<details>
<summary>Doing it manually instead (Settings → Connections)</summary>

Paste your **Comet** base URL into the Comet field — the URL that already
embeds your debrid key, like `https://comet…/<long-config-string>`. Hit
**Test** for a green dot, then **Save** and **Restart addon**.
</details>

### Lane B · Usenet

There are **two routes**, and they are not equally hard. Pick one; you can add
the other later.

#### B1 · The simple route — a provider that searches for you

Some usenet providers keep their own search index *and* serve the finished file
straight over HTTPS. There is nothing to assemble, so there are no indexers to
buy and no mounting helper to run: you paste a username and password, and the
app can search and play.

1. Subscribe to a usenet provider that includes its own search.
2. Dashboard → **Settings → Sources** → the **Easynews** block.
3. Type your **username** and **password**, press **Test** (it runs a real
   search and reports the hit count), then **Save sources** and restart.

Saving a login switches the source on by itself. Turning it off later keeps the
login stored, so switching back on doesn't mean retyping it.

> **Known-good:** tested end-to-end with **Easynews**, which is the provider
> this block is built around. Because the file is already assembled, seeking is
> as cheap as starting — jumping to the middle of a film takes about as long as
> opening it.
>
> **Its limits, honestly:** coverage is good but uneven. Some titles return
> only sample clips, and the app correctly shows you nothing rather than a
> 60-second sample. It is an excellent *first* source, not a complete one — most
> people run it alongside another lane.

#### B2 · The classic route — provider + indexers + nzbdav

More work, wider coverage. You need three things, and the first two must be in
place already:

1. A **usenet provider** subscription (where files actually come from).
2. One or more **Newznab indexers** (how releases are found). Most are paid.
3. **nzbdav**, a helper that makes usenet downloads look like a normal folder
   so the app can stream them.

In the dashboard, switch on the usenet cards and fill in your indexers
(`NZB_INDEXERS`) and the nzbdav address and login. Press **Test** on each.

> **Set expectations:** usenet releases go missing over time. Roughly 40% of
> results won't play — this is normal, and the app detects and skips them
> automatically, so you see working streams rather than dead ones. It just
> means this is the fiddliest route to tune.

### Lane C · Self-hosted torrents (private trackers)

This lane has its own complete walkthrough, because it's a bigger build — you
run a downloader and a VPN on your own storage box.

**→ Follow [PRIVATE_TRACKERS.md](PRIVATE_TRACKERS.md).**

In exchange you get your own permanent copy of everything you watch, and
playback starts while it's still downloading.

Two things worth knowing before you use it, because they change what you see:

- **Results you already have are listed first**, labelled *Already Downloaded ·
  Play Now*. A private search returns twenty near-identical rows; the one on
  your disk plays immediately, costs no ratio and uses no download slot, so it
  goes to the top instead of leaving you to spot it. Anything still in progress
  follows, showing its percentage.
- **Your clicked episode downloads first and alone**, so it can stream in
  order rather than waiting behind the rest of a season pack. Once it finishes,
  the remaining files resume so the release completes and seeds — no
  hit-and-run. Turn that second half off with `PRIVATE_TRACKER_WHOLE_TORRENT`
  if you only ever want the one episode.

---

## 5. Add it to your player

In your player (Stremio, or a compatible one like Nuvio/Vidi/Fusion), add these
addon URLs. Swap in your base URL and the `ADDON_SECRET` from Step 3:

| What | URL |
|------|-----|
| Fast picker | `<base>/<secret>/manifest.json` |
| Best quality | `<base>/<secret>/slow/manifest.json` |
| Fast, mobile | `<base>/<secret>/mobile/manifest.json` |
| Best quality, mobile | `<base>/<secret>/slow/mobile/manifest.json` |

`<base>` is `http://<LAN-IP>:8011` for home use.

**Install the fast and best-quality ones side by side** — they share one search,
so there's no extra cost. Use "fast" when you want to start watching now, and
"best quality" when you care more about the file.

---

## 6. Watch something (the real test)

1. Open a popular movie in your player. A stream should appear within a few
   seconds. Play it.
2. Back in the dashboard, **Source health** starts filling in with real results
   and the **Overview** page starts counting what you've watched.

**If you get no streams:** re-check the **Test** buttons in Settings and confirm
your source account is active. See §8.

**You're done.** Everything below is optional.

---

## 7. Optional extras (any time later)

Add these from the dashboard whenever you feel like it — none are required.

- **TMDB key** (free) — better titles, original-language detection, and release
  dates. Recommended; it improves picking quality.
- **Jellyfin library** — plays films you already own *first*, before any paid
  source. Enter the internal address the container can reach (e.g.
  `http://jellyfin:8096`) plus a dedicated Jellyfin user's login. Give that user
  only playback permission. **This can be your only source if you like** — no
  subscription needed.
- **OMDb key** — an independent title/runtime cross-check. Capped at 750 calls a
  day so it stays inside a free plan.
- **Auto-request** (Radarr / Sonarr / Jellyseerr) — when nothing can stream a
  title, request it automatically instead of showing a dead link.
- **Add another lane** — nothing stops you running debrid *and* usenet *and*
  your own torrents. They're searched together and the best verified result wins.

### Choosing how bytes reach your player

**Settings → Stream path**, pick one:

| Option | What you get |
|---|---|
| **Cache on disk** *(default)* | Best experience — reads ahead, and can switch sources mid-stream if one dies. Uses disk. |
| **Pass through** | Proxied, nothing stored. |
| **Direct links** | Lightest. No failover or stats, and usenet results are dropped. |

### Watching away from home

Only needed if you want to watch outside your house. Put a reverse proxy in
front (a small program that gives you an `https://` address) and point your
domain at the container. Minimal **Caddy** example:

```
autostream.example.com {
    reverse_proxy localhost:8011
}
```

Then set `ADDON_PUBLIC_URL=https://autostream.example.com` in `.env` and
restart. The dashboard stays invisible from the internet — only the
secret-gated addon URLs are reachable. A connected Jellyfin server does **not**
need its own public address.

---

## 8. Troubleshooting

- **No streams returned** — usually no source connected, a failed **Test**, or
  an inactive subscription. Start with one lane and get its Test green.
- **Dashboard won't load from my domain** — that's intentional; it's local-only.
  Use `http://<LAN-IP>:8011/`. Only set `DASHBOARD_LOCAL_ONLY=0` if you've put
  HTTPS in front of it.
- **Player can't play the stream** — `ADDON_PUBLIC_URL` must be reachable *from
  the player's device*; it's baked into the playback links.
- **I changed a setting and nothing happened** — settings apply on **restart**.
  Use the dashboard's Restart button, or `docker compose restart`.
- **Jellyfin logs in but won't play** — the *container* must be able to reach
  `JELLYFIN_URL`, and the user needs playback permission on that library. Your
  player never talks to Jellyfin directly.
- **Saved passwords stopped loading after moving machines** — restore the
  matching `secrets/stream-picker-config.key`. A different key cannot decrypt
  them, by design.
- **General health check** — `docker compose ps`, `docker compose logs -f`, and
  the **Source health** page.

---

## 9. Word list

| Term | In plain English |
|---|---|
| **Docker / container** | A way to run an app in a self-contained box, so it doesn't matter what else is on your computer. |
| **Compose** | A file listing which containers to run. `docker compose up -d` starts them. |
| **LAN IP** | Your computer's address on your home network, like `192.168.1.50`. |
| **Debrid** | A paid service that keeps popular releases ready and streams them to you instantly. |
| **Usenet / Newznab indexer** | An older file network; the indexer is the search engine for it. |
| **Addon / manifest URL** | The link you paste into your player to add a source. |
| **Reverse proxy** | A program that puts a proper `https://` web address in front of an app. |
| **Bind mount** | Sharing one folder from your computer into a container. |
| **Seeding** | Continuing to upload a torrent after it finishes, so others can get it. |

---

## 10. Appendix — automated / AI-agent setup

Everything is file- and API-driven, so no clicking is required:

- **Config via file:** every setting is an environment variable. `.env.reference`
  is the complete annotated list (defaults + one-line descriptions). Write the
  keys you want into `.env` and `docker compose up -d`. Required to boot:
  `ADDON_SECRET`. Strongly recommended: `ADDON_PUBLIC_URL`, one search source
  (`FAST_BASE_URL`), and `TMDB_API_KEY`.
- **Config via API** (dashboard endpoints, LAN/loopback only): first open `/`
  locally and create the administrator account. Use that account with HTTP
  Basic auth. Automated deployments can preseed `ADMIN_USERNAME` +
  `ADMIN_PASSWORD` instead. Fetch `GET /api/admin/csrf`, then send its
  `csrf_token` as `X-CSRF-Token` on every POST. Save with
  `POST /api/settings/save` and `{"values": {KEY: VALUE, …}}`; test with
  `POST /api/settings/test/<service>`; export with
  `GET /api/settings/export.env`; apply with `POST /api/settings/restart`.
  Unknown keys are rejected.
- **Secrets via API:** send secret values only over the authenticated dashboard
  API (and HTTPS if the dashboard is exposed). Sensitive values are sealed with
  AES-256-GCM before `config.json` is written and are redacted on export. Do not
  put `JELLYFIN_PASSWORD` in plaintext `.env` for a normal deployment.
- **Precedence:** stored config (`data/config.json`) overrides `.env`, which
  overrides code defaults. Changes apply on **restart**.
- **Verify programmatically:** `GET /health/ready` → `{"ok":true,…}`; open a
  title's stream endpoint `GET /<secret>/stream/movie/<imdb-id>.json` and
  confirm the `streams` array is non-empty.
