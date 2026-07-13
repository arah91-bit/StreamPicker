"""Direct usenet lane: search the household's Newznab indexers in parallel and
turn the promising releases into streamable URLs through nzbdav.

Why this exists: Usenet Ultimate (the upstream addon) answers slowly and its
health tags proved unreliable — only ~40% of usenet releases actually play, but
the ones that do are often the best available quality. This lane makes usenet
fast enough to race the debrid sources and cheap enough to verify honestly:

  1. Search all indexers concurrently (Newznab API, ~1-3s each).
  2. For the top releases by quality, fetch the NZB and PUT it into nzbdav's
     WebDAV watch folder (/nzbs/{movies|tv}/) — nzbdav mounts the content as
     streamable files under /content/{cat}/{job}/ within a couple of seconds,
     without downloading anything.
  3. Return stream dicts whose URLs point at the mounted video over WebDAV
     (basic auth embedded; the proxy always wraps these so credentials never
     reach a player).

The picker probes every returned URL like any other candidate — that probe is
the real playability check that catches the ~60% with missing articles. Mounts
persist in nzbdav, so a re-search or re-open reuses them instantly.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from app import meta, telemetry, usenet_health

logger = logging.getLogger("stream-picker")

# "name|api-base|apikey;name|api-base|apikey;..."
_INDEXER_SPEC = os.environ.get("NZB_INDEXERS", "")
NZBDAV_URL = (os.environ.get("NZBDAV_URL") or "").rstrip("/")
NZBDAV_USER = os.environ.get("NZBDAV_USER", "")
NZBDAV_PASS = os.environ.get("NZBDAV_PASS", "")
NZBDAV_API_KEY = os.environ.get("NZBDAV_API_KEY", "")
# How many releases to mount per title (top-quality first). Each mount is a
# ~100KB NZB fetch + WebDAV PUT, but nzbdav article-checks every import over the
# same NNTP connections that serve active streams — keep the batch modest so a
# search can't starve someone's playback (or trip the provider's connection cap).
MOUNT_MAX = int(os.environ.get("NZB_MOUNT_MAX", "6"))
SEARCH_TIMEOUT = float(os.environ.get("NZB_SEARCH_TIMEOUT", "8"))
# nzbdav processes its watch folder serially with article checks, so a batch of
# concurrent PUTs mounts over ~10-60s. We collect what lands inside this window;
# stragglers persist in nzbdav and get reused instantly on the next search.
MOUNT_WAIT = float(os.environ.get("NZB_MOUNT_WAIT", "600"))
# Direct usenet is opportunistic for the latency-first picker: expose the first
# mounted candidate immediately, while the rest continue into the shared cache.
MOUNT_RETURN_WANT = int(os.environ.get("NZB_MOUNT_RETURN_WANT", "1"))
MOUNT_EARLY_WAIT = float(os.environ.get("NZB_MOUNT_EARLY_WAIT", "30"))
MOUNT_STAGGER = float(os.environ.get("NZB_MOUNT_STAGGER", "1.5"))
IMPORT_CONCURRENCY = max(1, int(os.environ.get("NZB_IMPORT_CONCURRENCY", "2")))
# A healthy import has completed in roughly 100s on this deployment.  Keep its
# scarce submit slot long enough to avoid building a server-side queue; release
# the slot eventually if the item is dead while its mount poll continues.
IMPORT_SLOT_HOLD = float(os.environ.get("NZB_IMPORT_SLOT_HOLD", "150"))

# Reserve early mount slots for encodes whose size is plausibly deliverable,
# while still importing the largest remuxes for the slow/background quality
# pass.  These are selection targets, not hard caps.
MOVIE_TARGET_4K = int(float(os.environ.get("NZB_MOVIE_TARGET_4K_GB", "18")) * 1e9)
MOVIE_TARGET_1080 = int(float(os.environ.get("NZB_MOVIE_TARGET_1080_GB", "8")) * 1e9)
TV_TARGET_4K = int(float(os.environ.get("NZB_TV_TARGET_4K_GB", "6")) * 1e9)
TV_TARGET_1080 = int(float(os.environ.get("NZB_TV_TARGET_1080_GB", "3")) * 1e9)

INDEXERS: list[tuple[str, str, str]] = []
for part in _INDEXER_SPEC.split(";"):
    bits = part.strip().split("|")
    if len(bits) == 3 and all(bits):
        INDEXERS.append((bits[0], bits[1].rstrip("/"), bits[2]))

_client = httpx.AsyncClient(follow_redirects=True,
                            headers={"User-Agent": "stream-picker/1.0"})
_import_slots = asyncio.Semaphore(IMPORT_CONCURRENCY)


def enabled() -> bool:
    return bool(INDEXERS and NZBDAV_URL and NZBDAV_USER and NZBDAV_PASS)


def _dav_auth() -> tuple[str, str]:
    return (NZBDAV_USER, NZBDAV_PASS)


def _stream_base() -> str:
    """nzbdav base with credentials embedded, for URLs the probe/proxy fetch.
    These URLs are always proxy-wrapped before reaching a player."""
    p = urlsplit(NZBDAV_URL)
    netloc = f"{quote(NZBDAV_USER, safe='')}:{quote(NZBDAV_PASS, safe='')}@{p.netloc}"
    return urlunsplit((p.scheme, netloc, "", "", ""))


# ── Newznab search ───────────────────────────────────────────────────────────

def _parse_items(text: str) -> list[dict]:
    """Newznab XML → [{title, size, link}]. XML is the one format every indexer
    speaks (JSON support varies), so parse that only."""
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        size = 0
        enc = item.find("enclosure")
        if enc is not None:
            link = enc.get("url") or link
            try:
                size = int(enc.get("length") or 0)
            except ValueError:
                size = 0
        for attr in item.iter():
            if attr.tag.endswith("attr") and attr.get("name") == "size":
                try:
                    size = int(attr.get("value") or 0)
                except ValueError:
                    pass
        if title and link:
            out.append({"title": title, "size": size, "link": link})
    return out


async def _search_one(name: str, base: str, key: str, params: dict) -> list[dict]:
    t0 = time.monotonic()
    try:
        r = await _client.get(base, params={**params, "apikey": key},
                              timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        items = _parse_items(r.text)
        for it in items:
            it["indexer"] = name
        usenet_health.record_search(name, True, results=len(items),
                                    latency=time.monotonic() - t0)
        logger.info(f"nzb search {name}: {len(items)} results")
        return items
    except Exception as e:
        usenet_health.record_search(name, False,
                                    latency=time.monotonic() - t0)
        # httpx exceptions can stringify their request URL (including apikey).
        # Log only the exception class/status, never the URL-bearing message.
        status = getattr(getattr(e, "response", None), "status_code", None)
        detail = f" HTTP {status}" if status else ""
        logger.info(f"nzb search {name} failed: {type(e).__name__}{detail}")
        return []


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


async def _expected_info(media: str, media_id: str) -> tuple[list[str], int | None]:
    """Authoritative title prefixes and release year for this IMDb id."""
    try:
        title, orig, year = await meta.title_year(media, media_id.split(":")[0])
    except Exception:
        return [], None
    return list(dict.fromkeys(t for t in (title, orig) if t)), year


_TITLE_TAIL_MARKERS = {
    "uhd", "hd", "4k", "8k", "web", "webdl", "webrip", "bluray",
    "bdrip", "brrip", "remux", "hdtv", "dvdrip", "dvdscr", "cam",
    "telesync", "repack", "proper", "extended", "theatrical", "imax",
    "hdr", "hdr10", "dovi", "dv", "internal", "complete", "multi",
    "dual",
}


def _release_title_match(release: str, expected: str) -> bool:
    """Match an authoritative title without accepting a longer shared prefix.

    Newznab IMDb searches sometimes return another title beginning with the
    requested one (``It Follows`` for ``It``, ``Upgrade`` for ``Up``).  Scene
    punctuation is flexible, so consume the expected title as alphanumerics,
    then require the next token to look like release metadata rather than
    another title word.
    """
    target = _norm(expected)
    if not target:
        return False
    consumed = ""
    end = -1
    for end, char in enumerate(release or ""):
        if char.isalnum():
            consumed += char.lower()
            if len(consumed) == len(target):
                break
            if not target.startswith(consumed):
                return False
    if consumed != target:
        return False
    tail_match = re.search(r"[A-Za-z0-9]+", (release or "")[end + 1:])
    if not tail_match:
        return True
    token = tail_match.group().lower()
    return bool(
        re.fullmatch(r"(?:19|20)\d{2}", token)
        or re.fullmatch(r"(?:2160|1080|720|576|480)p", token)
        or re.fullmatch(r"s\d{1,3}e\d{1,4}", token)
        or re.fullmatch(r"\d{1,3}x\d{1,4}", token)
        or token in _TITLE_TAIL_MARKERS
    )


def _episode_match(text: str, season: int, episode: int) -> bool:
    """Exact SxxExx / x-style episode token; rejects packs and wrong episodes."""
    match = re.search(
        rf"(?<![A-Za-z0-9])(?:S0*{season}[^A-Za-z0-9]*E0*{episode}|"
        rf"0*{season}x0*{episode})(?!\d)", text or "", re.I)
    if not match:
        return False
    tail = (text or "")[match.end():]
    # Reject ranges and multi-episode bundles beginning with the requested
    # episode.  They are valid content but expensive pack imports, not the
    # exact episode this latency-sensitive lane asked for.
    if re.match(
            r"(?:[\s._]*(?:-|to|through)[\s._]*(?:S\d{1,3})?E?\d{1,3}\b|"
            r"[\s._-]*E\d{1,4}\b)", tail, re.I):
        return False
    return True


async def search(media: str, media_id: str) -> list[dict]:
    """All indexers in parallel → title-checked, deduped releases, best-quality
    first. The title check is load-bearing: indexers routinely return unrelated
    releases for an imdb query (a different film called 'The Rescue', a random
    2160p of another show), and a wrong-title release would mount, probe OK, and
    play — the wrong content. A release must start with the show/film's own
    (English or original) title to survive."""
    parts = media_id.split(":")
    imdb = parts[0].lstrip("t")
    if media == "movie":
        params = {"t": "movie", "imdbid": imdb}
    else:
        if len(parts) != 3:
            return []
        params = {"t": "tvsearch", "imdbid": imdb,
                  "season": parts[1], "ep": parts[2]}
    results, expected = await asyncio.gather(
        asyncio.gather(*(_search_one(n, b, k, params) for n, b, k in INDEXERS)),
        _expected_info(media, media_id))
    expected_titles, expected_year = expected
    # Wrong content is worse than no direct-usenet result. If authoritative
    # title metadata is unavailable, leave this optional lane out for now.
    if not expected_titles:
        logger.info(f"nzb search {media_id}: no authoritative title; skipping lane")
        return []
    releases, seen, dropped, suppressed = [], {}, 0, 0
    for lst in results:
        for it in lst:
            nt = _norm(it["title"])
            if not any(_release_title_match(it["title"], title)
                       for title in expected_titles):
                dropped += 1
                continue
            if media != "movie" and not _episode_match(
                    it["title"], int(parts[1]), int(parts[2])):
                dropped += 1
                continue
            if expected_year:
                years = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)",
                                                    it["title"])}
                if years and expected_year not in years:
                    dropped += 1
                    continue
            if not _mountable_release(it["title"]):
                dropped += 1
                continue
            key = usenet_health.release_key(it["title"], it["size"])
            if key and usenet_health.should_skip(key):
                suppressed += 1
                continue
            # Exact full-title + exact-size identity.  Preserve every offering
            # indexer so alternate downloads and learned attribution stay true.
            dedup = key or f"raw:{nt}:{it['size']}"
            offer = {"indexer": it["indexer"], "link": it["link"]}
            if dedup in seen:
                if offer not in seen[dedup]["offers"]:
                    seen[dedup]["offers"].append(offer)
                continue
            release = {"title": it["title"], "size": it["size"],
                       "release_key": key, "offers": [offer]}
            seen[dedup] = release
            releases.append(release)
    if dropped:
        logger.info(f"nzb search {media_id}: dropped {dropped} wrong-title results")
    if suppressed:
        logger.info(f"nzb search {media_id}: skipped {suppressed} known-bad/cooling results")
    releases.sort(key=_priority, reverse=True)
    return releases


_RES_ORDER = [(re.compile(r"2160p|\b4k\b|\buhd\b", re.I), 3),
              (re.compile(r"1080p", re.I), 2),
              (re.compile(r"720p", re.I), 1)]
_JUNK_RE = re.compile(
    r"\bsample\b|\.(?:iso|img|exe)\b|\bbdmv\b|\b3d\b|half-?sbs|full-?sbs|"
    r"\bh-?sbs\b|upscal|\bblu-?ray[\s._-]?(?:disc|untouched)\b|"
    r"\bcomplete\b.*\b(?:uhd|blu-?ray)\b|"
    r"\b(?:uhd|blu-?ray)\b.*\bcomplete\b|\bDV\b.*\bno.?fallback\b",
    re.I,
)
_DV_TITLE_RE = re.compile(r"\b(?:dv|dovi|dolby[\s._-]?vision)\b", re.I)
_HDR_FALLBACK_RE = re.compile(r"hdr10\+?|\bhdr\b", re.I)


def _mountable_release(title: str) -> bool:
    """Cheap format rejects before a release consumes scarce nzbdav slots."""
    if _JUNK_RE.search(title or ""):
        return False
    return not (_DV_TITLE_RE.search(title or "")
                and not _HDR_FALLBACK_RE.search(title or ""))


def _quality(r: dict) -> tuple:
    res = next((v for rx, v in _RES_ORDER if rx.search(r["title"])), 0)
    return (0 if _JUNK_RE.search(r["title"]) else 1, res, r["size"])


def _offer_score(release: dict) -> float:
    offers = release.get("offers") or []
    return max((usenet_health.indexer_score(o.get("indexer", ""))
                for o in offers), default=0.5)


def _priority(release: dict) -> tuple:
    """Quality first, then known-good release/indexer evidence, then size."""
    clean, res, size = _quality(release)
    state = usenet_health.status(release.get("release_key") or "")
    known_good = 1 if state.get("successes", 0) else 0
    return clean, res, known_good, _offer_score(release), size


def _select_releases(releases: list[dict], limit: int,
                     media: str = "movie") -> list[dict]:
    """Build a quality/reliability wave without queueing only giant remuxes.

    The first slots are delivery-sized 1080p and 4K candidates, because either
    can become the fast picker's verified answer.  The largest/highest-ranked
    remuxes still join the same wave for the slow/background picker.  One slot
    remains exploratory so indexer reputation can learn instead of becoming a
    permanent popularity loop.
    """
    if limit <= 0 or not releases:
        return []
    ranked = sorted(releases, key=_priority, reverse=True)
    target_4k, target_1080 = (
        (MOVIE_TARGET_4K, MOVIE_TARGET_1080)
        if media == "movie" else (TV_TARGET_4K, TV_TARGET_1080))
    selected: list[dict] = []
    chosen: set[object] = set()

    def identity(r: dict) -> object:
        return r.get("release_key") or id(r)

    def add(r: dict | None) -> None:
        if r is not None and len(selected) < limit and identity(r) not in chosen:
            selected.append(r)
            chosen.add(identity(r))

    def known_good(r: dict) -> int:
        state = usenet_health.status(r.get("release_key") or "")
        return 1 if state.get("successes", 0) else 0

    def tier_pool(resolution: int) -> list[dict]:
        return [r for r in ranked if _quality(r)[:2] == (1, resolution)
                and identity(r) not in chosen]

    def delivery_pick(resolution: int, target: int) -> dict | None:
        pool = tier_pool(resolution)
        if not pool:
            return None
        return max(pool, key=lambda r: (
            known_good(r),
            -abs(int(r.get("size") or 0) - target),
            _offer_score(r),
            r.get("release_key") or "",
        ))

    # A proven release wins the very first NNTP slot.  Otherwise a manageable
    # 1080p starts first, then a manageable 4K; both count as high quality.
    add(next((r for r in ranked if known_good(r)), None))
    add(delivery_pick(2, target_1080))
    add(delivery_pick(3, target_4k))
    add(next(iter(tier_pool(3)), None))       # best-quality remux
    add(next(iter(tier_pool(2)), None))       # best remaining 1080p

    # Leave the final slot for low-sample exploration when possible.
    reserve = 1 if limit >= 3 else 0
    for r in ranked:
        if len(selected) >= limit - reserve:
            break
        add(r)

    remaining = [r for r in ranked if identity(r) not in chosen
                 and _quality(r)[0] and _quality(r)[1] >= 2]
    if remaining and len(selected) < limit:
        def explore_key(r: dict) -> tuple:
            samples = min((usenet_health.indexer_samples(o.get("indexer", ""))
                           for o in r.get("offers") or []), default=0)
            target = target_4k if _quality(r)[1] >= 3 else target_1080
            return (-samples, _quality(r)[1],
                    -abs(int(r.get("size") or 0) - target),
                    _offer_score(r), r.get("release_key") or "")

        add(max(remaining, key=explore_key))

    for r in ranked:
        add(r)
        if len(selected) >= limit:
            break
    return selected


# ── nzbdav mounting ──────────────────────────────────────────────────────────

def _slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]+", ".", title).strip(".")[:110] or "release"


def _release_indexers(release: dict) -> list[str]:
    return list(dict.fromkeys(
        o.get("indexer", "") for o in release.get("offers") or []
        if o.get("indexer")))


def _evidence_token(value: str) -> str:
    """Stable correlation token without retaining a credentialed URL/path."""
    return hashlib.sha256((value or "").encode()).hexdigest()[:20]


def _exception_failure(exc: Exception) -> tuple[str, str]:
    """Credential-safe structured reason plus the exact exception/status shape."""
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"http-{status}", f"{name} HTTP {status}"
    return ("timeout" if "timeout" in name.lower() else "transport", name)


def _record_failure_sample(
        release: dict, *, stage: str, decision: str, reason: str, detail: str,
        evidence: str, seen: set[str] | None = None,
        indexers: list[str] | None = None) -> None:
    """Write one rich failure shape, deduped within a mount attempt.

    ``evidence`` is stable across retries so telemetry's stored evidence hash can
    group the same failure shape.  The local ``seen`` set prevents a WebDAV poll
    from writing the identical timeout every 700 ms.
    """
    identity = f"{stage}\0{decision}\0{reason}\0{evidence}"
    if seen is not None:
        if identity in seen:
            return
        seen.add(identity)
    telemetry.record_usenet_failure(
        release_key=release.get("release_key") or "",
        label=release.get("title") or "",
        indexers=indexers if indexers is not None else _release_indexers(release),
        stage=stage, decision=decision, reason=reason, detail=detail,
        evidence_id=f"{release.get('release_key') or ''}:{identity}",
    )


async def _dav_list(path: str, release: dict | None = None,
                    failure_seen: set[str] | None = None,
                    ) -> list[tuple[str, int]] | None:
    """PROPFIND depth-1 → [(href_path, size)], or None if the dir doesn't exist."""
    try:
        r = await _client.request(
            "PROPFIND", NZBDAV_URL + quote(path), auth=_dav_auth(),
            headers={"Depth": "1"}, timeout=10)
    except Exception as exc:
        if release is not None:
            reason, detail = _exception_failure(exc)
            _record_failure_sample(
                release, stage="nzbdav-dav", decision="transient",
                reason=reason, detail=detail,
                evidence=f"list:{_evidence_token(path)}:{detail}",
                seen=failure_seen)
        return None
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        if release is not None:
            _record_failure_sample(
                release, stage="nzbdav-dav", decision="transient",
                reason=f"http-{r.status_code}", detail=f"HTTP {r.status_code}",
                evidence=f"list:{_evidence_token(path)}:http-{r.status_code}",
                seen=failure_seen)
        return None
    out = []
    try:
        root = ET.fromstring(r.content)
        for resp in root.iter("{DAV:}response"):
            href = resp.findtext("{DAV:}href") or ""
            href = re.sub(r"^https?://[^/]+", "", href)
            size = 0
            ln = resp.find(".//{DAV:}getcontentlength")
            if ln is not None and (ln.text or "").isdigit():
                size = int(ln.text)
            out.append((href, size))
    except ET.ParseError as exc:
        if release is not None:
            _record_failure_sample(
                release, stage="nzbdav-dav", decision="transient",
                reason="invalid-xml", detail=type(exc).__name__,
                evidence=f"list:{_evidence_token(path)}:invalid-xml",
                seen=failure_seen)
        return None
    return out


_IMPORT_HARD_RE = re.compile(
    r"missing.*article|article.*missing|missing.*segment|segment.*missing|"
    r"\barticle\b|blocklist|health.?check.*fail|no healthy|"
    r"not enough.*article",
    re.I,
)
_IMPORT_TRANSIENT_RE = re.compile(
    r"auth|login|connection|too many|limit|timeout|timed out|network|"
    r"temporar|unavailable|nntp\s*50[023]",
    re.I,
)


def _history_failure_class(message: str) -> tuple[str, str]:
    """Sanitize an nzbdav failure into health policy enums."""
    if _IMPORT_TRANSIENT_RE.search(message or ""):
        return "transient", "transport"
    if _IMPORT_HARD_RE.search(message or ""):
        return "hard", "missing-articles"
    # Unknown import errors are cooldown-only: never permanently suppress a
    # release based on an unrecognized provider/server condition.
    return "transient", "transport"


async def _history_failure(job: str) -> tuple[str, str, str, str] | None:
    """Return a safe failure class for this exact nzbdav job, if finalized."""
    if not NZBDAV_API_KEY:
        return None
    try:
        common = {"output": "json", "pageSize": 100,
                  "apikey": NZBDAV_API_KEY}
        queue_response = await _client.get(
            f"{NZBDAV_URL}/api", params={**common, "mode": "queue"},
            timeout=10)
        if queue_response.status_code == 200:
            queued = ((queue_response.json().get("queue") or {}).get("slots")
                      or [])
            if any(str(s.get("filename") or "").removesuffix(".nzb") == job
                   for s in queued):
                return None
        response = await _client.get(
            f"{NZBDAV_URL}/api",
            params={**common, "mode": "history"},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        slots = ((response.json().get("history") or {}).get("slots") or [])
        slot = next((s for s in slots if s.get("name") == job), None)
        if not slot or str(slot.get("status", "")).lower() != "failed":
            return None
        detail = str(slot.get("fail_message") or "")
        kind, reason = _history_failure_class(detail)
        evidence = str(slot.get("nzo_id") or "")
        return kind, reason, evidence, detail
    except Exception:
        # Best-effort only; WebDAV polling remains the compatibility fallback.
        return None


def _record_import_failure(release: dict, kind: str, reason: str,
                           evidence: str = "", detail: str = "",
                           stage: str = "nzbdav-import") -> None:
    key = release.get("release_key") or ""
    if not key:
        return
    # nzbdav's immutable history id makes the same failed import idempotent
    # forever.  Only a genuinely new import (new nzo_id) can become strike two.
    if evidence:
        attempt = f"import:{key}:{evidence}"
    else:
        seconds = HARD_BUCKET if kind == "hard" else TRANSIENT_BUCKET
        bucket = int(time.time() // max(seconds, 1))
        attempt = f"import:{key}:{bucket}"
    indexers = [o.get("indexer", "") for o in release.get("offers") or []]
    accepted = usenet_health.record_failure(
        key, release["title"],
        indexers,
        reason, attempt)
    if accepted:
        telemetry.record_usenet_failure(
            release_key=key, label=release["title"], indexers=indexers,
            stage=stage, decision=kind, reason=reason,
            detail=detail or reason, evidence_id=evidence)


_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".wmv")


def _pick_video(entries: list[tuple[str, int]],
                episode: tuple[int, int] | None = None) -> tuple[str, int] | None:
    vids = [(h, s) for h, s in entries if h.lower().endswith(_VIDEO_EXT)]
    if episode:
        # The parent job directory itself contains the requested episode token;
        # matching the full href would therefore let a wrong-episode basename
        # pass.  Only the actual mounted filename is authoritative here.
        vids = [(h, s) for h, s in vids
                if _episode_match(unquote(h).rsplit("/", 1)[-1], *episode)]
    return max(vids, key=lambda v: v[1]) if vids else None


def _missing_content_reason(entries: list[tuple[str, int]],
                            episode: tuple[int, int] | None,
                            directory_seen: bool) -> str:
    """Classify a completed mount that did not yield the requested video."""
    if episode and _pick_video(entries):
        return "wrong-episode"
    if directory_seen:
        return "not-video"
    return "never-appeared"


def _content_evidence(entries: list[tuple[str, int]]) -> str:
    stable = "\n".join(
        f"{unquote(h).rsplit('/', 1)[-1]}\0{size}"
        for h, size in sorted(entries))
    return _evidence_token(stable)


async def _mount(release: dict, cat: str, delay: float = 0,
                 episode: tuple[int, int] | None = None) -> dict | None:
    """Ensure this release is mounted in nzbdav; return a stream dict or None.
    Reuses an existing mount instantly; otherwise NZB-fetch → watch-folder PUT →
    poll for the content dir (nzbdav mounts in ~2s; missing-article releases
    that mount anyway are caught later by the picker's probe)."""
    key = release.get("release_key") or ""
    suffix = f"-{key[-8:]}" if key else ""
    job = _slug(release["title"]) + suffix
    dir_path = f"/content/{cat}/{job}"
    failure_seen: set[str] = set()
    entries = await _dav_list(dir_path, release, failure_seen)
    directory_seen = entries is not None
    fetched_from = ""
    if entries is None:
        prior_failure = await _history_failure(job)
        if prior_failure:
            kind, reason, evidence, detail = prior_failure
            if kind == "hard":
                _record_import_failure(
                    release, kind, reason, evidence, detail)
                logger.info(f"nzb mount {job[:40]}: prior import {reason}")
                return None
        if delay > 0:
            await asyncio.sleep(delay)
        # Fetch the NZB, falling through the alternate indexer links for the
        # same release — download endpoints individually 403/503 at times.
        content = None
        offers = sorted(release.get("offers") or [],
                        key=lambda o: usenet_health.indexer_score(
                            o.get("indexer", "")), reverse=True)
        for offer in offers:
            indexer, link = offer.get("indexer", ""), offer.get("link", "")
            try:
                nzb = await _client.get(link, timeout=30)
                nzb.raise_for_status()
                if b"<nzb" in nzb.content[:2000]:
                    content = nzb.content
                    fetched_from = indexer
                    usenet_health.record_fetch(indexer, True)
                    break
                usenet_health.record_fetch(indexer, False)
                _record_failure_sample(
                    release, stage="nzb-fetch", decision="transient",
                    reason="non-nzb",
                    detail=f"HTTP {nzb.status_code} response was not NZB XML",
                    evidence=(f"fetch:{indexer}:{_evidence_token(link)}:non-nzb"),
                    seen=failure_seen, indexers=[indexer])
                logger.info(f"nzb mount {job[:40]}: link returned non-NZB")
            except Exception as e:
                usenet_health.record_fetch(indexer, False)
                status = getattr(getattr(e, "response", None), "status_code", None)
                detail = f" HTTP {status}" if status else ""
                reason, shape = _exception_failure(e)
                _record_failure_sample(
                    release, stage="nzb-fetch", decision="transient",
                    reason=reason, detail=shape,
                    evidence=f"fetch:{indexer}:{_evidence_token(link)}:{shape}",
                    seen=failure_seen, indexers=[indexer])
                logger.info(f"nzb mount {job[:40]}: {type(e).__name__}{detail}")
        if content is None:
            return None
        for attempt in (1, 2):        # nzbdav PUT can 500 transiently under load
            try:
                put = await _client.put(
                    f"{NZBDAV_URL}/nzbs/{cat}/{quote(job)}.nzb",
                    content=content, auth=_dav_auth(), timeout=20)
                if put.status_code < 400:
                    break
                _record_failure_sample(
                    release, stage="nzbdav-put", decision="transient",
                    reason=f"http-{put.status_code}",
                    detail=f"HTTP {put.status_code}",
                    evidence=f"put:{_evidence_token(job)}:http-{put.status_code}",
                    seen=failure_seen)
                logger.info(f"nzb mount {job[:40]}: PUT {put.status_code}"
                            f" (attempt {attempt})")
            except Exception as e:
                reason, shape = _exception_failure(e)
                _record_failure_sample(
                    release, stage="nzbdav-put", decision="transient",
                    reason=reason, detail=shape,
                    evidence=f"put:{_evidence_token(job)}:{shape}",
                    seen=failure_seen)
                logger.info(f"nzb mount {job[:40]}: PUT {type(e).__name__}")
            if attempt == 2:
                return None
            await asyncio.sleep(2)
    video = _pick_video(entries or [], episode)
    if not video:
        deadline = time.monotonic() + MOUNT_WAIT
        # Give the just-submitted watch-folder item time to appear in queue;
        # this prevents an older transient history row for the same deterministic
        # job name from being mistaken for the current attempt.
        next_history_check = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.7)
            listed = await _dav_list(dir_path, release, failure_seen)
            if listed is not None:
                entries = listed
                directory_seen = True
            video = _pick_video(entries or [], episode)
            if video:
                break
            now = time.monotonic()
            if now >= next_history_check:
                next_history_check = now + 3.0
                failure = await _history_failure(job)
                if failure:
                    kind, reason, evidence, detail = failure
                    _record_import_failure(
                        release, kind, reason, evidence, detail)
                    logger.info(f"nzb mount {job[:40]}: import {reason}")
                    return None
    if not video:
        failure_reason = _missing_content_reason(
            entries or [], episode, directory_seen)
        failure_text = {
            "wrong-episode": "no matching video",
            "not-video": "mounted content was not video",
            "never-appeared": "never appeared",
        }[failure_reason]
        logger.info(f"nzb mount {job[:40]}: "
                    f"{failure_text}")
        if key and failure_reason in ("wrong-episode", "not-video"):
            detail = (f"mounted directory contained {len(entries or [])} entries; "
                      f"no {'requested episode' if failure_reason == 'wrong-episode' else 'supported video'}")
            evidence = (f"content:{key}:{failure_reason}:"
                        f"{_content_evidence(entries or [])}")
            _record_import_failure(
                release, "hard", failure_reason, evidence, detail,
                stage="nzbdav-content")
        elif key:
            seconds = TRANSIENT_BUCKET
            bucket = int(time.time() // max(seconds, 1))
            _record_import_failure(
                release, "transient", "never-appeared",
                f"mount:{key}:{bucket}",
                f"content directory exposed no video within {MOUNT_WAIT:g}s",
                stage="nzbdav-mount")
        return None
    href, size = video
    size = size or release["size"]
    fname = href.rsplit("/", 1)[-1]
    gb = f"{size / 1e9:.2f} GB" if size else "?"
    all_indexers = list(dict.fromkeys(
        o.get("indexer", "") for o in release.get("offers") or []
        if o.get("indexer")))
    source = fetched_from or (all_indexers[0] if all_indexers else "unknown")
    return {
        "name": f"NZB\n{release['title'][:60]}",
        "description": (f"Source: {source}\nSize: {gb}\n"
                        f"{release['title']}"),
        "url": _stream_base() + quote(href),
        "behaviorHints": {"filename": fname},
        "_nzb_release_key": key,
        "_nzb_label": release["title"][:180],
        "_nzb_indexer": source,
        "_nzb_indexers": all_indexers,
    }


async def _mount_limited(release: dict, cat: str, delay: float = 0,
                         episode: tuple[int, int] | None = None) -> dict | None:
    """Globally stage imports to the number of nzbdav NNTP work slots."""
    if delay > 0:
        await asyncio.sleep(delay)
    mount_task: asyncio.Task | None = None
    try:
        async with _import_slots:
            mount_task = asyncio.create_task(
                _mount(release, cat, 0, episode))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(mount_task), IMPORT_SLOT_HOLD)
            except asyncio.TimeoutError:
                # The import is already submitted.  Let its WebDAV poll keep
                # running, but allow the next release/title to submit rather
                # than creating an unbounded queue immediately.
                pass
        return await mount_task
    except asyncio.CancelledError:
        if mount_task is not None and not mount_task.done():
            mount_task.cancel()
            await asyncio.gather(mount_task, return_exceptions=True)
        raise


TRANSIENT_BUCKET = float(os.environ.get("NZB_MOUNT_FAILURE_BUCKET", "1800"))
HARD_BUCKET = float(os.environ.get("NZB_CONTENT_FAILURE_BUCKET", "86400"))
_mount_background: set[asyncio.Task] = set()
_mount_events: dict[tuple[str, str], asyncio.Event] = {}
_mount_outputs: dict[tuple[str, str], list[dict]] = {}


def _refresh_out(out: list[dict], results: dict[int, dict]) -> None:
    # Mutate the shared list in place: app.sources caches this exact object, so
    # mounts that finish after the foreground return become visible to the slow
    # picker/retry without launching another indexer search.
    out[:] = [results[i] for i in sorted(results)]


async def _finish_mounts(pending: dict[asyncio.Task, int], results: dict[int, dict],
                         out: list[dict], media_id: str, total: int,
                         started: float, done_event: asyncio.Event) -> None:
    try:
        while pending:
            done, _ = await asyncio.wait(set(pending),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                idx = pending.pop(task)
                try:
                    mounted = task.result()
                except Exception:
                    mounted = None
                if mounted:
                    results[idx] = mounted
            _refresh_out(out, results)
        logger.info(f"nzb lane {media_id}: background mounts complete "
                    f"{len(out)}/{total} in {time.monotonic() - started:.1f}s")
    except asyncio.CancelledError:
        for task in pending:
            task.cancel()
        raise
    finally:
        done_event.set()


async def wait_complete(media: str, media_id: str, wait: float) -> list[dict] | None:
    """Wait for a lane's detached mount tail; used only by off-request finishers."""
    key = (media, media_id)
    event = _mount_events.get(key)
    if event is None:
        return _mount_outputs.get(key)
    try:
        await asyncio.wait_for(asyncio.shield(event.wait()), max(wait, 0.01))
    except asyncio.TimeoutError:
        pass
    return _mount_outputs.get(key)


async def wait_for_more(media: str, media_id: str, known: int,
                        wait: float) -> list[dict] | None:
    """Wait for one more progressive mount, without requiring the whole tail."""
    key = (media, media_id)
    out = _mount_outputs.get(key)
    event = _mount_events.get(key)
    if out is None or event is None:
        return out
    deadline = time.monotonic() + max(wait, 0)
    while len(out) <= known and not event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(min(0.2, max(deadline - time.monotonic(), 0.01)))
    return out


def in_progress(media: str, media_id: str) -> bool:
    event = _mount_events.get((media, media_id))
    return bool(event and not event.is_set())


async def streams(media: str, media_id: str) -> list[dict]:
    """The lane's entry point (called via app.sources like any other source):
    search all indexers, mount the top MOUNT_MAX releases concurrently, and
    return stream dicts for the ones that mounted. Quality order preserved —
    the picker's probe then decides which actually play."""
    if not enabled():
        return []
    t0 = time.monotonic()
    lane_key = (media, media_id)
    # A previous source call may have returned an empty/partial progressive list
    # while its detached nzbdav imports are still running.  Rejoin that exact
    # lane instead of enqueueing duplicate NZBs after the short negative TTL.
    existing_event = _mount_events.get(lane_key)
    existing_out = _mount_outputs.get(lane_key)
    if existing_event is not None and not existing_event.is_set():
        out = (existing_out if existing_out else
               await wait_for_more(media, media_id, 0, MOUNT_EARLY_WAIT))
        logger.info(f"nzb lane {media_id}: rejoined in-progress mounts, "
                    f"returning {len(out or [])}")
        return out if out is not None else []
    done_event = asyncio.Event()
    _mount_events[lane_key] = done_event
    releases = await search(media, media_id)
    if not releases:
        out: list[dict] = []
        _mount_outputs[lane_key] = out
        done_event.set()
        return []
    cat = "movies" if media == "movie" else "tv"
    episode = ((int(media_id.split(":")[1]), int(media_id.split(":")[2]))
               if media != "movie" else None)
    top = _select_releases(releases, MOUNT_MAX, media)
    order = []
    for release in top:
        best = max((o.get("indexer", "") for o in release.get("offers") or []),
                   key=usenet_health.indexer_score, default="")
        order.append(f"{best}:{usenet_health.indexer_score(best):.2f}")
    if order:
        logger.info(f"nzb lane {media_id}: mount priority {' > '.join(order)}")
    tasks = {asyncio.create_task(
                 _mount_limited(r, cat, i * MOUNT_STAGGER, episode)): i
             for i, r in enumerate(top)}
    pending = dict(tasks)
    results: dict[int, dict] = {}
    out: list[dict] = []
    _mount_outputs[lane_key] = out
    deadline = time.monotonic() + MOUNT_EARLY_WAIT
    want = max(1, MOUNT_RETURN_WANT)
    while pending and len(results) < want and time.monotonic() < deadline:
        done, _ = await asyncio.wait(
            set(pending), timeout=max(0, deadline - time.monotonic()),
            return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for task in done:
            idx = pending.pop(task)
            try:
                mounted = task.result()
            except Exception:
                mounted = None
            if mounted:
                results[idx] = mounted
        _refresh_out(out, results)
    if pending:
        finisher = asyncio.create_task(
            _finish_mounts(pending, results, out, media_id, len(top), t0,
                           done_event))
        _mount_background.add(finisher)
        finisher.add_done_callback(_mount_background.discard)
    else:
        done_event.set()
    if len(_mount_events) > 500:
        old = next(iter(_mount_events))
        if _mount_events[old].is_set():
            _mount_events.pop(old, None)
            _mount_outputs.pop(old, None)
    logger.info(f"nzb lane {media_id}: {len(releases)} eligible releases from "
                f"{len(INDEXERS)} indexers, returning {len(out)}/{len(top)} now "
                f"in {time.monotonic() - t0:.1f}s")
    return out
