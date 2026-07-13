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
import secrets
import time
from dataclasses import dataclass

import httpx

from app import telemetry, usenet_health

logger = logging.getLogger("stream-picker")

PROBE_BYTES = 4 * 1024 * 1024  # how much of the file to actually pull
SAFETY_FACTOR = 1.5            # required headroom over the file's bitrate
MIN_SPEED_BPS = 2_500_000      # floor when the file size is unknown

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


def _record(stream: dict, result: ProbeResult, attempt_id: str) -> None:
    telemetry.record(stream, result)
    usenet_health.record_probe(stream, result, attempt_id)


async def probe(url: str, need_bps: float | None, ttfb_max: float) -> ProbeResult:
    """need_bps is the file's real bitrate (size/runtime) or None if unknown."""
    required = _required_bps(need_bps)
    t0 = time.monotonic()
    # The read timeout must outlast ttfb_max, or a legitimately slow starter
    # (nzbdav assembling its first segments can take 20-35s) dies as ReadTimeout
    # before the TTFB allowance it was promised ever comes into play.
    timeout = httpx.Timeout(connect=10, read=max(20.0, ttfb_max + 5),
                            write=10, pool=10)
    try:
        async with _client.stream(
            "GET", url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"},
            timeout=timeout,
        ) as resp:
            if resp.status_code not in (200, 206):
                return ProbeResult(False, reason=f"HTTP {resp.status_code}")
            ni = telemetry.netinfo(resp)
            got = 0
            t_first = None
            async for chunk in resp.aiter_bytes():
                now = time.monotonic()
                if t_first is None:
                    t_first = now
                    if t_first - t0 > ttfb_max:
                        return ProbeResult(
                            False, ttfb=t_first - t0, **ni,
                            reason=f"first byte took {t_first - t0:.1f}s",
                        )
                got += len(chunk)
                if got >= PROBE_BYTES:
                    break
                # sustained-throughput bail-out: if we're this far in and
                # already too slow, don't wait for the full read timeout
                if now - t_first > 8 and got / (now - t_first) < required / 2:
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
            # A real feature/episode is vastly larger than this range.  A clean
            # EOF before the requested 4 MiB is therefore evidence of a
            # truncated/missing-article response, not a successful probe.
            if got < PROBE_BYTES:
                return ProbeResult(False, ttfb=ttfb, speed_bps=speed, **ni,
                                   reason=f"short body ({got} bytes)")
            if speed < required:
                return ProbeResult(
                    False, ttfb=ttfb, speed_bps=speed, **ni,
                    reason=f"{speed / 1e6:.1f} MB/s < required {required / 1e6:.1f} MB/s",
                )
            return ProbeResult(True, ttfb=ttfb, speed_bps=speed, **ni)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Exception text from HTTP clients can contain the full credentialed
        # request URL. The class is enough for retry/reputation classification.
        return ProbeResult(False, reason=type(e).__name__)


async def probe_batch(
    candidates: list[dict], need_bps_of, ttfb_max: float,
    want: int, batch_size: int = 3, deadline: float | None = None,
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
            *(probe(s["url"], need_bps_of(s), ttfb_max) for s in batch)
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
                probe(s["url"], need_bps_of(s), ttfb_max))] = (
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
