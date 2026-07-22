"""Probe telemetry: persist how every candidate actually *delivered* so we can
see, after the fact, which sources buffer and which fail — and refine the picker
(or blacklist a source) from real data instead of guesses.

What we can and can't see. Playback goes straight from the debrid host to the
viewer's device; we never observe that stream, so we can't directly detect a
buffer on the TV. What we *can* observe is our own probe: the time-to-first-byte
and sustained throughput we measured pulling the opening megabytes. That is the
same signal that predicts buffering (a congested/uncached host is slow for us
and for the viewer alike), so aggregating it by *source* surfaces the repeat
offenders. Every debrid link here is proxied through one host, so the network
host is useless as a key — the real identity is in the labels: the debrid tag
(`[TB+]` = TorBox cached), the indexer (`Source:` line), the release group, and
the file size. Those are what we bucket on.

Privacy: stream URLs carry debrid API keys, so we record only the URL's bare
host (no path/query) plus the human labels — never the raw URL.

Records are newline-delimited JSON at $TELEMETRY_DIR/probes.jsonl (one rotated
backup). Writing is best-effort and must never break a pick.
"""

import json
import hashlib
import logging
import os
import re
import statistics
import threading
import time
from contextvars import ContextVar
from urllib.parse import urlparse

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("TELEMETRY", "1") not in ("0", "false", "")
_DIR = os.environ.get("TELEMETRY_DIR", "/data")
_PATH = os.path.join(_DIR, "probes.jsonl")
# Total raw-telemetry budget, split into bounded segments.  A 1 GiB default
# retains long-term evidence without making the stats endpoint read 1 GiB just
# to return its recent window.
_MAX_BYTES = int(os.environ.get("TELEMETRY_MAX_BYTES", str(1024 * 1024 * 1024)))
_SEGMENTS = min(64, max(2, int(os.environ.get("TELEMETRY_SEGMENTS", "8"))))
_SEGMENT_BYTES = max(1024 * 1024, _MAX_BYTES // _SEGMENTS)
_write_lock = threading.Lock()

# Per-request context (media/id/picker), set once in the request handler and
# read here; asyncio copies it into the probe tasks the request spawns.
request_ctx: ContextVar[dict] = ContextVar("request_ctx", default={})

_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(api[_-]?key|nzbkey|token|password|passwd|authorization|cookie)"
    r"(\s*[:=]\s*)([^\s,;]+)")
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=_-]+")
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")


def sanitize_failure_detail(detail: str, limit: int = 2000) -> str:
    """Keep diagnostic error structure while removing credential material."""
    value = str(detail or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _URL_USERINFO_RE.sub(r"\1<userinfo>@", value)
    value = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} <redacted>", value)
    value = _SECRET_FIELD_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}<redacted>", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    return value[:max(0, limit)]


def record_usenet_failure(*, release_key: str = "", label: str = "",
                          indexers: list[str] | None = None, stage: str,
                          decision: str, reason: str, detail: str,
                          evidence_id: str = "") -> None:
    """Persist a rich but credential-safe Usenet failure sample.

    These records share the bounded 1 GiB telemetry rotation.  Picker policy
    continues to use the small allowlisted ``reason``; ``detail`` is retained
    only for later error-shape analysis and checker improvements.
    """
    safe_detail = sanitize_failure_detail(detail)
    ctx = request_ctx.get()
    rec = {
        "ts": round(time.time(), 1),
        "kind": "nzb_failure",
        "picker": ctx.get("picker", ""),
        "media": ctx.get("media", ""),
        "id": ctx.get("media_id", ""),
        "stage": re.sub(r"[^a-z0-9_-]", "", (stage or "").lower())[:40],
        "decision": decision if decision in ("hard", "transient") else "transient",
        "reason": re.sub(r"[^a-z0-9_-]", "", (reason or "").lower())[:60],
        "detail": safe_detail,
        "detail_hash": hashlib.sha256(safe_detail.encode()).hexdigest()[:20],
        "sig": release_key if str(release_key).startswith("nzb:") else "",
        "label": re.sub(r"[\x00-\x1f\x7f]+", " ", label or "")[:180],
        "indexers": [re.sub(r"[^A-Za-z0-9 ._+\-]", "", str(x))[:60]
                     for x in (indexers or [])][:12],
        # Correlate repeat observations without storing an external/raw id.
        "evidence_hash": (hashlib.sha256(evidence_id.encode()).hexdigest()[:20]
                          if evidence_id else ""),
    }
    _append(rec)


def record_identity(stream: dict, *, state: str, reason: str,
                    source: str = "", evidence: str = "",
                    expected_years: list[int] | tuple[int, ...] = (),
                    observed_years: list[int] | tuple[int, ...] = ()) -> None:
    """Persist why a candidate was accepted, held back, or rejected by identity.

    This deliberately stores no URL and only a short, sanitized release label.
    It is diagnostic evidence for improving the parser: transport failures tell
    us whether bytes play, while these records tell us what a wrong-title/year/
    episode result looked like before it was allowed near automatic #1.
    """
    allowed = {"strong", "compatible", "unknown", "contradiction"}
    state = state if state in allowed else "unknown"
    safe_evidence = sanitize_failure_detail(evidence, 240)
    rec = _base({k: v for k, v in stream.items() if k != "url"})
    rec.update({
        "kind": "identity",
        "state": state,
        "reason": re.sub(r"[^a-z0-9_-]", "", (reason or "").lower())[:60],
        "source_key": re.sub(r"[^A-Za-z0-9:._+-]", "", source or "")[:80],
        "evidence": safe_evidence,
        "evidence_hash": (hashlib.sha256(safe_evidence.encode()).hexdigest()[:20]
                          if safe_evidence else ""),
        "expected_years": [int(y) for y in expected_years
                           if isinstance(y, int)][:6],
        "observed_years": [int(y) for y in observed_years
                           if isinstance(y, int)][:6],
        "sig": signature(stream),
    })
    _append(rec)


# ── label parsing (the source identity we bucket on) ─────────────────────────
_DEBRID_RE = re.compile(r"\[([A-Za-z]{2,3})([^\]]*)\]")
_SOURCE_RE = re.compile(r"Source:\s*([^\n]+)", re.I)
_GROUP_RE = re.compile(r"-([A-Za-z0-9]{2,20})$")
_RES_RE = re.compile(r"\b(2160|1440|1080|720|480)p?\b")
_SIZE_RE = re.compile(r"Size:\s*([\d.]+)\s*(GB|MB)", re.I)
_EXT_RE = re.compile(r"\.(mkv|mp4|avi|ts|m2ts|wmv)$", re.I)


def debrid_tag(name: str) -> str:
    """The debrid service code + cached marker, robust to the decorations each
    addon puts inside the bracket: AIOStreams '[TB+]', Comet '[TB⚡]' / '[RD]'.
    Returns e.g. 'TB+' (cached TorBox), 'RD' (uncached Real-Debrid), '' if none.
    The service code (TB/RD/AD…) is what distinguishes one debrid's node from
    another's — the basis for byte-identical twin detection in the proxy."""
    m = _DEBRID_RE.search(name or "")
    if not m:
        return ""
    svc = m.group(1).upper()
    cached = "+" in m.group(2) or "⚡" in m.group(2)
    return svc + ("+" if cached else "")


def source_of(text: str) -> str:
    """The 'Source:' line value, e.g. 'StremThru' or 'TorBox|bitsearch' — the
    indexer/tracker the release came from."""
    m = _SOURCE_RE.search(text or "")
    return re.sub(r"\s+", " ", m.group(1)).strip()[:40] if m else ""


def group_of(filename: str) -> str:
    m = _GROUP_RE.search(_EXT_RE.sub("", filename or ""))
    return m.group(1) if m else ""


def _res_of(text: str) -> int:
    m = _RES_RE.search(text or "")
    return int(m.group(1)) if m else 0


def _size_of(text: str) -> float | None:
    m = _SIZE_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1)) * (1e9 if m.group(2).upper() == "GB" else 1e6)


def _host(url: str | None) -> str:
    # .hostname, not .netloc: netloc includes any user:password@ userinfo (the
    # direct-nzb lane embeds WebDAV credentials) which must never be recorded.
    try:
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""


_CODEC_RE = re.compile(r"\b(av1|x265|hevc|h\.?265|x264|h\.?264|avc)\b", re.I)
_HDRT_RE = re.compile(r"\b(dv|dolby\s*vision|hdr10\+|hdr10|hdr|hlg)\b", re.I)


def _codec(text: str) -> str:
    m = _CODEC_RE.search(text or "")
    if not m:
        return ""
    v = m.group(1).lower().replace(".", "")
    if v in ("x265", "h265", "hevc"):
        return "hevc"
    return "av1" if v == "av1" else "h264"


def _hdr(text: str) -> str:
    m = _HDRT_RE.search(text or "")
    if not m:
        return "sdr"
    v = m.group(1).lower().replace(" ", "")
    return "dv" if v in ("dv", "dolbyvision") else v


def netinfo(resp) -> dict:
    """The delivery-node signal we can see: the final host after redirects plus
    whatever CDN/cache headers the upstream exposes. All debrid links are proxied
    so this rarely pins the debrid's own node, but it's the raw material for a map
    of which delivery hosts/POPs/cache-states run slow."""
    try:
        host = resp.url.host
    except Exception:
        host = ""
    h = getattr(resp, "headers", {}) or {}
    via = (h.get("x-served-by") or h.get("cf-ray") or h.get("server") or "")[:40]
    cache = (h.get("x-cache") or h.get("cf-cache-status") or "")[:24]
    return {"node": host, "via": via, "cache": cache}


def identity(stream: dict) -> dict:
    """The source identity we bucket telemetry on and key reputation on."""
    name = stream.get("name") or ""
    desc = stream.get("description") or stream.get("title") or ""
    fname = (stream.get("behaviorHints", {}) or {}).get("filename") or name
    text = f"{name} {desc} {fname}"
    dbr = debrid_tag(name)
    return {
        "debrid": dbr,
        "cached": "+" in dbr,          # AIOStreams '+' = already cached on the debrid
        "src": source_of(desc),
        "grp": group_of(fname),
        "res": _res_of(name) or _res_of(fname),
        "size": _size_of(desc),
        "codec": _codec(text),
        "hdr": _hdr(text),
    }


def signature(stream: dict) -> str:
    """Stable per-release key for the reputation blocklist: the normalised
    full filename, so the same release keys the same across debrid providers
    and a different release keys differently.

    The old key stored only the first 80 normalised characters.  Long release
    names that differed near the end (often the edition, episode, or release
    group) therefore collided, and the playback cache could reuse one file for
    another.  Hash the *entire* canonical filename instead.  Metadata-poor
    streams deliberately have no signature: group/resolution/display-size is
    not a content identity and is unsafe for cache reuse or mid-stream splicing.
    """
    # The direct-NZB lane computes a high-entropy key before mounting, allowing
    # the same identity to suppress future mount work and to follow the stream
    # through probe/proxy telemetry.
    nzb_key = stream.get("_nzb_release_key") or ""
    if isinstance(nzb_key, str) and nzb_key.startswith("nzb:"):
        return nzb_key
    fname = (stream.get("behaviorHints", {}) or {}).get("filename") or ""
    norm = re.sub(r"[^a-z0-9]+", "", _EXT_RE.sub("", fname).lower())
    if len(norm) >= 12:
        # Reputation is title-scoped.  A generic/yearless filename can be reused
        # by remakes or regional editions; evidence learned while serving one
        # IMDb id must never block or rehabilitate the other.  Background picker
        # tasks inherit this ContextVar, and the scoped signature is persisted in
        # the proxy token before playback leaves the request context.
        media_id = str(request_ctx.get().get("media_id") or "")
        return "file:" + hashlib.sha256(
            f"{media_id}\0{norm}".encode()).hexdigest()
    return ""


def _base(stream: dict) -> dict:
    ctx = request_ctx.get()
    return {
        "ts": round(time.time(), 1),
        "picker": ctx.get("picker", ""),
        "media": ctx.get("media", ""),
        "id": ctx.get("media_id", ""),
        "host": _host(stream.get("url")),
        **identity(stream),
    }


# ── writing ──────────────────────────────────────────────────────────────────
def _append(rec: dict) -> None:
    if not ENABLED:
        return
    try:
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with _write_lock:
            os.makedirs(_DIR, exist_ok=True)
            try:
                rotate = os.path.getsize(_PATH) + len(line.encode()) > _SEGMENT_BYTES
            except FileNotFoundError:
                rotate = False
            if rotate:
                oldest = f"{_PATH}.{_SEGMENTS - 1}"
                try:
                    os.remove(oldest)
                except FileNotFoundError:
                    pass
                for i in range(_SEGMENTS - 2, 0, -1):
                    src, dst = f"{_PATH}.{i}", f"{_PATH}.{i + 1}"
                    try:
                        os.replace(src, dst)
                    except FileNotFoundError:
                        pass
                try:
                    os.replace(_PATH, _PATH + ".1")
                except FileNotFoundError:
                    pass
            with open(_PATH, "a") as f:
                f.write(line)
            os.chmod(_PATH, 0o600)
    except Exception:
        logger.debug("telemetry write failed", exc_info=True)


def record(stream: dict, result) -> None:
    """One record per probe attempt (OK or FAIL) — the raw material for spotting
    which sources start slow, deliver slow, or die outright."""
    rec = _base(stream)
    rec["kind"] = "probe"
    rec["ok"] = bool(result.ok)
    rec["ttfb"] = round(result.ttfb, 2)
    rec["mbps"] = round(result.speed_bps / 1e6, 2) if result.speed_bps else None
    rec["reason"] = ("" if result.ok else
                     sanitize_failure_detail(result.reason or "", 500))
    rec["node"] = getattr(result, "node", "") or ""
    rec["via"] = getattr(result, "via", "") or ""
    rec["cache"] = getattr(result, "cache", "") or ""
    rec["sig"] = signature(stream)
    if stream.get("_nzb_release_key"):
        rec["lane"] = "nzb"
        rec["indexers"] = [re.sub(r"[^A-Za-z0-9 ._+\-]", "", str(x))[:60]
                           for x in (stream.get("_nzb_indexers") or [])][:12]
        rec["fetch_indexer"] = re.sub(
            r"[^A-Za-z0-9 ._+\-]", "", stream.get("_nzb_indexer") or "")[:60]
        if stream.get("_nzb_mount_secs") is not None:
            rec["mount_secs"] = stream.get("_nzb_mount_secs")
            rec["mount_reused"] = bool(stream.get("_nzb_mount_reused"))
    _append(rec)


def record_served(stream: dict) -> None:
    """The stream we actually returned as #1 for a request (fresh or cached), so
    every user-facing answer is traceable: when a title buffers, look up what we
    auto-picked and how it had probed."""
    if not stream:
        return
    rec = _base(stream)
    rec["kind"] = "served"
    rec["ttfb"] = round(stream.get("_ttfb", 0.0), 2)
    speed = stream.get("_speed", 0.0)
    rec["mbps"] = round(speed / 1e6, 2) if speed else None
    rec["ok"] = True
    rec["label"] = re.sub(r"\s+", " ", (stream.get("name") or ""))[:80]
    _append(rec)


# ── reading / aggregation (for the /stats dashboard) ─────────────────────────
def _tail_lines(path: str, limit: int) -> list[bytes]:
    """Read at most the last `limit` lines without scanning a large segment."""
    if limit <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            chunks: list[bytes] = []
            newlines = 0
            while pos > 0 and newlines <= limit:
                n = min(64 * 1024, pos)
                pos -= n
                f.seek(pos)
                chunk = f.read(n)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        return b"".join(reversed(chunks)).splitlines()[-limit:]
    except FileNotFoundError:
        return []


def load(limit: int = 300_000) -> list[dict]:
    if limit <= 0:
        return []
    # Newest file first while collecting, then reverse the file groups so the
    # returned records remain chronological.
    groups: list[list[bytes]] = []
    remaining = limit
    for p in [_PATH] + [f"{_PATH}.{i}" for i in range(1, _SEGMENTS)]:
        lines = _tail_lines(p, remaining)
        if lines:
            groups.append(lines)
            remaining -= len(lines)
        if remaining <= 0:
            break
    recs: list[dict] = []
    for lines in reversed(groups):
        for line in lines:
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    return recs[-limit:]


def aggregate_usenet_failures(recs: list[dict], limit: int = 200) -> list[dict]:
    """Deduplicate detailed Usenet error shapes while retaining their samples."""
    groups: dict[tuple, dict] = {}
    for rec in recs:
        if rec.get("kind") != "nzb_failure":
            continue
        key = (rec.get("stage", ""), rec.get("detail_hash", ""),
               rec.get("reason", ""), rec.get("decision", ""))
        row = groups.setdefault(key, {
            "stage": key[0], "detail_hash": key[1], "reason": key[2],
            "decision": key[3], "detail": rec.get("detail", ""),
            "label": rec.get("label", ""),
            "indexers": rec.get("indexers") or [], "count": 0,
            "first_ts": rec.get("ts", 0), "last_ts": rec.get("ts", 0),
        })
        row["count"] += 1
        row["first_ts"] = min(row["first_ts"], rec.get("ts", 0))
        if rec.get("ts", 0) >= row["last_ts"]:
            row["last_ts"] = rec.get("ts", 0)
            row["detail"] = rec.get("detail", "")
            row["label"] = rec.get("label", "")
            row["indexers"] = rec.get("indexers") or []
    rows = list(groups.values())
    rows.sort(key=lambda r: (r["last_ts"], r["count"]), reverse=True)
    return rows[:max(0, limit)]


def _median(xs: list[float]) -> float:
    return round(statistics.median(xs), 2) if xs else 0.0


def _p90(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(0.9 * len(s)))], 2)


def aggregate(recs: list[dict], key: str, kind: str = "probe",
              min_n: int = 1) -> list[dict]:
    """Group probe records by one identity field and summarise delivery: how
    often it fails, and how it starts / streams when it works."""
    groups: dict[str, dict] = {}
    for r in recs:
        if r.get("kind") != kind:
            continue
        k = (r.get(key) or "").strip() or "(none)"
        g = groups.setdefault(k, {"n": 0, "fail": 0, "ttfbs": [], "mbps": []})
        g["n"] += 1
        if not r.get("ok"):
            g["fail"] += 1
        else:
            g["ttfbs"].append(r.get("ttfb") or 0.0)
            if r.get("mbps") is not None:
                g["mbps"].append(r["mbps"])
    rows = []
    for k, g in groups.items():
        if g["n"] < min_n:
            continue
        rows.append({
            "key": k,
            "n": g["n"],
            "fail_pct": round(100 * g["fail"] / g["n"], 1),
            "ttfb_med": _median(g["ttfbs"]),
            "ttfb_p90": _p90(g["ttfbs"]),
            "mbps_med": _median(g["mbps"]),
        })
    # Worst first: failures, then slow starts, then slow throughput.
    rows.sort(key=lambda r: (r["fail_pct"], r["ttfb_p90"], -r["mbps_med"]),
              reverse=True)
    return rows


def record_play(entry: dict, cand: dict, idx: int, *, served: int, dur: float,
                ttfb: float, reconnects: int, reason: str, net: dict | None = None,
                session: str = "", up_mbps: float | None = None,
                slow: bool = False) -> None:
    """One record per actual playback through the proxy — real delivery to the
    device: which candidate served (idx>0 = we auto-switched), throughput, how
    it ended, how much got watched, and the delivery node it came from."""
    size = cand.get("size")
    net = net or {}
    _append({
        "ts": round(time.time(), 1),
        "kind": "play",
        "session": session,
        "picker": entry.get("picker", ""),
        "id": entry.get("id", ""),
        "debrid": cand.get("dbr", ""),
        "cached": "+" in (cand.get("dbr", "") or ""),
        "src": cand.get("src", ""),
        "grp": cand.get("grp", ""),
        "res": cand.get("res", 0),
        "codec": cand.get("codec", ""),
        "hdr": cand.get("hdr", ""),
        "size": size,
        "node": net.get("node", ""),
        "via": net.get("via", ""),
        "cache": net.get("cache", ""),
        "ttfb": round(ttfb, 2),
        "mbps": round(served / dur / 1e6, 2) if dur > 0 and served else 0.0,
        "up_mbps": up_mbps,          # rate the SOURCE fed us while we waited on it
        "slow": slow,                # source couldn't keep up mid-stream (buffering)
        "mb": round(served / 1e6, 1),
        "secs": round(dur, 1),
        "idx": idx,
        "switched": idx > 0,
        "reconnects": reconnects,
        "reason": reason,
        "watched": round(100 * served / size, 1) if size else None,
    })


def record_buffer(event: str, *, sig: str = "", picker: str = "",
                  media_id: str = "", source: str = "", dbr: str = "",
                  node: str = "", offset: int | None = None,
                  avail: int | None = None, total: int | None = None,
                  reason: str = "", mbps: float | None = None) -> None:
    """One durable record per notable event on the buffering proxy's *producer*
    side — the part playback records can't see because it happens behind the
    read-ahead buffer. `event` is start | drop | reconnect | failed | twin | slow
    | complete. This is the post-mortem trail: when a stream misbehaves, these
    lines say which source fed it, where (byte offset) it dropped or slowed, what
    it switched to, and whether it ever recovered. Persisted to probes.jsonl."""
    _append({
        "ts": round(time.time(), 1),
        "kind": "buffer",
        "event": event,
        "picker": picker,
        "id": media_id,
        "sig": (sig or "")[:80],
        "src": (source or "")[:60],
        "debrid": dbr,
        "node": (node or "")[:40],
        "offset": offset,
        "avail": avail,
        "total": total,
        "reason": (reason or "")[:80],
        "mbps": mbps,
    })


def record_tbcache(media_id: str, stream: dict, *, res: int,
                   status: str) -> None:
    """One record per TorBox auto-cache trigger (kind=tbcache): which uncached
    release we asked TorBox to start downloading for a title, its evidence-
    backed resolution, and what the upstream playback endpoint answered."""
    _append({
        "ts": round(time.time(), 1),
        "kind": "tbcache",
        "id": media_id,
        "label": (stream.get("name") or "").replace("\n", " ")[:60],
        "file": (stream.get("behaviorHints", {}).get("filename") or "")[:80],
        "res": res,
        "status": str(status)[:40],
    })


def record_cache_event(event: str, *, target_id: str = "",
                       seconds: float | None = None,
                       age_seconds: float | None = None,
                       count: int = 0, active: bool | None = None,
                       detail: str = "") -> None:
    """Record cache/prewarm control-plane outcomes without retaining URLs.

    These events make the new desired-state prewarmer measurable: how long E+1
    took to become ready, whether stale leaders survived revalidation, and how
    many known-dead exact links were avoided.
    """
    ctx = request_ctx.get()
    rec = {
        "ts": round(time.time(), 1),
        "kind": "cache",
        "event": re.sub(r"[^a-z0-9_-]", "", (event or "").lower())[:48],
        "picker": str(ctx.get("picker", ""))[:60],
        "media": str(ctx.get("media", ""))[:20],
        "id": str(ctx.get("media_id", ""))[:80],
        "target": str(target_id or "")[:80],
        "count": max(0, int(count or 0)),
        "detail": re.sub(r"[^A-Za-z0-9 ._+:-]", "", detail or "")[:100],
    }
    if seconds is not None:
        rec["seconds"] = round(max(0.0, float(seconds)), 2)
    if age_seconds is not None:
        rec["age_h"] = round(max(0.0, float(age_seconds)) / 3600, 2)
    if active is not None:
        rec["active"] = bool(active)
    _append(rec)


def aggregate_cache(recs: list[dict]) -> dict:
    """Headline stale-cache and next-episode prewarm effectiveness metrics."""
    rows = [r for r in recs if r.get("kind") == "cache"]
    by_event: dict[str, int] = {}
    for row in rows:
        event = row.get("event") or "unknown"
        by_event[event] = by_event.get(event, 0) + 1
    ready_secs = [float(r.get("seconds") or 0) for r in rows
                  if r.get("event") == "prewarm_ready"
                  and r.get("seconds") is not None]
    stale_ok = by_event.get("stale_revalidate_ok", 0)
    stale_fail = by_event.get("stale_revalidate_fail", 0)
    stale_attempts = stale_ok + stale_fail
    return {
        "events": len(rows),
        "prewarm_intents": by_event.get("prewarm_intent", 0),
        "prewarm_ready": by_event.get("prewarm_ready", 0),
        "prewarm_cache_hits": by_event.get("prewarm_cache_hit", 0),
        "prewarm_timeouts": by_event.get("prewarm_wait_timeout", 0),
        "prewarm_seconds_med": _median(ready_secs),
        "prewarm_seconds_p90": _p90(ready_secs),
        "stale_attempts": stale_attempts,
        "stale_revalidated": stale_ok,
        "stale_success_pct": (round(100 * stale_ok / stale_attempts, 1)
                              if stale_attempts else 0.0),
        "probes_avoided": sum(int(r.get("count") or 1) for r in rows
                              if r.get("event") == "probe_avoided"),
        "pack_members_verified": by_event.get("pack_member_verified", 0),
        "pack_members_reused": by_event.get("pack_member_reused", 0),
        "identity_rejected": by_event.get(
            "transport_ok_identity_rejected", 0),
        "by_event": by_event,
    }


def aggregate_play(recs: list[dict], key: str, min_n: int = 1) -> list[dict]:
    """Group real-playback records by identity: how often the source died, how
    often we had to switch away from it, and the real throughput/watched-fraction."""
    groups: dict[str, dict] = {}
    for r in recs:
        if r.get("kind") != "play":
            continue
        k = (r.get(key) or "").strip() or "(none)"
        g = groups.setdefault(k, {"n": 0, "dead": 0, "slow": 0, "switched": 0,
                                  "mbps": [], "watched": []})
        g["n"] += 1
        if r.get("reason") in ("upstream_dead", "all_failed"):
            g["dead"] += 1
        if r.get("slow"):
            g["slow"] += 1
        if r.get("switched"):
            g["switched"] += 1
        if r.get("mbps"):
            g["mbps"].append(r["mbps"])
        if r.get("watched") is not None:
            g["watched"].append(r["watched"])
    rows = []
    for k, g in groups.items():
        if g["n"] < min_n:
            continue
        rows.append({
            "key": k, "n": g["n"],
            "dead_pct": round(100 * g["dead"] / g["n"], 1),
            "slow_pct": round(100 * g["slow"] / g["n"], 1),
            "switch_pct": round(100 * g["switched"] / g["n"], 1),
            "mbps_med": _median(g["mbps"]),
            "watched_med": _median(g["watched"]),
        })
    rows.sort(key=lambda r: (r["dead_pct"] + r["slow_pct"], -r["mbps_med"]),
              reverse=True)
    return rows
