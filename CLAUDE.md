# Diagnosing playback buffering

Field notes from a live investigation into "streams take ~20s to start and then
buffer". Written for whoever picks this up next with no memory of it — the goal
is that you can skip the two hours of wrong guesses.

Companion docs: [DESIGN.md](DESIGN.md) for why the picker is shaped the way it
is, [AGENTS.md](AGENTS.md) for how to change settings.

---

## The delivery path

Buffering can come from any layer, and the layers fail in ways that look
identical from the sofa. Know which one you are in before changing anything.

```
player (Stremio/Nuvio/Jellyfin)
  └── stream-picker proxy            app/proxy.py
        ├── start gate               _select_start   — picks a live candidate
        ├── byte cache + producer    _produce        — sequential read-ahead to disk
        └── source
              ├── debrid / HTTP      one hop, usually fine
              ├── easynews           one hop, already-assembled file  app/easynews.py
              └── usenet             WebDAV mount (nzbdav) → NNTP providers
```

The usenet lane is the deep one: a single HTTP range request against the mount
fans out into dozens of NNTP article fetches across multiple providers. Nearly
all the pain lives there, and almost none of it is visible in HTTP status codes
— a wedged NNTP pool returns a perfectly valid `206` that takes 41 seconds.

The Easynews lane is the shallow one, and it is the structural answer to most
of what follows: Easynews indexes files it has **already assembled** and serves
them over ordinary range-capable HTTPS, so failure modes 1, 2 and 4 cannot
occur on it. See "The Easynews lane" below before reaching for the usenet lane
to explain a slow start.

---

## The five failure modes, in the order they cost us time

### 1. nzbdav leaks NNTP sockets until its connection pool is all corpses

**The big one.** Everything else was a rounding error next to this.

*Symptom.* Playback is fine after a restart and degrades over hours. Reads that
took 0.3 s take 40 s. It looks random, affects every release at once, and the
releases are all fine when tested later.

*Confirm it.* Count sockets to the NNTP port by state, per remote host, inside
the nzbdav container. A healthy provider cycles through `ESTABLISHED` →
`FIN_WAIT1` → gone. A leaking one accumulates `CLOSE_WAIT` — the remote closed
and nzbdav never closed its side:

```bash
pid=$(docker inspect -f '{{.State.Pid}}' nzbdav)
python3 - "$pid" <<'PY'
import collections, socket, sys
STATE = {"01":"EST","02":"SYN_SENT","04":"FIN_WAIT1","06":"TIME_WAIT","08":"CLOSE_WAIT"}
cnt = collections.Counter()
for fam in ("tcp", "tcp6"):
    for ln in open(f"/proc/{sys.argv[1]}/net/{fam}").read().splitlines()[1:]:
        p = ln.split(); ip_hex, port_hex = p[2].rsplit(":", 1)
        if int(port_hex, 16) != 563:          # NNTP over TLS
            continue
        if fam == "tcp":
            ip = socket.inet_ntoa(bytes.fromhex(ip_hex)[::-1])
        else:
            b = bytes.fromhex(ip_hex)
            ip = socket.inet_ntop(socket.AF_INET6, b"".join(
                b[i:i+4][::-1] for i in range(0, 16, 4))).replace("::ffff:", "")
        cnt[(ip, STATE.get(p[3].upper(), p[3]))] += 1
for k, v in sorted(cnt.items()):
    print(f"{v:4d}  {k[1]:11s} {k[0]}")
PY
```

Cross-check against the log, which fills with the downstream consequence:

```bash
docker logs nzbdav --since 24h 2>&1 | grep -c "Error getting connection-lock"
docker logs nzbdav --since 24h -t 2>&1 | grep "Error getting connection-lock" \
  | awk '{print substr($1,1,13)}' | uniq -c        # errors per hour — watch it climb
```

Live reading during the investigation: **1547 errors in 24 h**, rising
663 → 823 per hour until a restart dropped it to 61. Forty minutes after a
fresh start, every socket nzbdav held was a leaked `CLOSE_WAIT` against one
backup provider, against a configured cap of 8 for that provider. Its sub-pool
was over 100 % occupied by dead sockets.

*Mechanism.* nzbdav's pool picks "the provider with the most free connections".
Leaked sockets are counted as live, so that provider's pool never frees; any
article routed there waits out the NNTP login timeout, surfacing as
`CouldNotLoginToUsenetException: Timeout reading from NNTP stream`. It
compounds with uptime. Upstream: nzbdav issue #148 ("limited to one active
connection… recreating the container resolves it immediately").

*Fixes.*
- Remove the leaking provider from nzbdav's config, or restart nzbdav.
- In this repo: the transport watchdog in `app/usenet_health.py`
  (`_note_transport` / `transport_stalled`) infers the condition from our own
  traffic and raises it on the home page via `app/home_ui.py`.

*The trap this exists to avoid.* Per-release health policy answers an outage by
striking every release that fails during it. An hour of wedged transport
quietly retires a shelf of perfectly good releases and reports "everything is
broken". Several **different** releases failing the same transport-shaped way
inside a few minutes, with nothing succeeding in between, is the pipe — say
that instead. The watchdog is deliberately in-memory only and clears the
instant anything reads successfully; it must never become a stored strike.

### 2. Every file open pays a cold fetch for the file tail

*Symptom.* Slow start on every open, including healthy sources. Seeks are
expensive too.

*Confirm it.* Time a range GET at EOF against one at offset 0 on a file nothing
has touched yet. Cold tail measured **7.68 s TTFB**; the same read warm was
0.26 s.

*Mechanism.* Players read the container index before anything else — MKV cues
and MP4 `moov` live at the end of the file. The sequential producer starts at
byte 0 and will never have those bytes. Observed live: a reader at offset
368,449,152 of a 368,477,225-byte file while the cache held only the front.

*Fix.* `_fetch_tail` pulls the last `BUFFER_TAIL_MB` (default 8) into memory
when a producer starts; `_tail_response` serves any range inside it from
memory, both for suffix ranges in `serve()` and absolute tail offsets in
`_serve_buffered`. Best-effort — on failure those ranges fall through to the
direct path exactly as before.

### 3. A source that sends nothing was scored "slow" instead of dead

*Symptom.* ~40 s of "Starting stream" across two 502 rounds, then it works on
the player's own third try.

*Mechanism.* Two bugs compounding:
- The start gate had no per-chunk timeout, so a source that connected and then
  said nothing blocked on the HTTP client's 60 s read timeout rather than the
  8 s TTFB budget it would be judged against anyway.
- Zero bytes was recorded as `slow`, and reputation needs two bad sessions to
  cool a release — so the same corpse was served again on the retry.

*Fix.* `_select_start` wraps the first-byte read in
`asyncio.wait_for(..., START_TTFB + 0.5)`, and a candidate that connects and
delivers zero bytes gets the verdict `dead` with `extreme=True`, which cools it
on the first offence. Covered by `DeadStartTests` in `tests/test_proxy_safety.py`.

### 4. The producer parked while the viewer was still watching

*Mechanism.* Backpressure waited on `e.consumers > 0`. But players fetch in
discrete ranges — request a chunk, disconnect, come back a minute later — so
`consumers` is legitimately 0 between requests, and a range past the write head
is served directly without ever taking a consumer ref. The fill froze for the
rest of the film and every later range became a cold upstream fetch.

*Fix.* `_reader_interested` also counts a touch within `BUFFER_IDLE_GRACE`
(180 s). `_watch_fill` now emits a `stalled` telemetry event when a buffer
stops growing with a reader waiting — previously the only way to establish this
was `stat()`-ing the cache file twice by hand.

### 5. decode_health was being taught that starvation is a codec problem

*Symptom.* Universally-supported codecs (AAC, H.264) sitting at ~17 % "failure"
in the learned store, demoting releases for no reason.

*Mechanism.* When a player gave up, the proxy struck the file's codecs — even
when the player had never received enough bytes to make a codec decision. It
also fired while the viewer was actively watching, because the silence detector
looked only at `consumers`, which a player streaming from its own buffer drops
to 0 between range reads.

*Fix.* Two gates. `_spawn_learn` only runs when `e.avail >=
DECODE_LEARN_MIN_BYTES` (8 MB) or the file is complete — otherwise the release
is still cooled but the verdict is logged as starvation. And the silence timer
bails if `e.last` was touched inside the window. The poisoned store was deleted
so it could start collecting honest data.

---

## What did not work

Time spent on these so you do not repeat it:

- **Blaming the picker for offering an unready NZB mount.** The original theory
  was a race between mount-readiness probing and serving. The mount was ready;
  the NNTP transport under it was wedged. Gating on mount readiness would have
  caught nothing.
- **Testing providers one connection at a time.** Every provider authenticated
  in 0.16–0.66 s when probed individually, which proves nothing about a pool
  that has wedged itself. Only the socket-state census showed the problem.
- **Suspecting provider account connection caps.** Worth checking (multiple
  apps sharing one account will exceed it), but here the account limits were
  far above the configured demand.
- **Suspecting IPv6.** The container had no global IPv6; all traffic was
  IPv4-mapped. Not it.
- **Reverse-proxy overhead.** Reaching the WebDAV host by public hostname
  through a reverse proxy instead of the container network is real waste — a
  container-network hop measured 4 ms — but it is single-digit milliseconds
  against 41-second article fetches. Fix it for tidiness, not for buffering.
- **Aggregate telemetry alone.** Failure rates and probe tables never showed
  this. What showed it was per-range TTFB timing and socket states.

---

## Measuring it yourself

The decisive experiment is a cold range read with the head and the tail timed
separately, issued from inside the app container so it uses the same
credentials and network path the proxy does:

```bash
docker exec -i stream-picker python3 - <<'PY'
import os, time, httpx
auth = httpx.BasicAuth(os.environ["NZBDAV_USER"], os.environ["NZBDAV_PASS"])
base, path, size = "http://nzbdav:8080", "/content/tv/<dir>/<file>.mkv", 0
for label, rng in [("head", "bytes=0-1048575"),
                   ("tail", f"bytes={size-1048576}-{size-1}")]:
    t0 = time.monotonic(); first = None; got = 0
    with httpx.Client(auth=auth, timeout=60.0) as c:
        with c.stream("GET", base + path, headers={"Range": rng}) as r:
            for ch in r.iter_raw():
                if first is None: first = time.monotonic() - t0
                got += len(ch)
    print(f"{label} HTTP {r.status_code} ttfb {first:.2f}s "
          f"{got/1e6:.2f}MB in {time.monotonic()-t0:.2f}s")
PY
```

What healthy looks like, measured end-to-end through the proxy on a direct
usenet source with the transport in good shape:

```
first byte            0.3 – 0.8 s
tail / index read     0.01 s          (served from the warmed tail, not upstream)
re-open of byte 0     0.01 s          (served from the byte cache)
sustained throughput  ~34 MB/s        on a 6 GB 4K file needing ~3–6 MB/s
nzbdav CLOSE_WAIT     0
connection-lock errs  0
```

Log line that proves tail warming ran: `bufcache <sig>: tail warmed (8192 KB @
<offset>)`, roughly 0.8 s after `bufcache <sig>: start`.

Interpretation:
- both fast → the transport is healthy; look at the player or the start gate
- tail slow, head fast → tail warming is off or failed
- wildly variable run to run → NNTP pool problem; go to failure mode 1
- resolve paths from nzbdav's SQLite `DavItems` table by walking `ParentId`

Watch the fill telemetry (`fill` / `stalled` events from `_watch_fill`) rather
than inferring from file sizes.

---

## How nzbdav actually serves bytes

Read from the source at 0.6.4. These are the facts that decide what an
integration should look like.

**Byte 0 is free; every other offset costs NNTP round-trips.**
`NzbFileStream.GetFileStream` short-circuits `rangeStart == 0` straight into a
segment stream. Any other offset runs an *interpolation search* over the
segment list (`InterpolationSearch.Find`), and each probe calls
`GetYencHeadersAsync` — a real article-header fetch through the connection
pool. There is **no cross-request article or header cache in the streaming
path**: `ArticleCachingNntpClient` is documented as short-lived and is only
constructed by `QueueManager` during import. So every seek, on every open, pays
the search again. This is the mechanism behind the cold-tail cost in failure
mode 2, and the reason warming the tail once is worth so much.

**Reads are forward-only with bounded prefetch.** `MultiSegmentStream` pulls
segments into a bounded channel of `usenet.article-buffer-size` (**default 40**)
and serves them in order. WebDAV stream reads run at `SemaphorePriority.High`
(`BaseStoreStreamFile`), so streaming already outranks background work.

**Settings are mostly already right at defaults.** Community advice to "set
article buffer to 200, streaming priority to 80" is aimed at people who set
them low. The shipped defaults are `article-buffer-size = 40`,
`streaming-priority = 80`, `max-download-connections = min(total pooled, 15)`.
An article buffer larger than the connection count buys little.

**There are two ways in, and the obvious one is the worse one.**

| | WebDAV `/content/<dir>/<file>` | `/view/.ids/<a>/<b>/<c>/<d>/<e>/<guid>` |
|---|---|---|
| Addressing | by directory + file name | by immutable `DavItem` id, first 5 hex chars sharded |
| Survives re-import | **no** — the release directory carries an attempt suffix that changes | yes |
| Discovery | PROPFIND per candidate path | none, id comes from the mount result |
| Auth | Basic, per connection | `?downloadKey=sha256(f"{path}_{strm_key}")`, no challenge |
| Ranges | yes | yes — 206, `Content-Range`, `Accept-Ranges`, `Content-Encoding: identity` |

**…but the id route is unreachable from here — do not spend time on it again.**
The `/view/.ids/…` endpoint works (verified: `206`, correct `Content-Range`,
keyless request `401`s). The problem is *learning the id*. It is not exposed
anywhere we can reach:

- `BaseStoreItemPropertyManager` defines displayname, getcontentlength,
  getcontenttype, getlastmodified, Win32FileAttributes, resourcetype and
  iscollection — **no `getetag`, no custom id property**. Confirmed against a
  live PROPFIND: the guid appears nowhere in the response.
- No `ETag` or other id-bearing response header on GET/HEAD. Confirmed live.
- `api/list-webdav-directory` and the other `/api/*` controllers authenticate
  against `FRONTEND_BACKEND_API_KEY`, an internal frontend↔backend secret.
- The SAB-compatible history API returns a filesystem `DownloadPath`, not the
  `/view` URL. `.ids` URLs are only written into `.strm` *files on disk* by
  `CreateStrmFilesPostProcessor`, and only under `importStrategy == "strm"` —
  a completed-downloads workflow this lane does not use.

So path-name addressing is forced. Its two costs, measured rather than assumed:
the `PROPFIND … 404` volume is real but each one costs 2–6 ms and is not worth
optimising; the stale-URL failure mode is real and would need *re-resolution on
failure* rather than id addressing.

Other caveats if this ever becomes reachable: `/view` copies with
`bufferSize: 1024`, and its download key is a static SHA-256 with no expiry, so
it must stay server-side. An A/B of 32 MB reads was inconclusive on throughput
because article-fetch speed varied **13.7 → 1.6 MB/s between consecutive runs
of the same request**; NNTP variance swamps everything else.

### Suffix ranges (`bytes=-N`) are answered with the head of the file

The single highest-value thing found by reading the source. `GetWebdavItemRequest`
parses the Range header with `rangeHeader[6..].Split("-", RemoveEmptyEntries)`,
which throws away the leading empty part, and the WebDAV path is no better.
Live proof against a 6,108,448,173-byte file:

```
suffix   bytes=-8388608    ->  Content-Range: bytes 0-8388608/6108448173      WRONG
absolute bytes=6100059565- ->  Content-Range: bytes 6100059565-.../...        OK
```

`_range_response_ok` correctly refuses the wrong answer — which meant
`_fetch_tail` **silently never warmed a tail on any direct-usenet source**, the
exact lane it was written for, and a player's opening index read against an
uncached file became a 502.

Both now send absolute ranges: `_fetch_tail` always (it derives its size from
`total`, so the total is known by construction), and `_serve_direct` rewrites a
player's suffix range whenever `expected_total` is known, leaving it untouched
when it isn't. Covered by `SuffixRangeTests`.

**Rule of thumb: never send `bytes=-N` upstream when the total is known.** Both
forms are legal; agreement on the second one is universal.

**The upstream fix for failure mode 1 exists but is unreleased.** Commit
`c5fa860 fix(nntp): Skip failing usenet providers with circuit breaker` adds
`ProviderCircuitBreaker` (3 consecutive failures → 60 s cooldown, doubling to
5 min, single probe to recover), and `794948b` tags the provider name into
connection-lock errors so a grep replaces the socket census. Both are on `main`
only — `latest`, `v0.6.x`, `v0.x` and `v0.6.4` all point at the 0.6.4 release
commit, and no `main`/`edge` image is published. Getting them means building
from source or waiting for the next release.

Why the leak happens, from `ConnectionPool`: `Return()` pushes a connection
back onto the idle stack without any liveness check, and borrowing only tests
*age*, never whether the socket is still open. The 30 s idle sweeper would
still reap a dead socket sitting idle — so sockets that persist in `CLOSE_WAIT`
are ones whose `ConnectionLock` was never disposed. That never releases the
semaphore permit either, so the provider's effective capacity shrinks toward
zero with uptime. Matches the reported symptom "limited to one active
connection, fixed by recreating the container" exactly.

---

## The Easynews lane

`app/easynews.py`. Easynews has its own search index over files it has already
assembled, served over plain range-capable HTTPS. A search result *is* a
playable URL — no NZB, no nzbdav, no NNTP. Measured against the same content on
the direct-usenet path:

| | Easynews | usenet lane |
|---|---|---|
| cold head TTFB | 0.33 s | 0.3 – 0.8 s |
| **cold tail / index read** | **0.29 s** | **7.68 s** |
| random mid-file seek | 0.29 s | interpolation search + article fetches |
| throughput | 10 – 44 MB/s | ~34 MB/s |

Enable it by saving an Easynews login in the Sources panel; the credentials are
the switch (`EASYNEWS_SOURCE` defaults to `1`, and turning the engine off writes
`0` rather than clearing it, so the login survives).

**What the lane must never stop doing.** Easynews searches posted *filenames*,
not a release index. It is not a curated source and its raw output is dangerous
in three specific ways, all of which the gates in `_candidates` exist to answer
— measured, not hypothetical:

- **Wrong show.** `The Bear S03E05` returns *The Island With Bear Grylls* and
  *The Yogi Bear Show* in the top five. They would resolve and probe fine.
- **Samples.** `Dune Part Two 2024` returns **57 results, every one of them a
  60-second `.sample`**. The correct answer for that title is zero rows, and
  the lane returns zero rows. Samples outrank the real file because they carry
  the same release name.
- **Adult content matched on a cast member's name.** An unrestricted query for
  `Game of Thrones` returns pornography, matched via actresses who appeared in
  it. Easynews' own safe-search flag is **not** the fix — `safeO=1` is broken
  server-side (it answers `{"results":0}` plus a raw PHP `Undefined variable:
  SearchId` notice, so it is not even valid JSON). The title gate is the fix,
  and it works because it anchors on the filename *starting* with the title.

So: the lane reuses `usenet._release_title_match` / `_release_year_match` /
`_episode_match` — one implementation, shared with the usenet and Prowlarr
lanes — and adds a sample gate. Rows are then emitted **untrusted**: no trust
sentinel, so `picker._lane_of` files them under `https` and they earn their
place through the full probe (payload sniff, TTFB, bitrate-relative throughput,
duration check, codec sniff) exactly like any other HTTPS source. Easynews'
metadata is used to pre-rank and to skip obviously-wasted probes, never to skip
validation.

**Credentials go in the URL userinfo, not in `proxyHeaders`.** This is
load-bearing. `proxy._must_wrap` treats a URL carrying userinfo as
never-serve-raw, so an Easynews row is always proxied (even past `WRAP_MAX`)
and is dropped outright when the proxy is off. `proxy.wrap` strips
`behaviorHints.proxyHeaders` **only on its HLS branch** — the header route
would have handed the player the account password on any row past #8. The
direct-nzb lane embeds its WebDAV login the same way for the same reason.

**The account caps concurrent transfers, and the cap is low.** This is the one
operational gotcha of the lane, and it presents as a slow start rather than as
an error. Measured live, reading one file N ways at once:

```
2 – 4 connections   clean, 0.26 s TTFB
6 connections       all served, worst TTFB 5.94 s
8 and 12            RemoteProtocolError — connections killed mid-stream
```

A title routinely yields a dozen Easynews candidates, so an *ungated* probe wave
opens a dozen transfers and playback's own two connections (the producer and
`_fetch_tail`) queue behind them. Observed symptom: the picker answered in 2.0 s
and first byte took 0.36 s, yet the viewer sat on the splash screen — because
the tail warm silently lost its slot and probes died with `RemoteProtocolError`.
`probe.ingest_gate` therefore gates Easynews the same way it gates uncached
TorBox links (`EASYNEWS_MAX_PROBES`, default 2) — different reason, same
mechanism. **Do not raise it to "use the source harder"; that is the failure.**

### The 50-second black screen (failure mode 2, second edition)

Worth reading before touching the buffer, because every instinct here was wrong
and only the last measurement was right.

*Symptom.* Easynews stream, picker answered in 1.8 s, first byte in 0.36 s — and
the viewer sat on a black screen for ~50 s. Reproduced exactly with ffprobe:
**1.3 s reading the file directly, 49.5 s through our own proxy.**

*Mechanism.* A player cannot show a frame until it has the container index,
which lives at EOF. `_fetch_tail` fetches it — but it was fired as a background
task racing the sequential fill, which was pulling the file at 40 MB/s. An 8 MB
tail read that takes **0.36 s unopposed took 48.7 s** against one competing
fill. The picture appeared the instant the warm landed, every time.

The competing fill was usually not even for this stream: `_reader_interested`
kept producers running for `BUFFER_IDLE_GRACE` (180 s) after their reader left,
so an abandoned read-ahead — or the next-episode prefetch — held an Easynews
connection, and Easynews grants about two.

*Fix, in two parts.* `TAIL_WARM_HEADSTART` (3 s) holds the bulk fill back until
the warm lands, because the index is on the critical path to the first frame and
the fill is not; and `BUFFER_IDLE_GRACE_RATIONED` (10 s) stops reading ahead for
a departed viewer on a connection-rationed host. Measured after, six cold
streams: **0.43 – 1.11 s to first frame**, back-to-back included.

*What was wrong on the way.* Easynews' connection cap was real but capping
probes did not fix it; a sustained producer alone did **not** starve the tail
(0.42 s at 6.5 GB pulled); it was not the httpx pool, not bandwidth, not the
shared client. Only timing ffprobe direct-vs-proxy localised it, and only the
per-entry `tail warmed` timestamps explained it. **Instrument the boundary
before theorising about either side of it** — and note that `_fetch_tail` was
silent on failure, which is why this hid for so long. It logs now.

**Search latency has a fat tail; reads do not.** The same query measured 0.69 s,
then 8.58 s, then 1.34 s, and a 15 s timeout fired twice consecutively on a
query that answered in 0.47 s minutes later. `EASYNEWS_SEARCH_TIMEOUT` is
therefore deliberately generous (45 s): nothing blocks on it — callers in
`app.sources` wait only as long as their own deadline and the search is
shielded, so a slow one finishes into the shared cache for the next request. A
tight timeout just throws that work away.

**It is a supplement, not a replacement.** Coverage is real but uneven — a
title with only samples posted (Dune Part Two) or one whose releases don't
surface for the query (Game of Thrones S01E01) correctly yields nothing, while
Inception, Oppenheimer and The Bear return full 4K remuxes. It races in the
fast lane alongside the debrid scrapers; the other lanes still cover what it
misses.

**Keep `EASYNEWS_MAX_RESULTS` small, and resist raising it.** `_assemble` shows
the verified rows plus only `rest[:15]`, so candidates are a fixed budget shared
by every lane. Easynews returns many near-identical encodes of one episode (a
German dub, several HDR variants, two or three web-dls), and at the original
default of 12 it filled that cap on every single request — measured across four
real result lists it took **44-63% of the visible rows**, while the direct-usenet
lane contributed 0-3. The point of running five lanes is breadth, and one lane
returning twelve versions of the same file is the opposite of breadth. The same
number is also a probe budget against a source that rations connections, so
raising it costs start latency too. Both arguments point the same way.

---

## Invariants worth not breaking

- **Never advance `e.head` from a tail probe.** It is the backpressure anchor.
  A player reads the index from EOF on every open; pinning `head` at EOF makes
  `avail - head` permanently negative, so the producer never pauses and fetches
  the whole file however little of it gets watched.
- **The transport watchdog stays in memory.** It answers "is the pipe wedged
  *now*". A stored version would be the exact mistake it exists to correct.
- **Failure reasons must normalise into `_TRANSPORT_REASONS`.** Both halves of
  an outage — the proxy's `dead` verdict and nzbdav's login failure — have to
  land there or the watchdog sees half of it. Guarded by
  `TransportWatchdogTests`.
- **Codec learning requires evidence the player was fed.** See failure mode 5.
- **Content failures must stay content failures.** `missing-articles` is a bad
  post, not a bad pipe, however many of them arrive at once.
- **Easynews rows stay untrusted, and its credentials stay in the userinfo.**
  Both are one-line changes that look like tidying and are not. See "The
  Easynews lane". Guarded by `TrustTests` and `DownloadUrlTests` in
  `tests/test_easynews.py`.

---

## Settings introduced by this work

| Setting | Default | What it does |
|---|---|---|
| `BUFFER_TAIL_MB` | 8 | File tail warmed into memory per stream; 0 disables |
| `BUFFER_IDLE_GRACE` | 180 | Seconds the fill continues after the last reader touch |
| `DECODE_LEARN_MIN_MB` | 8 | Bytes a player must have had before a rejection may strike codecs |
| `NZB_START_RETRY_SECS` | 2 | One extra start attempt for a usenet source, after this pause |
| `NZB_TRANSPORT_MIN_RELEASES` | 3 | Distinct releases that must die before calling it a transport stall |
| `NZB_TRANSPORT_WINDOW_MINUTES` | 10 | Window those failures must land inside |
| `PRIVATE_TRACKER_MIN_SOURCES` | 0 | Below this many public releases, also search private trackers |
| `EASYNEWS_SEARCH_TIMEOUT` | 45 | Generous by design — search latency has a fat tail and nothing blocks on it |
| `EASYNEWS_MAX_RESULTS` | 6 | Easynews files offered per title — small so one lane can't own the list |
| `EASYNEWS_MIN_MB` | 50 | Size floor below which a "video" is a sample whatever it is named |
| `EASYNEWS_RUNTIME_MIN_FRAC` | 0.5 | Reject a file whose declared runtime is this far under the title's |
| `EASYNEWS_MAX_PROBES` | 2 | Easynews candidates probed at once — the account's transfer cap is low |
| `BUFFER_TAIL_HEADSTART` | 3 | Bulk fill waits this long for the tail warm; the index gates the first frame |
| `BUFFER_IDLE_GRACE_RATIONED` | 10 | Idle grace on a connection-rationed host (Easynews) instead of 180 |

The last one is not a bug fix but it is the only real answer to one class of
buffering: when a title has exactly one working copy, no amount of failover
logic helps. More candidates is the fix.

---

## Running the tests

The suite is environment-sensitive — the production env leaking in causes false
failures that look like regressions. Use `docker run` with a **fresh empty
`/data`**, never `docker compose run`:

```bash
D=$(mktemp -d)
docker run --rm -v "$PWD":/srv:ro -v "$D":/data -e CONFIG_FILE=/tmp/t.json \
  --entrypoint python docker-stream-picker -m unittest discover -s /srv/tests -t /srv
```

After changing `app/knobs.py`, regenerate the reference file or a parity test
fails:

```bash
docker run --rm -v "$PWD":/srv -w /srv --entrypoint python \
  docker-stream-picker -m tools.gen_env_reference
```
