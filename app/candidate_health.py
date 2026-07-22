"""Durable probe memory for ordinary HTTP/debrid candidates.

Result-cache entries contain ephemeral playback URLs and intentionally stop
being current after three hours.  The useful evidence behind them lives longer:
which *release* played, what codecs/detail the probe measured, and which exact
signed URL recently failed.  This module keeps those two identities separate:

* successes and stable media facts are keyed by ``telemetry.signature``;
* failure cooldowns are keyed by a SHA-256 of the full URL (the URL itself,
  including any credential, is never persisted).
* verified show/season packs retain only a credential-free nzbdav mount
  locator, allowing an exact sibling-episode member to be selected and probed.

That means an expired TorBox/debrid URL is not hammered repeatedly, while a
fresh URL for the same known-good release is still allowed and gets a modest
probe-order preference.  All persistence is best-effort and bounded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from app import telemetry

logger = logging.getLogger("stream-picker")

_FILE = os.path.join(os.environ.get("TELEMETRY_DIR", "/data"),
                     "candidate_health.json")
EVIDENCE_TTL = 30 * 86400
TRANSIENT_RETRY = 15 * 60
DEAD_LINK_RETRY = 3 * 3600
_MAX_LINKS = 50_000
_MAX_RELEASES = 50_000
_MAX_PACKS = 5_000
_SAVE_INTERVAL = 5.0

_lock = threading.Lock()
_store: dict[str, dict] = {"links": {}, "releases": {}, "packs": {}}
_last_save = 0.0


def _load() -> None:
    global _store
    try:
        with open(_FILE) as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("candidate health root is not an object")
        links = loaded.get("links")
        releases = loaded.get("releases")
        packs = loaded.get("packs")
        _store = {
            "links": links if isinstance(links, dict) else {},
            "releases": releases if isinstance(releases, dict) else {},
            "packs": packs if isinstance(packs, dict) else {},
        }
        try:
            os.chmod(_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        _store = {"links": {}, "releases": {}, "packs": {}}


_load()


def _prune(now: float) -> None:
    links = _store["links"]
    releases = _store["releases"]
    packs = _store["packs"]
    for key in [k for k, row in links.items()
                if now - float(row.get("ts", 0)) > EVIDENCE_TTL]:
        links.pop(key, None)
    for key in [k for k, row in releases.items()
                if now - float(row.get("ts", 0)) > EVIDENCE_TTL]:
        releases.pop(key, None)
    for key in [k for k, row in packs.items()
                if now - float(row.get("ts", 0)) > EVIDENCE_TTL]:
        packs.pop(key, None)
    if len(links) > _MAX_LINKS:
        oldest = sorted(links, key=lambda k: links[k].get("ts", 0))
        for key in oldest[:-_MAX_LINKS]:
            links.pop(key, None)
    if len(releases) > _MAX_RELEASES:
        oldest = sorted(releases, key=lambda k: releases[k].get("ts", 0))
        for key in oldest[:-_MAX_RELEASES]:
            releases.pop(key, None)
    if len(packs) > _MAX_PACKS:
        oldest = sorted(packs, key=lambda k: packs[k].get("ts", 0))
        for key in oldest[:-_MAX_PACKS]:
            packs.pop(key, None)


def _save(force: bool = False) -> None:
    global _last_save
    now_mono = time.monotonic()
    if not force and now_mono - _last_save < _SAVE_INTERVAL:
        return
    try:
        _prune(time.time())
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_store, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, _FILE)
        _last_save = now_mono
    except Exception:
        logger.debug("candidate health save failed", exc_info=True)


def _link_key(stream: dict) -> str:
    url = stream.get("url") or ""
    return hashlib.sha256(url.encode()).hexdigest() if url else ""


def _host(stream: dict) -> str:
    try:
        return (urlsplit(stream.get("url") or "").hostname or "")[:120]
    except Exception:
        return ""


_DEAD_LINK_RE = re.compile(
    r"(?:http\s+(?:401|403|404|410)|not (?:a )?(?:recognized )?(?:media|video)|"
    r"html|json|empty (?:body|playlist)|short body|playlist (?:too large|nested)|"
    r"hls segment is not media|duration)", re.I)


def _failure_policy(reason: str) -> tuple[str, float]:
    """Classify only the exact URL, never the whole release.

    Even a decisive 404/HTML response can be an expired signed URL.  It earns a
    longer link cooldown, but a newly generated URL for the same release remains
    eligible.  Network/provider failures get a short retry.
    """
    if _DEAD_LINK_RE.search(reason or ""):
        return "dead-link", DEAD_LINK_RETRY
    return "transient", TRANSIENT_RETRY


def record_probe(stream: dict, result) -> None:
    """Remember one completed probe without persisting its credentialed URL."""
    link = _link_key(stream)
    if not link:
        return
    now = time.time()
    sig = telemetry.signature(stream)
    with _lock:
        if bool(getattr(result, "ok", False)):
            _store["links"].pop(link, None)
            if sig:
                row = _store["releases"].get(sig) or {}
                row.update({
                    "ts": now,
                    "last_success": now,
                    "successes": int(row.get("successes", 0)) + 1,
                })
                quality = {
                    "media_bps": float(getattr(result, "media_bps", 0) or 0),
                    "media_height": int(getattr(result, "media_height", 0) or 0),
                    "media_codecs": str(getattr(result, "media_codecs", "") or "")[:160],
                    "vcodec": str(getattr(result, "vcodec", "") or "")[:40],
                    "acodecs": list(getattr(result, "acodecs", ()) or ())[:12],
                    "audio_langs": list(getattr(result, "audio_langs", ()) or ())[:12],
                    "content_kind": str(getattr(result, "content_kind", "") or "")[:20],
                }
                if any(v for v in quality.values()):
                    row["quality"] = quality
                _store["releases"][sig] = row
        else:
            reason = telemetry.sanitize_failure_detail(
                str(getattr(result, "reason", "") or ""), 160)
            kind, retry = _failure_policy(reason)
            _store["links"][link] = {
                "ts": now,
                "retry_at": now + retry,
                "kind": kind,
                "reason": reason,
                "host": _host(stream),
                "sig": sig,
            }
        _save()


def remember_stream_quality(stream: dict) -> None:
    """Persist refined stable media facts learned after the transport probe."""
    sig = telemetry.signature(stream)
    if not sig:
        return
    quality = {
        "vbitrate": float(stream.get("_vbitrate") or 0),
        "media_height": int(stream.get("_vheight") or 0),
        "vcodec": str(stream.get("_vcodec_real") or stream.get("_vcodec") or "")[:40],
        "acodecs": list(stream.get("_acodecs") or [])[:12],
        "audio_langs": list(stream.get("_audio_langs") or [])[:12],
        "content_kind": str(stream.get("_content_kind") or "")[:20],
    }
    if not any(v for v in quality.values()):
        return
    now = time.time()
    with _lock:
        row = _store["releases"].get(sig) or {}
        old = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        row["quality"] = {**old, **{k: v for k, v in quality.items() if v}}
        row["ts"] = now
        _store["releases"][sig] = row
        _save()


def should_skip(stream: dict) -> bool:
    """Whether this exact URL remains inside its recent failure cooldown."""
    key = _link_key(stream)
    row = _store["links"].get(key) if key else None
    return bool(row and time.time() < float(row.get("retry_at", 0)))


def prior_success(stream: dict) -> int:
    """Small, decaying probe-order hint; never verification evidence."""
    sig = telemetry.signature(stream)
    row = _store["releases"].get(sig) if sig else None
    if not row:
        return 0
    age = time.time() - float(row.get("last_success", 0))
    if age < 24 * 3600:
        return 2
    return 1 if age < EVIDENCE_TTL else 0


def quality_hint(stream: dict) -> dict:
    """Stable media facts for ranking a fresh URL of a known release."""
    sig = telemetry.signature(stream)
    row = _store["releases"].get(sig) if sig else None
    if not row or time.time() - float(row.get("ts", 0)) >= EVIDENCE_TTL:
        return {}
    quality = row.get("quality")
    if not isinstance(quality, dict):
        return {}
    out = dict(quality)
    if out.get("vbitrate") and not out.get("media_bps"):
        out["media_bps"] = out["vbitrate"]
    return out


_PACK_SCOPE_RE = re.compile(r"^series:tt\d+(?::\d+)?$")


def remember_verified_pack(stream: dict) -> None:
    """Persist a safe locator for a transport- and identity-verified pack.

    No NZB download URL or WebDAV credential is stored.  The locator is enough
    for the Usenet lane to revisit nzbdav's persistent mount and select an exact
    sibling-episode member.  If that mount has disappeared, the seed simply
    fails quickly and the ordinary fresh indexer search remains authoritative.
    """
    if not stream.get("_nzb_pack"):
        return
    scope = str(stream.get("_nzb_pack_scope") or "")
    release_key = str(stream.get("_nzb_release_key") or "")
    title = str(stream.get("_nzb_pack_title") or "").strip()[:300]
    if (not _PACK_SCOPE_RE.fullmatch(scope)
            or not release_key.startswith("nzb:") or not title):
        return
    try:
        size = max(0, int(stream.get("_nzb_pack_size") or 0))
        year = int(stream.get("_nzb_pack_year") or 0)
    except (TypeError, ValueError):
        size, year = 0, 0
    aliases = [str(value).strip()[:200]
               for value in (stream.get("_nzb_pack_titles") or [])
               if str(value).strip()][:8]
    row = {
        "ts": time.time(),
        "scope": scope,
        "release_key": release_key[:80],
        "legacy_release_key": str(
            stream.get("_nzb_pack_legacy_key") or "")[:80],
        "title": title,
        "size": size,
        "titles": aliases,
        "year": year if 1800 <= year <= 2200 else 0,
    }
    with _lock:
        _store["packs"][f"{scope}|{release_key}"] = row
        _save()


def pack_seeds(media_id: str) -> list[dict]:
    """Safe verified pack locators applicable to an episode request."""
    parts = str(media_id or "").lower().split(":")
    if (len(parts) != 3 or not re.fullmatch(r"tt\d+", parts[0])
            or not parts[1].isdigit() or not parts[2].isdigit()):
        return []
    scopes = {f"series:{parts[0]}:{int(parts[1])}",
              f"series:{parts[0]}"}
    now = time.time()
    rows = [dict(row) for row in _store["packs"].values()
            if row.get("scope") in scopes
            and now - float(row.get("ts", 0)) < EVIDENCE_TTL]
    rows.sort(key=lambda row: float(row.get("ts", 0)), reverse=True)
    return rows


def reset_for_tests() -> None:
    """Clear process memory; tests patch persistence separately."""
    global _store, _last_save
    with _lock:
        _store = {"links": {}, "releases": {}, "packs": {}}
        _last_save = 0.0


def flush() -> None:
    """Persist any debounced evidence during graceful shutdown."""
    with _lock:
        _save(force=True)
