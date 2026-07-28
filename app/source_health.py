"""What the operator has decided about a failing source.

The home page can tell you a source never works. That is only useful if you
can then *do* something about it, and if what you do sticks — an alert you
cannot answer is one you learn to scroll past, which costs you the next real
one too.

Three answers, deliberately distinct:

  dismissed  Stop warning me about this. The source is still searched and
             still picked; only the home-page alarm goes quiet. For the case
             where you already know (an addon you are about to remove, a host
             you know is rate-limiting today).
  blocked    Never use this source again. Enforced in the picker, so its
             releases stop being candidates at all. Reversible.
  cleared    Forget both, and let it be judged fresh from here.

Keyed by the source name telemetry parses from a stream's "Source:" line —
the same string the probe tables and the home page show — so what you click
and what gets enforced are the same identity. Stored beside reputation.json
rather than in config.json: this is evidence-driven per-source state written
from the dashboard, not a setting anyone hand-authors.
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger("stream-picker")

_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"),
                     "source_health.json")
# A decision about a source that has since gone quiet is not worth keeping
# forever; blocks are exempt, because "never use this" has no natural expiry.
_DISMISS_TTL = 90 * 86400

_lock = threading.Lock()
_store: dict[str, dict[str, float]] = {"dismissed": {}, "blocked": {}}


def _load() -> None:
    global _store
    try:
        with open(_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("root must be an object")
        _store = {
            "dismissed": {str(k): float(v) for k, v
                          in (data.get("dismissed") or {}).items()},
            "blocked": {str(k): float(v) for k, v
                        in (data.get("blocked") or {}).items()},
        }
    except FileNotFoundError:
        pass
    except Exception as exc:                       # corrupt file must not boot-loop
        logger.warning("source_health: ignoring unreadable %s (%s)", _FILE, exc)


def _save() -> None:
    now = time.time()
    _store["dismissed"] = {k: v for k, v in _store["dismissed"].items()
                           if now - v < _DISMISS_TTL}
    tmp = f"{_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_store, fh)
        os.replace(tmp, _FILE)
    except OSError as exc:
        logger.warning("source_health: could not save (%s)", exc)


def _norm(name: str) -> str:
    return (name or "").strip()[:40]


def dismiss(name: str) -> None:
    """Silence the home-page warning; keep using the source."""
    name = _norm(name)
    if not name:
        return
    with _lock:
        _store["dismissed"][name] = time.time()
        _save()


def block(name: str) -> None:
    """Stop picking this source's releases entirely."""
    name = _norm(name)
    if not name:
        return
    with _lock:
        _store["blocked"][name] = time.time()
        # A blocked source cannot also be warning about itself.
        _store["dismissed"].pop(name, None)
        _save()


def clear(name: str) -> None:
    """Forget every decision about this source, so it is judged fresh."""
    name = _norm(name)
    with _lock:
        dropped = (_store["dismissed"].pop(name, None) is not None
                   or _store["blocked"].pop(name, None) is not None)
        if dropped:
            _save()


def is_dismissed(name: str) -> bool:
    return _norm(name) in _store["dismissed"]


def is_blocked(name: str) -> bool:
    return _norm(name) in _store["blocked"]


def state(name: str) -> str:
    """"blocked", "dismissed", or "" — what the operator decided."""
    name = _norm(name)
    if name in _store["blocked"]:
        return "blocked"
    if name in _store["dismissed"]:
        return "dismissed"
    return ""


def decided_at(name: str) -> float:
    name = _norm(name)
    return _store["blocked"].get(name) or _store["dismissed"].get(name) or 0.0


def blocked_names() -> list[str]:
    return sorted(_store["blocked"])


def listing() -> list[dict]:
    now = time.time()
    return sorted(
        [{"name": name, "state": kind,
          "age_h": round((now - ts) / 3600, 1)}
         for kind in ("blocked", "dismissed")
         for name, ts in _store[kind].items()],
        key=lambda r: (r["state"], r["name"]))


_load()
