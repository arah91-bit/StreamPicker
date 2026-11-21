"""Event-loop lag watchdog.

One event loop serves every stream this process proxies. Anything that runs on
it without yielding — a big JSON parse, a synchronous file read, a CPU-heavy
ranking pass — stops *all* of them for its duration: no producer fills, no
consumer sends, every start gate's clock keeps running. That is exactly what
telemetry.load() did on every dashboard view (2.5-3.8 s against the live
store, found by measurement during the 2026-10 review, not by any alarm),
and its stalls were logged as `starve` events that blamed the *source*.

So measure it. A task sleeps for INTERVAL and records how late it woke; a
wake-up later than THRESHOLD is a stall. Each stall is logged, written to the
telemetry ledger (kind ``loop_lag``) with the requests that began just before
it — the likeliest culprits — and kept in memory so Home can say so.
"""

import asyncio
import logging
import os
import time
from collections import deque

from app import telemetry

logger = logging.getLogger("stream-picker")

INTERVAL = 0.5
THRESHOLD = float(os.environ.get("LOOP_LAG_WARN_SECS", "0.25"))
# Home only raises its voice for a stall a viewer would have felt.
SERIOUS = 1.0
_RECENT_WINDOW = 600.0

_requests: deque = deque(maxlen=64)       # (monotonic, "GET /path")
_stalls: deque = deque(maxlen=50)         # (wall time, lag secs, [requests])
_task: asyncio.Task | None = None


def note_request(method: str, path: str) -> None:
    """Called by the access middleware as each request begins."""
    _requests.append((time.monotonic(), f"{method} {path}"[:120]))


def _suspects(since: float) -> list[str]:
    return list(dict.fromkeys(p for ts, p in _requests if ts >= since))[-5:]


def _observe(lag: float, slept_from: float) -> None:
    if lag < THRESHOLD:
        return
    # The culprit was already running when we should have woken, so it began
    # at or before then; look back a little further than the sleep itself.
    suspects = _suspects(slept_from - 1.0)
    _stalls.append((time.time(), lag, suspects))
    logger.warning("event loop stalled %.0f ms — every stream paused%s",
                   lag * 1000,
                   f" (requests just before: {', '.join(suspects)})"
                   if suspects else "")
    telemetry.record_loop_lag(lag, suspects)


async def run() -> None:
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(INTERVAL)
        _observe(loop.time() - started - INTERVAL, time.monotonic() - INTERVAL)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(run())


async def shutdown() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


def worst_recent(window: float = _RECENT_WINDOW) -> dict | None:
    """The longest stall in the last `window` seconds, or None."""
    cutoff = time.time() - window
    recent = [s for s in _stalls if s[0] >= cutoff]
    if not recent:
        return None
    ts, lag, suspects = max(recent, key=lambda s: s[1])
    return {"ts": ts, "lag": lag, "suspects": suspects, "count": len(recent)}
