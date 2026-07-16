"""Direct usenet lane: search the household's Newznab indexers in parallel and
turn the promising releases into streamable URLs through nzbdav.

Why this exists: generic upstream Usenet addons answer slowly and their health
tags proved unreliable — only ~40% of releases actually play, but the ones that
do are often the best available quality. This direct lane makes usenet
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
import secrets
import time
import unicodedata
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

class _SearchRows(list):
    """List-compatible search result carrying success/failure separately."""

    def __init__(self, values=(), *, ok: bool = True, detail: str = ""):
        super().__init__(values)
        self.ok = ok
        self.detail = detail


class _ReleaseRows(list):
    """List-compatible aggregate used by the lane outcome diagnostics."""

    search_ok = 0
    search_failed = 0


def _local_name(tag: object) -> str:
    value = str(tag or "")
    return value.rsplit("}", 1)[-1].strip().lower()


def _child_text(node: ET.Element, name: str) -> str:
    wanted = name.lower()
    for child in node:
        if _local_name(child.tag) == wanted:
            return (child.text or "").strip()
    return ""


_NEWZNAB_IDENTITY_ATTRS = {
    "imdb": "imdb", "imdbid": "imdb", "imdb_id": "imdb",
    "season": "season", "tvseason": "season",
    "episode": "episode", "ep": "episode", "tvepisode": "episode",
}


def _parse_items_diagnostic(text: str) -> tuple[list[dict], tuple[str, str] | None]:
    """Parse namespace-tolerant Newznab XML and retain safe error shapes."""
    try:
        root = ET.fromstring((text or "").strip())
    except ET.ParseError as exc:
        # Line/column and parser class are useful for grouping malformed feeds;
        # the raw body may contain reflected credentials and is never retained.
        line, column = getattr(exc, "position", (0, 0))
        return [], ("invalid-xml", f"ParseError line={line} column={column}")

    for node in root.iter():
        if _local_name(node.tag) != "error":
            continue
        code = re.sub(r"[^A-Za-z0-9_.-]", "", node.get("code") or "")[:40]
        description = telemetry.sanitize_failure_detail(
            node.get("description") or (node.text or "").strip(), 500)
        detail = f"Newznab error code={code or 'unknown'}"
        if description:
            detail += f" description={description}"
        return [], ("newznab-error", detail)

    out = []
    for item in (node for node in root.iter()
                 if _local_name(node.tag) == "item"):
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        size = 0
        identity_attrs: dict[str, list[str]] = {}
        enclosure = next((node for node in item
                          if _local_name(node.tag) == "enclosure"), None)
        if enclosure is not None:
            link = (enclosure.get("url") or link).strip()
            try:
                size = int((enclosure.get("length") or "0").strip())
            except ValueError:
                size = 0
        for attr in item.iter():
            if _local_name(attr.tag) != "attr":
                continue
            attr_name = (attr.get("name") or "").strip().lower()
            attr_value = (attr.get("value") or "").strip()
            if attr_name == "size":
                try:
                    size = int(attr_value or "0")
                except ValueError:
                    pass
            identity_name = _NEWZNAB_IDENTITY_ATTRS.get(attr_name)
            if identity_name and attr_value:
                values = identity_attrs.setdefault(identity_name, [])
                bounded = attr_value[:80]
                if bounded not in values and len(values) < 16:
                    values.append(bounded)
        if title and link:
            row = {"title": title, "size": size, "link": link}
            if identity_attrs:
                # Private, bounded semantic evidence only.  Raw categories and
                # arbitrary attrs are deliberately not retained.
                row["_newznab_identity_attrs"] = identity_attrs
            out.append(row)
    return out, None


def _parse_items(text: str) -> list[dict]:
    """Newznab XML → [{title, size, link}]. XML is the one format every indexer
    speaks (JSON support varies), so parse that only."""
    return _parse_items_diagnostic(text)[0]


def _record_indexer_failure(name: str, *, stage: str, reason: str,
                            detail: str) -> None:
    safe = telemetry.sanitize_failure_detail(detail)
    telemetry.record_usenet_failure(
        indexers=[name], stage=stage, decision="transient", reason=reason,
        detail=safe, evidence_id=f"{name}:{stage}:{reason}:{safe}")


def _nzb_payload_issue(content: bytes) -> tuple[str, str] | None:
    """Return a safe, structured reason when a fetch is not valid NZB XML."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (0, 0))
        return "invalid-nzb-xml", f"ParseError line={line} column={column}"
    if _local_name(root.tag) == "nzb":
        return None
    error = next((node for node in root.iter()
                  if _local_name(node.tag) == "error"), None)
    if error is not None:
        code = re.sub(r"[^A-Za-z0-9_.-]", "", error.get("code") or "")[:40]
        description = telemetry.sanitize_failure_detail(
            error.get("description") or (error.text or "").strip(), 500)
        detail = f"Newznab error code={code or 'unknown'}"
        if description:
            detail += f" description={description}"
        return "newznab-error", detail
    tag = re.sub(r"[^a-z0-9_.-]", "", _local_name(root.tag))[:40]
    return "non-nzb", f"unexpected XML root={tag or 'unknown'}"


async def _search_one(name: str, base: str, key: str, params: dict) -> list[dict]:
    t0 = time.monotonic()
    try:
        r = await _client.get(base, params={**params, "apikey": key},
                              timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        items, issue = _parse_items_diagnostic(r.text)
        if issue:
            reason, detail = issue
            usenet_health.record_search(name, False, results=0,
                                        latency=time.monotonic() - t0)
            _record_indexer_failure(
                name, stage="newznab-search", reason=reason, detail=detail)
            logger.info(f"nzb search {name} failed: {detail[:120]}")
            return _SearchRows(ok=False, detail=detail)
        for it in items:
            it["indexer"] = name
        usenet_health.record_search(name, True, results=len(items),
                                    latency=time.monotonic() - t0)
        logger.info(f"nzb search {name}: {len(items)} results")
        return _SearchRows(items, ok=True)
    except Exception as e:
        usenet_health.record_search(name, False,
                                    latency=time.monotonic() - t0)
        # httpx exceptions can stringify their request URL (including apikey).
        # Log only the exception class/status, never the URL-bearing message.
        status = getattr(getattr(e, "response", None), "status_code", None)
        detail = f" HTTP {status}" if status else ""
        reason, shape = _exception_failure(e)
        _record_indexer_failure(
            name, stage="newznab-search", reason=reason, detail=shape)
        logger.info(f"nzb search {name} failed: {type(e).__name__}{detail}")
        return _SearchRows(ok=False, detail=shape)


def _fold(t: str) -> str:
    """Scene-name folding: strip diacritics and spell ``&``/``+`` out.

    Authoritative metadata carries accents and ampersands ("Amélie",
    "Fast & Furious") while release names are ASCII with "and" spelled out.
    Without folding, every legitimate release for such a title fails the
    title match and the whole lane goes dark for it.
    """
    t = unicodedata.normalize("NFKD", t or "").casefold()
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.replace("&", "and").replace("+", "and")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(t))


_IMDB_VALUE_RE = re.compile(r"^(?:tt)?(\d+)$", re.I)


def _canonical_imdb(value: object) -> str:
    match = _IMDB_VALUE_RE.fullmatch(str(value or "").strip())
    if not match:
        return ""
    digits = match.group(1).lstrip("0") or "0"
    return f"tt{digits}"


def _identity_number(value: object, prefix: str) -> int | None:
    raw = str(value or "").strip()
    match = re.fullmatch(rf"(?:{prefix})?0*(\d+)", raw, re.I)
    return int(match.group(1)) if match else None


def _identity_attr_values(attrs: dict, key: str) -> list[object]:
    values = attrs.get(key) or []
    if isinstance(values, (str, int)):
        return [values]
    if isinstance(values, (list, tuple, set)):
        return list(values)[:16]
    return []


def _newznab_identity_evidence(
        item: dict, media: str, media_id: str,
        ) -> tuple[bool, bool, list[str], str]:
    """Validate optional Newznab semantic attrs against the exact request.

    Returns ``(contradiction, fully_trusted, evidence, reason)``.  Malformed
    optional attrs are ignored rather than treated as proof; any *valid* value
    which contradicts the request rejects the item even when another value
    happens to match it.
    """
    attrs = item.get("_newznab_identity_attrs") or {}
    if not isinstance(attrs, dict):
        return False, False, [], ""
    parts = media_id.split(":")
    expected_imdb = _canonical_imdb(parts[0])
    evidence: list[str] = []

    imdb_values = {_canonical_imdb(v)
                   for v in _identity_attr_values(attrs, "imdb")}
    imdb_values.discard("")
    if imdb_values and imdb_values != {expected_imdb}:
        return True, False, ["newznab-imdb-mismatch"], "wrong-imdb"
    imdb_exact = bool(expected_imdb and imdb_values == {expected_imdb})
    if imdb_exact:
        evidence.append("newznab-imdb")

    season_values = {_identity_number(v, "s")
                     for v in _identity_attr_values(attrs, "season")}
    season_values.discard(None)
    episode_values = {_identity_number(v, "e")
                      for v in _identity_attr_values(attrs, "episode")}
    episode_values.discard(None)
    if media == "movie":
        if season_values or episode_values:
            return True, False, evidence + ["newznab-tv-attrs"], "wrong-media"
        return False, imdb_exact, evidence, ""

    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return True, False, evidence, "wrong-episode"
    expected_season, expected_episode = int(parts[1]), int(parts[2])
    if season_values and season_values != {expected_season}:
        return True, False, evidence + ["newznab-season-mismatch"], "wrong-season"
    if episode_values and episode_values != {expected_episode}:
        return True, False, evidence + ["newznab-episode-mismatch"], "wrong-episode"
    season_exact = season_values == {expected_season}
    episode_exact = episode_values == {expected_episode}
    if season_exact:
        evidence.append("newznab-season")
    if episode_exact:
        evidence.append("newznab-episode")
    return False, bool(imdb_exact and season_exact and episode_exact), evidence, ""


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
    "dual", "season", "seasons",
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
    # Fold the release side too: the consuming loop below compares character
    # by character, so an accented release ("Amélie") must decompose the same
    # way the expected title did.
    release = _fold(release or "")
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
        # Bare season token: "Show.S01.COMPLETE" packs and "Show.S01.E02"
        # spaced episode styles are this title, not a longer different one.
        or re.fullmatch(r"s\d{1,3}", token)
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


def _episode_range_match(text: str, season: int, episode: int) -> bool:
    """Explicit same-season episode range spanning the requested episode.

    Kids' shows ship as two-segment bundles ("S13E07-E08", AMZN's
    "S13E07-08") — for those the bundle IS the broadcast episode, and it is
    frequently the only English release of a segment."""
    for match in re.finditer(
            rf"(?<![A-Za-z0-9])S0*{season}[\s._-]*E0*(\d+)[\s._-]*"
            rf"(?:-|to|thru|through)[\s._-]*(?:S0*{season})?[\s._-]*E?0*(\d+)"
            r"(?!\d)", text or "", re.I):
        if int(match.group(1)) <= episode <= int(match.group(2)):
            return True
    return False


def _season_pack_match(text: str, season: int, episode: int) -> bool:
    """Whether a title names a season bundle that should contain the episode.

    Packs are a last resort: mounting one is slower and its per-file episode
    check (``_pick_video_identity``) is the real safety gate, so this only has
    to recognize plausible containers — a bare season token, "Season N", or an
    explicit episode range spanning the request.
    """
    t = text or ""
    if _episode_range_match(t, season, episode):
        return True
    if re.search(rf"(?<![A-Za-z0-9])(?:S|Season[\s._-]*)0*{season}"
                 r"(?![A-Za-z0-9])", t, re.I):
        # A same-season episode token means this is a single episode (or a
        # bundle already rejected above), not a whole-season container.
        return not any(s == season for s, _ in _episode_tokens(t))
    return False


def _query_text(title: str) -> str:
    """Free-text query form of an authoritative title: diacritics folded,
    apostrophes dropped, sentence punctuation spaced — the shapes indexer
    full-text search actually stores."""
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("'", "").replace("’", "")
    t = re.sub(r"[;:!?~]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _text_queries(media: str, parts: list[str], titles: list[str],
                  year: int | None) -> list[str]:
    queries = []
    for title in titles[:2]:
        q = _query_text(title)
        if not re.search(r"[A-Za-z0-9]", q):
            continue
        if media == "movie":
            queries.append(f"{q} {year}" if year else q)
        else:
            queries.append(f"{q} S{int(parts[1]):02d}E{int(parts[2]):02d}")
            # Double-episode bundles ("S13E07-E08") and season packs never
            # match an exact SxxEyy text query; a season-level query recovers
            # them and the strict matchers keep everything else out.
            queries.append(f"{q} S{int(parts[1]):02d}")
    return list(dict.fromkeys(queries))


async def _text_search(media: str, parts: list[str], titles: list[str],
                       year: int | None) -> list[list[dict]]:
    """Free-text fallback when the id-scoped query yields nothing usable.

    Anime and Asian dramas have the worst IMDb-id coverage on indexers —
    releases indexed under romaji or English alternate names are invisible to
    an imdbid query.  A ``t=search`` pass with the title and episode token in
    the query recovers them; every result still runs the full strict identity
    pipeline, so breadth here cannot admit wrong content."""
    queries = _text_queries(media, parts, titles, year)
    if not queries:
        return []
    cat = "2000" if media == "movie" else "5000"
    return list(await asyncio.gather(*(
        _search_one(n, b, k, {"t": "search", "q": q, "cat": cat})
        for q in queries for n, b, k in INDEXERS)))


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
    releases = _ReleaseRows()
    releases.search_ok = sum(1 for rows in results
                             if getattr(rows, "ok", True))
    releases.search_failed = len(results) - releases.search_ok
    season_episode = ((int(parts[1]), int(parts[2]))
                      if media != "movie" else None)
    packs: list[dict] = []
    seen, dropped, attr_dropped, suppressed = {}, 0, 0, 0

    def _ingest(lst: list[dict]) -> None:
        nonlocal dropped, attr_dropped, suppressed
        for it in lst:
            nt = _norm(it["title"])
            contradiction, attrs_trusted, attr_evidence, _ = (
                _newznab_identity_evidence(it, media, media_id))
            if contradiction:
                dropped += 1
                attr_dropped += 1
                continue
            if not any(_release_title_match(it["title"], title)
                       for title in expected_titles):
                dropped += 1
                continue
            is_pack = False
            if season_episode and not _episode_match(it["title"],
                                                     *season_episode):
                # A whole-season container is kept aside as a last resort;
                # the per-file episode check at mount time is its safety gate.
                if _season_pack_match(it["title"], *season_episode):
                    is_pack = True
                else:
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
            # A pack is the season container: key it to the season so every
            # episode of a binge reuses one mount instead of re-importing
            # the same multi-GB NZB, and so a broken pack is suppressed for
            # the whole season it fails to serve.
            key = usenet_health.release_key(
                it["title"], it["size"], media,
                f"{parts[0]}:{parts[1]}" if is_pack else media_id)
            legacy_key = usenet_health.release_key(it["title"], it["size"])
            if key and usenet_health.should_skip(key):
                suppressed += 1
                continue
            # Exact full-title + exact-size identity.  Preserve every offering
            # indexer so alternate downloads and learned attribution stay true.
            dedup = key or f"raw:{nt}:{it['size']}"
            offer = {"indexer": it["indexer"], "link": it["link"],
                     "_nzb_identity_trusted": attrs_trusted,
                     "_nzb_identity_evidence": attr_evidence}
            if dedup in seen:
                duplicate_offer = next((o for o in seen[dedup]["offers"]
                                        if o.get("indexer") == it["indexer"]
                                        and o.get("link") == it["link"]), None)
                if duplicate_offer is None:
                    seen[dedup]["offers"].append(offer)
                else:
                    duplicate_offer["_nzb_identity_trusted"] = bool(
                        duplicate_offer.get("_nzb_identity_trusted")
                        or attrs_trusted)
                    old_evidence = list(
                        duplicate_offer.get("_nzb_identity_evidence") or [])
                    duplicate_offer["_nzb_identity_evidence"] = list(
                        dict.fromkeys(old_evidence + attr_evidence))
                if attrs_trusted:
                    seen[dedup]["_nzb_attrs_trusted"] = True
                merged = seen[dedup]["_nzb_attr_evidence"]
                merged[:] = list(dict.fromkeys(merged + attr_evidence))
                continue
            release = {"title": it["title"], "size": it["size"],
                       "release_key": key, "legacy_release_key": legacy_key,
                       "offers": [offer],
                       "_nzb_attrs_trusted": attrs_trusted,
                       "_nzb_attr_evidence": list(attr_evidence),
                       "_nzb_expected": {
                           "media": media,
                           "media_id": media_id,
                           "titles": list(expected_titles),
                           "year": expected_year,
                       }}
            if is_pack:
                release["_nzb_pack"] = True
            seen[dedup] = release
            (packs if is_pack else releases).append(release)

    def _any_fetchable(rows: list[dict]) -> bool:
        return any(usenet_health.fetch_allowed(o.get("indexer", ""))
                   for r in rows for o in r.get("offers") or [])

    for lst in results:
        _ingest(lst)
    if not _any_fetchable(releases):
        before = len(releases)
        for lst in await _text_search(media, parts, expected_titles,
                                      expected_year):
            _ingest(lst)
        if len(releases) > before:
            logger.info(f"nzb search {media_id}: id query yielded nothing "
                        f"usable; free-text fallback recovered "
                        f"{len(releases) - before} release(s)")
    if not _any_fetchable(releases) and packs:
        logger.info(f"nzb search {media_id}: no single-episode release; "
                    f"admitting {len(packs)} season pack(s) as last resort")
        releases.extend(packs)
    if dropped:
        logger.info(f"nzb search {media_id}: dropped {dropped} wrong-title results")
    if attr_dropped:
        logger.info(f"nzb search {media_id}: rejected {attr_dropped} "
                    "Newznab identity contradictions")
    if suppressed:
        logger.info(f"nzb search {media_id}: skipped {suppressed} known-bad/cooling results")
    # Persistently dead download endpoints (an indexer may search perfectly yet
    # return 403 for every NZB fetch) must not consume a mount slot forever.
    # Retain a deduplicated release when *any* alternate indexer can still
    # provide it; remove only the proven-dead offers.
    fetch_suppressed = 0
    fetchable = []
    for release in releases:
        offers = release.get("offers") or []
        allowed = [o for o in offers if usenet_health.fetch_allowed(
            o.get("indexer", ""))]
        fetch_suppressed += len(offers) - len(allowed)
        if allowed:
            release["offers"] = allowed
            fetchable.append(release)
    releases[:] = fetchable
    if fetch_suppressed:
        logger.info(f"nzb search {media_id}: suppressed {fetch_suppressed} "
                    "offers from persistently failed NZB endpoints")
    releases.sort(key=_priority, reverse=True)
    return releases


_RES_ORDER = [(re.compile(r"2160p|\b4k\b|\buhd\b", re.I), 3),
              (re.compile(r"1080p", re.I), 2),
              (re.compile(r"720p", re.I), 1)]
_JUNK_RE = re.compile(
    r"\bsample\b|\.(?:iso|img|exe)\b|\bbdmv\b|\b3d\b|half-?sbs|full-?sbs|"
    r"\bh-?sbs\b|upscal|\bblu-?ray[\s._-]?(?:disc|untouched)\b|"
    r"\bbd(?:25|50|66|100)\b|\b(?:uhd|blu-?ray)[\s._-]*iso\b|"
    r"\bcomplete\b.*\b(?:uhd|blu-?ray)\b|"
    r"\b(?:uhd|blu-?ray)\b.*\bcomplete\b|\bDV\b.*\bno.?fallback\b",
    re.I,
)
_DV_TITLE_RE = re.compile(r"\b(?:dv|dovi|dolby[\s._-]?vision)\b", re.I)
_HDR_FALLBACK_RE = re.compile(r"hdr10\+?|\bhdr\b", re.I)
# Stray file-level entries from older indexer databases: bare parity/archive
# parts are never a playable release.  Numeric split parts (.001-.999) demand
# a literal dot so anime absolute numbering ("Show - 099") survives, and the
# H.26x codec family (x264 tags without the x) is explicitly exempt.
_ARCHIVE_PART_RE = re.compile(
    r"(?:(?:^|[^a-z0-9])(par2|nzb|rar|r\d{1,3})|\.(\d{3}))[^a-z0-9]*$", re.I)
_CODEC_NUMERAL_RE = re.compile(r"^26[1-6]$")


def _bare_archive_part(title: str) -> bool:
    match = _ARCHIVE_PART_RE.search(title or "")
    if not match:
        return False
    token = match.group(1) or match.group(2)
    return not _CODEC_NUMERAL_RE.fullmatch(token)


def _mountable_release(title: str) -> bool:
    """Cheap format rejects before a release consumes scarce nzbdav slots."""
    if _JUNK_RE.search(title or "") or _bare_archive_part(title):
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
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return f"http-{status}", f"{name} {_response_failure_detail(response)}"
    message = telemetry.sanitize_failure_detail(str(exc), 500)
    detail = name + (f": {message}" if message else "")
    return ("timeout" if "timeout" in name.lower() else "transport", detail)


def _response_failure_detail(response: httpx.Response) -> str:
    """Credential-safe status/header/body shape without ever retaining its URL."""
    parts = [f"HTTP {response.status_code}"]
    for header in ("content-type", "retry-after", "server"):
        value = telemetry.sanitize_failure_detail(
            response.headers.get(header, ""), 160)
        if value:
            parts.append(f"{header}={value}")
    raw = response.content[:800]
    if raw:
        decoded = raw.decode("utf-8", "replace")
        printable = sum(char.isprintable() or char.isspace() for char in decoded)
        if decoded and printable / len(decoded) >= 0.85:
            shape = re.sub(r"\s+", " ", decoded).strip()
            shape = telemetry.sanitize_failure_detail(shape, 500)
            if shape:
                parts.append(f"body={shape}")
    return " ".join(parts)


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
            detail = _response_failure_detail(r)
            _record_failure_sample(
                release, stage="nzbdav-dav", decision="transient",
                reason=f"http-{r.status_code}", detail=detail,
                evidence=f"list:{_evidence_token(path)}:http-{r.status_code}",
                seen=failure_seen)
        return None
    out: list[tuple[str, int]] = []
    try:
        root = ET.fromstring(r.content)
        for resp in (node for node in root.iter()
                     if _local_name(node.tag) == "response"):
            href = next(((node.text or "").strip() for node in resp.iter()
                         if _local_name(node.tag) == "href"), "")
            href = re.sub(r"^https?://[^/]+", "", href)
            size = 0
            length = next(((node.text or "").strip() for node in resp.iter()
                           if _local_name(node.tag) == "getcontentlength"), "")
            if length.isdigit():
                size = int(length)
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
_IMPORT_ENCRYPTED_RE = re.compile(
    r"password.?protect|password did not match|wrong password|"
    r"\bencrypted\b|requires? a password",
    re.I,
)
_IMPORT_TRANSIENT_RE = re.compile(
    r"auth|login|connection|too many|limit|timeout|timed out|network|"
    r"temporar|unavailable|nntp\s*50[023]",
    re.I,
)
# nzbdav's own faults ("Unable to load shared library 'rapidyenc'",
# "SQLite Error 14", "Value cannot be null") — the release is not evidence
# of anything when the backend is broken, so these must never strike it.
_IMPORT_BACKEND_RE = re.compile(
    r"shared librar|rapidyenc|sqlite|cannot be null",
    re.I,
)
# Permanently-dead archive shapes, phrased as nzbdav actually emits them
# (harvested from live HistoryItems).  Missing rar volumes are incomplete
# posts; the rest are structurally unusable no matter which provider serves
# the articles.
_IMPORT_HARD_CLASSES = (
    (re.compile(r"no importable video", re.I), "not-video"),
    (re.compile(r"missing rar volume", re.I), "missing-articles"),
    (re.compile(r"duplicate volume|rar signature|compression method", re.I),
     "broken-archive"),
)


def _history_failure_class(message: str) -> tuple[str, str]:
    """Sanitize an nzbdav failure into health policy enums."""
    if _IMPORT_BACKEND_RE.search(message or ""):
        return "backend", "nzbdav-backend"
    if _IMPORT_TRANSIENT_RE.search(message or ""):
        return "transient", "transport"
    # An encrypted/password-protected archive is permanently unplayable — we
    # manage no passwords, so retrying only burns another mount slot.  The
    # phrasing is kept archive-specific ("Password-protected rar archives
    # cannot be solid.", "The password did not match.") so a provider
    # credential complaint can never earn a release a permanent strike.
    if _IMPORT_ENCRYPTED_RE.search(message or ""):
        return "hard", "encrypted"
    for pattern, reason in _IMPORT_HARD_CLASSES:
        if pattern.search(message or ""):
            return "hard", reason
    if _IMPORT_HARD_RE.search(message or ""):
        return "hard", "missing-articles"
    # Unknown import errors are cooldown-only: never permanently suppress a
    # release based on an unrecognized provider/server condition.
    return "transient", "transport"


_API_SNAPSHOT_TTL = max(0.2, float(os.environ.get("NZB_API_POLL_TTL", "1.5")))
_api_snapshot_lock: asyncio.Lock | None = None
_api_snapshot_loop: asyncio.AbstractEventLoop | None = None
_api_snapshot_cache: tuple[float, list[dict], list[dict], list[tuple[str, str, str]]] = (
    0.0, [], [], [])


def _api_slots(payload: object, section: str) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("root-not-object")
    body = payload.get(section)
    if not isinstance(body, dict):
        raise ValueError(f"missing-{section}-object")
    slots = body.get("slots") or []
    if not isinstance(slots, list):
        raise ValueError(f"invalid-{section}-slots")
    return [slot for slot in slots if isinstance(slot, dict)]


async def _nzbdav_api_snapshot(*, force: bool = False
                                ) -> tuple[list[dict], list[dict],
                                           list[tuple[str, str, str]]]:
    """One shared queue/history poll for all active mounts.

    nzbdav imports are globally bounded, but polling each import independently
    still multiplied API traffic.  This short-lived snapshot coalesces those
    requests and carries structured endpoint failures back to every interested
    mount without retaining credentialed request URLs.
    """
    global _api_snapshot_cache, _api_snapshot_lock, _api_snapshot_loop
    if not NZBDAV_API_KEY:
        return [], [], []
    now = time.monotonic()
    cached_at, queue, history, issues = _api_snapshot_cache
    if not force and now - cached_at < _API_SNAPSHOT_TTL:
        return queue, history, issues
    loop = asyncio.get_running_loop()
    if _api_snapshot_lock is None or _api_snapshot_loop is not loop:
        _api_snapshot_lock = asyncio.Lock()
        _api_snapshot_loop = loop
    async with _api_snapshot_lock:
        now = time.monotonic()
        cached_at, queue, history, issues = _api_snapshot_cache
        if not force and now - cached_at < _API_SNAPSHOT_TTL:
            return queue, history, issues
        common = {"output": "json", "pageSize": 200,
                  "apikey": NZBDAV_API_KEY}
        queue, history, issues = [], [], []
        for mode in ("queue", "history"):
            try:
                response = await _client.get(
                    f"{NZBDAV_URL}/api",
                    params={**common, "mode": mode}, timeout=10)
                if response.status_code != 200:
                    issues.append((f"nzbdav-{mode}",
                                   f"http-{response.status_code}",
                                   _response_failure_detail(response)))
                    continue
                try:
                    slots = _api_slots(response.json(), mode)
                except (ValueError, TypeError) as exc:
                    issues.append((f"nzbdav-{mode}", "invalid-json-shape",
                                   f"{type(exc).__name__}: {exc}"))
                    continue
                if mode == "queue":
                    queue = slots
                else:
                    history = slots
            except Exception as exc:
                reason, detail = _exception_failure(exc)
                issues.append((f"nzbdav-{mode}", reason, detail))
        _api_snapshot_cache = (time.monotonic(), queue, history, issues)
        return queue, history, issues


def _slot_job(slot: dict) -> str:
    raw = str(slot.get("filename") or slot.get("name") or "").strip()
    raw = unquote(raw).replace("\\", "/").rsplit("/", 1)[-1]
    return raw[:-4] if raw.lower().endswith(".nzb") else raw


def _related_job(base_job: str, candidate: str) -> bool:
    attempt_prefix = f"{base_job[:96]}-a"
    return candidate == base_job or candidate.startswith(attempt_prefix)


def _record_api_issues(release: dict | None,
                       failure_seen: set[str] | None,
                       issues: list[tuple[str, str, str]]) -> None:
    if release is None:
        return
    for stage, reason, detail in issues:
        _record_failure_sample(
            release, stage=stage, decision="transient", reason=reason,
            detail=detail, evidence=f"api:{stage}:{reason}:{detail}",
            seen=failure_seen)


async def _related_attempts(
        base_job: str, release: dict | None = None,
        failure_seen: set[str] | None = None,
        ) -> tuple[list[str], list[str]]:
    """Queued/completed attempts retained by nzbdav for this release."""
    if not NZBDAV_API_KEY:
        return [], []
    queue, history, issues = await _nzbdav_api_snapshot()
    _record_api_issues(release, failure_seen, issues)
    queued = list(dict.fromkeys(
        job for slot in queue if (job := _slot_job(slot))
        and _related_job(base_job, job)))
    completed = list(dict.fromkeys(
        job for slot in history if (job := _slot_job(slot))
        and _related_job(base_job, job)
        and str(slot.get("status") or "").strip().lower()
        in ("completed", "complete", "success")))
    # APIs generally return newest first; preserve that order and cap the DAV
    # checks so a very old, repeatedly retried release cannot fan out reads.
    return queued[:3], completed[:5]


async def _history_failure(
        job: str, release: dict | None = None,
        failure_seen: set[str] | None = None,
        ) -> tuple[str, str, str, str] | None:
    """Return a safe failure class for this exact nzbdav job, if finalized."""
    if not NZBDAV_API_KEY:
        return None
    queue, history, issues = await _nzbdav_api_snapshot()
    _record_api_issues(release, failure_seen, issues)
    if any(_slot_job(slot) == job for slot in queue):
        return None
    # Attempt-specific watch names make this correlation exact.  A failed row
    # belonging to yesterday's deterministic name cannot suppress today's
    # retry, even if nzbdav retains history forever.
    slot = next((slot for slot in history if _slot_job(slot) == job), None)
    if not slot or str(slot.get("status", "")).strip().lower() != "failed":
        return None
    detail = telemetry.sanitize_failure_detail(
        str(slot.get("fail_message") or ""), 2000)
    kind, reason = _history_failure_class(detail)
    evidence = str(slot.get("nzo_id") or _evidence_token(f"{job}:{detail}"))
    return kind, reason, evidence, detail


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
    # A backend fault (nzbdav itself broke) says nothing about the release or
    # the indexer that offered it — telemetry only, no health strikes.
    accepted = False
    if kind != "backend":
        accepted = usenet_health.record_failure(
            key, release["title"],
            indexers,
            reason, attempt)
    # Diagnostic evidence is deliberately independent of policy acceptance.
    # A replay may be idempotent for strikes yet still be valuable when we are
    # learning all the shapes nzbdav/provider failures take in the wild.
    telemetry.record_usenet_failure(
        release_key=key, label=release["title"], indexers=indexers,
        stage=stage, decision=kind, reason=reason,
        detail=detail or reason,
        evidence_id=evidence or f"{attempt}:accepted={int(accepted)}")


async def _dav_tree(path: str, release: dict | None = None,
                    failure_seen: set[str] | None = None,
                    ) -> list[tuple[str, int]] | None:
    """Depth-1 listing plus a bounded descent into subdirectories.

    nzbdav usually mounts video files at the job root, but some imports
    materialize the release's own folder inside the job dir (live incident
    2026-07-15: a playable bundle was struck "not-video" twice because its
    mkv sat one level down).  Two levels and eight directories cover every
    layout nzbdav produces while bounding the PROPFIND fan-out."""
    top = await _dav_list(path, release, failure_seen)
    if top is None:
        return None
    out = list(top)
    root = path.rstrip("/")
    frontier = [(href, 1) for href, size in top
                if _dav_dir_candidate(href, size, root)]
    visited = 0
    while frontier:
        href, depth = frontier.pop(0)
        if depth > 2 or visited >= 8:
            break
        visited += 1
        sub = await _dav_list(href.rstrip("/"), release, failure_seen)
        for h, s in sub or []:
            if h.rstrip("/") == href.rstrip("/"):
                continue
            out.append((h, s))
            if _dav_dir_candidate(h, s, root):
                frontier.append((h, depth + 1))
    return out


def _dav_dir_candidate(href: str, size: int, root: str) -> bool:
    """Whether a listed entry looks like a subdirectory worth descending.

    nzbdav's PROPFIND returns collection hrefs WITHOUT a trailing slash, and
    release-named folders are full of dots ("PAW.Patrol...H.264-GRP"), so
    only a short file-style extension marks a zero-size entry as a file.
    Listing a plain file by mistake costs one bounded PROPFIND."""
    h = href.rstrip("/")
    if not h or h == root:
        return False
    if href.endswith("/"):
        return True
    if size:
        return False
    return not re.search(r"\.[A-Za-z0-9]{1,5}$", h.rsplit("/", 1)[-1])


_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".wmv")
_UNSUPPORTED_MOUNT_RE = re.compile(
    r"(?:^|/)(?:bdmv)(?:/|$)|\.(?:iso|img)$", re.I)
_EPISODE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:S0*(\d+)[^A-Za-z0-9]*E0*(\d+)|"
    r"0*(\d+)x0*(\d+))(?!\d)", re.I)
_GENERIC_FILE_WORDS = {
    "file", "media", "movie", "rarbg", "sample", "stream", "title",
    "unknown", "video",
}
_RELEASE_META_WORD_RE = re.compile(
    r"^(?:19|20)\d{2}$|^(?:2160|1080|720|576|480)p$|^(?:4|8)k$|"
    r"^(?:web|webdl|webrip|bluray|bdrip|brrip|remux|hdtv|dvdrip|"
    r"hdr|hdr10|dovi|dv|uhd|hevc|x26[45]|h26[45]|av1|aac|ac3|eac3|"
    r"dts|truehd|atmos|multi|dual|repack|proper|internal)$", re.I)


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


def _episode_tokens(text: str) -> set[tuple[int, int]]:
    out = set()
    for match in _EPISODE_TOKEN_RE.finditer(text or ""):
        season = match.group(1) or match.group(3)
        episode = match.group(2) or match.group(4)
        out.add((int(season), int(episode)))
    return out


def _looks_obfuscated_basename(stem: str) -> bool:
    """Whether a basename carries no credible human title to contradict.

    Obfuscation is common on Usenet (hashes, UUIDs, ``video.mkv``).  Such a file
    is unknown, not automatically wrong.  Conversely, a readable different
    title is decisive and must never ride an outer job-directory name into the
    auto picker.
    """
    compact = re.sub(r"[^A-Za-z0-9]", "", stem or "")
    if not compact:
        return True
    if (re.fullmatch(r"[a-f0-9]{16,}", compact, re.I)
            or re.fullmatch(r"\d{12,}", compact)):
        return True
    words = re.findall(r"[A-Za-z0-9]+", stem or "")
    semantic = []
    for word in words:
        low = word.lower()
        if (_RELEASE_META_WORD_RE.fullmatch(low)
                or re.fullmatch(r"s\d{1,3}e\d{1,4}", low)
                or re.fullmatch(r"\d{1,3}x\d{1,4}", low)
                or low in _GENERIC_FILE_WORDS):
            continue
        semantic.append(word)
    if not semantic:
        return True
    # One long delimiter-free token is more safely treated as ambiguous than as
    # proof of another title.  ``unknown`` cannot lead automatically; a readable
    # multiword different title still becomes a contradiction.
    return bool(len(semantic) == 1 and len(semantic[0]) >= 16)


def _basename_identity(href: str, release: dict,
                       episode: tuple[int, int] | None,
                       ) -> tuple[str, list[str], str]:
    """Classify the mounted *file basename*, never its trusted parent path."""
    expected = release.get("_nzb_expected") or {}
    if not isinstance(expected, dict) or not expected.get("titles"):
        return "unknown", ["basename-unscoped"], ""
    basename = unquote(href).rstrip("/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", basename)
    # Some releases prepend a bracketed group tag to every payload filename.
    # It is packaging, not part of the title, and bounded stripping avoids
    # turning arbitrary leading prose into a match.
    stem = re.sub(r"^(?:\[[^\]\r\n]{1,40}\][\s._-]*){1,2}", "", stem)
    titles = [str(t) for t in expected.get("titles") or [] if str(t).strip()]
    title_exact = any(_release_title_match(stem, title) for title in titles)
    years = {int(y) for y in re.findall(
        r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)}
    year = expected.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    evidence: list[str] = ["basename-title"] if title_exact else []

    if expected.get("media") == "movie":
        if title_exact:
            if year and years and years != {year}:
                return "contradiction", evidence + ["basename-year-mismatch"], "wrong-year"
            if year and years == {year}:
                return "strong", evidence + ["basename-year"], ""
            return "compatible", evidence + ["basename-year-missing"], ""
        if year and years and year not in years:
            return "contradiction", ["basename-year-mismatch"], "wrong-year"
        if _looks_obfuscated_basename(stem):
            return "unknown", ["basename-obfuscated"], ""
        return "contradiction", ["basename-title-mismatch"], "wrong-title"

    requested = episode
    tokens = _episode_tokens(stem)
    # A range spanning the request ("S13E07-E08" asked for E08) is this
    # episode: two-segment shows publish the broadcast half-hour as one file,
    # and _episode_tokens alone would read it as E07 and call it wrong.
    range_hit = bool(requested and _episode_range_match(stem, *requested))
    if requested and tokens and requested not in tokens and not range_hit:
        return "contradiction", evidence + ["basename-episode-mismatch"], "wrong-episode"
    episode_exact = bool(requested and (range_hit
                                        or _episode_match(stem, *requested)))
    if requested and tokens and not episode_exact:
        # The requested token began a range/multi-episode bundle.
        return "contradiction", evidence + ["basename-episode-bundle"], "wrong-episode"
    if year and years and year not in years:
        return "contradiction", evidence + ["basename-year-mismatch"], "wrong-year"
    if title_exact and episode_exact:
        marks = ["basename-episode"] + (["basename-episode-range"]
                                        if range_hit else [])
        return "strong", evidence + marks, ""
    if title_exact:
        return "compatible", evidence + ["basename-episode-missing"], ""
    if episode_exact:
        return "compatible", ["basename-episode", "basename-title-missing"], ""
    if _looks_obfuscated_basename(stem):
        return "unknown", ["basename-obfuscated"], ""
    return "contradiction", ["basename-title-mismatch"], "wrong-title"


def _pick_video_identity(
        entries: list[tuple[str, int]], release: dict,
        episode: tuple[int, int] | None = None,
        ) -> tuple[tuple[str, int] | None, str, list[str], str]:
    """Pick by semantic confidence before size; explicit mismatches never win."""
    videos = [(href, size) for href, size in entries
              if href.lower().endswith(_VIDEO_EXT)]
    ranked = []
    mismatch_reason = ""
    trusted = bool(release.get("_nzb_attrs_trusted"))
    trusted_evidence = list(release.get("_nzb_attr_evidence") or [])
    order = {"strong": 3, "compatible": 2, "unknown": 1}
    for video in videos:
        confidence, evidence, reason = _basename_identity(
            video[0], release, episode)
        if confidence == "contradiction":
            mismatch_reason = mismatch_reason or reason or "wrong-identity"
            continue
        evidence = list(dict.fromkeys(evidence + trusted_evidence))
        if trusted:
            confidence = "strong"
        ranked.append((order[confidence], int(video[1] or 0), video,
                       confidence, evidence))
    if release.get("_nzb_pack"):
        # A season-pack mount holds many sibling episodes.  A file whose name
        # carries only the title ranks "compatible" and the largest sibling
        # would win, so a pack may only serve a file that positively names
        # the requested episode.
        ranked = [row for row in ranked if "basename-episode" in row[4]]
    if not ranked:
        return None, "contradiction" if mismatch_reason else "unknown", [], mismatch_reason
    _, _, video, confidence, evidence = max(ranked, key=lambda row: row[:2])
    return video, confidence, evidence, ""


def _missing_content_reason(entries: list[tuple[str, int]],
                            episode: tuple[int, int] | None,
                            directory_seen: bool,
                            identity_reason: str = "") -> str:
    """Classify a completed mount that did not yield the requested video."""
    if identity_reason in ("wrong-title", "wrong-year", "wrong-episode"):
        return identity_reason
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


def _stable_nonvideo(entries: list[tuple[str, int]],
                     episode: tuple[int, int] | None = None,
                     release: dict | None = None) -> bool:
    """Whether a populated, finalized-looking listing has no playable file."""
    material = []
    for href, size in entries:
        path = unquote(href).rstrip("/")
        if not path:
            continue
        name = path.rsplit("/", 1)[-1]
        if size > 0 or "." in name or _UNSUPPORTED_MOUNT_RE.search(path):
            material.append((path, size))
    if not material:
        return False
    if release is not None:
        selected, _, _, _ = _pick_video_identity(material, release, episode)
        return selected is None
    return not _pick_video(material, episode)


def _new_attempt_job(base_job: str) -> str:
    # The exact watch name is the import correlation id.  It prevents an old
    # immutable history row for the same release from being interpreted as the
    # result of this PUT.  Keep enough entropy for concurrent/restarted jobs.
    token = f"{int(time.time() * 1000):x}{secrets.token_hex(3)}"
    return f"{base_job[:96]}-a{token}"


async def _fetch_and_submit(release: dict, cat: str, job: str,
                            failure_seen: set[str]) -> tuple[bool, str]:
    """Fetch an alternate NZB offer and submit one exact nzbdav attempt."""
    content = None
    fetched_from = ""
    offers = sorted(
        (offer for offer in (release.get("offers") or [])
         if usenet_health.fetch_allowed(offer.get("indexer", ""))),
        key=lambda offer: (
            usenet_health.fetch_score(offer.get("indexer", "")),
            usenet_health.indexer_score(offer.get("indexer", ""))),
        reverse=True)
    for offer in offers:
        indexer, link = offer.get("indexer", ""), offer.get("link", "")
        try:
            nzb = await _client.get(link, timeout=30)
            nzb.raise_for_status()
            payload_issue = _nzb_payload_issue(nzb.content)
            if payload_issue is None:
                content = nzb.content
                fetched_from = indexer
                usenet_health.record_fetch(indexer, True)
                break
            usenet_health.record_fetch(indexer, False)
            payload_reason, payload_detail = payload_issue
            _record_failure_sample(
                release, stage="nzb-fetch", decision="transient",
                reason=payload_reason, detail=payload_detail,
                evidence=(f"fetch:{indexer}:{_evidence_token(link)}:"
                          f"{payload_reason}:{payload_detail}"),
                seen=failure_seen, indexers=[indexer])
            logger.info(f"nzb mount {job[:40]}: link returned {payload_reason}")
        except Exception as exc:
            usenet_health.record_fetch(indexer, False)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            status_detail = f" HTTP {status}" if status else ""
            reason, shape = _exception_failure(exc)
            _record_failure_sample(
                release, stage="nzb-fetch", decision="transient",
                reason=reason, detail=shape,
                evidence=f"fetch:{indexer}:{_evidence_token(link)}:{shape}",
                seen=failure_seen, indexers=[indexer])
            logger.info(
                f"nzb mount {job[:40]}: {type(exc).__name__}{status_detail}")
    if content is None:
        return False, ""
    for attempt in (1, 2):
        try:
            put = await _client.put(
                f"{NZBDAV_URL}/nzbs/{cat}/{quote(job)}.nzb",
                content=content, auth=_dav_auth(), timeout=20)
            if put.status_code < 400:
                return True, fetched_from
            _record_failure_sample(
                release, stage="nzbdav-put", decision="transient",
                reason=f"http-{put.status_code}",
                detail=_response_failure_detail(put),
                evidence=f"put:{_evidence_token(job)}:http-{put.status_code}",
                seen=failure_seen)
            logger.info(f"nzb mount {job[:40]}: PUT {put.status_code}"
                        f" (attempt {attempt})")
        except Exception as exc:
            reason, shape = _exception_failure(exc)
            _record_failure_sample(
                release, stage="nzbdav-put", decision="transient",
                reason=reason, detail=shape,
                evidence=f"put:{_evidence_token(job)}:{shape}",
                seen=failure_seen)
            logger.info(f"nzb mount {job[:40]}: PUT {type(exc).__name__}")
        if attempt == 1:
            await asyncio.sleep(2)
    return False, ""


async def _mount(release: dict, cat: str, delay: float = 0,
                 episode: tuple[int, int] | None = None) -> dict | None:
    """Ensure this release is mounted in nzbdav; return a stream dict or None.
    Reuses an existing mount instantly; otherwise NZB-fetch → watch-folder PUT →
    poll for the content dir (nzbdav mounts in ~2s; missing-article releases
    that mount anyway are caught later by the picker's probe)."""
    t_start = time.monotonic()
    key = release.get("release_key") or ""
    legacy_key = release.get("legacy_release_key") or ""
    scoped = bool(legacy_key and legacy_key != key)
    # New scoped jobs use 64 bits of the digest.  The title slug plus the former
    # 32-bit suffix was serviceable, but avoidable collisions are unacceptable
    # when the exact mount is an identity boundary.
    suffix_chars = 16 if scoped else 8
    suffix = f"-{key[-suffix_chars:]}" if key else ""
    base_job = _slug(release["title"]) + suffix
    job = base_job
    dir_path = f"/content/{cat}/{base_job}"
    failure_seen: set[str] = set()
    entries = await _dav_tree(dir_path, release, failure_seen)
    # Scoped release keys intentionally change the deterministic job suffix.
    # Probe the legacy title+size mount once so upgrades can reuse existing
    # content, but validate its basename below before it becomes a candidate.
    legacy_base = (_slug(release["title"]) + f"-{legacy_key[-8:]}"
                   if legacy_key and legacy_key != key else "")
    if entries is None and legacy_base:
        legacy_path = f"/content/{cat}/{legacy_base}"
        legacy_entries = await _dav_list(legacy_path, release, failure_seen)
        if legacy_entries is not None:
            job, dir_path, entries = legacy_base, legacy_path, legacy_entries
    directory_seen = entries is not None
    reused = directory_seen                 # mount already existed in nzbdav
    fetched_from = ""
    if entries is None:
        queued, completed = await _related_attempts(
            base_job, release, failure_seen)
        if legacy_base:
            legacy_queued, legacy_completed = await _related_attempts(
                legacy_base, release, failure_seen)
            queued = list(dict.fromkeys(queued + legacy_queued))
            completed = list(dict.fromkeys(completed + legacy_completed))
        # Reuse a successful attempt-specific mount after a process restart or
        # raw-cache expiry.  This avoids importing the same NZB every six hours.
        for prior_job in completed:
            prior_path = f"/content/{cat}/{prior_job}"
            listed = await _dav_list(prior_path, release, failure_seen)
            if listed is not None:
                job, dir_path, entries = prior_job, prior_path, listed
                directory_seen = reused = True
                break

        pending_existing = entries is None and bool(queued)
        if pending_existing:
            # A prior process already submitted this exact attempt.  Join its
            # mount instead of duplicating the watch-folder PUT.
            job = queued[0]
            dir_path = f"/content/{cat}/{job}"
        elif entries is None:
            prior_failure = await _history_failure(
                base_job, release, failure_seen)
            if prior_failure:
                kind, reason, evidence, detail = prior_failure
                _record_import_failure(release, kind, reason, evidence, detail)
                logger.info(
                    f"nzb mount {base_job[:40]}: prior import {reason}; retrying")
                # Never let the immutable prior row answer for this retry.
                job = _new_attempt_job(base_job)
                dir_path = f"/content/{cat}/{job}"
            if delay > 0:
                await asyncio.sleep(delay)
            submitted, fetched_from = await _fetch_and_submit(
                release, cat, job, failure_seen)
            if not submitted:
                return None
    video, identity_confidence, identity_evidence, identity_reason = (
        _pick_video_identity(entries or [], release, episode))
    shape_settled = False
    if not video:
        deadline = time.monotonic() + MOUNT_WAIT
        next_history_check = time.monotonic() + 1.5
        poll_delay = 0.5
        stable_shape = ""
        stable_count = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(min(poll_delay,
                                    max(0.01, deadline - time.monotonic())))
            listed = await _dav_tree(dir_path, release, failure_seen)
            if listed is not None:
                entries = listed
                directory_seen = True
            video, identity_confidence, identity_evidence, identity_reason = (
                _pick_video_identity(entries or [], release, episode))
            if video:
                break
            shape = _content_evidence(entries or []) if entries else ""
            if (shape and shape == stable_shape
                    and _stable_nonvideo(entries or [], episode, release)):
                stable_count += 1
            else:
                stable_shape, stable_count = shape, 1 if shape else 0
            # ISO/BDMV is terminal immediately; other populated non-video
            # layouts get three observations so a just-materializing mount is
            # not rejected between its metadata and video file appearing.
            unsupported = any(_UNSUPPORTED_MOUNT_RE.search(unquote(href))
                              for href, _ in (entries or []))
            if unsupported or stable_count >= 3:
                shape_settled = True
                break
            now = time.monotonic()
            if now >= next_history_check:
                next_history_check = now + max(1.5, poll_delay)
                failure = await _history_failure(job, release, failure_seen)
                if failure:
                    kind, reason, evidence, detail = failure
                    _record_import_failure(
                        release, kind, reason, evidence, detail)
                    logger.info(f"nzb mount {job[:40]}: import {reason}")
                    return None
            poll_delay = min(5.0, poll_delay * 1.65)
    if not video:
        failure_reason = _missing_content_reason(
            entries or [], episode, directory_seen, identity_reason)
        # A "not-video" verdict is only trustworthy when the layout held
        # still for three observations (or was terminal ISO/BDMV).  When the
        # deadline expires mid-import, a directory without its video yet is
        # "import still running", never proof of junk content — a playable
        # release earned a 24h hard strike this way (PAW Patrol, 2026-07-15).
        # Identity contradictions (wrong-*) stay hard: those come from a
        # mounted video file that positively named other content.
        if failure_reason == "not-video" and not shape_settled:
            failure_reason = "never-appeared"
        failure_text = {
            "wrong-episode": "no matching video",
            "wrong-title": "mounted video title contradicted the request",
            "wrong-year": "mounted video year contradicted the request",
            "not-video": "mounted content was not video",
            "never-appeared": "never appeared",
        }[failure_reason]
        logger.info(f"nzb mount {job[:40]}: "
                    f"{failure_text}")
        if key and failure_reason in (
                "wrong-episode", "wrong-title", "wrong-year", "not-video"):
            detail = (f"mounted directory contained {len(entries or [])} entries; "
                      f"identity result={failure_reason}")
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
    hints = {"filename": fname}
    if size:
        # Preserve the exact DAV byte count. The slow picker's middle/tail
        # range verification compares Content-Range totals exactly; the rounded
        # human-readable GB value is deliberately unsuitable for that gate.
        hints["videoSize"] = int(size)
    return {
        "name": f"NZB\n{release['title'][:60]}",
        "description": (f"Source: {source}\nSize: {gb}\n"
                        f"{release['title']}"),
        "url": _stream_base() + quote(href),
        "behaviorHints": hints,
        "_nzb_release_key": key,
        "_nzb_label": release["title"][:180],
        "_nzb_indexer": source,
        "_nzb_indexers": all_indexers,
        "_nzb_identity_confidence": identity_confidence,
        "_nzb_identity_evidence": identity_evidence,
        # Mount economics, carried into this release's probe telemetry so the
        # time-to-streamable distribution (fresh vs reused) is measurable.
        "_nzb_mount_secs": round(time.monotonic() - t_start, 1),
        "_nzb_mount_reused": reused,
    }


async def _mount_limited(release: dict, cat: str, delay: float = 0,
                         episode: tuple[int, int] | None = None) -> dict | None:
    """Globally bound complete imports, including their DAV/history polling."""
    if delay > 0:
        await asyncio.sleep(delay)
    async with _import_slots:
        return await _mount(release, cat, 0, episode)


TRANSIENT_BUCKET = float(os.environ.get("NZB_MOUNT_FAILURE_BUCKET", "1800"))
HARD_BUCKET = float(os.environ.get("NZB_CONTENT_FAILURE_BUCKET", "86400"))
LANE_MAX_ACTIVE = max(1, int(os.environ.get("NZB_LANE_MAX_ACTIVE", "32")))
LANE_REGISTRY_MAX = max(LANE_MAX_ACTIVE,
                        int(os.environ.get("NZB_LANE_REGISTRY_MAX", "500")))
_mount_jobs: dict[tuple[str, str], asyncio.Task] = {}
_mount_events: dict[tuple[str, str], asyncio.Event] = {}
_mount_outputs: dict[tuple[str, str], list[dict]] = {}
_mount_outcomes: dict[tuple[str, str], dict] = {}


def _refresh_out(out: list[dict], results: dict[int, dict]) -> None:
    # Mutate the shared list in place: app.sources caches this exact object, so
    # mounts that finish after the foreground return become visible to the slow
    # picker/retry without launching another indexer search.
    out[:] = [results[i] for i in sorted(results)]


def _prune_mount_registry() -> None:
    """Evict completed lanes without letting one old active key pin the cap."""
    if len(_mount_events) <= LANE_REGISTRY_MAX:
        return
    for key in list(_mount_events):
        event = _mount_events.get(key)
        if event is None or not event.is_set():
            continue
        _mount_events.pop(key, None)
        _mount_outputs.pop(key, None)
        _mount_outcomes.pop(key, None)
        task = _mount_jobs.pop(key, None)
        if task is not None and not task.done():
            # Defensive only: a completed event must belong to a terminal job.
            _mount_jobs[key] = task
            continue
        if len(_mount_events) <= LANE_REGISTRY_MAX:
            break


def _lane_task_done(key: tuple[str, str], task: asyncio.Task) -> None:
    # Consume every exception and remove only the exact task; a newer refresh
    # for the same title may already own the registry entry.
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("detached nzb lane task failed")
    if _mount_jobs.get(key) is task:
        _mount_jobs.pop(key, None)
    _prune_mount_registry()


async def _run_lane(media: str, media_id: str, out: list[dict],
                    done_event: asyncio.Event) -> None:
    """Registry-owned title job; no requesting picker owns or cancels it."""
    lane_key = (media, media_id)
    started = time.monotonic()
    pending: dict[asyncio.Task, tuple[int, dict]] = {}
    results: dict[int, dict] = {}
    total = 0
    _mount_outcomes[lane_key] = {"state": "searching", "detail": "",
                                 "finished_at": 0.0}
    try:
        releases = await search(media, media_id)
        successful_searches = int(getattr(releases, "search_ok", 1))
        failed_searches = int(getattr(releases, "search_failed", 0))
        if not releases:
            state = "failed" if successful_searches == 0 and failed_searches else "empty"
            detail = ("all-indexers-failed" if state == "failed"
                      else "no-eligible-releases")
            _mount_outcomes[lane_key] = {
                "state": state, "detail": detail,
                "finished_at": time.monotonic()}
            return
        cat = "movies" if media == "movie" else "tv"
        parts = media_id.split(":")
        episode = ((int(parts[1]), int(parts[2]))
                   if media != "movie" and len(parts) == 3 else None)
        top = _select_releases(releases, MOUNT_MAX, media)
        total = len(top)
        order = []
        for release in top:
            best = max((o.get("indexer", "")
                        for o in release.get("offers") or []),
                       key=usenet_health.indexer_score, default="")
            order.append(f"{best}:{usenet_health.indexer_score(best):.2f}")
        if order:
            logger.info(f"nzb lane {media_id}: mount priority {' > '.join(order)}")
        pending = {
            asyncio.create_task(
                _mount_limited(release, cat, idx * MOUNT_STAGGER, episode)):
            (idx, release)
            for idx, release in enumerate(top)
        }
        _mount_outcomes[lane_key] = {
            "state": "mounting", "detail": f"0/{total}", "finished_at": 0.0}
        failures = 0
        while pending:
            done, _ = await asyncio.wait(set(pending),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                idx, release = pending.pop(task)
                try:
                    mounted = task.result()
                except asyncio.CancelledError:
                    failures += 1
                    _record_failure_sample(
                        release, stage="usenet-task", decision="transient",
                        reason="cancelled", detail="CancelledError",
                        evidence=f"lane:{media}:{media_id}:{idx}:cancelled")
                    continue
                except Exception as exc:
                    failures += 1
                    reason, detail = _exception_failure(exc)
                    _record_failure_sample(
                        release, stage="usenet-task", decision="transient",
                        reason=reason, detail=detail,
                        evidence=f"lane:{media}:{media_id}:{idx}:{detail}")
                    logger.exception("nzb mount task failed")
                    continue
                if mounted:
                    results[idx] = mounted
                else:
                    failures += 1
            _refresh_out(out, results)
            _mount_outcomes[lane_key] = {
                "state": "mounting",
                "detail": f"{len(out)}/{total}", "finished_at": 0.0}
        state = "ok" if out else "empty"
        detail = f"mounted={len(out)} failed={failures} total={total}"
        _mount_outcomes[lane_key] = {
            "state": state, "detail": detail,
            "finished_at": time.monotonic()}
        logger.info(f"nzb lane {media_id}: background mounts complete "
                    f"{len(out)}/{total} in {time.monotonic() - started:.1f}s")
    except asyncio.CancelledError:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        _mount_outcomes[lane_key] = {
            "state": "cancelled", "detail": "lane-task-cancelled",
            "finished_at": time.monotonic()}
        raise
    except Exception as exc:
        reason, detail = _exception_failure(exc)
        telemetry.record_usenet_failure(
            stage="usenet-lane", decision="transient", reason=reason,
            detail=detail,
            evidence_id=f"lane:{media}:{media_id}:{reason}:{detail}")
        _mount_outcomes[lane_key] = {
            "state": "failed", "detail": detail[:160],
            "finished_at": time.monotonic()}
        logger.exception("nzb lane failed")
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


def outcome(media: str, media_id: str) -> dict:
    """Last lane state, preserving failed vs successful-empty for callers."""
    key = (media, media_id)
    event = _mount_events.get(key)
    if event is not None and not event.is_set():
        current = dict(_mount_outcomes.get(key) or {})
        current.setdefault("state", "running")
        current.setdefault("detail", "")
        current.setdefault("finished_at", 0.0)
        return current
    return dict(_mount_outcomes.get(key) or {
        "state": "unknown", "detail": "", "finished_at": 0.0})


def _start_lane(media: str, media_id: str) -> tuple[list[dict], asyncio.Event] | None:
    key = (media, media_id)
    active = sum(1 for event in _mount_events.values() if not event.is_set())
    if active >= LANE_MAX_ACTIVE:
        detail = f"active={active} limit={LANE_MAX_ACTIVE}"
        telemetry.record_usenet_failure(
            stage="usenet-lane", decision="transient", reason="capacity",
            detail=detail,
            evidence_id=f"lane-capacity:{int(time.time() // 60)}")
        _mount_outcomes[key] = {
            "state": "failed", "detail": detail,
            "finished_at": time.monotonic()}
        return None
    out: list[dict] = []
    event = asyncio.Event()
    _mount_outputs[key] = out
    _mount_events[key] = event
    task = asyncio.create_task(_run_lane(media, media_id, out, event))
    _mount_jobs[key] = task
    task.add_done_callback(lambda done, lane=key: _lane_task_done(lane, done))
    _prune_mount_registry()
    return out, event


async def streams(media: str, media_id: str) -> list[dict]:
    """The lane's entry point (called via app.sources like any other source):
    search all indexers, mount the top MOUNT_MAX releases concurrently, and
    return stream dicts for the ones that mounted. Quality order preserved —
    the picker's probe then decides which actually play."""
    if not enabled():
        return []
    lane_key = (media, media_id)
    existing_event = _mount_events.get(lane_key)
    existing_out = _mount_outputs.get(lane_key)
    if existing_event is not None and not existing_event.is_set():
        out = (existing_out if existing_out else
               await wait_for_more(media, media_id, 0, MOUNT_EARLY_WAIT))
        logger.info(f"nzb lane {media_id}: rejoined in-progress mounts, "
                    f"returning {len(out or [])}")
        return out if out is not None else []
    started = _start_lane(media, media_id)
    if started is None:
        return []
    out, _ = started
    await wait_for_more(media, media_id,
                        max(0, MOUNT_RETURN_WANT - 1), MOUNT_EARLY_WAIT)
    # This coroutine is only a shielded view of registry state.  Cancellation
    # here cannot cancel _run_lane or any mount task.
    return out


async def shutdown() -> None:
    """Drain detached mount jobs, close HTTP, and checkpoint learned health."""
    tasks = list(_mount_jobs.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _mount_jobs.clear()
    await _client.aclose()
    usenet_health.close()
