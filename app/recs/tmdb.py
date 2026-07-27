"""TMDB client: discover/search/recommendations plus Stremio meta building.

Metas are cached in SQLite by (tmdb_id, media_type) together with the US age
certification — content metadata is the same for everyone, so this cache is
safe to share and makes each nightly refresh cheaper than the last.

Kid-profile safety: resolve_meta() is the single chokepoint every catalog item
passes through, so passing max_age filters ALL sources (Trakt recs, discover,
trending, Gemini suggestions) by certification. Unknown certification is
treated as not kid-safe."""

import asyncio
import datetime
import re
from typing import Any, Literal, TypedDict

import httpx

from app.recs import config, db, kids

IMG = "https://image.tmdb.org/t/p"

_client = httpx.AsyncClient(
    base_url="https://api.themoviedb.org/3",
    timeout=30,
)
_sem = asyncio.Semaphore(8)

# TMDB movie release types: 1 premiere, 2 limited theatrical, 3 theatrical,
# 4 digital, 5 physical, 6 TV. Only the latter three prove that a movie has a
# legitimate non-CAM home-viewing source.
HOME_RELEASE_TYPES = {4, 5, 6}
UNKNOWN_HOME_RELEASE = "?"

# Trakt genre slugs → TMDB genre ids
MOVIE_GENRES = {
    "action": 28, "adventure": 12, "animation": 16, "anime": 16, "comedy": 35,
    "crime": 80, "documentary": 99, "drama": 18, "family": 10751, "fantasy": 14,
    "history": 36, "horror": 27, "music": 10402, "musical": 10402, "mystery": 9648,
    "romance": 10749, "science-fiction": 878, "superhero": 28, "suspense": 53,
    "thriller": 53, "war": 10752, "western": 37,
}
TV_GENRES = {
    "action": 10759, "adventure": 10759, "animation": 16, "anime": 16, "comedy": 35,
    "crime": 80, "documentary": 99, "drama": 18, "family": 10751,
    "fantasy": 10765, "mystery": 9648, "reality": 10764,
    "science-fiction": 10765, "superhero": 10759, "suspense": 9648, "thriller": 80,
    "war": 10768, "western": 37,
}
GENRE_LABELS = {
    "science-fiction": "Sci-Fi", "documentary": "Documentary",
}

# ── certification → age, across rating systems worldwide ────────────────
# The kid filter takes the STRICTEST age among every country's rating for a
# title, so foreign content is judged by its own boards (FSK, BBFC, Eirin,
# Kijkwijzer, …), not just the US system. Titles unrated everywhere are
# blocked for kid profiles.

_CERT_ZERO = {"G", "U", "TP", "AL", "ALL", "APTA", "T", "L", "TV-Y", "TV-G",
              "GENERAL", "0", "0+", "SU", "ATP"}
_CERT_MAP = {
    # PG means "parental guidance", not 8+ — most family films (Frozen, Shrek,
    # Totoro) carry it, so treat as fine from age 6.
    "PG": 6, "TV-PG": 6,
    "TV-Y7": 7, "TV-Y7-FV": 7,
    "12A": 12, "PG12": 12, "UA": 12,
    "PG-13": 13,
    "TV-14": 14, "14A": 14,
    "M": 15, "MA15+": 15, "MA 15+": 15, "R15+": 15,
    "R": 17, "TV-MA": 17,
    "NC-17": 18, "X": 18, "R18+": 18, "18A": 18, "A18": 18,
}
# Letters that mean opposite things in different countries.
_CERT_COUNTRY = {
    ("IN", "A"): 18, ("IN", "S"): 18,
    ("ES", "A"): 0, ("PT", "A"): 0, ("MX", "A"): 0, ("AR", "A"): 0,
}
_CERT_UNRATED = {"NR", "UNRATED", "NOT RATED", "UR", ""}


def rating_to_age(country: str, rating: str | None) -> int | None:
    """Minimum age implied by one country's certification, None if unknown."""
    r = (rating or "").strip().upper()
    if r in _CERT_UNRATED:
        return None
    if (country, r) in _CERT_COUNTRY:
        return _CERT_COUNTRY[(country, r)]
    if r in _CERT_ZERO:
        return 0
    if r in _CERT_MAP:
        return _CERT_MAP[r]
    m = re.search(r"(\d{1,2})", r)  # FSK "12", "16+", "TV-14", "M/6", KR "15"
    if m:
        return min(int(m.group(1)), 18)
    return None

LANG_NAMES = {
    "ja": "Japanese", "ko": "Korean", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "hi": "Hindi",
    "zh": "Chinese", "cn": "Chinese", "sv": "Swedish", "da": "Danish",
    "no": "Norwegian", "nl": "Dutch", "tr": "Turkish", "ru": "Russian",
    "pl": "Polish", "th": "Thai",
}


def genre_label(slug: str) -> str:
    return GENRE_LABELS.get(slug, slug.replace("-", " ").title())


_KID_FRIENDLY_GENRES = {"Animation", "Family", "Kids"}


# Certification answers "may this child watch it?"; age appeal answers the
# separate, softer question "is this likely to interest a child this age?".
# Keep the latter explainable and based only on fields already present in a
# resolved Stremio meta so every catalog can apply it after resolve_meta().
KidAgeBand = Literal["preschool", "early-childhood", "school-age", "teen",
                     "not-applicable"]
KidAppealClassification = Literal["strong", "good", "neutral", "weak"]


class KidAgeAppealSignal(TypedDict):
    code: str
    weight: int
    explanation: str


class KidAgeAppeal(TypedDict):
    score: int
    classification: KidAppealClassification
    age_band: KidAgeBand
    signals: list[KidAgeAppealSignal]


_PRESCHOOL_TEXT = re.compile(
    r"\b(?:preschool(?:er)?s?|toddler(?:s)?|nursery rhymes?|alphabet|"
    r"counting|sing[ -]?along|playgroup|early learning|young viewers?)\b",
    re.IGNORECASE,
)
_CHILD_STORY_TEXT = re.compile(
    r"\b(?:child(?:ren)?|kids?|young (?:boy|girl)|friendship|schoolchildren|"
    r"pupp(?:y|ies)|kittens?|magical friends?)\b",
    re.IGNORECASE,
)
_TEEN_TEXT = re.compile(
    r"\b(?:teens?|teenage(?:r)?s?|adolescen(?:t|ce)|high school|"
    r"coming[ -]of[ -]age|first love)\b",
    re.IGNORECASE,
)
_ADULT_NARRATIVE_TEXT = re.compile(
    r"\b(?:marriage|divorce|politic(?:s|al)|president|career|corporate|"
    r"businessman|murder|homicide|war veteran|military campaign|"
    r"investigation|midlife|biograph(?:y|ical))\b",
    re.IGNORECASE,
)

_PRESCHOOL_BROAD_GENRES = {"adventure", "comedy", "fantasy", "music"}
_ADULT_SKEWING_GENRES = {
    "crime", "documentary", "drama", "history", "horror", "romance",
    "thriller", "war", "western",
}
_SCHOOL_AGE_GENRES = {
    "action", "adventure", "animation", "comedy", "fantasy", "mystery",
    "science fiction",
}
_TEEN_GENRES = {
    "action", "adventure", "comedy", "drama", "fantasy", "mystery",
    "science fiction", "thriller",
}


def _meta_year(meta: dict) -> int | None:
    match = re.search(r"\b(18|19|20)\d{2}\b", str(meta.get("releaseInfo") or ""))
    return int(match.group()) if match else None


def _kid_age_band(kid_age: int | None) -> KidAgeBand:
    # kids.AGE_BANDS is the single definition of these boundaries; the catalog
    # builder labels its age slider from the same table.
    band = kids.band_for_age(kid_age)
    return band["id"] if band else "not-applicable"


def kid_age_appeal(meta: dict, kid_age: int | None) -> KidAgeAppeal:
    """Return an explainable age-*appeal* signal for an already-resolved meta.

    This is intentionally not a content-safety filter.  Callers must still use
    resolve_meta(..., max_age=kid_age), whose certification check remains the
    authority on whether an item may appear.  Use ``score`` only as one ranking
    feature; a weak result is not a reason to discard an otherwise safe title.

    The scorer uses only the stable preview fields emitted by _build_meta:
    name, description, genres and releaseInfo.  A score of 4+ is ``strong``,
    2-3 ``good``, 0-1 ``neutral``, and a negative score ``weak``.  Each applied
    weight is returned in ``signals`` so ranking decisions can be inspected.
    """
    band = _kid_age_band(kid_age)
    if band == "not-applicable":
        return {
            "score": 0,
            "classification": "neutral",
            "age_band": band,
            "signals": [],
        }

    raw_genres = meta.get("genres") or []
    if isinstance(raw_genres, str):
        raw_genres = [raw_genres]
    genres = {str(genre).strip().casefold() for genre in raw_genres if genre}
    text = " ".join(str(meta.get(field) or "")
                    for field in ("name", "description"))
    preschool_text = bool(_PRESCHOOL_TEXT.search(text))
    child_story_text = bool(_CHILD_STORY_TEXT.search(text))
    teen_text = bool(_TEEN_TEXT.search(text))
    adult_narrative_text = bool(_ADULT_NARRATIVE_TEXT.search(text))
    child_genres = genres & {genre.casefold() for genre in _KID_FRIENDLY_GENRES}
    has_child_anchor = bool(child_genres or preschool_text)
    signals: list[KidAgeAppealSignal] = []

    def add(code: str, weight: int, explanation: str) -> None:
        signals.append({
            "code": code,
            "weight": weight,
            "explanation": explanation,
        })

    if band == "preschool":
        if "kids" in genres:
            add("kids-genre", 4, "Kids programming strongly matches preschool viewing")
        if "family" in genres:
            add("family-genre", 3, "Family programming is a good preschool signal")
        if "animation" in genres:
            add("animation-genre", 2, "Animation is a useful preschool signal")
        if {"family", "animation"} <= genres:
            add("animated-family-combination", 1,
                "Family animation is especially likely to hold preschool interest")
        if genres & _PRESCHOOL_BROAD_GENRES:
            add("playful-genre", 1,
                "Adventure, comedy, fantasy or music adds preschool appeal")
        if preschool_text:
            add("explicit-preschool-language", 3,
                "The title or synopsis explicitly describes preschool viewing")
        if child_story_text:
            add("child-centered-language", 1,
                "The title or synopsis describes children or child-friendly themes")

        # These are contextual penalties, never filters.  Applying them only
        # without child anchors keeps classics such as The Wizard of Oz or old
        # family animation eligible while moving adult-oriented G-rated drama,
        # history and documentaries below purpose-built preschool choices.
        if not has_child_anchor and genres & _ADULT_SKEWING_GENRES:
            add("adult-skewing-genres-without-child-anchor", -3,
                "The genre mix skews adult and has no Kids, Family, Animation, "
                "or explicit preschool anchor")
        if not has_child_anchor and adult_narrative_text:
            add("adult-narrative-without-child-anchor", -2,
                "The synopsis emphasizes adult life without a child-viewing anchor")
        year = _meta_year(meta)
        if not has_child_anchor and year is not None and year <= 1989:
            add("legacy-title-without-child-anchor", -1,
                "An older title without a child-viewing anchor is less likely to "
                "match a preschool browsing session")

    elif band == "early-childhood":
        if "kids" in genres:
            add("kids-genre", 3, "Kids programming strongly matches this age")
        if "family" in genres:
            add("family-genre", 2, "Family programming matches this age")
        if "animation" in genres:
            add("animation-genre", 1, "Animation adds appeal for this age")
        if genres & _PRESCHOOL_BROAD_GENRES:
            add("playful-genre", 1,
                "Adventure, comedy, fantasy or music adds age appeal")
        if child_story_text:
            add("child-centered-language", 1,
                "The story explicitly centers children or child-friendly themes")
        if preschool_text and kid_age is not None and kid_age >= 7:
            add("preschool-language-for-older-child", -1,
                "Explicit preschool framing may feel young for this viewer")
        if not has_child_anchor and genres & _ADULT_SKEWING_GENRES:
            add("adult-skewing-genres-without-child-anchor", -1,
                "The genre mix has no child-viewing anchor")
        if not has_child_anchor and adult_narrative_text:
            add("adult-narrative-without-child-anchor", -1,
                "The synopsis focuses on adult life without a child-viewing anchor")

    elif band == "school-age":
        if "kids" in genres:
            add("kids-genre", 1, "Kids programming remains relevant at this age")
        if "family" in genres:
            add("family-genre", 1, "Family programming remains relevant at this age")
        if genres & _SCHOOL_AGE_GENRES:
            add("school-age-genre", 2,
                "The genre mix commonly appeals to school-age viewers")
        if child_story_text or teen_text:
            add("peer-age-language", 1,
                "The story includes young characters or growing-up themes")
        if preschool_text:
            add("preschool-language-for-older-child", -2,
                "Explicit preschool framing is likely to feel young for this viewer")

    else:  # teen
        if genres & _TEEN_GENRES:
            add("teen-genre", 1, "The genre mix commonly appeals to teen viewers")
        if teen_text:
            add("teen-centered-language", 3,
                "The story explicitly centers teens or coming-of-age themes")
        if "kids" in genres:
            add("kids-genre-for-teen", -2,
                "A general Kids label may feel young for a teen viewer")
        if preschool_text:
            add("preschool-language-for-teen", -3,
                "Explicit preschool framing is unlikely to appeal to a teen viewer")

    score = sum(signal["weight"] for signal in signals)
    classification: KidAppealClassification
    if score >= 4:
        classification = "strong"
    elif score >= 2:
        classification = "good"
    elif score >= 0:
        classification = "neutral"
    else:
        classification = "weak"
    return {
        "score": score,
        "classification": classification,
        "age_band": band,
        "signals": signals,
    }


def kid_age_appeal_score(meta: dict, kid_age: int | None) -> int:
    """Convenience sort key for :func:`kid_age_appeal`."""
    return kid_age_appeal(meta, kid_age)["score"]


def cert_allowed(cert: str | None, max_age: int | None,
                 genres: list[str] | None = None) -> bool:
    """cert is what's in the meta cache: a numeric age string ("13"), "?" for
    fetched-but-unrated, a legacy US label from early cache rows, or None.

    Modern family films are almost never rated G — Mario, Frozen, Toy Story
    sequels are all PG — so PG/TV-PG *animated or family* titles are allowed
    for preschoolers too. PG live-action keeps its age-6 gate."""
    if max_age is None:
        return True
    if not cert or cert == "?":
        return False
    if cert.isdigit():
        age = int(cert)
    else:
        age = rating_to_age("US", cert)  # legacy cache rows stored US labels
        if age is None:
            return False
    if age <= max_age:
        return True
    if age == 6 and genres and _KID_FRIENDLY_GENRES & set(genres):
        return True
    return False


def _release_date(value: object) -> datetime.date | None:
    """Parse the calendar portion of a TMDB ISO release timestamp."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _home_release_eligibility(detail: dict) -> tuple[datetime.date | None, bool]:
    """Earliest date a clean home source should exist for a movie.

    A real Digital, Physical, or TV date from any country is authoritative.
    If TMDB has no usable home-release row, a conservative delay after the
    primary release prevents recent theatrical/CAM-only titles from appearing
    while retaining old films whose historical TMDB release data is sparse.
    A known future home release always wins over the fallback.
    """
    home_dates: list[datetime.date] = []
    release_groups = (detail.get("release_dates") or {}).get("results") or []
    if isinstance(release_groups, list):
        for group in release_groups:
            if not isinstance(group, dict):
                continue
            releases = group.get("release_dates") or []
            if not isinstance(releases, list):
                continue
            for release in releases:
                if not isinstance(release, dict):
                    continue
                try:
                    release_type = int(release.get("type"))
                except (TypeError, ValueError):
                    continue
                date = _release_date(release.get("release_date"))
                if release_type in HOME_RELEASE_TYPES and date is not None:
                    home_dates.append(date)
    if home_dates:
        return min(home_dates), True
    primary = _release_date(detail.get("release_date"))
    if primary is None:
        return None, False
    return (primary + datetime.timedelta(
        days=config.HOME_RELEASE_FALLBACK_DAYS), False)


def home_release_eligibility_date(detail: dict) -> datetime.date | None:
    """Public date-only view of :func:`_home_release_eligibility`."""
    return _home_release_eligibility(detail)[0]


def is_home_released(media_type: str, detail: dict,
                     as_of: datetime.date | None = None) -> bool:
    """Whether this title has reached a legitimate home-viewing release.

    Series remain governed by first-air dates. Movies require an arrived TMDB
    type 4/5/6 date, or the conservative old-title fallback described above.
    """
    if media_type != "movie":
        return True
    cutoff = as_of or datetime.datetime.now(datetime.timezone.utc).date()
    eligible, _ = _home_release_eligibility(detail)
    return eligible is not None and eligible <= cutoff


def _cached_home_release_state(cached: dict, as_of: datetime.date,
                               now: int) -> tuple[bool, bool]:
    """Return ``(allowed, needs_refetch)`` for one cached movie.

    Blocked/current-year records are periodically refreshed because TMDB can
    add a digital date after a theatrical premiere. Legacy records pre-dating
    this cache field are grandfathered only when their stored year proves they
    are already beyond the conservative fallback window.
    """
    marker = cached.get("home_release_date")
    verified = cached.get("home_release_verified") == 1
    checked_at = int(cached.get("updated_at") or 0)
    stale = checked_at <= now - config.HOME_RELEASE_RECHECK_HOURS * 3600
    if marker and marker != UNKNOWN_HOME_RELEASE:
        eligible = _release_date(marker)
        if eligible is not None and eligible <= as_of:
            # Fallback dates are deliberately not permanent proof. Continue
            # serving an old title, but recheck it periodically so a real TMDB
            # home date can replace the inference.
            return True, False if verified else stale
    legacy = marker is None
    if legacy:
        meta_year = _meta_year(cached.get("meta") or {})
        if meta_year is not None:
            latest_possible_primary = datetime.date(meta_year, 12, 31)
            if latest_possible_primary + datetime.timedelta(
                    days=config.HOME_RELEASE_FALLBACK_DAYS) <= as_of:
                return True, False
    return False, stale


async def _get(path: str, params: dict | None = None) -> Any:
    # The key rides on each request rather than on the client's default
    # params, so that an install without one fails here — where a row is
    # actually being built — and not on `import app.recs.tmdb`.
    params = {**(params or {}), "api_key": config.require("TMDB_API_KEY")}
    async with _sem:
        r = await _client.get(path, params=params)
    r.raise_for_status()
    return r.json()


async def discover(media_type: str, params: dict) -> list[dict]:
    """media_type: 'movie' | 'tv'. Returns raw TMDB result items."""
    data = await _get(f"/discover/{media_type}", params)
    return data.get("results", [])


async def tmdb_recommendations(media_type: str, tmdb_id: int) -> list[dict]:
    data = await _get(f"/{media_type}/{tmdb_id}/recommendations")
    return data.get("results", [])


async def trending(media_type: str, window: str = "week") -> list[dict]:
    """What is being watched right now. media_type: 'movie' | 'tv'.

    A velocity signal rather than a popularity one — /movie/popular ranks by a
    slow-moving score, so without this a "Trending Now" row would be nearly
    the same slate as "Popular Now" every day. Weekly rather than daily
    because a day's window is noisy enough to swing the row on one release.
    """
    data = await _get(f"/trending/{media_type}/{window}")
    return data.get("results", [])


async def popular(media_type: str) -> list[dict]:
    """Broadly popular titles. media_type: 'movie' | 'tv'."""
    data = await _get(f"/{media_type}/popular")
    return data.get("results", [])


async def movie_credits(tmdb_id: int) -> dict:
    """Returns {'cast': [...], 'crew': [...]} for a movie."""
    return await _get(f"/movie/{tmdb_id}/credits")


async def find_by_imdb(imdb_id: str) -> dict | None:
    """Resolve an IMDb id to a TMDB movie/tv result. Returns
    {'tmdb_id', 'media_type', 'genre_ids'} or None if not found."""
    data = await _get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    for media_type, key in (("movie", "movie_results"), ("tv", "tv_results")):
        results = data.get(key) or []
        if results:
            r = results[0]
            return {"tmdb_id": r["id"], "media_type": media_type,
                    "genre_ids": r.get("genre_ids") or []}
    return None


_season_cache: dict[tuple[int, int], dict[int, str]] = {}


async def episode_aired(tmdb_id: int, season: int, episode: int) -> bool:
    """Has this episode actually been released?

    Trakt used to answer this via `next_episode.first_aired`. Without it, a
    "next episode" derived by adding one can point past a season finale or at
    something merely announced — which becomes a card for content that does not
    exist. Seasons are cached: one lookup per show per process.
    """
    key = (int(tmdb_id), int(season))
    air_dates = _season_cache.get(key)
    if air_dates is None:
        try:
            data = await _get(f"/tv/{tmdb_id}/season/{season}")
        except (httpx.HTTPError, ValueError):
            return False        # unknown: do not offer it
        air_dates = {}
        for ep in data.get("episodes") or []:
            if ep.get("episode_number") is not None:
                air_dates[int(ep["episode_number"])] = ep.get("air_date") or ""
        _season_cache[key] = air_dates
    aired = air_dates.get(int(episode))
    if not aired:
        return False            # no such episode, or no air date yet
    return aired <= datetime.date.today().isoformat()


async def search_keywords(query: str, limit: int = 2) -> list[int]:
    data = await _get("/search/keyword", {"query": query})
    return [k["id"] for k in data.get("results", [])[:limit]]


async def search(media_type: str, query: str, year: int | None = None) -> list[dict]:
    params: dict[str, Any] = {"query": query}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    data = await _get(f"/search/{media_type}", params)
    return data.get("results", [])


# Which country's board decides, in order. US first (household baseline); a
# title unrated in the US is judged by its own/other major boards instead of
# slipping through or being blocked outright. Obscure-board outliers only
# matter when nothing in this list rated the title.
_CERT_PRIORITY = ["US", "GB", "CA", "AU", "DE", "FR", "JP", "KR", "ES", "IT",
                  "NL", "BR", "MX", "IN"]


def _extract_cert_age(media_type: str, detail: dict) -> int | None:
    """Minimum age per the highest-priority country that rated the title;
    None if unrated everywhere."""
    by_country: dict[str, int] = {}
    if media_type == "movie":
        for entry in (detail.get("release_dates") or {}).get("results", []):
            country = entry.get("iso_3166_1", "")
            for rel in entry.get("release_dates", []):
                age = rating_to_age(country, rel.get("certification"))
                if age is not None:
                    # strictest within one country (e.g. theatrical vs uncut)
                    by_country[country] = max(by_country.get(country, 0), age)
    else:
        for entry in (detail.get("content_ratings") or {}).get("results", []):
            country = entry.get("iso_3166_1", "")
            age = rating_to_age(country, entry.get("rating"))
            if age is not None:
                by_country[country] = max(by_country.get(country, 0), age)
    for country in _CERT_PRIORITY:
        if country in by_country:
            return by_country[country]
    if by_country:
        return max(by_country.values())
    return None


def _build_meta(media_type: str, imdb_id: str, detail: dict) -> dict:
    is_movie = media_type == "movie"
    title = detail.get("title") if is_movie else detail.get("name")
    date = (detail.get("release_date") if is_movie else detail.get("first_air_date")) or ""
    meta = {
        "id": imdb_id,
        "type": "movie" if is_movie else "series",
        "name": title,
        "description": detail.get("overview") or "",
    }
    if detail.get("poster_path"):
        meta["poster"] = f"{IMG}/w500{detail['poster_path']}"
    if detail.get("backdrop_path"):
        meta["background"] = f"{IMG}/original{detail['backdrop_path']}"
    if date[:4]:
        meta["releaseInfo"] = date[:4]
    if detail.get("vote_average"):
        meta["imdbRating"] = str(round(detail["vote_average"], 1))
    if detail.get("genres"):
        meta["genres"] = [g["name"] for g in detail["genres"]]
    return meta


async def resolve_meta(media_type: str, tmdb_id: int,
                       max_age: int | None = None,
                       require_home_release: bool = True) -> dict | None:
    """Return a Stremio meta preview (id = imdb tt-id) for a TMDB item, or
    None if it has no IMDb id, has no home release, or fails the kid filter.

    `require_home_release=False` is for surfaces built from what the viewer has
    already played — Continue Watching and Watch History. Home-release
    eligibility is a guess about whether a title can be watched yet, and a
    title in someone's own history has already answered that question; letting
    the guess win there would drop real entries out of their own backlog.
    """
    cached = await db.cache_get_meta(tmdb_id, media_type)
    # Rows cached before multi-country cert support have cert=None or a legacy
    # US label; for kid profiles re-fetch those so the worldwide-strictest age
    # is what gets enforced. "?" means already fetched and unrated everywhere.
    stale_cert = cached is not None and cached["cert"] is not None \
        and not (cached["cert"].isdigit() or cached["cert"] == "?")
    cert_needs_refetch = cached is not None and max_age is not None \
        and cached["imdb_id"] and (cached["cert"] is None or stale_cert)
    needs_refetch = cert_needs_refetch
    home_allowed = True
    if cached is not None and media_type == "movie" and require_home_release:
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        home_allowed, home_needs_refetch = _cached_home_release_state(
            cached, datetime.datetime.now(datetime.timezone.utc).date(), now)
        needs_refetch = needs_refetch or home_needs_refetch
    if cached is not None and not needs_refetch:
        if home_allowed and cached["imdb_id"] and cert_allowed(
                cached["cert"], max_age, (cached["meta"] or {}).get("genres")):
            return cached["meta"]
        return None
    append = "external_ids,release_dates" if media_type == "movie" \
        else "external_ids,content_ratings"
    try:
        detail = await _get(f"/{media_type}/{tmdb_id}", {"append_to_response": append})
    except (httpx.HTTPError, ValueError):
        # A transient status failure during a scheduled availability recheck
        # must not destroy previously good metadata. Refresh its timestamp to
        # avoid hammering the same failed lookup throughout this generation.
        if cached is not None:
            await db.cache_put_meta(
                tmdb_id, media_type, cached["imdb_id"], cached["meta"],
                cached["cert"], cached.get("home_release_date"),
                cached.get("home_release_verified"))
        else:
            await db.cache_put_meta(
                tmdb_id, media_type, None, None, None,
                UNKNOWN_HOME_RELEASE if media_type == "movie" else None,
                False if media_type == "movie" else None)
        if cached is not None and home_allowed and not cert_needs_refetch \
                and cached["imdb_id"] and cert_allowed(
                    cached["cert"], max_age,
                    (cached["meta"] or {}).get("genres")):
            return cached["meta"]
        return None
    imdb_id = (detail.get("external_ids") or {}).get("imdb_id") or detail.get("imdb_id")
    if not imdb_id or not imdb_id.startswith("tt") or detail.get("adult"):
        await db.cache_put_meta(
            tmdb_id, media_type, None, None, None,
            UNKNOWN_HOME_RELEASE if media_type == "movie" else None,
            False if media_type == "movie" else None)
        return None
    age = _extract_cert_age(media_type, detail)
    cert = str(age) if age is not None else "?"
    meta = _build_meta(media_type, imdb_id, detail)
    eligible, home_release_verified = _home_release_eligibility(detail) \
        if media_type == "movie" else (None, False)
    home_release_date = eligible.isoformat() if eligible \
        else UNKNOWN_HOME_RELEASE if media_type == "movie" else None
    await db.cache_put_meta(
        tmdb_id, media_type, imdb_id, meta, cert, home_release_date,
        home_release_verified if media_type == "movie" else None)
    if require_home_release and not is_home_released(media_type, detail):
        return None
    if not cert_allowed(cert, max_age, meta.get("genres")):
        return None
    return meta


async def resolve_many(media_type: str, tmdb_ids: list[int], exclude_imdb: set[str],
                       exclude_tmdb: set[int], limit: int,
                       max_age: int | None = None) -> list[dict]:
    """Resolve TMDB items to metas, dropping watched/excluded/age-blocked ones,
    keeping order."""
    seen: set[int] = set()
    ids = [i for i in tmdb_ids
           if i not in exclude_tmdb and not (i in seen or seen.add(i))]
    metas: list[dict] = []
    # Resolve in small batches so we stop early once we have enough.
    for start in range(0, len(ids), 12):
        batch = ids[start:start + 12]
        results = await asyncio.gather(
            *(resolve_meta(media_type, i, max_age) for i in batch))
        for meta in results:
            if meta and meta["id"] not in exclude_imdb:
                metas.append(meta)
        if len(metas) >= limit:
            break
    return metas[:limit]
