"""Ad-free trailer playback for the taste page.

**Why this is not a YouTube embed.** The requirement is a trailer with no ads,
and an `<iframe>` cannot deliver one: there is no player parameter that turns
ads off, because ads are the uploader's monetisation setting rather than a
property of the video. Ad blocking is the usual answer and it is the wrong
shape here — it is a filter that has to keep up with a format that changes on
purpose.

So the player is skipped entirely. The extractor hands back a *progressive*
MP4 — one file, video and audio already muxed, which is what `itag=18` and
`itag=22` are — and that file goes straight into a `<video>` tag. Ads live in
the player, not in the file, so there is nothing to block: this is ad-free by
construction rather than by filtering, and there is no blocklist to maintain.

**Why it is proxied rather than redirected to.** The URL the extractor returns
carries the requesting machine's address in it (`&ip=...`) and Google enforces
it. Handing that URL to a phone produces a 403 the moment the phone's address
differs from the server's — which on a household LAN behind one NAT it usually
does not, and on mobile data always does. Proxying is the only form of this
that works everywhere, so it is the only form implemented.

Extraction is slow (seconds) and its result is short-lived (the URL carries an
`expire=`), which is why both halves are cached and why the cache is checked
against that deadline rather than against a fixed TTL of our own invention.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx

from app.recs import config

logger = logging.getLogger("nuvio-recs")

# A YouTube video id. Anything else is refused before it reaches the extractor,
# so this endpoint can never be pointed at an arbitrary host.
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Progressive formats only — a `<video>` tag needs one file with both streams
# in it, and the adaptive DASH formats YouTube prefers are video-only. 22 is
# 720p, 18 is 360p; the fallbacks keep a trailer playing rather than perfect.
FORMAT = "22/18/best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]"

# Seconds of headroom on the extracted URL's own expiry. Re-extracting costs a
# few seconds, and starting a stream that dies half way through is worse than
# paying that early.
EXPIRY_MARGIN = 300
# Fallback lifetime for a URL with no readable `expire=`.
DEFAULT_TTL = 1800

# key -> (url, good_until). Bounded below.
_streams: dict[str, tuple[str, float]] = {}
# (media, tmdb_id) -> key or None. A title without a trailer is worth
# remembering too; otherwise every tap on it pays the TMDB round trip again.
_keys: dict[tuple[str, int], str | None] = {}
_MAX_CACHED = 512
# One extraction per key at a time. Without it, a double tap starts two
# subprocess-heavy extractions of the same video.
_locks: dict[str, asyncio.Lock] = {}


def _expiry_of(url: str) -> float:
    """When Google says this URL stops working."""
    try:
        expire = parse_qs(urlparse(url).query).get("expire", [None])[0]
        if expire:
            return float(expire)
    except (TypeError, ValueError):
        pass
    return time.time() + DEFAULT_TTL


def _trim(cache: dict) -> None:
    while len(cache) > _MAX_CACHED:
        cache.pop(next(iter(cache)))


async def key_for(media_type: str, tmdb_id: int) -> str | None:
    """The YouTube id of this title's trailer, cached."""
    from app.recs import tmdb

    slot = ("movie" if media_type == "movie" else "tv", int(tmdb_id))
    if slot in _keys:
        return _keys[slot]
    key = await tmdb.trailer_key(slot[0], slot[1])
    if key and not KEY_RE.match(key):
        # TMDB has been known to carry a full URL in this field.
        logger.debug("trailer: ignoring malformed key %r", key)
        key = None
    _keys[slot] = key
    _trim(_keys)
    return key


def _extract(key: str) -> str | None:
    """Blocking. The direct media URL for one video, or None."""
    try:
        import yt_dlp
    except ImportError:
        logger.warning("trailer: yt-dlp is not installed")
        return None
    options = {
        "format": FORMAT,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": config.TRAILER_EXTRACT_TIMEOUT,
        # Age-gated trailers (horror, some 18-rated films) answer only to a
        # client that does not demand a login.
        "extractor_args": {"youtube": {"player_client": ["android_vr", "web"]}},
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={key}", download=False)
    except Exception as exc:
        logger.info("trailer: extraction failed for %s: %s", key, exc)
        return None
    url = (info or {}).get("url")
    if not url:
        # A format-selected result puts the chosen stream in requested_formats
        # when it could not find a muxed one.
        for fmt in ((info or {}).get("requested_formats") or []):
            if fmt.get("acodec") != "none" and fmt.get("vcodec") != "none":
                url = fmt.get("url")
                break
    return url


async def stream_url(key: str) -> str | None:
    """A playable URL for one video, extracting only when the cache is cold."""
    now = time.time()
    cached = _streams.get(key)
    if cached and cached[1] - EXPIRY_MARGIN > now:
        return cached[0]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another waiter may have done the work while this one queued.
        cached = _streams.get(key)
        if cached and cached[1] - EXPIRY_MARGIN > time.time():
            return cached[0]
        started = time.monotonic()
        url = await asyncio.to_thread(_extract, key)
        if not url:
            return None
        _streams[key] = (url, _expiry_of(url))
        _trim(_streams)
        logger.info("trailer: extracted %s in %.1fs", key,
                    time.monotonic() - started)
        return url


# Headers worth passing upstream, and worth passing back. Range is what makes
# the scrubber work; without it a viewer can only watch a trailer forwards.
_FORWARD = ("range",)
_RETURN = ("content-type", "content-length", "content-range", "accept-ranges")


async def open_stream(key: str, request_headers: dict):
    """(upstream response, client) for one trailer, or (None, None).

    The caller owns closing both. Kept as a context-free pair rather than a
    context manager because it feeds a StreamingResponse, which outlives the
    handler that created it.
    """
    url = await stream_url(key)
    if not url:
        return None, None
    headers = {k: v for k, v in request_headers.items()
               if k.lower() in _FORWARD}
    client = httpx.AsyncClient(timeout=config.TRAILER_STREAM_TIMEOUT,
                               follow_redirects=True)
    try:
        req = client.build_request("GET", url, headers=headers)
        upstream = await client.send(req, stream=True)
    except Exception as exc:
        await client.aclose()
        logger.info("trailer: upstream failed for %s: %s", key, exc)
        return None, None
    if upstream.status_code >= 400:
        # Nearly always an expired or address-locked URL. Drop it so the next
        # request extracts a fresh one instead of failing the same way.
        _streams.pop(key, None)
        await upstream.aclose()
        await client.aclose()
        logger.info("trailer: upstream %s for %s", upstream.status_code, key)
        return None, None
    return upstream, client


def response_headers(upstream) -> dict:
    return {k: v for k, v in upstream.headers.items() if k.lower() in _RETURN}
