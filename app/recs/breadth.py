"""How wide the surface actually is, as numbers rather than an impression.

This exists because "the recommendations got narrow" is something you notice
three weeks late, by which point you cannot tell which change did it. Every
build records its own composition, so narrowing shows up as a moving number
next to the change that moved it.

Measured before any of this was tuned, on a real 22-row surface: animation,
comedy and adventure held 40% of all slots, six of twenty-two rows were
animation-dominated, and everything released before 2000 came to 12% of the
page. That is the baseline these numbers are read against — the surface was
already leaning before a fingerprint was applied to any of it.

The one number worth watching is `tailored_share`: the fraction of slots the
viewer's own fingerprint rates above an average popular title. Pushing it to
1.0 would mean a page with nothing on it the viewer has not already proved
they like, which is the failure this module was written to make visible.
"""

from __future__ import annotations

import collections
import math

# What a page should hold in spite of taste. Not targets to optimise toward —
# floors that a narrowing surface will cross first, and thresholds the log
# line is judged against.
MIN_GENRE_ENTROPY = 3.2       # bits; a 27-genre surface tops out near 4.75
MAX_GENRE_SHARE = 0.22        # no single genre should own more than this
MIN_OLD_SHARE = 0.12          # released before 2000
# The balance the household asked for: mostly theirs, with room to be
# surprised. Exploration below this and the service feels like a lens.
TARGET_EXPLORE_SHARE = 0.20


def _year(meta: dict) -> int | None:
    text = str(meta.get("releaseInfo") or "")[:4]
    return int(text) if text.isdigit() else None


def measure(rows: list[dict], lifts: dict[str, float] | None = None) -> dict:
    """Composition of a whole generated surface.

    `rows` are the catalogs as built — each a dict with `name` and `metas`.
    `lifts` maps IMDb id to fingerprint lift where it is known; slots without
    one simply do not contribute to the tailoring figures.
    """
    genres: collections.Counter = collections.Counter()
    decades: collections.Counter = collections.Counter()
    media: collections.Counter = collections.Counter()
    titles: set[str] = set()
    row_dominance: list[float] = []
    scored: list[float] = []

    for row in rows:
        metas = row.get("metas") or []
        row_genres: collections.Counter = collections.Counter()
        for meta in metas:
            imdb_id = meta.get("id")
            if imdb_id:
                titles.add(imdb_id)
            meta_genres = meta.get("genres") or []
            genres.update(meta_genres)
            row_genres.update(meta_genres)
            media[meta.get("type") or "series"] += 1
            year = _year(meta)
            if year:
                decades[year // 10 * 10] += 1
            if lifts and imdb_id in lifts:
                scored.append(lifts[imdb_id])
        if row_genres:
            top = row_genres.most_common(1)[0][1]
            row_dominance.append(top / max(1, sum(row_genres.values())))

    slots = sum(media.values())
    total_genres = sum(genres.values()) or 1
    entropy = -sum((n / total_genres) * math.log2(n / total_genres)
                   for n in genres.values()) if genres else 0.0
    dated = sum(decades.values()) or 1
    old = sum(n for decade, n in decades.items() if decade < 2000)

    out = {
        "rows": len(rows),
        "slots": slots,
        "titles": len(titles),
        "genres": len(genres),
        "genre_entropy": round(entropy, 2),
        "top_genre": genres.most_common(1)[0][0] if genres else None,
        "top_genre_share": round(genres.most_common(1)[0][1] / total_genres, 3)
                           if genres else 0.0,
        "old_share": round(old / dated, 3),
        "movie_share": round(media.get("movie", 0) / max(1, slots), 3),
        "mean_row_dominance": round(
            sum(row_dominance) / len(row_dominance), 3) if row_dominance else 0.0,
    }
    if scored:
        scored.sort()
        above = sum(1 for lift in scored if lift >= 1.0)
        out.update({
            "scored": len(scored),
            "tailored_share": round(above / len(scored), 3),
            "explore_share": round(1 - above / len(scored), 3),
            "median_lift": round(scored[len(scored) // 2], 2),
        })
    return out


def warnings(stats: dict) -> list[str]:
    """Ways this surface has narrowed past what a catalogue should look like."""
    out = []
    if stats.get("genre_entropy", 0) < MIN_GENRE_ENTROPY:
        out.append(f"genre entropy {stats['genre_entropy']} "
                   f"below {MIN_GENRE_ENTROPY}")
    if stats.get("top_genre_share", 0) > MAX_GENRE_SHARE:
        out.append(f"{stats.get('top_genre')} holds "
                   f"{stats['top_genre_share']:.0%} of the surface")
    if stats.get("old_share", 1) < MIN_OLD_SHARE:
        out.append(f"pre-2000 titles only {stats['old_share']:.0%}")
    explore = stats.get("explore_share")
    if explore is not None and explore < TARGET_EXPLORE_SHARE / 2:
        out.append(f"exploration down to {explore:.0%}; the page is a lens")
    return out
