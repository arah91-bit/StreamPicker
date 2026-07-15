"""Learned decode compatibility — codec attributes the household's players
provably cannot open.

The playback probe proves a stream *delivers*; it cannot prove the player can
*decode* it (live case: a 5×FLAC multi-audio remux the player rejected while
H.264+DTS played fine). This store turns those player-rejected events into a
lesson about the *class* of file, not just the instance:

  * every player-rejected stream whose cached head ffprobe can read strikes
    each of its codec attributes (``a:<audio codec>``, ``v:<video codec>``);
  * every stream that reaches real playback credits its attributes.

An attribute with enough strikes and no credits is *bad*: releases carrying it
(probe-sniffed, or declared in the release name) are demoted below every clean
candidate at ranking time — never removed, because evidence can be ambiguous.
A multi-audio reject strikes all its audio codecs, and the innocent ones earn
their credits back from other files that play; only the codec that appears in
rejects and never in a successful play stays bad. Decays after TTL so an
upgraded player gets a clean slate.
"""

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("stream-picker")

_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"),
                     "decode_health.json")
# Rejections (with zero successful plays) before an attribute counts as bad.
BAD_REJECTS = int(os.environ.get("DECODE_BAD_REJECTS", "2"))
TTL = float(os.environ.get("DECODE_TTL_DAYS", "90")) * 86400
_MAX_LABELS = 3      # example release names kept per attribute, for diagnosis

_store: dict[str, dict] = {}
_lock = threading.Lock()

# Release names that explicitly declare a codec — the only names trusted for
# demotion when no probe sniff is available. Keys match ffprobe codec names as
# normalized by vprobe._norm_codec.
_DECLARED = [
    (re.compile(r"\bflac\b", re.I), "a:flac"),
    (re.compile(r"\bl?pcm\b", re.I), "a:pcm"),
    (re.compile(r"true[\s._-]?hd", re.I), "a:truehd"),
    (re.compile(r"\bopus\b", re.I), "a:opus"),
    (re.compile(r"\bav1\b", re.I), "v:av1"),
    (re.compile(r"\bvp9\b", re.I), "v:vp9"),
]


def _load() -> None:
    global _store
    try:
        with open(_FILE) as f:
            loaded = json.load(f)
            _store = loaded if isinstance(loaded, dict) else {}
    except Exception:
        _store = {}


_load()


def _save() -> None:
    try:
        now = time.time()
        for k in [k for k, e in _store.items() if now - e.get("ts", 0) > TTL]:
            _store.pop(k, None)
        tmp = _FILE + ".tmp"
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(_store, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _FILE)
    except Exception:
        logger.debug("decode-health save failed", exc_info=True)


def _keys(acodecs, vcodec) -> list[str]:
    keys = [f"a:{a.lower()}" for a in (acodecs or []) if a]
    if vcodec:
        keys.append(f"v:{vcodec.lower()}")
    return keys


def _bump(key: str, field: str, label: str = "") -> dict:
    e = _store.get(key)
    if e and time.time() - e.get("ts", 0) > TTL:
        e = None
    e = e or {"rejects": 0, "plays": 0, "labels": []}
    e[field] = e.get(field, 0) + 1
    e["ts"] = time.time()
    if label and label not in e["labels"]:
        e["labels"] = (e["labels"] + [label])[-_MAX_LABELS:]
    _store[key] = e
    return e


def record_reject(acodecs, vcodec, label: str = "") -> None:
    """A player-rejected file carried these codecs: strike each of them."""
    with _lock:
        newly_bad = []
        for k in _keys(acodecs, vcodec):
            e = _bump(k, "rejects", label)
            if e["rejects"] == BAD_REJECTS and not e.get("plays"):
                newly_bad.append(k)
        _save()
    for k in newly_bad:
        logger.info(f"decode-health: {k} is now considered undecodable "
                    f"({BAD_REJECTS} player rejections, no successful plays) "
                    f"— matching releases will rank below clean ones")


def record_play(acodecs, vcodec) -> None:
    """A file with these codecs reached real playback: credit each of them."""
    with _lock:
        for k in _keys(acodecs, vcodec):
            _bump(k, "plays")
        _save()


def bad_keys() -> frozenset:
    """Attributes with BAD_REJECTS+ strikes and zero successful plays."""
    now = time.time()
    return frozenset(
        k for k, e in _store.items()
        if e.get("rejects", 0) >= BAD_REJECTS and not e.get("plays")
        and now - e.get("ts", 0) < TTL)


def declared_keys(text: str) -> set[str]:
    """Attribute keys a release *name* explicitly declares (FLAC, AV1, …)."""
    return {key for rx, key in _DECLARED if rx.search(text or "")}


def suspect(text: str, acodecs=(), vcodec: str = "") -> bool:
    """True when a stream carries a learned-undecodable attribute — by probe
    sniff when available (authoritative), else by explicit name declaration."""
    bad = bad_keys()
    if not bad:
        return False
    if acodecs or vcodec:
        return bool(set(_keys(acodecs, vcodec)) & bad)
    return bool(declared_keys(text) & bad)


def listing() -> list[dict]:
    """Dashboard-friendly view of every learned attribute."""
    now = time.time()
    bad = bad_keys()
    out = [{"key": k, "rejects": e.get("rejects", 0), "plays": e.get("plays", 0),
            "bad": k in bad, "labels": e.get("labels", []),
            "age_h": round((now - e.get("ts", now)) / 3600, 1)}
           for k, e in _store.items()]
    out.sort(key=lambda r: (r["bad"], r["rejects"]), reverse=True)
    return out
