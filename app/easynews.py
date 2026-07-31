"""Easynews as its own source — search Easynews' index, stream the file directly.

A sibling to :mod:`app.prowlarr` and the direct-usenet lane in :mod:`app.usenet`,
but the shortest lane in the addon: Easynews indexes *already-assembled files*
and serves them over ordinary HTTPS, so a search result **is** a playable URL.
There is no NZB, no nzbdav mount, no NNTP article fan-out — which is why this
lane exists at all. Measured against the same file on the direct-usenet path:

    cold head TTFB      0.33 s      (usenet lane: 0.3 - 0.8 s)
    cold tail / index   0.29 s      (usenet lane: 7.68 s)
    random mid seek     0.29 s      (usenet lane: pays an interpolation
                                     search + article fetches every time)

*Reads* are consistently that fast. **Search is not**: it is usually well under
a second but has a fat tail — the same query, repeated, measured 0.69 s, then
8.58 s, then 1.34 s, and a 15 s timeout fired twice in a row on a query that
answered in 0.47 s minutes later. That variance is why SEARCH_TIMEOUT is
generous rather than tight. A slow search costs nothing at the pick: callers in
:mod:`app.sources` wait only as long as their own deadline allows, and the
search is shielded, so it finishes into the shared cache for the background
finisher and the slow picker instead of being thrown away. A tight timeout
would throw it away.

Structurally, none of failure modes 1, 2 or 4 in CLAUDE.md can happen here:
there is no connection pool to leak, the tail is as cheap as the head, and the
proxy's byte cache fronts a plain range-capable origin. That is the whole
argument for the lane.

What it is NOT is trusted. Easynews search is a full-text search over *posted
filenames*, not a release index, so it returns two kinds of poison that a
newznab query does not:

  * **Wrong titles.** "The Bear S03E05" returns *The Island With Bear Grylls*
    and *The Yogi Bear Show* in the top five. They would resolve, probe fine,
    and play the wrong show.
  * **Samples.** A 192 MB ``dune.part.two...-ethel.sample`` ranks *above* the
    real file; Severance S02E01 returns one real file and two samples.

So every row runs the same strict title/year/episode gate the usenet and
Prowlarr lanes use (:mod:`app.usenet`'s matchers are the single implementation),
plus a sample gate, and then — deliberately — takes the ordinary HTTPS
validation path in the picker. Rows carry no trust sentinel, so
``picker._lane_of`` files them under "https" and they must earn their place
through the full probe: payload sniff, TTFB, bitrate-relative throughput,
declared-duration check and codec sniff. Easynews' own metadata (resolution,
codecs, runtime, exact byte size) is used only to pre-rank and to skip probes
that are obviously not worth spending — never to skip validation.

Credentials ride in the URL's userinfo rather than
``behaviorHints.proxyHeaders``. That is load-bearing, not stylistic:
``proxy._must_wrap`` treats a URL with userinfo as never-serve-raw, so an
Easynews row is always proxied (even past ``WRAP_MAX``) and is dropped outright
if the proxy is disabled. The proxyHeaders route leaks — ``proxy.wrap`` only
strips them on the HLS branch, so row #9 would have handed the player our
password. The direct-nzb lane embeds its WebDAV login the same way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from app import meta, usenet

logger = logging.getLogger("stream-picker")

# ── configuration (env, baked at import like every other knob) ───────────────
_USER = (os.environ.get("EASYNEWS_USER") or "").strip()
_PASS = os.environ.get("EASYNEWS_PASS") or ""
# Unlike the Prowlarr lane this defaults to ON: credentials are the switch.
# Saving a username/password in the Sources panel enables the lane; the toggle
# writes "0" to turn it off again *without* discarding the credentials, so it
# can be flipped back on without re-typing them.
_SOURCE_ON = (os.environ.get("EASYNEWS_SOURCE") or "1").lower() in (
    "1", "true", "yes", "on")

SEARCH_URL = (os.environ.get("EASYNEWS_SEARCH_URL")
              or "https://members.easynews.com/2.0/search/solr-search/advanced")
# Generous on purpose — see the module docstring: search latency has a fat tail
# and a timeout here discards a result nothing was waiting on anyway.
SEARCH_TIMEOUT = float(os.environ.get("EASYNEWS_SEARCH_TIMEOUT", "45"))
# Rows to ask for per query. Easynews answers a 100-row page as fast as a 25-row
# one, and the strict gates below discard most of them.
PAGE_SIZE = max(10, int(os.environ.get("EASYNEWS_PAGE_SIZE", "100")))
# Rows to emit after ranking. Each one costs a probe, so this is a probe budget.
MAX_RESULTS = max(1, int(os.environ.get("EASYNEWS_MAX_RESULTS", "12")))
# Floor under which a "video" is a sample/clip whatever its name claims.
MIN_MB = max(0, int(os.environ.get("EASYNEWS_MIN_MB", "50")))
# A row whose *declared* runtime is below this fraction of the title's expected
# runtime is a sample. Mirrors probe.DURATION_MIN_FRAC, which stays the
# authority — this only avoids spending a probe to learn what the index said.
RUNTIME_MIN_FRAC = float(os.environ.get("EASYNEWS_RUNTIME_MIN_FRAC", "0.5"))

_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm")
_SAMPLE_RE = re.compile(r"(?:^|[\W_])samples?(?:$|[\W_])", re.I)

_client = httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True,
                            headers={"User-Agent": "stream-picker/1.0"})

# (media, media_id) -> {"state", "detail", "finished_at"} for diagnostics.
_outcomes: dict[tuple, dict] = {}


def enabled() -> bool:
    """The lane runs when it is switched on and has credentials. Missing either
    disables it silently (no-op) — it can only ever add coverage."""
    return bool(_SOURCE_ON and _USER and _PASS)


def configured() -> bool:
    """Credentials are stored, whatever the toggle says — what the Sources panel
    needs to know to render the switch as available rather than grayed out."""
    return bool(_USER and _PASS)


def outcome(media: str, media_id: str) -> dict:
    return dict(_outcomes.get((media, media_id)) or {
        "state": "unknown", "detail": "", "finished_at": 0.0})


def _record(media: str, media_id: str, state: str, detail: str = "") -> None:
    _outcomes[(media, media_id)] = {"state": state, "detail": detail[:160],
                                    "finished_at": time.monotonic()}
    if len(_outcomes) > 512:
        _outcomes.pop(next(iter(_outcomes)), None)


# ── search ───────────────────────────────────────────────────────────────────

def _queries(media: str, media_id: str, titles: list[str],
             year: int | None) -> list[str]:
    """Focused full-text searches for the title's aliases.

    Easynews matches on posted filenames, so the query is the release name a
    poster would have used: "Title SxxEyy" for an episode, "Title Year" for a
    film. Alias fan-out is bounded at three — every extra query is another
    round trip and the strict gates make breadth cheap only in coverage, not in
    latency."""
    parts = media_id.split(":")
    episode = ""
    if media != "movie" and len(parts) >= 3:
        try:
            episode = f"S{int(parts[1]):02d}E{int(parts[2]):02d}"
        except ValueError:
            episode = ""
    out: list[str] = []
    seen: set[str] = set()
    for raw in titles:
        title = usenet._query_text(str(raw or "").strip())
        folded = title.casefold()
        if not title or folded in seen:
            continue
        seen.add(folded)
        out.append(f"{title} {episode}" if episode else
                   f"{title} {year}" if year else title)
        if len(out) >= 3:
            break
    return out


async def _search(query: str) -> dict:
    """One Easynews search. Returns the raw payload (the download-URL fields at
    the top level are needed to build a link, so the whole envelope is kept)."""
    params = {
        # safeO must stay "0". Easynews' safe-search flag is broken server-side:
        # safeO=1 answers {"results":0} followed by a raw PHP "Undefined
        # variable: SearchId" notice, so it is not even valid JSON. Turning it
        # on to filter adult results silently returns nothing for every title.
        # The title gate in _candidates is what actually keeps that content out
        # (an unrestricted query for a show really does return porn matched on
        # a cast member's name), and it does so by construction.
        "st": "adv", "sb": "1", "safeO": "0", "u": "1", "gx": "1",
        "fty[]": "VIDEO",            # video files only, server-side
        "s1": "relevance", "s1d": "-",
        "s2": "dsize", "s2d": "-",   # ties broken by size, largest first
        "pby": str(PAGE_SIZE), "pno": "1", "sS": "3",
        "gps": query,
    }
    r = await _client.get(SEARCH_URL, params=params, auth=(_USER, _PASS),
                          timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    return payload if isinstance(payload, dict) else {}


def _size_of(row: dict) -> int:
    for key in ("rawSize", "size"):
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _ext_of(row: dict) -> str:
    ext = str(row.get("11") or row.get("extension") or row.get("2") or "").strip()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext.lower()


def _name_of(row: dict) -> str:
    """The posted filename stem — the identity evidence for this row."""
    return str(row.get("10") or row.get("fn") or "").strip()


def _is_sample(row: dict, name: str, expect_runtime: float | None) -> bool:
    """Samples are the single most common wrong answer Easynews gives, and they
    outrank real files: they share the release name, so relevance sorting puts
    them first. Two independent gates, because neither catches everything — an
    unlabelled 2-minute clip has no "sample" token, and a labelled sample of an
    unknown-runtime title has no runtime to compare."""
    if _SAMPLE_RE.search(name) or _SAMPLE_RE.search(str(row.get("6") or "")):
        return True
    if _size_of(row) < MIN_MB * 1024 * 1024:
        return True
    try:
        runtime = float(row.get("runtime") or 0)
    except (TypeError, ValueError):
        runtime = 0.0
    if expect_runtime and runtime > 0 and RUNTIME_MIN_FRAC > 0:
        return runtime < expect_runtime * RUNTIME_MIN_FRAC
    return False


def _height_of(row: dict) -> int:
    for key in ("height", "yres"):
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _candidates(rows: list[dict], titles: list[str],
                season_episode: tuple[int, int] | None,
                year: int | None,
                expect_runtime: float | None) -> list[dict]:
    """Rows that are actually this title, best-first.

    The title/year/episode gate is :mod:`app.usenet`'s — the same one that keeps
    the usenet and Prowlarr lanes from playing wrong content. It is doing more
    work here than it does there, because Easynews has no notion of a release
    and will happily return a different show that merely shares a word."""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").upper() != "VIDEO":
            continue
        if row.get("passwd") or row.get("password") or row.get("virus"):
            continue
        name, ext = _name_of(row), _ext_of(row)
        if not name or ext not in _VIDEO_EXT:
            continue
        if not str(row.get("0") or row.get("hash") or "").strip():
            continue
        if not any(usenet._release_title_match(name, t) for t in titles if t):
            continue
        if not usenet._release_year_match(name, titles, year):
            continue
        if season_episode and not usenet._episode_match(name, *season_episode):
            continue
        if _is_sample(row, name, expect_runtime):
            continue
        out.append(row)
    # Collapse the same file posted several times, keeping the largest copy.
    best: dict[str, dict] = {}
    for row in out:
        key = f"{_name_of(row).lower()}{_ext_of(row)}"
        cur = best.get(key)
        if cur is None or _size_of(row) > _size_of(cur):
            best[key] = row
    return sorted(best.values(),
                  key=lambda r: (_height_of(r), _size_of(r)), reverse=True)


# ── stream rows ──────────────────────────────────────────────────────────────

def _download_url(payload: dict, row: dict) -> str:
    """The direct HTTPS URL for one row, with credentials in the userinfo.

    Easynews 302s this to a signed per-node CDN URL; the proxy follows it and
    the redirect target needs no auth of its own. Userinfo (not a header) is
    what marks the URL never-serve-raw — see the module docstring."""
    down = str(payload.get("downURL") or "").rstrip("/")
    farm = str(payload.get("dlFarm") or "auto")
    port = str(payload.get("dlPort") or 443)
    sig = str(row.get("sig") or "")
    file_hash, ext = str(row.get("0") or row.get("hash") or ""), _ext_of(row)
    name = _name_of(row)
    if not down or not file_hash:
        return ""
    sp = urlsplit(down)
    if not sp.scheme or not sp.netloc:
        return ""
    cred = f"{quote(_USER, safe='')}:{quote(_PASS, safe='')}"
    base = f"{sp.scheme}://{cred}@{sp.netloc}{sp.path}"
    # The sig-bearing form is what Easynews' own client uses; without one the
    # farm/port form is still served (both verified against a live account).
    prefix = f"{base}/{farm}/{port}" + (f"/{quote(sig, safe='')}" if sig else "")
    return (f"{prefix}/{quote(file_hash + ext, safe='')}"
            f"/{quote(name + ext, safe='')}")


def _row(payload: dict, row: dict) -> dict | None:
    """One Stremio stream for an Easynews file.

    ``behaviorHints.filename`` carries the posted filename because that is what
    the picker's identity parser reads, what the proxy's release signature is
    built from, and what pairs this copy with the same file on another source.
    ``videoSize`` is Easynews' exact byte count, which makes the probe's
    throughput test bitrate-relative from the first attempt instead of falling
    back to a flat floor."""
    url = _download_url(payload, row)
    name = _name_of(row)
    if not url or not name:
        return None
    size = _size_of(row)
    res = str(row.get("fullres") or "").replace(" ", "") or "?"
    vcodec = str(row.get("vcodec") or "").strip()
    acodec = str(row.get("acodec") or "").strip()
    gb = f"{size / 1e9:.2f} GB" if size else "?"
    tags = " · ".join(t for t in (res, vcodec, acodec) if t and t != "?")
    hints: dict = {"filename": name + _ext_of(row)}
    if size:
        hints["videoSize"] = size
    return {
        "name": f"Easynews\n{name[:60]}",
        "description": (f"Source: Easynews\nSize: {gb}"
                        + (f" · {tags}" if tags else "") + f"\n{name}"),
        "url": url,
        "behaviorHints": hints,
    }


async def streams(media: str, media_id: str) -> list[dict]:
    """Search Easynews and return title-matched, directly playable rows.

    Returns [] (never raises) so the lane can only add coverage, never break a
    pick. Rows are emitted untrusted: the picker probes them exactly like any
    other HTTPS source."""
    if not enabled():
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
        queries = _queries(media, media_id, titles, year)
        if not queries:
            _record(media, media_id, "failed", "no usable query")
            return []
        try:
            expect_runtime = await meta.expected_runtime(media, media_id)
        except Exception:
            expect_runtime = None
        searched = await asyncio.gather(*(_search(q) for q in queries),
                                        return_exceptions=True)
        if all(isinstance(p, BaseException) for p in searched):
            raise next(p for p in searched if isinstance(p, BaseException))
    except Exception as e:
        logger.warning(f"easynews search failed: {type(e).__name__}")
        _record(media, media_id, "failed", type(e).__name__)
        return []

    # Every page carries its own download-URL envelope (sid/farm/port), so a row
    # must be built against the payload it came from, not a merged one.
    out: list[dict] = []
    seen: set[str] = set()
    matched = 0
    for payload in searched:
        if not isinstance(payload, dict):
            continue
        rows = payload.get("data")
        cands = _candidates(rows if isinstance(rows, list) else [],
                            titles, season_episode, year, expect_runtime)
        matched += len(cands)
        for cand in cands:
            key = f"{_name_of(cand).lower()}{_ext_of(cand)}"
            if key in seen:
                continue
            seen.add(key)
            built = _row(payload, cand)
            if built:
                out.append(built)
    out = out[:MAX_RESULTS]
    if not out:
        _record(media, media_id, "empty", "no matching files")
    else:
        _record(media, media_id, "ok", f"{len(out)} file(s)")
    logger.info(f"easynews {media}/{media_id}: {matched} matched, "
                f"{len(out)} stream(s) from {len(queries)} query(s)")
    return out


async def shutdown() -> None:
    await _client.aclose()
