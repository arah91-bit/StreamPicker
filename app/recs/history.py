"""Watch history as the taste model already expects it, sourced locally.

`profile.build_profile` was written against Trakt's `/sync/watched` shape. Rather
than rewrite the taste model — and every row that reads its output — this module
produces that same shape from `play_history`, so the switch away from Trakt is a
change of source rather than a change of meaning.

Two things Trakt gave us have no local equivalent, and both are handled here
rather than left to silently degrade downstream:

* **Ratings.** Nobody rates anything in a player. Engagement stands in: a title
  someone played to the end, or came back to more than once, is treated as
  liked. This is a derived signal and is deliberately conservative — it can
  say "they liked this", never "they disliked this", because not finishing
  something is far too ambiguous to read as a bad review.
* **Metadata.** Trakt returned genres, year and language inline. Those come
  from TMDB here, through the same `meta_cache` the catalogs already use, so a
  profile build costs no new API calls after the first sight of a title.
"""

import logging
import time

from app.recs import db, tmdb

logger = logging.getLogger("nuvio-recs")

# A play reaching this share of the file counts as finished. Bytes delivered
# run ahead of what was watched, so this is deliberately short of 100%.
FINISHED_PCT = 85.0
# Derived stand-ins for a Trakt rating. 8+ is what `build_profile` treats as
# "loved", which is the signal the because-you-loved rows are built on.
RATING_FINISHED = 8
RATING_REWATCHED = 9


def _derived_rating(row: dict) -> int:
    """Engagement as a rating. Returns 0 for "no opinion", never a low score."""
    if (row.get("plays") or 0) >= 2:
        return RATING_REWATCHED
    if (row.get("best_pct") or 0) >= FINISHED_PCT:
        return RATING_FINISHED
    return 0


async def _meta_for(imdb_id: str, media_type: str) -> dict | None:
    """TMDB id + genres/year/language for one IMDb id, cache-first."""
    try:
        found = await tmdb.find_by_imdb(imdb_id)
    except Exception:
        logger.debug("history: TMDB lookup failed for %s", imdb_id, exc_info=True)
        return None
    if not found:
        return None
    tmdb_id = found["tmdb_id"]
    meta = await tmdb.resolve_meta(found["media_type"], tmdb_id,
                                   require_home_release=False)
    if not meta:
        return None
    year = None
    try:
        year = int(str(meta.get("releaseInfo") or "")[:4])
    except (TypeError, ValueError):
        pass
    return {
        "tmdb": tmdb_id,
        "title": meta.get("name"),
        "year": year,
        "genres": [g.lower().replace(" ", "-") for g in meta.get("genres") or []],
    }


async def watched_lists(viewer_key: str) -> tuple[list[dict], list[dict],
                                                  list[dict], list[dict]]:
    """(watched_movies, watched_shows, movie_ratings, show_ratings).

    Exactly the four arguments `build_profile` takes, in Trakt's own shape.
    """
    rows = await db.played_titles(viewer_key)
    movies: list[dict] = []
    shows: list[dict] = []
    movie_ratings: list[dict] = []
    show_ratings: list[dict] = []

    for row in rows:
        imdb_id = row["imdb_id"]
        kind = "movie" if row["media_type"] == "movie" else "show"
        meta = await _meta_for(imdb_id, row["media_type"])
        if not meta:
            continue
        ids = {"imdb": imdb_id, "tmdb": meta["tmdb"]}
        item = {
            "ids": ids,
            "title": meta["title"],
            "year": meta["year"],
            "genres": meta["genres"],
            "language": None,
        }
        entry = {
            kind: item,
            "plays": row.get("plays") or 1,
            "last_watched_at": _iso(row.get("last_played_at")),
        }
        rating = _derived_rating(row)
        if kind == "movie":
            movies.append(entry)
            if rating:
                movie_ratings.append({"movie": item, "rating": rating})
        else:
            shows.append(entry)
            if rating:
                show_ratings.append({"show": item, "rating": rating})
    logger.info("history: %d movies, %d shows from local plays (%d rated by "
                "engagement)", len(movies), len(shows),
                len(movie_ratings) + len(show_ratings))
    return movies, shows, movie_ratings, show_ratings


def _iso(epoch: int | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(int(epoch)))


async def last_play_at(viewer_key: str) -> int:
    """Most recent play, or 0. Replaces Trakt's `last_activities` as the
    "is a rebuild worth doing" signal."""
    rows = await db.play_history(viewer_key, limit=1)
    return int(rows[0]["played_at"]) if rows else 0


async def in_progress(viewer_key: str, limit: int = 200) -> list[dict]:
    """Part-watched plays, most recent first — the Continue Watching source."""
    out = []
    seen: set[str] = set()
    for row in await db.play_history(viewer_key, limit=limit * 4):
        pct = row.get("position_pct")
        if pct is None or not (2.0 <= pct < FINISHED_PCT):
            continue
        if row["content_id"] in seen:
            continue
        seen.add(row["content_id"])
        out.append(row)
        if len(out) >= limit:
            break
    return out
