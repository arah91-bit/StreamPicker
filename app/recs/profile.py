"""Builds a per-user taste profile from that viewer's own watch history."""

import time
from datetime import datetime, timezone


def _parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def build_profile(watched_movies: list[dict], watched_shows: list[dict],
                  movie_ratings: list[dict], show_ratings: list[dict]) -> dict:
    """Returns:
    {
      genres: {movie: [(slug, weight)...], show: [...]},   # sorted desc
      decades: [(decade, weight)...],
      languages: [(iso_code, weight)...],
      seeds: [{title, year, type, tmdb, imdb, last_watched, rating, score}...],
      loved: same shape, rating >= 8 sorted by rating then recency,
      watched_imdb: set, watched_tmdb_movie: set, watched_tmdb_show: set,
    }
    """
    now = time.time()
    ratings = {}
    for r in movie_ratings:
        ids = (r.get("movie") or {}).get("ids") or {}
        if ids.get("imdb"):
            ratings[("movie", ids["imdb"])] = r.get("rating", 0)
    for r in show_ratings:
        ids = (r.get("show") or {}).get("ids") or {}
        if ids.get("imdb"):
            ratings[("show", ids["imdb"])] = r.get("rating", 0)

    genre_w: dict[str, dict[str, float]] = {"movie": {}, "show": {}}
    decade_w: dict[int, float] = {}
    lang_w: dict[str, float] = {}
    seeds: list[dict] = []
    watched_imdb: set[str] = set()
    watched_tmdb: dict[str, set[int]] = {"movie": set(), "show": set()}

    for kind, entries in (("movie", watched_movies), ("show", watched_shows)):
        for entry in entries:
            item = entry.get(kind) or {}
            ids = item.get("ids") or {}
            if ids.get("imdb"):
                watched_imdb.add(ids["imdb"])
            if ids.get("tmdb"):
                watched_tmdb[kind].add(ids["tmdb"])

            plays = min(entry.get("plays", 1), 3)
            last = _parse_ts(entry.get("last_watched_at"))
            recency = 2.0 if now - last < 90 * 86400 else (1.3 if now - last < 365 * 86400 else 1.0)
            rating = ratings.get((kind, ids.get("imdb")), 0)
            rating_boost = 1.5 if rating >= 8 else (0.5 if 0 < rating <= 4 else 1.0)
            weight = plays * recency * rating_boost

            for slug in item.get("genres") or []:
                genre_w[kind][slug] = genre_w[kind].get(slug, 0) + weight
            if item.get("year"):
                decade = (item["year"] // 10) * 10
                decade_w[decade] = decade_w.get(decade, 0) + weight
            if item.get("language"):
                lang_w[item["language"]] = lang_w.get(item["language"], 0) + weight

            # Seed candidates: recently watched and liked (or rewatched)
            if ids.get("tmdb") and (rating >= 7 or rating == 0) and rating_boost >= 1.0:
                seeds.append({
                    "title": item.get("title"), "year": item.get("year"),
                    "type": kind, "tmdb": ids["tmdb"], "imdb": ids.get("imdb"),
                    "genres": item.get("genres") or [],
                    "last_watched": last, "rating": rating,
                    "score": weight + (rating / 10.0),
                })

    seeds.sort(key=lambda s: s["last_watched"], reverse=True)
    loved = sorted((s for s in seeds if s["rating"] >= 8),
                   key=lambda s: (s["rating"], s["last_watched"]), reverse=True)
    return {
        "genres": {
            k: sorted(v.items(), key=lambda kv: kv[1], reverse=True)
            for k, v in genre_w.items()
        },
        "decades": sorted(decade_w.items(), key=lambda kv: kv[1], reverse=True),
        "languages": sorted(lang_w.items(), key=lambda kv: kv[1], reverse=True),
        "seeds": seeds[:40],
        "loved": loved[:15],
        "watched_imdb": watched_imdb,
        "watched_tmdb_movie": watched_tmdb["movie"],
        "watched_tmdb_show": watched_tmdb["show"],
    }
