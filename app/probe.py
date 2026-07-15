"""Stream probing: fetch the first few MB of a candidate stream over a Range
request and measure (a) time to first byte and (b) sustained throughput after
the first byte. TTFB and throughput are judged separately because they mean
different things here: a slow first byte is a slow *start* (nzbdav assembling
segments — annoying but survivable), while low throughput after the first
byte means the user will buffer mid-movie.

Throughput passes only relative to the file's own bitrate (size / runtime):
a 4 GB encode is fine at 3 MB/s while a 90 GB remux needs ~15 MB/s, so a
fixed threshold would either block small files' viable sources or wave
through remuxes that will stall.
"""

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from app import hlsproxy, telemetry, usenet_health, vprobe

logger = logging.getLogger("stream-picker")

PROBE_BYTES = 4 * 1024 * 1024  # how much of the file to actually pull
SAFETY_FACTOR = 1.5            # required headroom over the file's bitrate
MIN_SPEED_BPS = 2_500_000      # floor when the file size is unknown
# Identify a passing stream's real codecs from the bytes the probe already
# pulled (ffprobe on the file head) — feeds the learned decode-compatibility
# demotion (app.decode_health) so files a player provably can't open stop
# ranking first. ~50ms per *passed* probe; skipped when ffprobe is absent.
CODEC_SNIFF = os.environ.get("PROBE_CODEC_SNIFF", "1") not in ("0", "false", "")
_SNIFF_BYTES = 2 * 1024 * 1024
# A stream's measured duration must roughly match the title's runtime — a
# 3-minute clip delivers beautifully and passes every speed check, but it is
# not the episode. Evidence comes free in both paths: HLS media playlists
# declare every segment's duration, and ffprobe reads a direct file's declared
# duration from the head bytes the probe already pulled. Fail when the
# measured duration is below this fraction of the expected runtime (or absurdly
# above it — a full movie file listed for a 24-minute episode). 0 disables.
DURATION_MIN_FRAC = float(os.environ.get("DURATION_MIN_FRAC", "0.5"))
_DURATION_MAX_FACTOR = 3.0            # ...and this much over, plus the slack
_DURATION_MAX_SLACK = 1200.0

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10),
    headers={"User-Agent": "Stremio"},
)


@dataclass
class ProbeResult:
    ok: bool
    ttfb: float = 0.0        # seconds to first body byte
    speed_bps: float = 0.0   # sustained, measured after the first byte
    reason: str = ""
    node: str = ""           # final delivery host + CDN/cache signal (see telemetry.netinfo)
    via: str = ""
    cache: str = ""
    # What the stream declared about its own content, when the probe descended
    # an HLS master playlist: the chosen variant's EXT-X-STREAM-INF attributes.
    # Free HTTP addons label anything "4K"; the playlist can't lie about the
    # bandwidth it will actually serve, so the picker re-ranks on this.
    media_bps: float = 0.0   # declared BANDWIDTH (bits/s), 0 = unknown
    media_height: int = 0    # declared RESOLUTION height, 0 = unknown
    media_codecs: str = ""   # declared CODECS string, "" = unknown
    # Measured codecs, ffprobe'd from the probe's own bytes (direct files only;
    # HLS variants declare theirs above). Feeds decode-compatibility demotion.
    acodecs: tuple = ()      # audio codec names, () = not sniffed
    vcodec: str = ""         # video codec name, "" = not sniffed
    audio_langs: tuple = ()  # normalized ISO-639-1 audio-track languages
    media_secs: float = 0.0  # measured duration (playlist sum / container), 0 = unknown
    content_kind: str = ""   # "file" or "hls" after content sniffing
    encrypted: bool = False  # HLS playlist declares encrypted media segments
    head: bytes = field(default=b"", repr=False)   # transient; cleared by probe()


def _required_bps(need_bps: float | None) -> float:
    """Delivery rate needed for this stream to play without buffering.

    When size/runtime gives us the stream's real average bitrate, require the
    configured safety headroom over that measurement.  The fixed floor exists
    only for candidates whose bitrate is unknown; applying it to a known small
    encode rejects streams that are demonstrably fast enough for their content.
    """
    if need_bps and need_bps > 0:
        return need_bps * SAFETY_FACTOR
    return MIN_SPEED_BPS


def _too_slow(got: int, measured: float, required: float) -> bool:
    """Graduated mid-read bail-out: the further into the measurement window we
    are, the closer to the requirement the stream must be. A passing stream
    finishes the whole probe window in ~1-2s (telemetry: OK probes complete
    sub-second at median), so anything still reading past 3s is already far
    below need — the ladder only distinguishes hopeless trickles (bail early,
    they used to sit for the full 8s) from marginal streams (measure longer
    so TCP ramp-up can't fail a viable source)."""
    if measured <= 3:
        return False
    speed = got / measured
    if speed < required / 6:
        return True
    if measured > 5 and speed < required / 3:
        return True
    return measured > 8 and speed < required / 2


def _record(stream: dict, result: ProbeResult, attempt_id: str) -> None:
    telemetry.record(stream, result)
    usenet_health.record_probe(stream, result, attempt_id)


# HLS streams (custom HTTP addons often serve .m3u8) can't be judged by the
# byte probe alone: the playlist is a ~1-2 KB text file, so a Range read gets a
# clean tiny EOF and used to fail as "short body" forever. Instead, descend the
# playlist to its first real media segment and measure THAT: master playlist →
# variant playlist → segment, two text hops at most.
_PLAYLIST_HOPS = 2
_PLAYLIST_MAX_BYTES = 512 * 1024
_SEGMENT_JUDGE_BYTES = 512 * 1024   # min sample before speed can fail a segment


def _looks_hls(content_type: str, head: bytes) -> bool:
    return ("mpegurl" in (content_type or "").lower()
            or head.lstrip()[:7] == b"#EXTM3U")


def _looks_text(head: bytes) -> bool:
    """HTML/JSON error pages (expired tokens, geo blocks) masquerading as
    streams. No video container starts with '<' or '{'."""
    return head.lstrip()[:1] in (b"<", b"{")


def _looks_media_payload(content_type: str, head: bytes,
                         encrypted: bool = False) -> bool:
    """Conservative media check for direct files and HLS segments."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith("text/") or any(x in ct for x in ("json", "html", "xml")):
        return False
    h = bytes(head[:256])
    magic = (
        h.startswith(b"\x1aE\xdf\xa3") or h.startswith(b"OggS") or
        h.startswith(b"FLV") or
        (h.startswith(b"RIFF") and h[8:12] in (b"AVI ", b"WAVE")) or
        (len(h) >= 12 and h[4:8] in (b"ftyp", b"styp", b"moof")) or
        b"ftyp" in h[:64] or
        h.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")) or
        h[:1] == b"G" or h.startswith(b"ID3") or
        (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF0) == 0xF0)
    )
    if magic or ct.startswith(("video/", "audio/")):
        return True
    # Declared AES HLS segments are opaque until the player decrypts them.
    return encrypted and ct in ("", "application/octet-stream", "binary/octet-stream")


def _hls_variants(text: str) -> list[tuple[str, dict]]:
    """Master-playlist variants paired with their declared quality."""
    variants: list[tuple[str, dict]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = line
            continue
        if line and not line.startswith("#") and pending:
            info: dict = {}
            m = _BANDWIDTH_RE.search(pending)
            if m:
                info["media_bps"] = float(m.group(1))
            m = _RESOLUTION_RE.search(pending)
            if m:
                info["media_height"] = int(m.group(1))
            m = _CODECS_RE.search(pending)
            if m:
                info["media_codecs"] = m.group(1)[:60]
            variants.append((line, info))
            pending = ""
    return variants


def _hls_target(text: str) -> tuple[str, dict]:
    """Best master variant, or first segment for an ordinary media playlist."""
    variants = _hls_variants(text)
    if variants:
        return max(variants, key=lambda item: (
            item[1].get("media_height", 0), item[1].get("media_bps", 0)))
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line, {}
    return "", {}


def _hls_first_uri(text: str) -> str:
    """Compatibility helper returning the URI selected by current policy."""
    return _hls_target(text)[0]


_EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)")


def _hls_duration(text: str) -> float:
    """Total declared duration of a complete (ENDLIST) media playlist — the
    sum of its segment durations. 0.0 for masters and live playlists, where
    duration is unknown or still growing."""
    if "#EXT-X-ENDLIST" not in text:
        return 0.0
    return sum(float(m.group(1)) for m in _EXTINF_RE.finditer(text))


def _duration_reason(secs: float, expect: float) -> str:
    """Non-empty when a measured duration can't be the expected title."""
    if not DURATION_MIN_FRAC or not secs or not expect:
        return ""
    if secs < expect * DURATION_MIN_FRAC:
        return (f"runs {secs / 60:.0f}min, title needs "
                f"~{expect / 60:.0f}min (clip/sample?)")
    if secs > expect * _DURATION_MAX_FACTOR + _DURATION_MAX_SLACK:
        return (f"runs {secs / 60:.0f}min, far beyond the title's "
                f"~{expect / 60:.0f}min (wrong content?)")
    return ""


_BANDWIDTH_RE = re.compile(r"\bBANDWIDTH=(\d+)")
_RESOLUTION_RE = re.compile(r"\bRESOLUTION=\d+x(\d+)")
_CODECS_RE = re.compile(r'\bCODECS="([^"]*)"')


def _hls_variant_info(text: str) -> dict:
    """Declared quality of the best variant selected by the probe."""
    return _hls_target(text)[1]


_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.I)


async def _probe_spots(url: str, total: int, ttfb_max: float,
                       headers: dict | None) -> str:
    """Validate middle and tail availability for a slow direct-file probe."""
    if total < 2 * 1024 * 1024:
        return ""
    sample = min(256 * 1024, max(total // 64, 64 * 1024))
    starts = dict.fromkeys((max(total // 2 - sample // 2, 0),
                            max(total - sample, 0)))
    timeout = httpx.Timeout(connect=10, read=max(20.0, ttfb_max + 5),
                            write=10, pool=10)
    for label, start in zip(("middle", "tail"), starts):
        end = min(start + sample - 1, total - 1)
        req = dict(headers or {})
        req["Range"] = f"bytes={start}-{end}"
        try:
            async with _client.stream("GET", url, headers=req,
                                      timeout=timeout) as resp:
                if resp.status_code != 206:
                    return f"{label} range HTTP {resp.status_code}"
                m = _CONTENT_RANGE_RE.fullmatch(
                    (resp.headers.get("content-range") or "").strip())
                if not m or int(m.group(1)) != start or int(m.group(3)) != total:
                    return f"{label} bad Content-Range"
                got = 0
                async for chunk in resp.aiter_bytes():
                    got += len(chunk)
                    if got >= end - start + 1:
                        break
                if got < end - start + 1:
                    return f"{label} short body ({got} bytes)"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return f"{label} {type(exc).__name__}"
    return ""


async def probe(url: str, need_bps: float | None, ttfb_max: float,
                headers: dict | None = None,
                expect_secs: float | None = None,
                verify_size: int | None = None) -> ProbeResult:
    """need_bps is the file's real bitrate (size/runtime) or None if unknown.
    `headers` are the stream's declared upstream request headers (proxyHeaders)
    — referer-gated hosts reject bare requests, so probing without them fails
    streams that would play fine through the proxy. `expect_secs` is the
    title's runtime: a stream whose measured duration can't be the title (a
    trailer-length clip, a whole movie for one episode) fails verification
    however fast it delivers."""
    r = await _probe_url(url, _required_bps(need_bps), ttfb_max,
                         time.monotonic(), hops=0, headers=headers,
                         expect_secs=expect_secs)
    if r.ok and r.head and CODEC_SNIFF and vprobe.enabled():
        try:
            ac, vc, secs, langs = await vprobe.media_info_of(r.head)
            r.acodecs, r.vcodec = tuple(ac), vc
            r.audio_langs = tuple(langs)
            if secs:
                r.media_secs = secs
        except Exception:
            pass
    r.head = b""            # transient sniff buffer — never leaves this module
    if r.ok and expect_secs:
        reason = _duration_reason(r.media_secs, expect_secs)
        if reason:
            r.ok, r.reason = False, reason
    if r.ok and verify_size:
        reason = await _probe_spots(url, int(verify_size), ttfb_max, headers)
        if reason:
            r.ok, r.reason = False, reason
    return r


async def _probe_url(url: str, required: float, ttfb_max: float,
                     t0: float, hops: int,
                     media: dict | None = None,
                     headers: dict | None = None,
                     expect_secs: float | None = None) -> ProbeResult:
    """One GET of the probe descent. hops>0 means we're past an HLS playlist,
    probing a media segment: clean EOF short of the 4 MiB window is then a
    complete segment, not truncation. ttfb is always measured from the original
    t0, so playlist hops spend the same first-byte allowance as a direct file.
    `media` carries the master playlist's declared variant quality down the
    descent so the final (segment-judged) result can report it."""
    # The read timeout must outlast ttfb_max, or a legitimately slow starter
    # (nzbdav assembling its first segments can take 20-35s) dies as ReadTimeout
    # before the TTFB allowance it was promised ever comes into play.
    timeout = httpx.Timeout(connect=10, read=max(20.0, ttfb_max + 5),
                            write=10, pool=10)
    playlist_url = ""
    req_headers = dict(headers or {})
    req_headers["Range"] = f"bytes=0-{PROBE_BYTES - 1}"
    try:
        async with _client.stream(
            "GET", url, headers=req_headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code not in (200, 206):
                return ProbeResult(False, reason=f"HTTP {resp.status_code}")
            ni = telemetry.netinfo(resp)
            ctype = resp.headers.get("content-type", "")
            got = 0
            t_first = None
            head = b""
            is_playlist = False
            body = bytearray()
            sniff = bytearray()      # file head kept for codec identification
            async for chunk in resp.aiter_bytes():
                now = time.monotonic()
                if t_first is None:
                    t_first = now
                    if t_first - t0 > ttfb_max:
                        return ProbeResult(
                            False, ttfb=t_first - t0, **ni,
                            reason=f"first byte took {t_first - t0:.1f}s",
                        )
                if not head and chunk:
                    head = bytes(chunk[:64])
                    if _looks_hls(ctype, head):
                        if hops >= _PLAYLIST_HOPS:
                            return ProbeResult(
                                False, ttfb=t_first - t0, **ni,
                                reason="playlist nested too deep")
                        is_playlist = True
                    elif _looks_text(head):
                        return ProbeResult(
                            False, ttfb=t_first - t0, **ni,
                            reason="not video (html/json response)")
                got += len(chunk)
                if is_playlist:
                    body += chunk
                    if len(body) > _PLAYLIST_MAX_BYTES:
                        return ProbeResult(False, ttfb=t_first - t0, **ni,
                                           reason="playlist too large")
                    continue
                if hops == 0 and len(sniff) < _SNIFF_BYTES:
                    sniff += chunk
                if got >= PROBE_BYTES:
                    break
                # sustained-throughput bail-out: if we're this far in and
                # already too slow, don't wait for the full read timeout
                if _too_slow(got, now - t_first, required):
                    return ProbeResult(
                        False, ttfb=t_first - t0, **ni,
                        speed_bps=got / (now - t_first),
                        reason="throughput far below need",
                    )
            if t_first is None:
                return ProbeResult(False, reason="empty body", **ni)
            elapsed = max(time.monotonic() - t_first, 0.05)
            speed = got / elapsed
            ttfb = t_first - t0
            if is_playlist:
                text = body.decode("utf-8", errors="replace")
                uri, selected = _hls_target(text)
                if not uri:
                    return ProbeResult(False, ttfb=ttfb, **ni,
                                       reason="empty playlist")
                media = dict(media or selected)
                if "#EXT-X-KEY" in text and "METHOD=NONE" not in text:
                    media["encrypted"] = True
                dur = _hls_duration(text)
                if dur:
                    # The playlist declares its full runtime — judge it before
                    # spending a segment fetch on a trailer-length clip.
                    media["media_secs"] = dur
                    if expect_secs:
                        reason = _duration_reason(dur, expect_secs)
                        if reason:
                            return ProbeResult(False, ttfb=ttfb, **ni,
                                               reason=reason, media_secs=dur)
                playlist_url = str(resp.url)   # recurse outside the stream ctx
            elif got < PROBE_BYTES:
                # A real feature/episode is vastly larger than this range.  A
                # clean EOF before the requested 4 MiB is evidence of a
                # truncated/missing-article response for a direct file — but a
                # complete HLS media segment when we descended a playlist.
                if hops == 0 or got == 0:
                    return ProbeResult(False, ttfb=ttfb, speed_bps=speed, **ni,
                                       reason=f"short body ({got} bytes)")
                if not _looks_media_payload(
                        ctype, head, encrypted=bool((media or {}).get("encrypted"))):
                    return ProbeResult(False, ttfb=ttfb, speed_bps=speed, **ni,
                                       reason="HLS segment is not media")
                if got >= _SEGMENT_JUDGE_BYTES and speed < required:
                    return ProbeResult(
                        False, ttfb=ttfb, speed_bps=speed, **ni,
                        reason=f"{speed / 1e6:.1f} MB/s < required "
                               f"{required / 1e6:.1f} MB/s",
                    )
                return ProbeResult(True, ttfb=ttfb, speed_bps=speed, **ni,
                                   content_kind="hls", **(media or {}))
            elif speed < required:
                return ProbeResult(
                    False, ttfb=ttfb, speed_bps=speed, **ni,
                    reason=f"{speed / 1e6:.1f} MB/s < required {required / 1e6:.1f} MB/s",
                )
            else:
                if not _looks_media_payload(
                        ctype, head, encrypted=bool((media or {}).get("encrypted"))):
                    return ProbeResult(False, ttfb=ttfb, speed_bps=speed, **ni,
                                       reason="not a recognized media container")
                return ProbeResult(True, ttfb=ttfb, speed_bps=speed, **ni,
                                   head=bytes(sniff) if hops == 0 else b"",
                                   content_kind="hls" if hops else "file",
                                   **(media or {}))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Exception text from HTTP clients can contain the full credentialed
        # request URL. The class is enough for retry/reputation classification.
        return ProbeResult(False, reason=type(e).__name__)
    next_required = required
    if media and media.get("media_bps"):
        # HLS BANDWIDTH is bits/s; probe throughput is measured in bytes/s.
        next_required = float(media["media_bps"]) / 8 * SAFETY_FACTOR
    return await _probe_url(urljoin(playlist_url, uri), next_required, ttfb_max,
                            t0, hops + 1, media=media, headers=headers,
                            expect_secs=expect_secs)


async def probe_batch(
    candidates: list[dict], need_bps_of, ttfb_max: float,
    want: int, batch_size: int = 3, deadline: float | None = None,
    expect_secs: float | None = None, deep_check_of=None,
) -> list[tuple[dict, ProbeResult]]:
    """Probe candidates in upstream-preference order, batch_size at a time,
    stopping once `want` have passed. Returns [(stream, result)] for passes,
    best (upstream) order preserved.

    `deadline` (a time.monotonic() value) bounds total work: no *new* batch is
    started once it's passed, so whatever verified so far is returned instead of
    marching through every candidate. The caller keeps a hard ceiling of its
    own — this just avoids wasting the tail of the budget."""
    passed: list[tuple[dict, ProbeResult]] = []
    for i in range(0, len(candidates), batch_size):
        if deadline is not None and time.monotonic() >= deadline:
            break
        batch = candidates[i:i + batch_size]
        results = await asyncio.gather(
            *(probe(s["url"], need_bps_of(s), ttfb_max,
                    headers=hlsproxy.request_headers(s),
                    expect_secs=expect_secs,
                    verify_size=(deep_check_of(s) if deep_check_of else None))
              for s in batch)
        )
        for s, r in zip(batch, results):
            _record(s, r, secrets.token_urlsafe(12))
            label = (s.get("behaviorHints", {}).get("filename")
                     or s.get("name", "?")).replace("\n", " ")[:70]
            if r.ok:
                logger.info(f"probe OK  [{r.ttfb:4.1f}s ttfb, {r.speed_bps/1e6:5.1f} MB/s] {label}")
                passed.append((s, r))
            else:
                logger.info(f"probe FAIL ({r.reason}) {label}")
        if len(passed) >= want:
            break
    return passed


async def probe_race(
    candidates: list[dict], need_bps_of, ttfb_max: float,
    want: int, concurrency: int = 8, deadline: float | None = None,
    expect_secs: float | None = None, deep_check_of=None, outcomes=None,
) -> list[tuple[dict, ProbeResult]]:
    """Like probe_batch, but keeps up to `concurrency` probes in flight at once
    and returns the instant `want` have passed — every still-pending probe is
    then cancelled. This is the key difference from probe_batch's fixed batches:
    one slow or hanging candidate (an uncached debrid link taking 30s to connect)
    never holds up the answer once enough fast ones have verified. Candidates are
    still *started* in preference order, so the best-quality links get first crack
    at the concurrency slots. Bounded by `deadline` (a time.monotonic() value):
    no new probe starts past it and the wait unblocks at it."""
    passed: list[tuple[dict, ProbeResult]] = []
    running: dict[asyncio.Task, tuple[dict, str]] = {}
    nxt = 0
    n = len(candidates)

    def _fill() -> None:
        nonlocal nxt
        while (len(running) < concurrency and nxt < n
               and (deadline is None or time.monotonic() < deadline)):
            s = candidates[nxt]
            nxt += 1
            running[asyncio.create_task(
                probe(s["url"], need_bps_of(s), ttfb_max,
                      headers=hlsproxy.request_headers(s),
                      expect_secs=expect_secs,
                      verify_size=(deep_check_of(s) if deep_check_of else None)))] = (
                    s, secrets.token_urlsafe(12))

    _fill()
    try:
        while running and len(passed) < want:
            timeout = (None if deadline is None
                       else max(deadline - time.monotonic(), 0.0))
            done, _ = await asyncio.wait(
                set(running), timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
            if not done:                       # deadline reached, nothing settled
                break
            for t in done:
                s, attempt_id = running.pop(t)
                try:
                    r = t.result()
                except Exception as e:         # defensive; probe() rarely raises
                    r = ProbeResult(False, reason=type(e).__name__)
                _record(s, r, attempt_id)
                if outcomes is not None:
                    outcomes.append((s, r))
                label = (s.get("behaviorHints", {}).get("filename")
                         or s.get("name", "?")).replace("\n", " ")[:70]
                if r.ok:
                    logger.info(f"probe OK  [{r.ttfb:4.1f}s ttfb, {r.speed_bps/1e6:5.1f} MB/s] {label}")
                    passed.append((s, r))
                else:
                    logger.info(f"probe FAIL ({r.reason}) {label}")
            _fill()
    finally:
        for t in running:                      # drop the stragglers
            t.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
    return passed


async def shutdown() -> None:
    await _client.aclose()
