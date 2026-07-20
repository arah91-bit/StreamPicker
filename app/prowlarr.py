"""Native Prowlarr source — search a Prowlarr instance and resolve through debrid.

This is the in-process "Prowlarr as its own source" lane, a sibling to the
direct-usenet lane in :mod:`app.usenet`. It exists so an operator can point the
addon at a Prowlarr they already run (added in the dashboard's Sources panel)
and get first-class, debrid-resolved streams from it — no external addon, no
back-end wiring. When Prowlarr, a debrid key, or the StremThru store gateway is
absent, :func:`enabled` is False and the lane simply never registers, so nothing
here can break a working install.

Pipeline for one title:
  1. Search Prowlarr's aggregate API (``/api/v1/search``). Prowlarr live-scrapes
     its public indexers, so this is slow (tens of seconds) — the lane rides the
     slow/quality build and later fast requests, exactly like MediaFusion.
  2. Keep only torrents whose title actually matches the requested one (the same
     load-bearing title/episode gate the usenet lane uses — indexers routinely
     return a different show for an id/title query, and a wrong-title release
     would resolve, probe OK, and play the wrong content).
  3. Cache-check the candidate hashes against the operator's debrid via the
     StremThru store API — a free, batched availability check. We resolve *only*
     cached hashes, never adding an uncached torrent (which would start a real
     debrid download and burn an account slot).
  4. For the best cached releases, ask the store for a direct playable link and
     emit a normal Stremio stream. The picker then probes/ranks these like any
     other source, so only verified links reach the player.

Resolution is delegated to StremThru's store gateway (the same StremThru the
addon already depends on): it turns a magnet hash + the operator's debrid token
into a cached-check and a direct link across every supported debrid, so this
module carries no per-provider debrid code of its own. The store name StremThru
expects is the provider id itself (torbox, realdebrid, …).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import quote

import httpx

from app import debrid, usenet

logger = logging.getLogger("stream-picker")

# ── configuration (env, baked at import like every other knob) ───────────────
_URL = (os.environ.get("PROWLARR_URL") or "").rstrip("/")
_API_KEY = os.environ.get("PROWLARR_API_KEY") or ""
# The Prowlarr lane is off unless the operator explicitly switches it on in the
# Sources panel (which sets this) — configuring Prowlarr for MediaFusion alone
# must not silently turn the addon into a Prowlarr client too.
_SOURCE_ON = (os.environ.get("PROWLARR_SOURCE") or "0").lower() in (
    "1", "true", "yes", "on")
# StremThru store gateway that resolves magnet hashes to links. Defaults to the
# origin of the configured StremThru Torz URL (same StremThru host), so it is
# usually zero-config; override for a separate store instance.
_STORE_URL = (os.environ.get("PROWLARR_STORE_URL") or "").rstrip("/")
SEARCH_TIMEOUT = float(os.environ.get("PROWLARR_SEARCH_TIMEOUT", "60"))
RESOLVE_MAX = max(1, int(os.environ.get("PROWLARR_RESOLVE_MAX", "12")))
RESOLVE_CONCURRENCY = max(1, int(os.environ.get("PROWLARR_RESOLVE_CONCURRENCY", "4")))
MIN_SEEDERS = max(0, int(os.environ.get("PROWLARR_MIN_SEEDERS", "1")))

_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm")

_client = httpx.AsyncClient(timeout=20, follow_redirects=True,
                            headers={"User-Agent": "stream-picker/1.0"})

# (media, media_id) -> {"state", "detail", "finished_at"} for diagnostics.
_outcomes: dict[tuple, dict] = {}


def _store_base() -> str:
    """The StremThru store gateway base, explicit or derived from the Torz URL."""
    if _STORE_URL:
        return _STORE_URL
    base, _ = debrid.parse_stremthru(os.environ.get("STREMTHRU_BASE_URL") or "")
    return base.rstrip("/")


def _stores() -> list[tuple[str, str]]:
    """Every debrid the operator has configured, as (store_name, token) — read
    from the same Comet/StremThru URLs the debrid manager edits, deduped by
    provider. StremThru's store name is the provider id itself.

    No provider is treated as primary: one debrid is enough, and each extra one
    is just another cache to check, so a hash cached on any of them resolves.
    Whatever you've added — TorBox only, Real-Debrid only, or several — is what
    gets used; nothing here assumes a specific service."""
    services = debrid.current(os.environ.get("FAST_BASE_URL") or "")
    seen = {s["service"] for s in services}
    for d in debrid.stremthru_current(os.environ.get("STREMTHRU_BASE_URL") or ""):
        if d["service"] not in seen:
            services.append(d)
            seen.add(d["service"])
    out, kept = [], set()
    for d in services:
        if d.get("key") and d["service"] not in kept:
            out.append((d["service"], d["key"]))
            kept.add(d["service"])
    return out


def enabled() -> bool:
    """The lane runs only when it is switched on and can actually resolve:
    Prowlarr credentials, a store gateway, and at least one debrid must all be
    present. Any missing piece disables it silently (no-op), never an error."""
    return bool(_SOURCE_ON and _URL and _API_KEY
                and _store_base() and _stores())


def outcome(media: str, media_id: str) -> dict:
    return dict(_outcomes.get((media, media_id)) or {
        "state": "unknown", "detail": "", "finished_at": 0.0})


def _record(media: str, media_id: str, state: str, detail: str = "") -> None:
    _outcomes[(media, media_id)] = {"state": state, "detail": detail[:160],
                                    "finished_at": time.monotonic()}
    if len(_outcomes) > 512:
        _outcomes.pop(next(iter(_outcomes)), None)


# ── Prowlarr search ──────────────────────────────────────────────────────────

def _query(media: str, media_id: str, titles: list[str],
           year: int | None) -> str:
    """A single search string: the primary title, narrowed by the release year
    (movies) or the episode token (series), so Prowlarr's live scrape returns a
    focused set before we title/episode-filter it. A bare title is too broad —
    it makes indexers slow and noisy."""
    title = titles[0] if titles else ""
    parts = media_id.split(":")
    if media != "movie" and len(parts) >= 3:
        try:
            return f"{title} S{int(parts[1]):02d}E{int(parts[2]):02d}"
        except ValueError:
            pass
    return f"{title} {year}" if year else title


async def _search(query: str) -> list[dict]:
    r = await _client.get(f"{_URL}/api/v1/search",
                          headers={"X-Api-Key": _API_KEY},
                          params={"query": query, "type": "search", "limit": 100},
                          timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _candidates(results: list[dict], titles: list[str],
                season_episode: tuple[int, int] | None) -> list[dict]:
    """Torrent releases with a hash whose title matches the requested one (and
    episode, for a series), best-first by seeders then size. The title gate is
    the same one the usenet lane relies on to never play wrong content."""
    out: list[dict] = []
    for it in results:
        if str(it.get("protocol") or "").lower() != "torrent":
            continue
        info_hash = str(it.get("infoHash") or "").strip().lower()
        title = str(it.get("title") or "").strip()
        if not info_hash or not title:
            continue
        if (it.get("seeders") or 0) < MIN_SEEDERS:
            continue
        if not any(usenet._release_title_match(title, t) for t in titles if t):
            continue
        if season_episode and not usenet._episode_match(title, *season_episode):
            continue
        out.append({"hash": info_hash, "title": title,
                    "size": int(it.get("size") or 0),
                    "seeders": int(it.get("seeders") or 0),
                    "indexer": str(it.get("indexer") or "")})
    # Collapse duplicate hashes returned by several indexers, keeping the copy
    # with the most seeders.
    best: dict[str, dict] = {}
    for c in out:
        cur = best.get(c["hash"])
        if cur is None or c["seeders"] > cur["seeders"]:
            best[c["hash"]] = c
    ranked = sorted(best.values(),
                    key=lambda c: (c["seeders"], c["size"]), reverse=True)
    return ranked


# ── StremThru store resolution ───────────────────────────────────────────────

def _store_headers(store: str, token: str) -> dict:
    return {"X-StremThru-Store-Name": store,
            "X-StremThru-Store-Authorization": f"Bearer {token}"}


async def _cached_hashes(hashes: list[str], store: str, token: str) -> set[str]:
    """Batch availability check — the subset of hashes the debrid has cached."""
    if not hashes:
        return set()
    base = _store_base()
    r = await _client.get(f"{base}/v0/store/magnets/check",
                          headers=_store_headers(store, token),
                          params={"magnet": ",".join(hashes)})
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("items") or []
    return {str(it.get("hash") or "").lower()
            for it in items if it.get("status") == "cached"}


def _pick_file(files: list[dict],
               season_episode: tuple[int, int] | None) -> dict | None:
    """The file to stream: the episode's own file for a series, else the largest
    video. Falls back to the largest file of any kind when nothing looks video."""
    videos = [f for f in files
              if str(f.get("name") or f.get("path") or "").lower()
              .endswith(_VIDEO_EXT)]
    pool = videos or files
    if not pool:
        return None
    if season_episode:
        for f in pool:
            name = str(f.get("name") or f.get("path") or "")
            if usenet._episode_match(name, *season_episode):
                return f
        if videos:                 # a series pack with no clear episode file
            return None
    return max(pool, key=lambda f: f.get("size") or 0)


async def _resolve(cand: dict, store: str, token: str,
                   season_episode: tuple[int, int] | None) -> dict | None:
    """Add a cached magnet, choose the right file, and generate a direct link.
    Returns a Stremio stream dict, or None if it cannot be turned into one."""
    base = _store_base()
    headers = _store_headers(store, token)
    try:
        r = await _client.post(f"{base}/v0/store/magnets", headers=headers,
                               json={"magnet": cand["hash"]}, timeout=30)
        r.raise_for_status()
        magnet = r.json().get("data") or {}
        pick = _pick_file(magnet.get("files") or [], season_episode)
        if not pick or not pick.get("link"):
            return None
        g = await _client.post(f"{base}/v0/store/link/generate", headers=headers,
                               json={"link": pick["link"]}, timeout=30)
        g.raise_for_status()
        url = (g.json().get("data") or {}).get("link") or ""
        if not url:
            return None
    except Exception as e:
        logger.info(f"prowlarr resolve failed for {cand['title'][:50]}: "
                    f"{type(e).__name__}")
        return None
    size = int(pick.get("size") or cand["size"] or 0)
    fname = str(pick.get("name") or "").rsplit("/", 1)[-1]
    gb = f"{size / 1e9:.2f} GB" if size else "?"
    hints: dict = {}
    if fname:
        # The filename is what makes copies of this file across debrids share a
        # release signature — the basis for twin-splice/failover in the proxy.
        hints["filename"] = fname
    if size:
        hints["videoSize"] = size
    # Tag the debrid service ([TB+]/[RD+], '+' = cached — Prowlarr only resolves
    # cached), so telemetry.debrid_tag distinguishes this copy from the same
    # file on another debrid and the twin detector can pair them.
    badge = debrid.BY_ID.get(store, {}).get("badge") or store[:2].upper()
    return {
        "name": f"Prowlarr [{badge}+]\n{cand['title'][:60]}",
        "description": (f"Source: {cand['indexer'] or 'Prowlarr'} · {store}\n"
                        f"Size: {gb}\n{cand['title']}"),
        "url": url,
        "behaviorHints": hints,
    }


async def streams(media: str, media_id: str) -> list[dict]:
    """Search Prowlarr, keep title-matched cached torrents, and resolve the best
    ones to playable links. Returns [] (never raises) so the lane can only add
    coverage, never break a pick."""
    if not enabled():
        return []
    stores = _stores()
    if not stores:
        _record(media, media_id, "failed", "no debrid key")
        return []
    parts = media_id.split(":")
    season_episode = None
    if media != "movie" and len(parts) >= 3:
        try:
            season_episode = (int(parts[1]), int(parts[2]))
        except ValueError:
            season_episode = None
    try:
        titles, year = await usenet._expected_info(media, media_id)
        if not titles:
            _record(media, media_id, "failed", "no title metadata")
            return []
        results = await _search(_query(media, media_id, titles, year))
    except Exception as e:
        logger.warning(f"prowlarr search failed: {type(e).__name__}")
        _record(media, media_id, "failed", type(e).__name__)
        return []

    cands = _candidates(results, titles, season_episode)
    if not cands:
        _record(media, media_id, "empty", "no matching torrents")
        return []

    # Cache-check every configured debrid in parallel, then record *all* the
    # stores that have each hash — redundancy we deliberately keep.
    hashes = [c["hash"] for c in cands]
    checks = await asyncio.gather(
        *(_cached_hashes(hashes, name, token) for name, token in stores),
        return_exceptions=True)
    if all(isinstance(r, BaseException) for r in checks):
        why = type(next(r for r in checks if isinstance(r, BaseException))).__name__
        logger.warning(f"prowlarr cache-check failed: {why}")
        _record(media, media_id, "failed", f"cache-check {why}")
        return []
    stores_by_hash: dict[str, list[tuple[str, str]]] = {}
    for (name, token), res in zip(stores, checks):
        if isinstance(res, set):
            for h in res:
                stores_by_hash.setdefault(h, []).append((name, token))
    wanted = [c for c in cands if c["hash"] in stores_by_hash][:RESOLVE_MAX]
    if not wanted:
        names = ", ".join(n for n, _ in stores)
        _record(media, media_id, "empty",
                f"{len(cands)} matched, none cached on {names}")
        return []

    # Resolve each release on EVERY debrid that has it cached. Same file, same
    # filename → same release signature, but each copy carries its own debrid
    # tag ([TB+]/[RD+]), so the picker ranks the fastest source first and the
    # proxy has byte-identical twins to splice/fail over to when one stalls.
    sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)

    async def one(c: dict, store: str, token: str) -> dict | None:
        async with sem:
            return await _resolve(c, store, token, season_episode)

    tasks = [one(c, store, token) for c in wanted
             for store, token in stores_by_hash[c["hash"]]]
    settled = await asyncio.gather(*tasks, return_exceptions=True)
    out = [s for s in settled if isinstance(s, dict)]
    _record(media, media_id, "ok" if out else "empty",
            f"{len(out)} link(s) for {len(wanted)} release(s) "
            f"across {len(stores)} debrid(s)")
    logger.info(f"prowlarr {media}/{media_id}: {len(cands)} matched, "
                f"{len(stores_by_hash)} cached, {len(out)} links "
                f"({len(wanted)} releases × up to {len(stores)} debrid(s))")
    return out


async def shutdown() -> None:
    await _client.aclose()
