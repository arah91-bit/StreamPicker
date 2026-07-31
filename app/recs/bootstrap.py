"""The taste bootstrapper: a phone-sized way to tell the system what you like.

A profile with no history has nothing to recommend from, and waiting for it to
accumulate takes weeks of watching things it chose badly. This shortcuts that:
pick a few genres and countries, then tap through recognisable titles marking
each one liked, not for me, or never seen.

The point is not volume. Twenty verdicts is a better cold start than twenty
plays, because a play says only that something was opened while a tap says
which way it went — and one of those directions has never existed here before.
Everything else in this system infers taste from plays, where "did not watch"
and "never heard of" are the same observation, so nothing may be scored badly
in case the real cause was our own delivery failing. A tap on "not for me" is
unambiguous, and it is what lets the fingerprint carry a negative term at all.

Which is also why the cards have to be titles people *recognise*. A verdict on
something nobody has heard of is a coin flip dressed as data, so candidates
come from a high vote-count floor: famous, not obscure.
"""

from __future__ import annotations

import logging
import random

from app.recs import db, tmdb

logger = logging.getLogger("nuvio-recs")

# Genres offered on the opening screen. Slugs match `tmdb.MOVIE_GENRES` and
# `tmdb.TV_GENRES` so a pick is directly usable as a discover filter.
PICKABLE_GENRES = [
    ("action", "Action"), ("adventure", "Adventure"), ("animation", "Animation"),
    ("comedy", "Comedy"), ("crime", "Crime"), ("documentary", "Documentary"),
    ("drama", "Drama"), ("family", "Family"), ("fantasy", "Fantasy"),
    ("history", "History"), ("horror", "Horror"), ("music", "Music"),
    ("mystery", "Mystery"), ("romance", "Romance"),
    ("science-fiction", "Sci-Fi"), ("thriller", "Thriller"),
    ("war", "War"), ("western", "Western"),
]

# Countries worth offering as an origin filter. Kept short on purpose: this is
# a phone screen, and a list of two hundred is not a choice.
PICKABLE_COUNTRIES = [
    ("US", "United States"), ("GB", "United Kingdom"), ("JP", "Japan"),
    ("KR", "South Korea"), ("FR", "France"), ("ES", "Spain"),
    ("DE", "Germany"), ("IT", "Italy"), ("IN", "India"), ("CN", "China"),
    ("MX", "Mexico"), ("BR", "Brazil"), ("SE", "Sweden"), ("AU", "Australia"),
    ("CA", "Canada"), ("NG", "Nigeria"),
]

# A verdict on a title nobody recognises is noise. This floor is what keeps
# the deck famous.
MIN_VOTES = 900
MIN_SCORE = 6.0
# How deep into the popularity ranking a session may reach. Beyond this the
# titles stop being recognisable and the verdicts stop meaning anything.
MAX_PAGE = 8
DECK_SIZE = 24


def _slug_ids(slugs: list[str], media: str) -> str:
    table = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
    ids = {str(table[s]) for s in slugs if s in table}
    return "|".join(sorted(ids))


async def deck(user: dict, seen: set[str], size: int = DECK_SIZE,
               rng: random.Random | None = None) -> list[dict]:
    """The next handful of cards for one viewer.

    `seen` is every title they have already ruled on or played, so a session
    never asks twice and never asks about something already in their history.
    """
    rng = rng or random.Random()
    genres, countries = db.bootstrap_taste(user)
    out: list[dict] = []
    medias = ["movie", "tv"]
    rng.shuffle(medias)
    for media in medias:
        params = {
            "sort_by": "popularity.desc",
            "vote_count.gte": MIN_VOTES,
            "vote_average.gte": MIN_SCORE,
            "include_adult": "false",
            "page": rng.randint(1, MAX_PAGE),
        }
        picked = _slug_ids(genres, media)
        if picked:
            params["with_genres"] = picked
        elif genres:
            # They asked for something this medium does not have — TMDB's TV
            # taxonomy carries no Horror or Romance, for instance. Skipping is
            # the honest answer; sweeping unfiltered would quietly hand back
            # generic popular television and call it their pick.
            continue
        if countries:
            params["with_origin_country"] = "|".join(countries)
        try:
            results = await tmdb.discover(media, params)
        except Exception:
            logger.debug("bootstrap: discover failed", exc_info=True)
            continue
        rng.shuffle(results)
        for item in results:
            if len(out) >= size:
                break
            meta = await _card(media, item, seen)
            if meta:
                out.append(meta)
                seen.add(meta["id"])
    return out[:size]


async def _card(media: str, item: dict, seen: set[str]) -> dict | None:
    """One card, or None if it is unusable or already ruled on."""
    try:
        meta = await tmdb.resolve_meta(media, item["id"],
                                       require_home_release=False)
    except Exception:
        return None
    if not meta or meta.get("id") in seen:
        return None
    return {
        "id": meta["id"],
        "type": "movie" if media == "movie" else "series",
        "title": meta.get("name"),
        "year": (meta.get("releaseInfo") or "")[:4],
        "poster": meta.get("poster"),
        "genres": (meta.get("genres") or [])[:3],
        "rating": meta.get("imdbRating"),
        "overview": (meta.get("description") or "")[:220],
    }


async def already_known(user_token: str, viewer_key: str) -> set[str]:
    """Titles this viewer has already rated or played."""
    known = set(await db.feedback_for(user_token))
    try:
        for row in await db.played_titles(viewer_key):
            known.add(row["imdb_id"])
    except Exception:
        logger.debug("bootstrap: could not read play history", exc_info=True)
    return known


async def progress(user_token: str) -> dict:
    """Counts for the progress readout, and whether it is worth rebuilding."""
    counts = await db.feedback_counts(user_token)
    rated = counts.get("liked", 0) + counts.get("disliked", 0)
    return {
        "liked": counts.get("liked", 0),
        "disliked": counts.get("disliked", 0),
        "seen": counts.get("seen", 0),
        "rated": rated,
        # Enough to build a fingerprint from nothing else. Below this the
        # session is still worth finishing; above it, a rebuild will produce
        # something meaningfully different.
        "enough": rated >= 12,
    }
