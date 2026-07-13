"""Local library source via Jellio (a Jellyfin -> player addon).

A title already in the Jellyfin library is the best possible source: local, no
debrid, instant first byte and rock-solid playback. So both pickers query this
and surface library hits *first*, ahead of any online result.

Queried via Jellio's PUBLIC base URL (JELLIO_URL) on purpose: Jellio builds its
`/videos/...` playback URLs from the host it was reached on, so hitting the
public URL yields links the user's device can actually play. Jellio may also
return its own "Request via Jellyseerr" placeholder for titles it doesn't have;
we keep only real files (direct `/videos/` playback URLs) and let the slow
picker's Sonarr/Radarr path handle acquisition instead.
"""

import asyncio
import logging
import os
import re
import time
import urllib.parse

import httpx

logger = logging.getLogger("stream-picker")

JELLIO_URL = (os.environ.get("JELLIO_URL") or "").rstrip("/") or None
LIB_TTL = float(os.environ.get("JELLIO_CACHE_TTL", "300"))
NEG_TTL = float(os.environ.get("JELLIO_NEG_TTL", "60"))
TIMEOUT = float(os.environ.get("JELLIO_TIMEOUT", "8"))

# The Jellio plugin bakes transcode params (AudioCodec=aac,
# TranscodingMaxAudioChannels=2, CopyTimestamps) into its /videos/ URLs. A
# transcoded Jellyfin stream is delivered progressively: no Content-Length, so
# the player shows no total duration and can't seek (track bar pinned to the
# end), and it drops the file's embedded subtitle tracks. Forcing static=true
# and stripping those params serves the original file directly — real duration,
# seeking, and embedded subs all come back. Trade-off: the client must decode
# the file's original audio itself (no server-side downmix to AAC stereo), so
# make it a toggle in case a household device can't. Default on.
DIRECT_PLAY = (os.environ.get("JELLIO_DIRECT_PLAY", "true").lower()
               in ("1", "true", "yes", "on"))
# Jellyfin delivery/transcode query params to drop when forcing direct play.
_TRANSCODE_PARAMS = {
    "audiocodec", "videocodec", "subtitlecodec", "subtitlemethod",
    "subtitlestreamindex", "transcodingmaxaudiochannels", "transcodingcontainer",
    "transcodingprotocol", "copytimestamps", "maxstreamingbitrate",
    "audiobitrate", "videobitrate", "maxaudiochannels", "audiochannels",
    "requireavc", "breakonnonkeyframes", "enableautostreamcopy",
    "allowaudiostreamcopy", "allowvideostreamcopy", "segmentcontainer",
    "minsegments", "maxwidth", "maxheight", "videobitdepth", "level", "profile",
    "deinterlace", "static",
}


def _direct_play(url: str) -> str:
    """Rewrite a Jellio transcode URL to a Jellyfin direct-play (static) URL so
    it carries a real duration and its embedded subtitles."""
    try:
        p = urllib.parse.urlparse(url)
        if "/stream" not in p.path.lower():
            return url
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
                if k.lower() not in _TRANSCODE_PARAMS]
        kept.append(("static", "true"))
        return urllib.parse.urlunparse(
            p._replace(query=urllib.parse.urlencode(kept)))
    except Exception:
        return url

_client = httpx.AsyncClient(timeout=None, follow_redirects=True,
                            headers={"User-Agent": "Stremio"})
_cache: dict[str, tuple[float, list[dict]]] = {}

_RES = re.compile(r"(2160p|4k|uhd|1080p|720p|480p)", re.I)


def enabled() -> bool:
    return JELLIO_URL is not None


# Whether to ask Jellyfin for each library file's exact video bitrate + size.
# The item id and api_key are already in the Jellio `/videos/{id}/stream` URL,
# so no extra config is needed. This is the *true video* bitrate (audio tracks
# excluded), which the picker ranks on — so a fat, honest library 1080p beats a
# starved fake-4K, and library isn't unfairly demoted for its extra audio dubs.
ENRICH = (os.environ.get("JELLIO_ENRICH", "true").lower()
          in ("1", "true", "yes", "on"))
_ITEM_RE = re.compile(r"/videos/([^/]+)/", re.I)


async def _enrich(s: dict) -> None:
    """Populate `_vbitrate` (true video bitrate, bps) and behaviorHints.videoSize
    for one library stream from Jellyfin. Best-effort; silent on any failure."""
    url = s.get("url") or ""
    try:
        p = urllib.parse.urlparse(url)
        m = _ITEM_RE.search(p.path)
        key = dict(urllib.parse.parse_qsl(p.query)).get("api_key")
        if not m or not key:
            return
        r = await _client.post(
            f"{p.scheme}://{p.netloc}/Items/{m.group(1)}/PlaybackInfo",
            params={"api_key": key}, json={}, timeout=TIMEOUT)
        r.raise_for_status()
        srcs = r.json().get("MediaSources") or []
        if not srcs:
            return
        ms = srcs[0]
        video = next((x for x in ms.get("MediaStreams", [])
                      if x.get("Type") == "Video"), None)
        if video and video.get("BitRate"):
            s["_vbitrate"] = int(video["BitRate"])
        if ms.get("Size"):
            s.setdefault("behaviorHints", {})["videoSize"] = int(ms["Size"])
    except Exception as e:
        logger.debug(f"jellio enrich failed: {type(e).__name__}")


def _label(s: dict) -> dict:
    text = " ".join(filter(None, (s.get("title"), s.get("name"),
                                  (s.get("behaviorHints") or {}).get("filename"),
                                  s.get("description"))))
    m = _RES.search(text)
    res = f" {m.group(1).upper()}" if m else ""
    out = dict(s)
    out["name"] = f"📚 Library{res}"
    if DIRECT_PLAY and out.get("url"):
        out["url"] = _direct_play(out["url"])
    return out


async def streams(media: str, media_id: str) -> list[dict]:
    """Real library streams for this title (empty if not in the library)."""
    if not JELLIO_URL:
        return []
    key = f"{media}:{media_id}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < (LIB_TTL if hit[1] else NEG_TTL):
        return hit[1]
    try:
        r = await _client.get(f"{JELLIO_URL}/stream/{media}/{media_id}.json",
                              timeout=TIMEOUT)
        r.raise_for_status()
        raw = r.json().get("streams") or []
    except Exception as e:
        logger.warning(f"jellio {key}: {type(e).__name__}")
        return hit[1] if hit else []          # serve stale on a transient error
    out = [_label(s) for s in raw if "/videos/" in (s.get("url") or "")]
    if out and ENRICH:
        await asyncio.gather(*(_enrich(s) for s in out), return_exceptions=True)
    _cache[key] = (time.monotonic(), out)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    if out:
        logger.info(f"jellio {key}: {len(out)} library stream(s)")
    return out
