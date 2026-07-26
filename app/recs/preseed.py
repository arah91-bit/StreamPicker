"""Preseeds: hand-picked taste anchors and/or real watch history (e.g.
pulled from Nuvio's local history, which the addon protocol otherwise can't
read) that stand in for Trakt data until a user has built up enough of their
own. They feed the same profile machinery as real Trakt seeds — genre
weights + rotating "Because You …" rows — so the transition to real history
is gradual: real Trakt watches always outweigh preseeds, which stop applying
entirely once the user has PRESEED_MAX_HISTORY real watches.

Two kinds of entry, per user, in data/preseed.json:
  "taste":   [{type, tmdb, title}]  — no watch data, just "this fits them".
             Surfaces as "Because You Like {title}".
  "history": [{type, imdb, title, watched_at, progress, episodes?}] — an
             actual watch, with recency and engagement computed the same way
             build_profile() does for real Trakt data. Surfaces as
             "Because You Watched {title}".
"""

import datetime
import json
import logging
import os
from typing import Any

from app.recs import tmdb

logger = logging.getLogger("nuvio-recs")

PRESEED_FILE = os.environ.get("PRESEED_FILE", "/data/preseed.json")
# Preseeds apply while the user has fewer watched items than this.
PRESEED_MAX_HISTORY = int(os.environ.get("PRESEED_MAX_HISTORY", "15"))

# Below this computed engagement (0..1), a history entry is treated as noise
# (abandoned after a minute) and dropped rather than seeding a row.
MIN_ENGAGEMENT = 0.15


def _watched_timestamp(value: Any) -> float:
    """Parse an ISO watch date/time as a Unix timestamp in UTC.

    Nuvio exports date-only values as well as full ISO timestamps.  Naive
    values are defined as UTC so catalog generation is independent of the
    container's local timezone.
    """
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        watched = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if watched.tzinfo is None:
        watched = watched.replace(tzinfo=datetime.timezone.utc)
    return watched.timestamp()


def _seed_key(seed: dict) -> tuple | None:
    """Stable identity for deduplicating real and synthetic profile seeds."""
    kind = seed.get("type")
    if seed.get("tmdb") is not None:
        return kind, "tmdb", str(seed["tmdb"])
    if seed.get("imdb"):
        return kind, "imdb", seed["imdb"]
    if seed.get("title"):
        return kind, "title", str(seed["title"]).casefold(), seed.get("year")
    return None


def _dedupe_seeds(seeds: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[tuple] = set()
    for seed in seeds:
        key = _seed_key(seed)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        unique.append(seed)
    return unique


def _invert_first_wins(table: dict[str, int]) -> dict[int, str]:
    """MOVIE_GENRES/TV_GENRES alias multiple slugs to one TMDB id (e.g.
    'animation' and 'anime' both -> 16, so Trakt-style tags route to the same
    TMDB query). For the REVERSE lookup we want the canonical/first-declared
    slug, not whichever happened to be inserted last — otherwise every
    animated title (Toy Story, Bluey, Monsters Inc.) gets mislabeled 'anime'."""
    out: dict[int, str] = {}
    for slug, tmdb_id in table.items():
        out.setdefault(tmdb_id, slug)
    return out


_GENRE_ID_TO_SLUG = {
    "movie": _invert_first_wins(tmdb.MOVIE_GENRES),
    "tv": _invert_first_wins(tmdb.TV_GENRES),
}


def _load_raw() -> dict:
    try:
        with open(PRESEED_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"preseed file unreadable: {e}")
        return {}


def load_for(name: str | None) -> dict:
    """Returns {'taste': [...], 'history': [...]}, tolerating the old flat-
    list format (treated as all-taste) for any user not yet migrated."""
    if not name:
        return {"taste": [], "history": []}
    entry = _load_raw().get(name.strip().lower())
    if entry is None:
        return {"taste": [], "history": []}
    if isinstance(entry, list):  # legacy format
        return {"taste": entry, "history": []}
    return {"taste": entry.get("taste") or [], "history": entry.get("history") or []}


async def taste_seeds(entries: list[dict]) -> list[dict]:
    """Hand-picked anchors with no watch data — flat weight, no recency."""
    seeds = []
    for e in entries:
        media = "movie" if e.get("type") == "movie" else "tv"
        try:
            detail = await tmdb._get(f"/{media}/{e['tmdb']}",
                                     {"append_to_response": "external_ids"})
        except Exception:
            logger.warning(f"preseed taste lookup failed: {e}")
            continue
        genre_ids = {g["id"] for g in detail.get("genres", [])}
        slugs = [s for gid, s in _GENRE_ID_TO_SLUG[media].items() if gid in genre_ids]
        imdb = (detail.get("external_ids") or {}).get("imdb_id")
        date = detail.get("release_date") or detail.get("first_air_date") or ""
        seeds.append({
            "title": e.get("title") or detail.get("title") or detail.get("name"),
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "type": "movie" if media == "movie" else "show",
            "tmdb": e["tmdb"], "imdb": imdb, "genres": slugs,
            "last_watched": 0.0, "rating": 0, "score": 3.0,
            "preseed": True, "source": "taste", "watched": False,
        })
    return seeds


def _engagement(entry: dict) -> float:
    progress = max(0.0, min(1.0, entry.get("progress") or 0.0))
    if entry.get("type") == "movie":
        return progress
    episodes = max(1, entry.get("episodes") or 1)
    return min(1.0, 0.15 * episodes + progress)


async def history_seeds(entries: list[dict]) -> list[dict]:
    """Real watches (e.g. from Nuvio local history) resolved via IMDb id,
    weighted by actual recency and how much of it was watched — same shape
    as build_profile()'s seeds, so they compete fairly with real Trakt data."""
    seeds = []
    for e in entries:
        engagement = _engagement(e)
        if engagement < MIN_ENGAGEMENT:
            logger.info(f"preseed history: skipping '{e.get('title')}'"
                        f" (engagement {engagement:.2f}, likely abandoned)")
            continue
        try:
            found = await tmdb.find_by_imdb(e["imdb"])
        except Exception as exc:
            logger.warning(f"preseed history lookup failed for {e.get('title')}: {exc}")
            continue
        if not found:
            continue
        media = found["media_type"]
        slugs = [s for gid, s in _GENRE_ID_TO_SLUG[media].items()
                 if gid in found["genre_ids"]]
        last_watched = _watched_timestamp(e.get("watched_at"))
        # engagement -> rating-proxy, same >=8 threshold build_profile() uses
        # for the "loved" tier that unlocks More-Like/person rows
        rating = 9 if engagement >= 0.6 else 7 if engagement >= 0.35 else 5
        seeds.append({
            "title": e.get("title"), "year": None,
            "type": "movie" if media == "movie" else "show",
            "tmdb": found["tmdb_id"], "imdb": e["imdb"], "genres": slugs,
            "last_watched": last_watched, "rating": rating,
            "score": engagement * 3,
            "preseed": True, "source": "history", "watched": True,
            "engagement": engagement,
        })
    return seeds


def apply_watched_exclusions(profile: dict[str, Any], seeds: list[dict]) -> None:
    """Add imported-history IDs to the profile's watched exclusion sets.

    Taste anchors intentionally remain eligible recommendations.  ``source``
    is accepted as a fallback for callers holding seeds produced before the
    explicit ``watched`` marker was added.
    """
    watched_imdb = profile.setdefault("watched_imdb", set())
    watched_tmdb_movie = profile.setdefault("watched_tmdb_movie", set())
    watched_tmdb_show = profile.setdefault("watched_tmdb_show", set())
    for seed in seeds:
        if not seed.get("watched", seed.get("source") == "history"):
            continue
        if seed.get("imdb"):
            watched_imdb.add(seed["imdb"])
        if seed.get("tmdb") is not None:
            target = watched_tmdb_movie if seed.get("type") == "movie" \
                else watched_tmdb_show
            target.add(seed["tmdb"])


def apply_to_profile(profile: dict[str, Any], seeds: list[dict]) -> None:
    """Blend synthetic seeds into a (thin) profile: genre weights + seed pool."""
    for s in seeds:
        weight = 2.0 if s.get("source") == "taste" else 1.5 + 4 * s.get("engagement", 0.5)
        table = profile["genres"][s["type"]]  # list of (slug, weight)
        weights = dict(table)
        for slug in s["genres"]:
            weights[slug] = weights.get(slug, 0) + weight
        profile["genres"][s["type"]] = sorted(weights.items(),
                                              key=lambda kv: kv[1], reverse=True)
    apply_watched_exclusions(profile, seeds)

    # Existing Trakt seeds come first and therefore win identity collisions
    # with synthetic data.
    merged = _dedupe_seeds(profile["seeds"] + seeds)
    profile["seeds"] = sorted(
        merged, key=lambda s: s["last_watched"], reverse=True)[:40]

    loved_candidates = [s for s in merged if s["rating"] >= 8]
    loved_candidates.extend(profile["loved"])
    profile["loved"] = sorted(
        _dedupe_seeds(loved_candidates),
        key=lambda s: (s["rating"], s["last_watched"]),
        reverse=True,
    )[:15]
