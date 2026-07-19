"""Auto-cache to TorBox: make the *next* visit play what this one couldn't.

When a full pick ends with nothing verified, or with a best verified stream
whose resolution tier is below what an uncached TorBox torrent offers, tell
TorBox to start downloading the best such release. It won't help the current
request — TorBox needs minutes — but the title is cached on a later visit,
where the normal cached-only search sees it as [TB⚡], probes it, and serves
it verified.

Mechanics — no TorBox API key needed: the user's own Comet deploy already
knows every torrent and holds the TorBox credentials. We mint a
cachedOnly:false variant of the exact FAST_BASE_URL config (standard base64
with padding — Comet's strict config check rejects urlsafe/unpadded), query
it on demand, filter to uncached-TorBox entries ([TB⬇️], never [RD⬇️]), and
GET the best one's playback URL once: Comet adds the magnet to TorBox
*before* it can answer "not cached yet", and TorBox keeps downloading
server-side after we hang up.

On by default: TorBox's cache is global, so every release one user caches
becomes instantly available to every TorBox user — running this lane helps
everyone, not just this household. TB_AUTO_CACHE=0 turns it off (also in
the dashboard's Advanced tuning).

Restraint, because downloads occupy the plan's TORBOX_MAX_DOWNLOADS slots:
auto-cache assumes at most TB_CACHE_MAX_SLOTS of them (default 2 of the
default 3 — one slot always stays free for the user's own activity), each
triggered download assumed busy for TB_CACHE_SLOT_SECS; per-title cooldown
(TB_CACHE_TITLE_HOURS) remembers which releases were already tried (a retry
picks the next-best copy); and the trigger request itself holds the probe
layer's ingestion gate, so probes and triggers together never exceed the
plan limit. State survives restarts in tbcache.json next to the other
telemetry stores.
"""

import asyncio
import base64
import json
import logging
import os
import time

import httpx

from app import probe, telemetry

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("TB_AUTO_CACHE", "1") not in ("0", "false", "")
MAX_SLOTS = max(1, int(os.environ.get("TB_CACHE_MAX_SLOTS", "2")))
TITLE_COOLDOWN = float(os.environ.get("TB_CACHE_TITLE_HOURS", "24")) * 3600
SLOT_SECS = float(os.environ.get("TB_CACHE_SLOT_SECS", "900"))
_SEARCH_TIMEOUT = 45.0
_TRIGGER_TIMEOUT = 30.0
_CHECK_DAMPER = 900.0      # min seconds between uncached searches per title
_STATE_MAX_AGE = 30 * 86400
_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"), "tbcache.json")

_client = httpx.AsyncClient(follow_redirects=False,
                            headers={"User-Agent": "Stremio"})

_state: dict | None = None
_checked: dict[str, float] = {}    # in-memory damper (twin fast+slow finishers)
_base_cache: tuple[str | None, str | None] | None = None   # (env value, base)


def _uncached_base() -> str | None:
    """FAST_BASE_URL with the same Comet config flipped to cachedOnly:false,
    or None when the fast source isn't a Comet-with-TorBox deploy."""
    global _base_cache
    env = (os.environ.get("FAST_BASE_URL") or "").rstrip("/")
    if _base_cache is not None and _base_cache[0] == env:
        return _base_cache[1]
    base = None
    if env and "/" in env:
        prefix, b64 = env.rsplit("/", 1)
        try:
            cfg = json.loads(base64.b64decode(b64 + "=" * (-len(b64) % 4)))
            if any((d or {}).get("service") == "torbox"
                   for d in cfg.get("debridServices") or []):
                cfg["cachedOnly"] = False
                nb = base64.b64encode(json.dumps(cfg).encode()).decode()
                base = f"{prefix}/{nb}"
        except Exception:
            base = None
    _base_cache = (env, base)
    return base


def enabled() -> bool:
    return ENABLED and _uncached_base() is not None


def _is_uncached_tb(s: dict) -> bool:
    name = s.get("name") or ""
    return telemetry.debrid_tag(name) == "TB" and "⬇" in name


def _label(s: dict) -> str:
    return (s.get("behaviorHints", {}).get("filename")
            or s.get("name", "?")).replace("\n", " ")[:70]


def _load() -> dict:
    global _state
    if _state is None:
        try:
            with open(_FILE) as f:
                _state = json.load(f)
        except Exception:
            _state = {}
        _state.setdefault("titles", {})
        _state.setdefault("fired", [])
    return _state


def _save() -> None:
    try:
        cutoff = time.time() - _STATE_MAX_AGE
        _state["titles"] = {k: v for k, v in _state["titles"].items()
                            if v.get("ts", 0) > cutoff}
        tmp = _FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f)
        os.replace(tmp, _FILE)
    except Exception:
        logger.debug("tbcache: state save failed", exc_info=True)


async def _uncached_candidates(media: str, media_id: str) -> list[dict]:
    base = _uncached_base()
    try:
        r = await _client.get(f"{base}/stream/{media}/{media_id}.json",
                              timeout=_SEARCH_TIMEOUT)
        streams = r.json().get("streams", [])
    except Exception as e:
        logger.info(f"tbcache: uncached search failed ({type(e).__name__})")
        return []
    return [s for s in streams if _is_uncached_tb(s) and s.get("url")]


def _pick(cands: list[dict], best_verified_res: int, runtime: float,
          tried: list[str]) -> dict | None:
    """Best uncached-TB release that would actually improve on what verified:
    quality-ordered by the picker's own key (audio gate, decode health, real
    effective resolution — sizes are present so 4K claims are evidence-backed),
    first release above the verified resolution tier that wasn't tried before."""
    from app import picker
    picker._annotate_quality(cands, runtime)
    cands.sort(key=picker._quality_key, reverse=True)
    for s in cands:
        if _label(s) in tried:
            continue
        if int(s.get("_effres") or 0) > best_verified_res:
            return s
    return None


async def _trigger(s: dict, media_id: str) -> None:
    """One GET on the uncached playback URL — Comet adds the magnet to TorBox
    during handling, so the response itself (usually 'not cached yet') is
    irrelevant; the body is never downloaded. Holds the shared TorBox
    ingestion gate alongside the probes."""
    gate = probe.ingest_gate(s)
    sem = gate if gate is not None else asyncio.Semaphore(1)
    async with sem:
        try:
            async with _client.stream("GET", s["url"],
                                      timeout=_TRIGGER_TIMEOUT) as r:
                status = str(r.status_code)
        except Exception as e:
            status = type(e).__name__
    res = int(s.get("_effres") or 0)
    logger.info(f"tbcache: asked TorBox to cache [{res}p] {_label(s)}"
                f" (upstream {status})")
    telemetry.record_tbcache(media_id, s, res=res, status=status)


async def maybe_cache(media: str, media_id: str, best_verified_res: int,
                      runtime: float) -> None:
    """Called by the background finishers with the pick's best verified
    resolution tier (0 = nothing plays). Fire-and-forget; never raises."""
    try:
        if not enabled():
            return
        key = f"{media}:{media_id}"
        now = time.time()
        if now - _checked.get(key, 0) < _CHECK_DAMPER:
            return
        _checked[key] = now
        st = _load()
        ent = st["titles"].get(key)
        if ent and now - ent.get("ts", 0) < TITLE_COOLDOWN:
            return
        st["fired"] = [t for t in st["fired"] if now - t < SLOT_SECS]
        limit = min(MAX_SLOTS, probe.TORBOX_MAX_DOWNLOADS)
        if len(st["fired"]) >= limit:
            logger.info(f"tbcache: {key} skipped — all {limit} "
                        f"auto-cache slots assumed busy")
            return
        cands = await _uncached_candidates(media, media_id)
        if not cands:
            return
        pick = _pick(cands, best_verified_res, runtime,
                     tried=(ent or {}).get("tried", []))
        if pick is None:
            return
        # Record before firing: a concurrent caller or a crash must never
        # double-trigger the same release.
        st["titles"][key] = {
            "ts": now,
            "tried": (ent or {}).get("tried", [])[-8:] + [_label(pick)],
        }
        st["fired"].append(now)
        _save()
        await _trigger(pick, media_id)
    except Exception:
        logger.exception(f"tbcache: auto-cache failed for {media_id}")


async def shutdown() -> None:
    await _client.aclose()
