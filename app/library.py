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
import hashlib
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
_item_cache: dict[tuple[str, str, str], tuple[float, dict | None]] = {}
_item_inflight: dict[tuple[str, str, str], asyncio.Task] = {}
ITEM_TTL = 1800.0
_IDENTITY_TRUST_KEY = "_library_identity_trust"
_IDENTITY_TRUST_SENTINEL = object()

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


def _jellyfin_ref(url: str) -> tuple[str, str, str] | None:
    """Return (origin, item id, api key) from a Jellio playback URL.

    The API key is used only for the local Jellyfin metadata request.  It is
    never logged or stored in the cache value; the cache key contains only a
    short one-way digest so two Jellyfin accounts cannot share metadata by
    accident.
    """
    try:
        p = urllib.parse.urlparse(url)
        m = _ITEM_RE.search(p.path)
        key = dict(urllib.parse.parse_qsl(p.query)).get("api_key")
        if not m or not key or p.scheme not in ("http", "https") or not p.netloc:
            return None
        return f"{p.scheme}://{p.netloc}", m.group(1), key
    except Exception:
        return None


async def _item(origin: str, item_id: str, api_key: str) -> dict | None:
    """Small shared Jellyfin item lookup used to bind Jellio URLs to IMDb.

    Jellio already handed us the item id and credential in the playback URL, so
    this costs no new configuration and stays on the local Jellyfin service.
    Concurrent versions of one item coalesce into a single request.
    """
    key = (origin.lower(), item_id,
           hashlib.sha256(api_key.encode()).hexdigest()[:16])
    cached = _item_cache.get(key)
    if cached and time.monotonic() - cached[0] < ITEM_TTL:
        return cached[1]
    task = _item_inflight.get(key)
    if task is None:
        async def fetch():
            value = None
            try:
                r = await _client.get(f"{origin}/Items/{item_id}",
                                      params={"api_key": api_key}, timeout=TIMEOUT)
                r.raise_for_status()
                raw = r.json()
                if isinstance(raw, dict):
                    # Retain only identity fields.  In particular, never cache
                    # paths, URLs, user data, or the Jellyfin credential.
                    value = {
                        "Id": str(raw.get("Id") or item_id),
                        "Type": str(raw.get("Type") or ""),
                        "ProviderIds": dict(raw.get("ProviderIds") or {}),
                        "ProductionYear": raw.get("ProductionYear"),
                        "SeriesId": str(raw.get("SeriesId") or ""),
                        "ParentIndexNumber": raw.get("ParentIndexNumber"),
                        "IndexNumber": raw.get("IndexNumber"),
                    }
            except Exception as e:
                logger.debug("jellio item metadata failed: %s", type(e).__name__)
            _item_cache[key] = (time.monotonic(), value)
            if len(_item_cache) > 2000:
                _item_cache.pop(next(iter(_item_cache)))
            return value

        task = asyncio.create_task(fetch())
        _item_inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _item_inflight.pop(k, None))
    return await asyncio.shield(task)


def _provider_imdb(item: dict | None) -> str:
    providers = (item or {}).get("ProviderIds") or {}
    for key, value in providers.items():
        if str(key).lower() == "imdb":
            return str(value or "").lower()
    return ""


async def _identity_of(s: dict, media: str, media_id: str) -> bool | None:
    """True/False for an exact Jellyfin identity match, None when unavailable.

    Movies must carry the requested IMDb provider id.  Episodes must match both
    requested coordinates and the parent series' IMDb provider id.  A definite
    mismatch is discarded; an unavailable metadata endpoint remains a visible
    fallback but is not marked eligible for automatic #1 by the picker.
    """
    ref = _jellyfin_ref(s.get("url") or "")
    if not ref:
        return None
    origin, item_id, api_key = ref
    item = await _item(origin, item_id, api_key)
    if item is None:
        return None
    base = media_id.split(":", 1)[0].lower()
    typ = str(item.get("Type") or "").lower()
    if media == "movie":
        return typ == "movie" and _provider_imdb(item) == base
    parts = media_id.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return False
    try:
        coords = (int(item.get("ParentIndexNumber")), int(item.get("IndexNumber")))
    except (TypeError, ValueError):
        return False
    if typ != "episode" or coords != (int(parts[1]), int(parts[2])):
        return False
    series_id = str(item.get("SeriesId") or "")
    if not series_id:
        return False
    series = await _item(origin, series_id, api_key)
    return bool(series and str(series.get("Type") or "").lower() == "series"
                and _provider_imdb(series) == base)


async def _enrich(s: dict) -> None:
    """Populate `_vbitrate` (true video bitrate, bps) and behaviorHints.videoSize
    for one library stream from Jellyfin. Best-effort; silent on any failure."""
    url = s.get("url") or ""
    try:
        ref = _jellyfin_ref(url)
        if not ref:
            return
        origin, item_id, key = ref
        p = urllib.parse.urlparse(url)
        r = await _client.post(
            f"{origin}/Items/{item_id}/PlaybackInfo",
            params={"api_key": key}, json={}, timeout=TIMEOUT)
        r.raise_for_status()
        srcs = r.json().get("MediaSources") or []
        if not srcs:
            return
        query = {k.lower(): v for k, v in urllib.parse.parse_qsl(p.query)}
        source_id = query.get("mediasourceid")
        ms = next((x for x in srcs if source_id and
                   str(x.get("Id") or x.get("id")) == source_id), None)
        if ms is None and len(srcs) == 1:
            ms = srcs[0]
        if ms is None:
            # Guessing MediaSources[0] annotates every Jellyfin version with
            # another version's size/bitrate and can incorrectly make it #1.
            return
        video = next((x for x in ms.get("MediaStreams", [])
                      if x.get("Type") == "Video"), None)
        if video and video.get("BitRate"):
            s["_vbitrate"] = int(video["BitRate"])
        if ms.get("Size"):
            s.setdefault("behaviorHints", {})["videoSize"] = int(ms["Size"])
    except Exception as e:
        logger.debug(f"jellio enrich failed: {type(e).__name__}")


async def _prepare(s: dict, media: str, media_id: str) -> dict | None:
    """Validate identity and enrich delivery metadata for one library URL."""
    identity_task = asyncio.create_task(_identity_of(s, media, media_id))
    enrich_task = asyncio.create_task(_enrich(s)) if ENRICH else None
    try:
        identity = await identity_task
    except Exception:
        identity = None
    if enrich_task is not None:
        await asyncio.gather(enrich_task, return_exceptions=True)
    if identity is False:
        logger.warning("jellio %s: discarded item whose Jellyfin identity mismatched",
                       media_id)
        return None
    if identity is True:
        # This private evidence is trusted only on the picker path explicitly
        # marked as library provenance; it is stripped at the HTTP boundary.
        s["_library_identity_confidence"] = "strong"
        s["_library_identity_evidence"] = "jellyfin-imdb"
        s[_IDENTITY_TRUST_KEY] = _IDENTITY_TRUST_SENTINEL
    else:
        s["_library_identity_confidence"] = "unknown"
        s["_library_identity_evidence"] = "metadata-unavailable"
    # In-process provenance. app.sources overwrites this field for every HTTP
    # addon, so the picker can distinguish real Jellyfin ProviderId validation
    # from an upstream object that merely spells our private field names.
    s["_source_key"] = "library"
    return s


def identity_trusted(stream: dict) -> bool:
    """True only after this process validated Jellyfin ProviderIds/episode."""
    return stream.get(_IDENTITY_TRUST_KEY) is _IDENTITY_TRUST_SENTINEL


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
    labelled = [_label(s) for s in raw if "/videos/" in (s.get("url") or "")]
    prepared = (await asyncio.gather(
        *(_prepare(s, media, media_id) for s in labelled),
        return_exceptions=True)) if labelled else []
    out = [s for s in prepared if isinstance(s, dict)]
    _cache[key] = (time.monotonic(), out)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    if out:
        logger.info(f"jellio {key}: {len(out)} library stream(s)")
    return out


async def shutdown() -> None:
    for task in list(_item_inflight.values()):
        task.cancel()
    if _item_inflight:
        await asyncio.gather(*_item_inflight.values(), return_exceptions=True)
    _item_inflight.clear()
    await _client.aclose()
