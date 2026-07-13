"""Persistent health and indexer learning for the direct Usenet lane.

Raw probe telemetry is useful for diagnosis, but picker decisions need a small,
transactional summary that can be consulted before an NZB is fetched or mounted.
This module keeps that summary in SQLite and deliberately stores no URLs, API
keys, WebDAV credentials, or free-form exception strings.

Release policy is intentionally asymmetric:

* a verified probe immediately rehabilitates a release;
* a network/provider-style failure only creates a short retry cooldown;
* one decisive release failure creates a longer cooldown; and
* two separated decisive failures permanently suppress that exact release until
  a later success (or database/manual maintenance) clears it.

Indexer counters are exponentially decayed and Bayesian-smoothed.  They guide
which releases are mounted first, but never stop the parallel search of every
configured indexer.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("stream-picker")

ENABLED = os.environ.get("NZB_HEALTH", "1").lower() not in ("0", "false", "")
DB_PATH = os.environ.get(
    "NZB_HEALTH_DB",
    os.path.join(os.environ.get("TELEMETRY_DIR", "/data"),
                 "usenet-health.sqlite3"),
)
MAX_BYTES = int(os.environ.get("NZB_HEALTH_MAX_BYTES", str(64 * 1024 * 1024)))
HARD_RETRY = float(os.environ.get("NZB_HARD_RETRY_HOURS", "24")) * 3600
TRANSIENT_RETRY = float(os.environ.get("NZB_TRANSIENT_RETRY_MINUTES", "30")) * 60
HARD_FAILURES_TO_BLOCK = int(os.environ.get("NZB_HARD_FAILURES_TO_BLOCK", "2"))
HALF_LIFE = float(os.environ.get("NZB_INDEXER_HALF_LIFE_DAYS", "45")) * 86400

_CANON_RE = re.compile(r"[^a-z0-9]+")


def release_key(title: str, size: int | float | None) -> str:
    """High-entropy, non-secret identity for one exact Usenet release."""
    canonical = _CANON_RE.sub("", (title or "").lower())
    try:
        exact_size = max(0, int(size or 0))
    except (TypeError, ValueError):
        exact_size = 0
    if len(canonical) < 8:
        return ""
    digest = hashlib.sha256(f"{canonical}\0{exact_size}".encode()).hexdigest()
    return f"nzb:{digest}"


_HARD_REASON_RE = re.compile(
    r"missing[\s._-]*articles?|not[\s._-]*(?:video|media)|wrong[\s._-]*episode|"
    r"empty body|"
    r"short body|http\s+(?:404|410)\b",
    re.I,
)


def classify_reason(reason: str) -> str:
    """Map a probe detail to an allowlisted decision class."""
    return "hard" if _HARD_REASON_RE.search(reason or "") else "transient"


def _safe_indexer(name: str) -> str:
    # Indexer names come from local config, but still constrain what reaches the
    # database/dashboard.  Never persist a URL accidentally passed as a name.
    value = re.sub(r"[^A-Za-z0-9 ._+\-]", "", name or "").strip()
    return value[:60]


def _safe_label(label: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", label or "").strip()[:180]


def _safe_reason(reason: str, kind: str) -> str:
    """Persist an enum-like reason, never an exception/URL-bearing message."""
    r = (reason or "").lower()
    if kind == "hard":
        if "missing" in r and "article" in r:
            return "missing-articles"
        if "404" in r:
            return "http-404"
        if "410" in r:
            return "http-410"
        if "short body" in r:
            return "short-body"
        if "empty body" in r:
            return "empty-body"
        if "episode" in r:
            return "wrong-episode"
        return "not-video"
    if "timeout" in r or "first byte" in r:
        return "timeout"
    if "never" in r and "appear" in r:
        return "mount-timeout"
    if "throughput" in r or "mb/s" in r or "slow" in r:
        return "slow"
    m = re.search(r"http\s+(\d{3})", r)
    return f"http-{m.group(1)}" if m else "transport"


class HealthStore:
    """Small synchronous SQLite store; every operation is best-effort/fail-open."""

    def __init__(self, path: str, max_bytes: int = MAX_BYTES, clock=time.time):
        self.path = path
        self.max_bytes = max(int(max_bytes), 128 * 1024)
        self.clock = clock
        self._lock = threading.RLock()
        self._writes = 0
        self._conn: sqlite3.Connection | None = None
        self._open()

    def _now(self) -> float:
        return float(self.clock() if callable(self.clock) else self.clock.time())

    def _open(self) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            max_pages = max(32, self.max_bytes // page_size)
            conn.execute(f"PRAGMA max_page_count={max_pages}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS release_health (
                    release_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    last_success REAL,
                    last_failure REAL,
                    hard_failures INTEGER NOT NULL DEFAULT 0,
                    transient_failures INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS release_health_seen
                    ON release_health(last_seen);
                CREATE TABLE IF NOT EXISTS probe_evidence (
                    release_key TEXT NOT NULL,
                    attempt_hash TEXT NOT NULL,
                    ts REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (release_key, attempt_hash),
                    FOREIGN KEY (release_key) REFERENCES release_health(release_key)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS probe_evidence_ts
                    ON probe_evidence(ts);
                CREATE TABLE IF NOT EXISTS indexer_health (
                    name TEXT PRIMARY KEY,
                    search_ok REAL NOT NULL DEFAULT 0,
                    search_fail REAL NOT NULL DEFAULT 0,
                    fetch_ok REAL NOT NULL DEFAULT 0,
                    fetch_fail REAL NOT NULL DEFAULT 0,
                    probe_ok REAL NOT NULL DEFAULT 0,
                    probe_fail REAL NOT NULL DEFAULT 0,
                    probe_transient REAL NOT NULL DEFAULT 0,
                    updated REAL NOT NULL
                );
                """
            )
            conn.commit()
            self._conn = conn
            self._secure_files()
        except Exception:
            logger.exception("usenet health database unavailable; failing open")
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            self._conn = None

    def _secure_files(self) -> None:
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            finally:
                self._conn = None

    def _decay(self, row: sqlite3.Row, now: float) -> dict[str, float]:
        fields = ("search_ok", "search_fail", "fetch_ok", "fetch_fail",
                  "probe_ok", "probe_fail", "probe_transient")
        if HALF_LIFE <= 0:
            factor = 1.0
        else:
            factor = math.pow(0.5, max(0.0, now - row["updated"]) / HALF_LIFE)
        return {f: float(row[f]) * factor for f in fields}

    def _bump_indexer(self, name: str, field: str, amount: float = 1.0) -> None:
        conn = self._conn
        name = _safe_indexer(name)
        if conn is None or not name:
            return
        now = self._now()
        row = conn.execute("SELECT * FROM indexer_health WHERE name=?", (name,)).fetchone()
        if row is None:
            vals = {f: 0.0 for f in ("search_ok", "search_fail", "fetch_ok",
                                     "fetch_fail", "probe_ok", "probe_fail",
                                     "probe_transient")}
        else:
            vals = self._decay(row, now)
        vals[field] += amount
        conn.execute(
            """INSERT INTO indexer_health
               (name,search_ok,search_fail,fetch_ok,fetch_fail,probe_ok,
                probe_fail,probe_transient,updated)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 search_ok=excluded.search_ok, search_fail=excluded.search_fail,
                 fetch_ok=excluded.fetch_ok, fetch_fail=excluded.fetch_fail,
                 probe_ok=excluded.probe_ok, probe_fail=excluded.probe_fail,
                 probe_transient=excluded.probe_transient, updated=excluded.updated""",
            (name, vals["search_ok"], vals["search_fail"], vals["fetch_ok"],
             vals["fetch_fail"], vals["probe_ok"], vals["probe_fail"],
             vals["probe_transient"], now),
        )

    def record_search(self, name: str, ok: bool, *, results: int = 0,
                      latency: float = 0.0) -> None:
        del results, latency  # kept in the API for future diagnostic aggregation
        self._simple_indexer(name, "search_ok" if ok else "search_fail")

    def record_fetch(self, name: str, ok: bool) -> None:
        self._simple_indexer(name, "fetch_ok" if ok else "fetch_fail")

    def _simple_indexer(self, name: str, field: str) -> None:
        if not ENABLED:
            return
        with self._lock:
            try:
                if self._conn is None:
                    return
                self._bump_indexer(name, field)
                self._conn.commit()
                self._secure_files()
                self._after_write()
            except Exception:
                if self._conn:
                    self._conn.rollback()
                logger.debug("usenet indexer health write failed", exc_info=True)

    def record_probe(self, key: str, label: str, indexers: list[str], ok: bool,
                     reason: str, attempt_id: str) -> bool:
        """Record one completed direct-NZB probe; return False if it was a replay."""
        if not ENABLED or not key.startswith("nzb:") or not attempt_id:
            return False
        attempt_hash = hashlib.sha256(attempt_id.encode()).hexdigest()[:24]
        now = self._now()
        kind = "ok" if ok else classify_reason(reason)
        with self._lock:
            try:
                conn = self._conn
                if conn is None:
                    return False
                if conn.execute(
                        "SELECT 1 FROM probe_evidence WHERE release_key=? AND attempt_hash=?",
                        (key, attempt_hash)).fetchone():
                    return False
                row = conn.execute(
                    "SELECT * FROM release_health WHERE release_key=?", (key,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO release_health
                           (release_key,label,first_seen,last_seen)
                           VALUES (?,?,?,?)""",
                        (key, _safe_label(label), now, now),
                    )
                    row = conn.execute(
                        "SELECT * FROM release_health WHERE release_key=?", (key,)
                    ).fetchone()

                if ok:
                    conn.execute(
                        """UPDATE release_health SET label=?,last_seen=?,last_success=?,
                           hard_failures=0,successes=successes+1,blocked=0,retry_at=0,
                           last_reason='' WHERE release_key=?""",
                        (_safe_label(label), now, now, key),
                    )
                elif kind == "hard":
                    # Calls made during the first strike's cooldown may be sibling
                    # probes of the same outage. Keep the evidence but do not let
                    # them manufacture the second, permanent strike.
                    count_it = now >= float(row["retry_at"] or 0)
                    hard = int(row["hard_failures"]) + (1 if count_it else 0)
                    blocked = int(hard >= HARD_FAILURES_TO_BLOCK)
                    conn.execute(
                        """UPDATE release_health SET label=?,last_seen=?,last_failure=?,
                           hard_failures=?,blocked=?,retry_at=?,last_reason=?
                           WHERE release_key=?""",
                        (_safe_label(label), now, now, hard, blocked,
                         now + HARD_RETRY, _safe_reason(reason, kind), key),
                    )
                else:
                    conn.execute(
                        """UPDATE release_health SET label=?,last_seen=?,last_failure=?,
                           transient_failures=transient_failures+1,retry_at=?,
                           last_reason=? WHERE release_key=?""",
                        (_safe_label(label), now, now, now + TRANSIENT_RETRY,
                         _safe_reason(reason, kind), key),
                    )

                conn.execute(
                    "INSERT INTO probe_evidence VALUES (?,?,?,?)",
                    (key, attempt_hash, now, kind),
                )
                # At most eight replay/idempotency markers per release.
                conn.execute(
                    """DELETE FROM probe_evidence WHERE release_key=? AND attempt_hash
                       NOT IN (SELECT attempt_hash FROM probe_evidence
                               WHERE release_key=? ORDER BY ts DESC LIMIT 8)""",
                    (key, key),
                )
                field = "probe_ok" if ok else (
                    "probe_fail" if kind == "hard" else "probe_transient")
                for indexer in dict.fromkeys(_safe_indexer(x) for x in indexers):
                    if indexer:
                        self._bump_indexer(indexer, field)
                conn.commit()
                self._secure_files()
                self._after_write()
                return True
            except sqlite3.OperationalError as e:
                if self._conn:
                    self._conn.rollback()
                if "full" in str(e).lower():
                    self._compact(force=True)
                else:
                    logger.debug("usenet probe health write failed", exc_info=True)
                return False
            except Exception:
                if self._conn:
                    self._conn.rollback()
                logger.debug("usenet probe health write failed", exc_info=True)
                return False

    def release_status(self, key: str) -> dict:
        default = {"hard_failures": 0, "blocked": False, "retry_at": 0.0,
                   "successes": 0, "last_reason": ""}
        if not key:
            return default
        with self._lock:
            try:
                if self._conn is None:
                    return default
                row = self._conn.execute(
                    "SELECT * FROM release_health WHERE release_key=?", (key,)
                ).fetchone()
                if row is None:
                    return default
                return {"hard_failures": int(row["hard_failures"]),
                        "blocked": bool(row["blocked"]),
                        "retry_at": float(row["retry_at"] or 0),
                        "successes": int(row["successes"]),
                        "last_reason": row["last_reason"] or ""}
            except Exception:
                return default

    def unblock(self, key: str) -> None:
        with self._lock:
            try:
                if self._conn is None:
                    return
                self._conn.execute(
                    """UPDATE release_health SET blocked=0,hard_failures=0,
                       retry_at=0,last_reason='' WHERE release_key=?""", (key,))
                self._conn.commit()
            except Exception:
                if self._conn:
                    self._conn.rollback()

    def blocked_listing(self) -> list[dict]:
        with self._lock:
            try:
                if self._conn is None:
                    return []
                now = self._now()
                rows = self._conn.execute(
                    """SELECT release_key,label,last_seen,hard_failures,blocked,
                              last_reason FROM release_health
                       WHERE hard_failures > 0 OR blocked=1
                       ORDER BY blocked DESC,last_seen DESC LIMIT 1000""").fetchall()
                return [{"sig": r["release_key"], "sessions": r["hard_failures"],
                         "nodes": 0, "reason": r["last_reason"],
                         "label": r["label"] or r["release_key"],
                         "age_h": round((now - r["last_seen"]) / 3600, 1),
                         "blocked": bool(r["blocked"]), "kind": "nzb"}
                        for r in rows]
            except Exception:
                return []

    def should_skip(self, key: str) -> bool:
        if not ENABLED or not key:
            return False
        status = self.release_status(key)
        return bool(status["blocked"] or status["retry_at"] > self._now())

    def indexer_score(self, name: str) -> float:
        """0..1 reliability score with a strong cold-start prior."""
        name = _safe_indexer(name)
        with self._lock:
            try:
                if self._conn is None or not name:
                    return 0.5
                row = self._conn.execute(
                    "SELECT * FROM indexer_health WHERE name=?", (name,)
                ).fetchone()
                if row is None:
                    return 0.5
                v = self._decay(row, self._now())
                # Beta(4,4): one lucky hit cannot outrank sustained evidence.
                bad = v["probe_fail"] + 0.5 * v["probe_transient"]
                play = (v["probe_ok"] + 4) / (v["probe_ok"] + bad + 8)
                fetch = (v["fetch_ok"] + 3) / (
                    v["fetch_ok"] + v["fetch_fail"] + 6)
                search = (v["search_ok"] + 3) / (
                    v["search_ok"] + v["search_fail"] + 6)
                return round(0.80 * play + 0.15 * fetch + 0.05 * search, 6)
            except Exception:
                return 0.5

    def fetch_score(self, name: str) -> float:
        """0..1 NZB-download reliability, separate from the composite score:
        an indexer can search fine and its releases can play fine, yet its
        download endpoint may 403/429 every fetch (observed live: one indexer
        at 0/80). This orders which offer's link is tried first, so a dead
        download endpoint stops costing a round-trip per mount."""
        name = _safe_indexer(name)
        with self._lock:
            try:
                if self._conn is None or not name:
                    return 0.5
                row = self._conn.execute(
                    "SELECT * FROM indexer_health WHERE name=?", (name,)
                ).fetchone()
                if row is None:
                    return 0.5
                v = self._decay(row, self._now())
                return round((v["fetch_ok"] + 3) /
                             (v["fetch_ok"] + v["fetch_fail"] + 6), 6)
            except Exception:
                return 0.5

    def indexer_samples(self, name: str) -> int:
        name = _safe_indexer(name)
        with self._lock:
            try:
                if self._conn is None or not name:
                    return 0
                row = self._conn.execute(
                    "SELECT * FROM indexer_health WHERE name=?", (name,)
                ).fetchone()
                if row is None:
                    return 0
                v = self._decay(row, self._now())
                return int(round(sum(v.values())))
            except Exception:
                return 0

    def indexer_listing(self) -> list[dict]:
        with self._lock:
            try:
                if self._conn is None:
                    return []
                names = [r[0] for r in self._conn.execute(
                    "SELECT name FROM indexer_health ORDER BY name")]
                rows = [{"name": name, "score": self.indexer_score(name),
                         "samples": self.indexer_samples(name)} for name in names]
                rows.sort(key=lambda r: (r["score"], r["samples"]), reverse=True)
                return rows
            except Exception:
                return []

    def _after_write(self) -> None:
        self._writes += 1
        if self._writes % 32 == 0:
            self._compact(force=False)

    def _compact(self, force: bool = False) -> None:
        """Bound storage while preserving blocks and the newest observations."""
        conn = self._conn
        if conn is None:
            return
        try:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
            if not force and pages * page_size < self.max_bytes * 0.72:
                return
            # Delete the oldest half of non-blocked release rows. Active hard
            # blocks are permanent and are never sacrificed to make room.
            count = int(conn.execute(
                "SELECT count(*) FROM release_health WHERE blocked=0"
            ).fetchone()[0])
            if count:
                conn.execute(
                    """DELETE FROM release_health WHERE release_key IN
                       (SELECT release_key FROM release_health WHERE blocked=0
                        ORDER BY last_seen ASC LIMIT ?)""",
                    (max(1, count // 2),),
                )
            cutoff = self._now() - 180 * 86400
            conn.execute("DELETE FROM probe_evidence WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            self._secure_files()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.debug("usenet health compaction failed", exc_info=True)


_default: HealthStore | None = None
_default_lock = threading.Lock()


def _store() -> HealthStore | None:
    global _default
    if not ENABLED:
        return None
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = HealthStore(DB_PATH, MAX_BYTES)
    return _default


def should_skip(key: str) -> bool:
    store = _store()
    return store.should_skip(key) if store else False


def status(key: str) -> dict:
    store = _store()
    return store.release_status(key) if store else {}


def unblock(key: str) -> None:
    store = _store()
    if store:
        store.unblock(key)


def blocked_listing() -> list[dict]:
    store = _store()
    return store.blocked_listing() if store else []


def indexer_score(name: str) -> float:
    store = _store()
    return store.indexer_score(name) if store else 0.5


def fetch_score(name: str) -> float:
    store = _store()
    return store.fetch_score(name) if store else 0.5


def indexer_samples(name: str) -> int:
    store = _store()
    return store.indexer_samples(name) if store else 0


def indexer_listing() -> list[dict]:
    store = _store()
    return store.indexer_listing() if store else []


def record_search(name: str, ok: bool, *, results: int = 0,
                  latency: float = 0.0) -> None:
    store = _store()
    if store:
        store.record_search(name, ok, results=results, latency=latency)


def record_fetch(name: str, ok: bool) -> None:
    store = _store()
    if store:
        store.record_fetch(name, ok)


def record_failure(key: str, label: str, indexers: list[str], reason: str,
                   attempt_id: str) -> bool:
    """Record a pre-probe mount/content failure using the same safe policy."""
    store = _store()
    if store:
        return store.record_probe(key, label, indexers, False, reason, attempt_id)
    return False


def record_success(key: str, label: str, indexers: list[str],
                   attempt_id: str) -> None:
    store = _store()
    if store:
        store.record_probe(key, label, indexers, True, "", attempt_id)


def record_probe(stream: dict, result, attempt_id: str) -> None:
    """Probe-module hook; ignores every lane except explicit direct NZB streams."""
    key = stream.get("_nzb_release_key") or ""
    if not key:
        return
    store = _store()
    if store:
        store.record_probe(
            key,
            stream.get("_nzb_label") or
            (stream.get("behaviorHints") or {}).get("filename") or "",
            list(stream.get("_nzb_indexers") or []),
            bool(result.ok),
            result.reason or "",
            attempt_id,
        )
