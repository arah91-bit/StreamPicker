"""Source reputation — a persistent, decaying, *evidence-based* blocklist.

The subtlety (learned the hard way): a debrid like TorBox has hundreds of nodes,
so one bad playback is usually *that link on that node right then*, not the
release being bad — the same release via a fresh link often draws a healthy node
and plays fine. So we do NOT block on a single bad event: the proxy's real-time
failover already drops the bad link in the moment. We only *block* a release once
it has delivered badly across several **separate playback sessions** (each a
fresh link, almost certainly different nodes) — strong evidence the release
itself is bad (dead torrent / thin cache), not a node lottery.

Every bad event is still recorded in full (telemetry) with its delivery node, so
the data can later show whether badness tracks the release, the node, the debrid,
the indexer, cache-state, size… — the map of what actually makes a source bad.

Keyed by release signature (telemetry.signature). Decays after BLOCK_TTL; a bad
node long ago doesn't count. Manually clearable from the dashboard.
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("REPUTATION_BLOCK", "1") not in ("0", "false", "")
_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"), "reputation.json")
# Distinct bad *sessions* (fresh links) before a release is dropped. 2 = it has
# to fail on at least two separate plays, so one unlucky node can't block it.
MIN_BLOCK_SESSIONS = int(os.environ.get("MIN_BLOCK_SESSIONS", "2"))
BLOCK_TTL = float(os.environ.get("BLOCK_TTL_DAYS", "30")) * 86400
_MAX_SESS = 30    # cap sessions kept per release
_MAX_ENTRIES = int(os.environ.get("REPUTATION_MAX_ENTRIES", "100000"))
# Short-term avoidance: a release that just delivered badly is skipped by the
# proxy on the very next open (so 'back out, hit play again' lands on the next
# source, not the same bad one), then self-heals — distinct from the slow,
# evidence-based persistent block above.
COOLDOWN = float(os.environ.get("COOLDOWN_MINUTES", "15")) * 60

_store: dict[str, dict] = {}
_cooldowns: dict[str, float] = {}
_lock = threading.Lock()


def _load() -> None:
    global _store
    try:
        with open(_FILE) as f:
            loaded = json.load(f)
            _store = loaded if isinstance(loaded, dict) else {}
            # Legacy fallback identity: unrelated metadata-poor streams all
            # became `gr0s0`. It is not a valid release identity and must never
            # remain actionable (telemetry.signature no longer emits it).
            _store.pop("gr0s0", None)
        try:
            os.chmod(_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        _store = {}


_load()


def _save() -> None:
    try:
        now = time.time()
        for sig in [s for s, e in _store.items()
                    if now - e.get("ts", 0) > BLOCK_TTL]:
            _store.pop(sig, None)
        if len(_store) > _MAX_ENTRIES:
            oldest = sorted(_store, key=lambda s: _store[s].get("ts", 0))
            for sig in oldest[:-_MAX_ENTRIES]:
                _store.pop(sig, None)
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_store, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _FILE)
    except Exception:
        logger.debug("reputation save failed", exc_info=True)


def observe(sig: str, session: str, reason: str, label: str = "",
            node: str = "", extreme: bool = False) -> None:
    """Record one bad-delivery observation. Dedups by session (a session that
    stalls twice still counts once), tracks which nodes it was bad on, and blocks
    only once enough *distinct* sessions have gone bad."""
    if not sig:
        return
    now = time.time()
    with _lock:
        e = _store.get(sig)
        if e and now - e.get("ts", 0) > BLOCK_TTL:
            e = None
        e = e or {"label": label, "sessions": {}, "nodes": {}, "first": now}
        sess = e["sessions"]
        sess[session or f"anon{now}"] = {"ts": now, "reason": reason,
                                         "extreme": extreme or
                                         sess.get(session, {}).get("extreme", False)}
        if len(sess) > _MAX_SESS:                    # keep the most recent
            for k in sorted(sess, key=lambda k: sess[k]["ts"])[:-_MAX_SESS]:
                sess.pop(k, None)
        if node:
            e["nodes"][node] = e["nodes"].get(node, 0) + 1
        e["ts"] = now
        e["reason"] = reason
        e["label"] = label or e.get("label", "")
        _store[sig] = e
        _save()
    n = len(_store[sig]["sessions"])
    if n >= MIN_BLOCK_SESSIONS:
        logger.info(f"reputation: BLOCK {label or sig} "
                    f"({n} bad sessions, last: {reason})")
    else:
        logger.info(f"reputation: noted {label or sig} "
                    f"({n}/{MIN_BLOCK_SESSIONS} bad sessions, {reason})")


def blocked(sig: str) -> bool:
    if not ENABLED or not sig:
        return False
    e = _store.get(sig)
    return bool(e and time.time() - e["ts"] < BLOCK_TTL
               and len(e.get("sessions", {})) >= MIN_BLOCK_SESSIONS)


def cooldown(sig: str) -> None:
    """Avoid this release on the next open for COOLDOWN seconds (short-term)."""
    if not sig:
        return
    now = time.time()
    _cooldowns[sig] = now
    if len(_cooldowns) > 2000:                       # opportunistic prune
        for k in [k for k, t in _cooldowns.items() if now - t > COOLDOWN]:
            _cooldowns.pop(k, None)


def cooled(sig: str) -> bool:
    if not ENABLED or not sig:
        return False
    t = _cooldowns.get(sig)
    return bool(t and time.time() - t < COOLDOWN)


def unblock(sig: str) -> None:
    with _lock:
        if _store.pop(sig, None) is not None:
            _save()


def listing() -> list[dict]:
    now = time.time()
    out = []
    for sig, e in _store.items():
        n = len(e.get("sessions", {}))
        out.append({
            "sig": sig,
            "sessions": n,
            "nodes": len(e.get("nodes", {})),
            "reason": e.get("reason", ""),
            "label": e.get("label", "") or sig,
            "age_h": round((now - e["ts"]) / 3600, 1),
            "blocked": n >= MIN_BLOCK_SESSIONS and now - e["ts"] < BLOCK_TTL,
        })
    out.sort(key=lambda r: (r["blocked"], r["sessions"]), reverse=True)
    return out
