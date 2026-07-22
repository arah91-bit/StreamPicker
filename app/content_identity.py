"""Pure, conservative release-name identity classification.

Transport probing can prove that a URL contains playable media, but it cannot
prove that the media is the title the user requested.  This module supplies the
small semantic half of that decision without doing I/O or trusting arbitrary
addon fields.

Only an actual filename or an internally-held release label belongs in
``text``.  In particular, callers must not turn an upstream ``_verified`` (or
similarly named) JSON field into either trust flag.  The flags describe trusted
request context established by the caller: an exact IMDb lookup and, for TV, an
exact episode lookup.

The four states intentionally distinguish absence of evidence from evidence of
the wrong content:

``strong``
    The text and trusted context are sufficient for an automatic first result.
``compatible``
    Nothing conflicts, but an ambiguity remains (notably a yearless movie).
``unknown``
    There is no usable title evidence.
``contradiction``
    A title, year, region, season, or episode explicitly conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata
from urllib.parse import unquote


STRONG = "strong"
COMPATIBLE = "compatible"
UNKNOWN = "unknown"
CONTRADICTION = "contradiction"

STATES = frozenset({STRONG, COMPATIBLE, UNKNOWN, CONTRADICTION})

# Numeric evidence ordering for picker policy.  State remains the public
# compatibility contract; the evidence tier explains *why* that state was
# reached and lets ranking prefer direct semantic proof over corroboration.
EVIDENCE_CONTRADICTION = "contradiction"
EVIDENCE_UNKNOWN = "unknown"
EVIDENCE_COMPATIBLE = "compatible"
EVIDENCE_RUNTIME = "runtime-corroborated"
EVIDENCE_ANIME = "anime-episode"
EVIDENCE_CANONICAL = "canonical-title"
EVIDENCE_TRUSTED_IMDB = "trusted-imdb"

EVIDENCE_RANKS = {
    EVIDENCE_CONTRADICTION: 0,
    EVIDENCE_UNKNOWN: 1,
    EVIDENCE_COMPATIBLE: 2,
    EVIDENCE_RUNTIME: 3,
    EVIDENCE_ANIME: 4,
    EVIDENCE_CANONICAL: 4,
    EVIDENCE_TRUSTED_IMDB: 5,
}

# Process-local, unforgeable handoff from picker verification to the playback
# proxy. Upstream JSON can spell the key but cannot manufacture this object;
# app.sources also strips all upstream underscore fields before classification.
_AUTO_ELIGIBLE_KEY = "_picker_auto_identity"
_AUTO_ELIGIBLE_SENTINEL = object()


def mark_auto_eligible(stream: dict) -> None:
    """Mark a transport-verified, strong-identity stream for safe auto-failover."""
    stream[_AUTO_ELIGIBLE_KEY] = _AUTO_ELIGIBLE_SENTINEL


def auto_eligible(stream: dict) -> bool:
    """True only for a mark created inside this Python process."""
    return stream.get(_AUTO_ELIGIBLE_KEY) is _AUTO_ELIGIBLE_SENTINEL


_VIDEO_EXT_RE = re.compile(
    r"\.(?:mkv|mp4|m4v|avi|mov|wmv|m2ts|mts|ts|webm)$", re.I)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_RES_RE = re.compile(r"^(?:4320|2160|1440|1080|720|576|540|480|360)p$")
_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:S0*(\d{1,3})[^A-Za-z0-9]*E0*(\d{1,4})|"
    r"0*(\d{1,3})x0*(\d{1,4}))(?!\d)", re.I)
_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])S0*(\d{1,3})(?!\d)", re.I)
_SEASON_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])Seasons?[\s._-]*0*(\d{1,3})(?!\d)", re.I)
_SEASON_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:S0*(\d{1,3})|Seasons?[\s._-]*0*(\d{1,3}))"
    r"[\s._]*(?:-|to|thru|through)[\s._]*"
    r"(?:S(?:easons?)?[\s._-]*)?0*(\d{1,3})(?!\d)", re.I)


# A safe title boundary is not merely "the expected letters came first".  The
# next token must look like release metadata; otherwise ``It`` would match
# ``It Follows`` and ``Up`` could match a longer title.
_TAIL_MARKERS = frozenset({
    "uhd", "hd", "sd", "4k", "8k", "web", "webdl", "webrip",
    "bluray", "bdrip", "brrip", "remux", "hdtv", "dvdrip", "dvd",
    "repack", "proper", "extended", "theatrical", "imax", "internal",
    "hdr", "hdr10", "hdr10plus", "dovi", "dv", "sdr", "complete",
    "season", "seasons",
    "multi", "dual", "dubbed", "subbed", "hevc", "avc", "x264",
    "x265", "h264", "h265", "av1", "vc1", "aac", "ac3", "eac3",
    "ddp", "dts", "truehd", "atmos", "proper", "rerip",
})


_REGION_CANON = {
    "us": "us", "usa": "us", "unitedstates": "us",
    "unitedstatesofamerica": "us", "american": "us",
    "uk": "uk", "gb": "uk", "gbr": "uk", "unitedkingdom": "uk",
    "british": "uk",
    "au": "au", "aus": "au", "australia": "au", "australian": "au",
    "ca": "ca", "can": "ca", "canada": "ca", "canadian": "ca",
    "jp": "jp", "jpn": "jp", "japan": "jp", "japanese": "jp",
    "kr": "kr", "kor": "kr", "korea": "kr", "korean": "kr",
    "fr": "fr", "fra": "fr", "france": "fr", "french": "fr",
    "de": "de", "deu": "de", "germany": "de", "german": "de",
    "es": "es", "esp": "es", "spain": "es", "spanish": "es",
    "it": "it", "ita": "it", "italy": "it", "italian": "it",
}

_GENERIC_NAMES = frozenset({
    "file", "video", "stream", "movie", "episode", "default", "unknown",
    "download", "play", "source", "index", "media",
})


def _fold(value: str) -> str:
    """Case/diacritic fold while retaining non-Latin letters and digits."""
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", _fold(value), flags=re.UNICODE))


def _canonical_region(value: str) -> str:
    joined = "".join(_tokens(value))
    return _REGION_CANON.get(joined, joined)


def _clean_evidence_text(value: str) -> str:
    """Take a basename when given a path and remove only a known video suffix."""
    text = unquote(str(value or "")).strip().replace("\\", "/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return _VIDEO_EXT_RE.sub("", text).strip()


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    """Authoritative identity expected for one picker request.

    ``aliases`` should contain canonical English and original/native titles.
    ``years`` may contain more than one authoritative release year when
    providers legitimately disagree about festival/theatrical dates.  Region
    values accept common country names and tags and are canonicalized.
    """

    media: str
    imdb_id: str
    aliases: tuple[str, ...]
    years: frozenset[int] = frozenset()
    season: int | None = None
    episode: int | None = None
    region_tags: frozenset[str] = frozenset()
    metadata_conflict: bool = False
    runtime_seconds: float | None = None

    def __post_init__(self) -> None:
        media = (self.media or "").strip().lower()
        if media == "tv":
            media = "series"
        if media not in {"movie", "series"}:
            raise ValueError("media must be 'movie' or 'series'")
        imdb_id = (self.imdb_id or "").strip().lower().split(":", 1)[0]
        if imdb_id and not re.fullmatch(r"tt\d+", imdb_id):
            raise ValueError("imdb_id must be empty or an IMDb tt-number")

        aliases = tuple(dict.fromkeys(
            alias.strip() for alias in self.aliases if str(alias).strip()))
        years = frozenset(int(year) for year in self.years
                          if 1800 <= int(year) <= 2200)
        regions = frozenset(filter(None, (
            _canonical_region(tag) for tag in self.region_tags)))

        if (self.season is None) != (self.episode is None):
            raise ValueError("season and episode must be supplied together")
        if self.season is not None and (self.season < 0 or self.episode < 0):
            raise ValueError("season and episode must be non-negative")
        if media == "movie" and self.season is not None:
            raise ValueError("movie profiles cannot carry an episode")
        runtime = self.runtime_seconds
        if runtime is not None:
            runtime = float(runtime)
            if not math.isfinite(runtime) or runtime <= 0:
                raise ValueError("runtime_seconds must be positive and finite")

        object.__setattr__(self, "media", media)
        object.__setattr__(self, "imdb_id", imdb_id)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "years", years)
        object.__setattr__(self, "region_tags", regions)
        object.__setattr__(self, "runtime_seconds", runtime)


@dataclass(frozen=True, slots=True)
class IdentityAssessment:
    """Classification plus its independently rankable evidence tier."""

    state: str
    evidence: str
    rank: int


def _tail_boundary(token: str) -> bool:
    return bool(
        token in _TAIL_MARKERS
        or _YEAR_RE.fullmatch(token)
        or _RES_RE.fullmatch(token)
        or re.fullmatch(r"s\d{1,3}(?:e\d{1,4})?", token)
        or re.fullmatch(r"\d{1,3}x\d{1,4}", token)
        or re.fullmatch(r"(?:x|h)26[45]", token)
        or token in _REGION_CANON
    )


def _title_remainder(profile: IdentityProfile,
                     text_tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return tokens after the best safe alias match, or None for no match."""
    matches: list[tuple[int, tuple[str, ...]]] = []
    for alias in profile.aliases:
        alias_tokens = _tokens(alias)
        target = "".join(alias_tokens)
        if not target:
            continue
        # Compare compact alphanumerics so scene punctuation variation remains
        # harmless: ``Schindler's``/``Schindlers`` and ``WALL-E``/``WALLE``.
        # Consumption still ends on a complete candidate token; that, plus the
        # metadata-boundary check, preserves It/It Follows and Up/Upgrade.
        compact = ""
        for consumed, token in enumerate(text_tokens, 1):
            compact += token
            if not target.startswith(compact):
                break
            if compact != target:
                continue
            remainder = text_tokens[consumed:]
            pack_boundary = (len(remainder) >= 2 and (
                remainder[:2] in (("all", "seasons"),
                                  ("series", "complete"),
                                  ("show", "complete"))))
            if remainder and not (_tail_boundary(remainder[0])
                                  or pack_boundary):
                break
            matches.append((len(target), remainder))
            break
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _episode_details(
        text: str,
        ) -> tuple[list[tuple[int, int]], set[int],
                   list[tuple[tuple[int, int], tuple[int, int]]],
                   list[tuple[int, int]]]:
    """Ordered episode mentions, seasons, and inclusive episode ranges.

    Scene names compress bundles in several ways (``S01E01-E06``,
    ``S01E01-06``, ``S01E01E02``). Keeping their order and ranges separately
    lets callers distinguish a file that *starts* at the requested episode from
    a season container that merely contains it.
    """
    mentions: list[tuple[int, int]] = []
    seasons = {int(m.group(1)) for m in _SEASON_RE.finditer(text)}
    seasons.update(int(m.group(1)) for m in _SEASON_WORD_RE.finditer(text))
    ranges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    season_ranges: list[tuple[int, int]] = []
    for match in _SEASON_RANGE_RE.finditer(text):
        start = int(match.group(1) or match.group(2))
        end = int(match.group(3))
        season_ranges.append((start, end))
        seasons.update((start, end))
    consumed_until = -1
    for match in _EPISODE_RE.finditer(text):
        if match.start() < consumed_until:
            continue
        season = int(match.group(1) or match.group(3))
        episode = int(match.group(2) or match.group(4))
        first = (season, episode)
        mentions.append(first)
        seasons.add(season)
        previous = first
        pos = match.end()
        while True:
            # E-prefixed continuations cover E01-E03, E01E02, and
            # cross-season S01E06-S02E01 forms.
            extra = re.match(
                r"(?P<sep>[\s._-]*(?:(?:to|thru|through)[\s._-]*)?)"
                r"(?:S0*(?P<season>\d{1,3})[^A-Za-z0-9]*)?"
                r"E0*(?P<episode>\d{1,4})(?!\d)",
                text[pos:], re.I)
            bare = None
            if extra is None:
                # The common S01E01-03 shorthand omits the second E. Require
                # an explicit separator so release-group digits cannot become
                # accidental episode evidence.
                bare = re.match(
                    r"[\s._]*(?:-|to|thru|through)[\s._]*"
                    r"0*(?P<episode>\d{1,4})(?!\d)",
                    text[pos:], re.I)
            if extra is None and bare is None:
                break
            if extra is not None:
                next_season = int(extra.group("season") or season)
                next_episode = int(extra.group("episode"))
                separator = extra.group("sep") or ""
                step = extra.end()
            else:
                next_season = season
                next_episode = int(bare.group("episode"))
                separator = bare.group(0)
                step = bare.end()
            current = (next_season, next_episode)
            mentions.append(current)
            seasons.add(next_season)
            if ("-" in separator
                    or re.search(r"\b(?:to|thru|through)\b", separator, re.I)):
                ranges.append((previous, current))
            previous = current
            pos += step
        consumed_until = max(consumed_until, pos)
    return mentions, seasons, ranges, season_ranges


def _episode_evidence(text: str) -> tuple[set[tuple[int, int]], set[int], bool]:
    mentions, seasons, ranges, _season_ranges = _episode_details(text)
    return set(mentions), seasons, bool(ranges or len(mentions) > 1)


def _episode_relation(text: str, wanted: tuple[int, int]) -> str:
    """Relationship of release text to one requested episode.

    ``exact`` and ``starts`` are safe for automatic playback. ``contains`` and
    ``pack`` are useful container evidence but need an exact selected member
    before they can lead. ``contradiction`` explicitly excludes the request;
    ``missing`` has no episode information at all.
    """
    mentions, seasons, ranges, season_ranges = _episode_details(text)
    contained = set(mentions)
    for start, end in ranges:
        if start[0] == end[0]:
            lo, hi = sorted((start[1], end[1]))
            if wanted[0] == start[0] and lo <= wanted[1] <= hi:
                contained.add(wanted)
        elif min(start, end) <= wanted <= max(start, end):
            contained.add(wanted)
    if mentions:
        if wanted not in contained:
            return "contradiction"
        if len(mentions) == 1 and not ranges:
            return "exact"
        return "starts" if mentions[0] == wanted else "contains"
    if season_ranges:
        if any(min(start, end) <= wanted[0] <= max(start, end)
               for start, end in season_ranges):
            return "pack"
        return "contradiction"
    if seasons:
        return "pack" if seasons == {wanted[0]} else "contradiction"
    return "missing"


def _prefix_regions(remainder: tuple[str, ...]) -> set[str]:
    """Region/edition tags before technical release metadata.

    A release-group token at the very end should not accidentally become
    country evidence.  Region tags are identity-bearing only in the short
    prefix between the title and the first episode/resolution/source marker.
    """
    found: set[str] = set()
    for token in remainder:
        if (_RES_RE.fullmatch(token)
                or re.fullmatch(r"s\d{1,3}(?:e\d{1,4})?", token)
                or re.fullmatch(r"\d{1,3}x\d{1,4}", token)
                or token in _TAIL_MARKERS):
            break
        region = _REGION_CANON.get(token)
        if region:
            found.add(region)
    return found


def _missing_identity(tokens: tuple[str, ...]) -> bool:
    if not tokens:
        return True
    if len(tokens) == 1:
        token = tokens[0]
        if token in _GENERIC_NAMES:
            return True
        # Obfuscated DAV filenames and opaque hashes carry no semantic title.
        if len(token) >= 16 and (re.fullmatch(r"[0-9a-f]+", token)
                                 or not re.search(r"[aeiou]", token)):
            return True
    return False


def classify(profile: IdentityProfile, text: str, *,
             trusted_imdb: bool = False,
             trusted_episode: bool = False) -> str:
    """Classify one filename or trusted release label against ``profile``.

    Trust flags are explicit call-site context, never fields read from ``text``
    or an upstream mapping.  A trusted IMDb binding supports positive evidence
    but cannot rescue an explicit contradiction.  It means the individual item
    itself carried the requested IMDb id (Newznab/Jellyfin), not merely that an
    addon route was queried with it. ``trusted_episode`` is meaningful only for
    a series request.
    """
    evidence = _clean_evidence_text(text)
    tokens = _tokens(evidence)
    if not profile.aliases or _missing_identity(tokens):
        return UNKNOWN

    remainder = _title_remainder(profile, tokens)
    if remainder is None:
        return CONTRADICTION

    candidate_years = {int(token) for token in remainder
                       if _YEAR_RE.fullmatch(token)}
    year_match = bool(profile.years and candidate_years)
    if candidate_years and profile.years:
        if not candidate_years.issubset(profile.years):
            return CONTRADICTION
    elif candidate_years and not profile.years:
        # The candidate is explicit, but there is no authoritative year to
        # compare. It remains compatible rather than inventing confidence.
        year_match = False

    regions = _prefix_regions(remainder)
    region_match = bool(profile.region_tags and regions)
    if regions and profile.region_tags and not regions.issubset(profile.region_tags):
        return CONTRADICTION

    pairs, seasons, _multi_episode = _episode_evidence(evidence)
    exact_episode = False
    episode_relation = "missing"
    if profile.media == "movie":
        if pairs or seasons:
            return CONTRADICTION
    elif profile.season is not None:
        wanted = (profile.season, profile.episode)
        episode_relation = _episode_relation(evidence, wanted)
        if episode_relation == "contradiction":
            return CONTRADICTION
        exact_episode = episode_relation in {"exact", "starts"}

    # Conflicting provider metadata cannot establish confidence from a parsed
    # year alone. Exact per-item IMDb evidence still binds the file to the
    # requested catalog identity and is deliberately stronger than the
    # provider disagreement.
    if profile.metadata_conflict:
        if trusted_imdb and (profile.media == "movie"
                             or profile.season is None
                             or trusted_episode
                             or episode_relation in {"exact", "starts"}):
            return STRONG
        return COMPATIBLE

    if profile.media == "movie":
        if profile.years:
            # Exact per-item IMDb evidence safely resolves a yearless same-name
            # release. An explicit wrong year was rejected above before either
            # signal can promote it.
            return STRONG if (year_match or trusted_imdb) else COMPATIBLE
        return STRONG if trusted_imdb else COMPATIBLE

    if profile.season is not None:
        episode_proven = exact_episode or trusted_episode
        if not episode_proven:
            return COMPATIBLE
        # Most TV scene names omit the show's start year. Exact canonical title
        # + exact episode is sufficient unless an authoritative year/region was
        # explicitly contradicted above. Requiring the edition tag to be
        # repeated turned ordinary SxxExx files into unchecked fallbacks.
        return STRONG

    # Series-level requests have no episode identity to prove.
    return STRONG if (trusted_imdb and (year_match or region_match
                                       or not (profile.years or profile.region_tags))) \
        else COMPATIBLE


def classify_evidence(profile: IdentityProfile, *, filename: str = "",
                      release_label: str = "", trusted_imdb: bool = False,
                      trusted_episode: bool = False) -> str:
    """Combine filename and release-label evidence conservatively.

    Contradiction dominates: a correct job label cannot bless a mounted file
    whose basename explicitly names another title/episode.  Strong dominates
    compatible/unknown only when no supplied evidence contradicts it.
    """
    states = [
        classify(profile, value, trusted_imdb=trusted_imdb,
                 trusted_episode=trusted_episode)
        for value in (filename, release_label) if str(value or "").strip()
    ]
    if not states:
        return UNKNOWN
    if CONTRADICTION in states:
        return CONTRADICTION
    if STRONG in states:
        return STRONG
    if COMPATIBLE in states:
        return COMPATIBLE
    return UNKNOWN


def assess(profile: IdentityProfile, text: str, *,
           trusted_imdb: bool = False,
           trusted_episode: bool = False,
           measured_runtime_seconds: float | None = None,
           movie_min_delta: float = 8 * 60,
           movie_fraction: float = 0.15,
           episode_min_delta: float = 5 * 60,
           episode_fraction: float = 0.20) -> IdentityAssessment:
    """Classify and optionally corroborate compatible evidence by runtime.

    Runtime is deliberately one-way supporting evidence.  It can promote an
    exact canonical-title ``compatible`` result to ``strong`` when measured
    duration is close to the authoritative runtime.  It can never promote
    unknown/contradictory text, and TV additionally needs exact episode
    evidence (in the filename or trusted request context).

    The default tolerance admits ordinary alternate cuts while still
    distinguishing materially different same-name works: the larger of eight
    minutes or 15% for movies, and five minutes or 20% for episodes.
    """
    state = classify(profile, text, trusted_imdb=trusted_imdb,
                     trusted_episode=trusted_episode)
    if state == CONTRADICTION:
        return IdentityAssessment(state, EVIDENCE_CONTRADICTION,
                                  EVIDENCE_RANKS[EVIDENCE_CONTRADICTION])
    if state == UNKNOWN:
        return IdentityAssessment(state, EVIDENCE_UNKNOWN,
                                  EVIDENCE_RANKS[EVIDENCE_UNKNOWN])
    if state == STRONG:
        evidence = EVIDENCE_TRUSTED_IMDB if trusted_imdb else EVIDENCE_CANONICAL
        return IdentityAssessment(state, evidence, EVIDENCE_RANKS[evidence])

    # From here on the title boundary is known to match exactly: classify()
    # returns unknown/contradiction before compatible when it does not.
    expected = profile.runtime_seconds
    measured = measured_runtime_seconds
    runtime_ok = False
    if expected is not None and measured is not None:
        try:
            measured = float(measured)
            min_delta = movie_min_delta if profile.media == "movie" \
                else episode_min_delta
            fraction = movie_fraction if profile.media == "movie" \
                else episode_fraction
            tolerance = max(float(min_delta), expected * float(fraction))
            runtime_ok = (math.isfinite(measured) and measured > 0
                          and abs(measured - expected) <= tolerance)
        except (TypeError, ValueError):
            runtime_ok = False

    if runtime_ok and profile.media == "series":
        if profile.season is None:
            runtime_ok = False
        else:
            relation = _episode_relation(
                _clean_evidence_text(text),
                (profile.season, profile.episode))
            runtime_ok = relation in {"exact", "starts"} or trusted_episode

    if runtime_ok:
        return IdentityAssessment(STRONG, EVIDENCE_RUNTIME,
                                  EVIDENCE_RANKS[EVIDENCE_RUNTIME])
    return IdentityAssessment(COMPATIBLE, EVIDENCE_COMPATIBLE,
                              EVIDENCE_RANKS[EVIDENCE_COMPATIBLE])


def corroborate_runtime(profile: IdentityProfile, text: str,
                        measured_runtime_seconds: float | None, *,
                        trusted_imdb: bool = False,
                        trusted_episode: bool = False,
                        **tolerances: float) -> str:
    """State-only convenience wrapper around :func:`assess`."""
    return assess(
        profile, text, trusted_imdb=trusted_imdb,
        trusted_episode=trusted_episode,
        measured_runtime_seconds=measured_runtime_seconds,
        **tolerances,
    ).state


def evidence_rank(profile: IdentityProfile, text: str, **kwargs) -> int:
    """Return the stable numeric evidence rank from :func:`assess`."""
    return assess(profile, text, **kwargs).rank


__all__ = [
    "IdentityProfile", "IdentityAssessment", "classify", "classify_evidence",
    "assess", "corroborate_runtime", "evidence_rank",
    "STRONG", "COMPATIBLE", "UNKNOWN", "CONTRADICTION", "STATES",
    "EVIDENCE_TRUSTED_IMDB", "EVIDENCE_CANONICAL", "EVIDENCE_RUNTIME",
    "EVIDENCE_ANIME", "EVIDENCE_COMPATIBLE", "EVIDENCE_UNKNOWN",
    "EVIDENCE_CONTRADICTION", "EVIDENCE_RANKS",
    "mark_auto_eligible", "auto_eligible", "_AUTO_ELIGIBLE_KEY",
]
