# Why I built Stream Picker

I wanted something that would work for my wife and family: turn on the
player's "auto-play first source" option and have it *just work*, 99.9% of
the time. I couldn't get there with what existed, so I built this.

## The problems I kept hitting

I ran an [AIOStreams](https://github.com/Viren070/AIOStreams) setup for a
while, and to be clear — AIOStreams is *really* good. But I kept running
into the same walls:

- You can tune dynamic exit conditions to get a pretty good result, but I
  always felt I was leaving quality on the table by exiting early.
- About 1 in 10 times, the first pick in the list wouldn't play — even with
  "must be cached" style filters.
- Streams would sometimes start fine, then buffer in the middle of playback.

The root cause: those setups rank streams by their *labels* — title, size,
seeder counts, cached flags. They can't check the actual video. Stream
Picker's whole philosophy is the opposite: **don't trust the label, probe
the bytes.**

## What it does

**It probes every candidate before showing it to you.** Each stream is
opened for real: does the first byte arrive fast enough? Is it actually a
video? Does it have the runtime of a real episode, or is it a 3-minute clip?
ffprobe inspects the bytes the probe already pulled to learn the real codecs
and bitrate. Only candidates that pass get served — and the #1 result is a
link that has *already proven it plays*. That's the whole trick behind the
99.9%.

**Two addons, one search.**

- The **fast picker** answers in about 4 seconds on average, with results
  verified by real playback probes. The search keeps running in the
  background after it answers.
- The **slow picker** (best quality) takes up to ~55s, digs much deeper into
  the candidate list, and keeps finishing in the background even past that —
  so when someone opens the same title later, the thoroughly-vetted answer
  is already cached and instant.

They share one search, so installing both doesn't double your API calls, and
there are mobile variants (1080p cap) for phones.

**Playback that survives bad sources.**

- Everything is proxied through the home server. By default it caches to
  disk as it plays — a small stall upstream never becomes a stall on the TV
  (there's a plain pass-through toggle if you'd rather not use the disk).
- If two debrid providers hold a byte-identical copy of the same file, it
  can switch between them mid-stream — proactively, before a stall — and the
  viewer can't tell it happened.
- Every playback URL carries backup candidates, so if a source dies
  mid-episode it fails over instead of erroring out.

**HTTPS streams get filtered, not trusted.** Direct HTTP(S) links — the kind
MediaFusion, scraper sources, and custom addons return — are the flakiest
streams in any setup, and a lot of work went into them specifically:

- **Error pages masquerading as video.** Expired tokens and geo-blocks come
  back as HTML or JSON behind a cheerful HTTP 200 — filters and seeder
  counts can't see that. Every candidate's first bytes are sniffed, both the
  declared content-type and the actual container magic, so a "stream" that's
  really an error page never makes your list.
- **A playlist is not a video.** An `.m3u8` is a ~1 KB text file, so probing
  it directly proves nothing. The probe walks master playlist → variant →
  the first real media segment and measures *that*, reads the declared
  resolution/bandwidth/codecs out of the master playlist, and sums the
  segment durations to catch clips and samples hiding inside playlists.
- **Referer- and IP-locked hosts.** A link that worked for the machine that
  scraped it often 403s when your TV fetches it — wrong headers, wrong IP.
  Playlists are rewritten through the proxy so the upstream host only ever
  sees the server's headers and address.
- **HLS used to dodge the whole safety net.** Hundreds of tiny segment
  requests meant no stats, no read-ahead, no rejection detection. Now
  segments get the same treatment as file streams: read-ahead caching,
  per-session delivery stats, player-rejection detection (playlist fetched
  but no segments ever pulled = dead link), and automatic recovery to
  another candidate when segments start failing mid-episode.
- **Container compatibility.** MP4/MKV are preferred over raw transport
  streams when starting playback, and encrypted (AES) HLS segments are
  handled correctly instead of being rejected as unrecognizable bytes.

**It learns what your devices can't play.** If your TV chokes on a codec,
that rejection is remembered and matching releases get demoted — for that
device — instead of being offered again. Same for bare Dolby Vision
profiles that tint green/purple on non-DV screens, and releases with
burned-in hardcoded subs. An audio-language gate demotes releases whose
only audio is neither English nor the title's original language.

**It fills its own gaps.** If nothing playable exists anywhere:

- It checks your **Jellyfin library first** — before any search — and serves
  titles you already have through signed, credential-free proxy URLs. Files
  in codecs your player can't handle (MPEG-2, XviD/DivX, VC-1, WMV) are
  transcoded by Jellyfin and proxied back as seekable HLS instead of being
  dropped.
- If the title isn't in your library either, it automatically requests it
  through [Jellyseerr](https://github.com/fallenbagel/jellyseerr) (falling
  back to Radarr/Sonarr) and shows a "being added" notice instead of a dead
  link. I have private trackers that almost never fail to have something but
  can't be streamed directly — this way they get downloaded and streamed
  from Jellyfin to fill the gaps.
- When the next episode starts, the search for the one after it is already
  running.

**Sources beyond debrid.** A single debrid API key mints both torrent
search lanes. On top of that you can add:

- a **direct usenet lane** — it searches your Newznab indexers, mounts the
  NZB with [nzbdav](https://github.com/nzbdav-dev/nzbdav), and streams from
  your provider without downloading the release (see
  [USENET-NUVIO-GUIDE.md](USENET-NUVIO-GUIDE.md));
- MediaFusion or Prowlarr as additional sources;
- **any other Stremio addons you already use** — including your existing
  AIOStreams instance. Their results get pulled in and re-verified by the
  same probe pipeline, so a dead link from another addon gets caught before
  your family ever sees it.

**A dashboard that shows its work.** Per-source delivery stats, learned
indexer ordering with failure evidence, a direct-usenet report card, and
probe/playback telemetry — so when something does go wrong, you can see why
instead of guessing. Secrets are encrypted at rest, and the dashboard is
LAN-only by default.

## Honest limitations

I've been running it daily and it's been solid, but it's not perfect:

- Once, an Italian-only audio stream slipped through for an English show.
  The audio gate catches most of these, but not all.
- For obscure non-English shows where metadata only lists the English name,
  it sometimes returns nothing. I believe that's a metadata problem, not a
  search problem.
- It's a niche addon: it requires self-hosting and a real setup effort
  (Docker, a domain, API keys). There's a
  [step-by-step guide](USENET-NUVIO-GUIDE.md), but this isn't install-and-go.

Nothing else I've found gets as close to "the first link always plays and
meets my quality bar," because nothing else checks the actual video stream
the way this does.

**https://github.com/arah91-bit/StreamPicker**
