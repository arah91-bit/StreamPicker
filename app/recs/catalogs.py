"""Per-user catalog generation. Runs once a day per user (staggered) so every
catalog is precomputed and serving is a pure SQLite read.

Everything here operates on ONE user's data, drawn from what this service
played for them, and is written back keyed by that user's addon token.

Rows are scored by how likely the user is to pick from them (explicit intent >
recent loved seeds > strong genres > trending > broad rows) and stored in that
order, with Top Picks always first. Kid profiles pass kid_age into every meta
resolution, which filters all sources by US certification."""

import asyncio
import datetime
import logging
import random
import time
from itertools import combinations

from app.recs import breadth, config, db, fingerprint, preseed, taste, tmdb
from app.recs.holidays import active_holiday
from app.recs.kids import band_for_age, effective_kid_age
from app.recs.profile import build_profile
from app.recs import history as local_history

logger = logging.getLogger("nuvio-recs")

TOP_PICKS_ID = "nr-top-picks"

# The addon is a viewing surface, not a short recommendation widget.  Keep
# enough vertical depth that reaching the bottom is unusual, while retaining
# a hard per-row ceiling that clients can render comfortably.
TARGET_CATALOG_ROWS = config.CATALOG_TARGET_ROWS
MIN_CATALOG_ROWS = config.CATALOG_MIN_ROWS
ROW_TARGET_ITEMS = config.CATALOG_ROW_ITEMS
DISCOVER_PAGES = 4
FRESHNESS_RESERVE = 18

# Exposure in SQLite is currently a server-side approximation: clients often
# prefetch every row even when a person never scrolls to it.  Treat the first
# screen as a real-ish exposure, the middle lightly, and deep rows as unseen.
OPENING_ROW_COUNT = config.CATALOG_TOP_ZONE_ROWS
MIDDLE_ROW_COUNT = config.CATALOG_MIDDLE_ZONE_ROWS

# Base affinity scores per row kind (daily jitter of ±4 is added on top).
SCORE_BYW = 62          # + recency/rating bonus up to ~15
SCORE_RELATED = 60
SCORE_NEW_RELEASES = 55
SCORE_TRENDING = 52
SCORE_GENRE = 45        # + up to 15 scaled by genre strength
SCORE_PERSON = 44
SCORE_ACCLAIMED = 40
SCORE_GEMS = 38
SCORE_DECADE = 34
SCORE_LANG = 36
SCORE_POPULAR = 30
SCORE_DEPTH = 33

KID_EXCLUDED_GENRES = {"horror", "war", "crime"}

# Top Picks draws from many seeds rather than a handful, because the row is
# assembled by competition now: a seed that returns weak similars simply loses
# slots instead of blocking the seeds behind it. Ten seeds at twelve
# candidates each gives the selector roughly four times the row to choose
# from, which is what makes the diversity pressure meaningful.
TOP_PICKS_SEED_COUNT = 10
TOP_PICKS_PER_SEED = 12

# Candidate generation from the fingerprint. Seeds can only reach what TMDB
# already lists as similar to something watched; these sweep the catalogue by
# the features the viewer's own history is made of, which is what takes the
# pool from roughly a hundred candidates to several hundred.
FINGERPRINT_KEYWORD_QUERIES = 6     # discover calls built from top keywords
FINGERPRINT_KEYWORDS_PER_QUERY = 3  # OR-ed together, so each returns breadth
FINGERPRINT_PEOPLE_QUERIES = 3
FINGERPRINT_DISCOVER_PAGES = 2
FINGERPRINT_PER_QUERY = 20

# Weight of the fingerprint's own verdict when ranking a candidate. It is the
# only signal that can compare titles from different seeds — or from no seed
# at all — on one scale, so it leads; the rest survive as corroboration.
PICK_WEIGHT_FINGERPRINT = 0.45
# Lift at which a candidate is treated as a perfect match for scaling. Three
# times an average popular title is already a strong signal; measured on real
# history the best genuine matches land between 2 and 7.
LIFT_FULL_MATCH = 3.0

# The exploration band. A candidate qualifies as a departure when the
# fingerprint does *not* already vouch for it — but only up to a point: below
# the floor there is no thread connecting it to anything the viewer likes, and
# a page of unrelated titles is not a wider catalogue, it is noise.
#
# The household's own example is the specification. Mythic Quest is neither
# animation nor a nature documentary and is one of their favourite things;
# what connects it is Rob McElhenney, who is also in It's Always Sunny. That
# is one step outside the core, and reachable. Random is not.
EXPLORE_LIFT_FLOOR = 0.55
EXPLORE_LIFT_CEILING = 1.6
# Quality bar for a departure. If we are spending a slot on something the
# viewer has not asked for, it had better be good.
EXPLORE_MIN_RATING = 6.8

# What decides a candidate's place in Top Picks. Seed strength leads because
# "similar to something you loved" is the row's premise; TMDB's own ordering
# within a seed is the next most reliable signal; genre affinity generalises
# across seeds; rating is a weak tiebreak and is weighted like one.
PICK_WEIGHT_SEED = 0.40
PICK_WEIGHT_ORDER = 0.20
PICK_WEIGHT_GENRE = 0.25
PICK_WEIGHT_RATING = 0.15

# Strength for a seed the taste model has never seen — a hand-picked preseed
# anchor for a viewer with too little history to learn from. Positive, so a
# new viewer still gets the row, but below anything actually measured, so it
# yields the moment real plays exist.
UNMEASURED_SEED_RANK = 0.15

# A kid age is more than a certification ceiling: it says which rows are worth
# building at all. Both weights below drive tmdb.kid_age_appeal, so the same
# explainable signal that orders items inside a row also orders the rows, and
# steers which strategies the depth planner reaches for. Capped so a strong
# developmental fit can rearrange the broad/deep part of the surface without
# ever lifting a discovery row above an intent row such as Watchlist.
BAND_FIT_WEIGHT = 2.0
BAND_FIT_LIMIT = 10.0
BAND_STRATEGY_WEIGHT = 1.2
BAND_STRATEGY_LIMIT = 6.0

FALLBACK_GENRES = {
    "movie": [
        "drama", "comedy", "action", "adventure", "science-fiction",
        "thriller", "romance", "mystery", "documentary", "animation",
        "family", "fantasy", "history", "music", "western",
    ],
    "show": [
        "drama", "comedy", "mystery", "science-fiction", "action",
        "adventure", "documentary", "animation", "reality", "family",
        "fantasy", "crime", "thriller", "western",
    ],
}

# These are still ordinary, inspectable TMDB discover filters.  A strategy is
# eligible when at least one side overlaps the viewer's taste, so cold-start
# fallback rows remain coherent without relying on an LLM to invent titles.
MOOD_STRATEGIES = [
    ("High-Energy Adventures", "action", "adventure"),
    ("Imaginative Sci-Fi", "science-fiction", "adventure"),
    ("Witty Sci-Fi", "science-fiction", "comedy"),
    ("Twisty Mysteries", "mystery", "thriller"),
    ("Character-Driven Crime", "crime", "drama"),
    ("Feel-Good Romances", "romance", "comedy"),
    ("Animated Adventures", "animation", "adventure"),
    ("Family Adventures", "family", "adventure"),
    ("Stories from History", "history", "drama"),
    ("Music and Romance", "music", "romance"),
]

# Tone guards for single-genre rows. TMDB tags dark films with token genres
# (Parasite and Pulp Fiction are both "Comedy"), so e.g. a Comedy row drops
# anything that is also thriller/crime/horror/war. TMDB ids per media type.
GENRE_EXCLUDE = {
    "comedy": {"movie": "27,53,80,10752", "tv": "80,10768"},
    "family": {"movie": "27,53,80,10752", "tv": "80,10768"},
    "animation": {"movie": "27", "tv": ""},
    "anime": {"movie": "27", "tv": ""},
    "romance": {"movie": "27,53", "tv": ""},
    "music": {"movie": "27,53,80", "tv": ""},
}


def _genre_params(media: str, slug: str, table: dict) -> dict:
    params = {"with_genres": table[slug]}
    excl = GENRE_EXCLUDE.get(slug, {}).get(media)
    if excl:
        params["without_genres"] = excl
    return params


def _kind_to_tmdb(kind: str) -> str:
    return "movie" if kind == "movie" else "tv"


def _kind_to_stremio(kind: str) -> str:
    return "movie" if kind == "movie" else "series"


class Generator:
    def __init__(self, user: dict, trigger: str = "refresh"):
        self.user = user
        self.token = user["token"]
        self.trigger = trigger
        self.kid_age: int | None = effective_kid_age(user)
        try:
            adventurousness = int(user.get("adventurousness", 30))
        except (TypeError, ValueError):
            adventurousness = 30
        self.adventurousness = max(0, min(100, adventurousness))
        today = datetime.date.today()
        self.today = today
        self.rng = random.Random(f"{self.token}:{today.isoformat()}")
        self.day = today.toordinal()
        self.rows: list[tuple[float, dict]] = []
        self.taste: taste.TasteModel | None = None
        self.fingerprint: fingerprint.Fingerprint | None = None
        self.used_imdb: set[str] = set()
        self.recently_shown: dict[str, int] = {}
        self.pinned_rows = 0
        # Opaque per-viewer key that play_history is written under.
        from app.recs.profile_streaming import private_namespace_for_user
        self.viewer_key = private_namespace_for_user(user)

    # exclusion sets: everything the user watched + anything already placed
    # in an earlier catalog today (so rows don't repeat each other)
    def _exclude(self) -> set[str]:
        return self.profile["watched_imdb"] | self.used_imdb

    def _excl_tmdb(self, media: str) -> set[int]:
        return (self.profile["watched_tmdb_movie"] if media == "movie"
                else self.profile["watched_tmdb_show"])

    def _catalog_count(self) -> int:
        return self.pinned_rows + len(self.rows)

    @staticmethod
    def _freshness_scale(depth: int) -> float:
        if depth < OPENING_ROW_COUNT:
            return 1.0
        if depth < MIDDLE_ROW_COUNT:
            return 0.4
        return 0.0

    def _band_fit(self, metas: list[dict]) -> float | None:
        """Mean developmental-age appeal across a row's items.

        None for a viewer who is not a kid profile, which is what keeps every
        adult surface byte-for-byte unchanged.
        """
        scorer = getattr(tmdb, "kid_age_appeal_score", None)
        if self.kid_age is None or not metas or not callable(scorer):
            return None
        scored = []
        for meta in metas:
            try:
                scored.append(float(scorer(meta, self.kid_age)))
            except (TypeError, ValueError):
                continue
        return sum(scored) / len(scored) if scored else None

    def _band_strategy_shift(self, slugs: list[str]) -> float:
        """How well a candidate row strategy suits this viewer's age band.

        Scored from the strategy's own genres, so "Acclaimed Drama Movies"
        sinks for a preschooler while "Family + Adventure" rises, and a
        school-age viewer still gets mystery and science fiction rather than
        being pushed back to preschool programming.
        """
        appeal = getattr(tmdb, "kid_age_appeal_score", None)
        if self.kid_age is None or not slugs or not callable(appeal):
            return 0.0
        try:
            score = float(appeal(
                {"genres": [tmdb.genre_label(slug) for slug in slugs]},
                self.kid_age))
        except (TypeError, ValueError):
            return 0.0
        return max(-BAND_STRATEGY_LIMIT,
                   min(BAND_STRATEGY_LIMIT, score * BAND_STRATEGY_WEIGHT))

    def _freshen(self, metas: list[dict], limit: int,
                 depth: int | None = None) -> list[dict]:
        """Daily, relevance-preserving rotation of a candidate list.

        Source rank still matters. A title shown very recently receives a
        rank penalty near the top of the surface, allowing nearby unseen
        candidates to move ahead.  The penalty fades with row depth because a
        prefetched row near the bottom probably was not viewed.  Titles are not
        permanently excluded and can return when the pool is thin.  Small
        deterministic daily jitter breaks ties without making a refresh change
        repeatedly during the same day.
        """
        if depth is None:
            depth = self._catalog_count()
        exposure_scale = self._freshness_scale(depth)
        kid_appeal = getattr(tmdb, "kid_age_appeal_score", None)
        now = time.time()
        ranked: list[tuple[float, dict]] = []
        seen: set[str] = set()
        for source_rank, meta in enumerate(metas):
            imdb_id = meta.get("id")
            if not imdb_id or imdb_id in seen:
                continue
            seen.add(imdb_id)
            shown_at = self.recently_shown.get(meta["id"])
            penalty = 0.0
            if shown_at:
                age_days = max(0.0, (now - shown_at) / 86400)
                base_penalty = 12.0 if age_days < 2 else 7.0 if age_days < 7 else 3.0
                penalty = base_penalty * exposure_scale
            appeal_bonus = 0.0
            if self.kid_age is not None and callable(kid_appeal):
                try:
                    # Feature-detected integration point: helpers should return
                    # a higher value for a stronger developmental-age match.
                    appeal_bonus = float(kid_appeal(meta, self.kid_age))
                except (TypeError, ValueError):
                    appeal_bonus = 0.0
            ranked.append((source_rank + penalty - appeal_bonus
                           + self.rng.uniform(-1.5, 1.5), meta))
        ranked.sort(key=lambda item: item[0])
        return [meta for _, meta in ranked[:limit]]

    async def _resolve_ids(self, media: str, tmdb_ids: list[int],
                           limit: int, depth: int | None = None) -> list[dict]:
        # Resolve a reserve only where exposure rotation can actually matter.
        # Deep rows get no reserve: this both reflects uncertain exposure and
        # prevents the larger surface from multiplying metadata work needlessly.
        if depth is None:
            depth = self._catalog_count()
        scale = self._freshness_scale(depth)
        reserve = round(FRESHNESS_RESERVE * scale) if self.recently_shown else 0
        candidate_limit = limit + reserve
        candidate_limit = min(candidate_limit, len(dict.fromkeys(tmdb_ids)))
        metas = await tmdb.resolve_many(
            media, tmdb_ids, self._exclude(), self._excl_tmdb(media),
            candidate_limit, self.kid_age,
        )
        return self._freshen(metas, limit, depth)

    def _add(self, score: float, cat_id: str, ctype: str, name: str,
             metas: list[dict], min_items: int = 5) -> bool:
        # Defense in depth for sources (notably optional search-based sources)
        # that do not go through resolve_many, and for accidental duplicate
        # rows produced by overlapping TMDB filters.
        excluded = self._exclude()
        clean: list[dict] = []
        seen: set[str] = set()
        for meta in metas:
            imdb_id = meta.get("id")
            if not imdb_id or imdb_id in excluded or imdb_id in seen:
                continue
            seen.add(imdb_id)
            clean.append(meta)
            if len(clean) == ROW_TARGET_ITEMS:
                break
        if any(cat["id"] == cat_id or cat["name"] == name for _, cat in self.rows):
            logger.info(f"[{self.token[:8]}] skipping duplicate row '{name}'")
            return False
        if len(clean) < min_items:
            logger.info(f"[{self.token[:8]}] skipping row '{name}' ({len(clean)} items)")
            return False
        # A row that is technically allowed for this age is not necessarily a
        # row worth putting near the top of a child's home screen. The items
        # already passed the certification gate; this orders what survived by
        # how well it actually suits the viewer's stage.
        fit = self._band_fit(clean)
        fit_bonus = 0.0 if fit is None else max(
            -BAND_FIT_LIMIT, min(BAND_FIT_LIMIT, fit * BAND_FIT_WEIGHT))
        row_score = score + fit_bonus + self.rng.uniform(-4, 4)
        measurement = {
            "strategy": cat_id,
            "candidate_source": self._candidate_source(cat_id),
            "rank_score": row_score,
        }
        if fit is not None:
            measurement["kid_age"] = self.kid_age
            measurement["kid_age_band"] = band_for_age(self.kid_age)["id"]
            measurement["kid_band_fit"] = round(fit, 2)
        self.rows.append((row_score,
                          {"id": cat_id, "type": ctype, "name": name,
                           "metas": clean,
                           "measurement": measurement}))
        self.used_imdb.update(m["id"] for m in clean)
        return True

    @staticmethod
    def _candidate_source(cat_id: str) -> str:
        if cat_id.startswith("nr-holiday"):
            return "tmdb-keyword"
        if cat_id in {TOP_PICKS_ID, "nr-trending", "nr-popular"}:
            return "tmdb-feed"
        if cat_id.startswith("nr-byw") or cat_id == "nr-related":
            return "tmdb-recommendations"
        return "tmdb-discover"

    def _movie_share(self) -> float:
        """Movie share inferred from actual history and cold-start anchors.

        A 20% floor for either medium keeps mixed rows useful without forcing
        an artificial 50/50 split on a strongly movie- or series-led viewer.
        """
        movie = float(len(self.profile.get("watched_tmdb_movie", ())))
        series = float(len(self.profile.get("watched_tmdb_show", ())))
        movie += sum(float(weight) for _, weight in
                     self.profile.get("genres", {}).get("movie", ()))
        series += sum(float(weight) for _, weight in
                      self.profile.get("genres", {}).get("show", ()))
        preference = self.user.get("preferred_media", "balanced")
        if movie + series <= 0:
            return {"movies": 0.7, "series": 0.3}.get(preference, 0.5)
        share = movie / (movie + series)
        if preference in {"movies", "series"}:
            stated_share = 0.8 if preference == "movies" else 0.2
            share = share * 0.7 + stated_share * 0.3
        return max(0.2, min(0.8, share))

    def _mix_media(self, movies: list[dict], series: list[dict],
                   limit: int = ROW_TARGET_ITEMS) -> list[dict]:
        """Blend movies/series to the profile's preference, filling short pools."""
        used: set[str] = set()

        def unique(items: list[dict]) -> list[dict]:
            out = []
            for item in items:
                item_id = item.get("id")
                if item_id and item_id not in used:
                    used.add(item_id)
                    out.append(item)
            return out

        movies = unique(movies)
        series = unique(series)
        desired_movies = round(limit * self._movie_share())
        movie_count = min(desired_movies, len(movies))
        series_count = min(limit - movie_count, len(series))
        remaining = limit - movie_count - series_count
        if remaining:
            extra_movies = min(remaining, len(movies) - movie_count)
            movie_count += extra_movies
            remaining -= extra_movies
        if remaining:
            series_count += min(remaining, len(series) - series_count)

        movies = movies[:movie_count]
        series = series[:series_count]
        total = movie_count + series_count
        if not total:
            return []
        effective_movie_share = movie_count / total
        out: list[dict] = []
        m = s = 0
        for position in range(total):
            wanted_movies = round((position + 1) * effective_movie_share)
            if m < movie_count and (m < wanted_movies or s >= series_count):
                out.append(movies[m])
                m += 1
            else:
                out.append(series[s])
                s += 1
        return out

    def _released_by_taste(self, results: list[dict], media: str) -> list[int]:
        """TMDB ids from a raw feed: out-of-date-range dropped, taste first.

        The feed endpoints (/trending, /popular) take no date or genre filter
        the way /discover does, so both have to happen here — otherwise an
        announced-but-unreleased title reaches a surface that promises things
        a viewer can choose now.
        """
        table = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
        kind = "movie" if media == "movie" else "show"
        top = {table[g] for g, _ in self.profile["genres"][kind][:6] if g in table}
        date_key = "release_date" if media == "movie" else "first_air_date"

        released = [r for r in results
                    if not (str(r.get(date_key) or "")[:10] > self.today.isoformat())]
        # Stable, so within an equal number of matching genres the feed's own
        # ordering — which is the trending/popularity signal — still decides.
        ranked = sorted(released,
                        key=lambda r: len(set(r.get("genre_ids") or []) & top),
                        reverse=True)
        return [r["id"] for r in ranked if r.get("id")]

    async def _resolve_feed(self, media: str, results: list[dict],
                            limit: int) -> list[dict]:
        return await self._resolve_ids(media, self._released_by_taste(results, media),
                                       limit)

    async def _resolve_discover(self, media: str, params: dict, limit: int,
                                pages: int = DISCOVER_PAGES) -> list[dict]:
        results = []
        base = dict(params)
        # TMDB popularity and recommendation feeds routinely include announced
        # titles.  The surface promises things a user can choose now.
        date_key = "primary_release_date.lte" if media == "movie" \
            else "first_air_date.lte"
        base.setdefault(date_key, self.today.isoformat())
        base.setdefault("include_adult", "false")
        if self.kid_age is not None and media == "movie":
            # pre-filter at the API to waste fewer lookups; the cert check in
            # resolve_meta is still the authority (tv discover lacks this)
            base["certification_country"] = "US"
            base["certification.lte"] = "PG" if self.kid_age < 13 else "PG-13"
        for page in range(1, pages + 1):
            try:
                page_items = await tmdb.discover(media, {**base, "page": page})
            except Exception:
                break
            results += page_items
            if not page_items:
                break
        return await self._resolve_ids(media, [r["id"] for r in results], limit)

    def _top_genres(self, kind: str, n: int = 4) -> list[str]:
        table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
        out = []
        seen_ids: set[int] = set()
        for slug, _ in self.profile["genres"][kind]:
            if slug not in table:
                continue
            if self.kid_age is not None and slug in KID_EXCLUDED_GENRES:
                continue
            if table[slug] in seen_ids:
                continue
            seen_ids.add(table[slug])
            out.append(slug)
        return out[:n]

    def _genre_strength(self, kind: str, slug: str) -> float:
        """0..1 share of this genre relative to the user's strongest genre."""
        weights = dict(self.profile["genres"][kind])
        top = max(weights.values(), default=0)
        return (weights.get(slug, 0) / top) if top else 0.0

    def _catalog_genres(self, kind: str, n: int = 8) -> list[str]:
        """Taste genres followed by safe exploration genres for thin profiles."""
        preferred = self._top_genres(kind, n)
        if self.kid_age is not None:
            fallback = [
                "animation", "family", "adventure", "comedy", "fantasy",
                "science-fiction", "documentary", "music",
            ]
        else:
            fallback = FALLBACK_GENRES[kind]
        table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
        out: list[str] = []
        seen_ids: set[int] = set()
        for slug in preferred + fallback:
            genre_id = table.get(slug)
            if genre_id is not None and genre_id not in seen_ids \
                    and not (self.kid_age is not None and slug in KID_EXCLUDED_GENRES):
                seen_ids.add(genre_id)
                out.append(slug)
            if len(out) == n:
                break
        return out

    @staticmethod
    def _multi_genre_params(media: str, slugs: list[str],
                            operator: str = ",") -> dict:
        table = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
        genre_ids: list[str] = []
        for slug in slugs:
            genre_id = str(table.get(slug, ""))
            if genre_id and genre_id not in genre_ids:
                genre_ids.append(genre_id)
        params: dict = {"with_genres": operator.join(genre_ids)}
        excludes: set[str] = set()
        for slug in slugs:
            excludes.update(
                genre_id for genre_id in
                (GENRE_EXCLUDE.get(slug, {}).get(media) or "").split(",")
                if genre_id
            )
        # A tone guard on one half of a deliberate crossover must not exclude
        # the other half (for example, Comedy + Thriller).
        excludes.difference_update(genre_ids)
        if excludes:
            params["without_genres"] = ",".join(sorted(excludes))
        return params

    def _depth_row_specs(self) -> list[dict]:
        """Specific, deterministic row strategies used to complete the surface.

        Candidate order changes daily but is reproducible for a viewer/day.
        Profile genres always lead; fallback genres only provide exploration
        depth for sparse histories.  No row is a numbered generic catch-all.
        """
        seeded = random.Random(f"{self.token}:{self.today.isoformat()}:depth")
        specs: list[tuple[float, dict]] = []
        profile_genres = {
            kind: set(self._top_genres(kind, 8))
            for kind in ("movie", "show")
        }
        genre_lists = {
            kind: self._catalog_genres(kind, 8)
            for kind in ("movie", "show")
        }
        mood_pairs = {frozenset((left, right))
                      for _, left, right in MOOD_STRATEGIES}

        def append(priority: float, spec_id: str, kind: str, name: str,
                   params: dict, score: float = SCORE_DEPTH,
                   exploration: float = 0.3,
                   genres: list[str] | None = None) -> None:
            media = _kind_to_tmdb(kind)
            # The control only rearranges the broad/deep candidate plan. It
            # never dislodges intent rows such as Watchlist or Top Picks, and
            # it never changes certification safety. Thirty is the historical
            # default, so existing profiles retain the original ordering.
            adventure_shift = (
                (self.adventurousness - 30) * (exploration - 0.3) * 0.18
            )
            # Which strategies are worth attempting at all: the planner stops
            # once the surface is full, so for a kid profile this decides
            # whether the last slots go to another adult-shaped lens or to
            # something built for their stage.
            band_shift = self._band_strategy_shift(genres or [])
            specs.append((priority + adventure_shift + band_shift
                          + seeded.uniform(-0.75, 0.75), {
                "id": f"nr-depth-{spec_id}",
                "media": media,
                "type": _kind_to_stremio(kind),
                "name": name,
                "params": params,
                # These complete the broad/deep part of the surface. Even a
                # high-quality lens must not jump above the high-confidence
                # intent and seed rows that were generated before it.
                "score": min(score, SCORE_DEPTH),
            }))

        # Genre intersections are more specific than another broad genre row.
        # Spread their priority so other strategies appear between them.
        for kind in ("movie", "show"):
            media = _kind_to_tmdb(kind)
            noun = "Movies" if kind == "movie" else "Series"
            table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
            pairs = []
            for left, right in combinations(genre_lists[kind][:6], 2):
                if table[left] == table[right] or frozenset((left, right)) in mood_pairs:
                    continue
                affinity = self._genre_strength(kind, left) \
                    + self._genre_strength(kind, right)
                # With no evidence, preserve fallback order instead of claiming
                # a strong affinity that is not there.
                rank = genre_lists[kind].index(left) + genre_lists[kind].index(right)
                pairs.append((affinity * 10 - rank, left, right))
            pairs.sort(reverse=True)
            for index, (_, left, right) in enumerate(pairs[:6]):
                params = self._multi_genre_params(media, [left, right])
                params.update({"sort_by": "popularity.desc", "vote_count.gte": 35})
                title = f"{tmdb.genre_label(left)} + {tmdb.genre_label(right)} {noun}"
                append(98 - index * 7, f"combo-{kind}-{left}-{right}", kind,
                       title, params, SCORE_DEPTH + 3 - index,
                       exploration=(0.15 if left in profile_genres[kind]
                                    and right in profile_genres[kind] else 0.65),
                       genres=[left, right])

        # Human-readable moods backed by explicit, auditable genre pairs.
        for mood_index, (title, left, right) in enumerate(MOOD_STRATEGIES):
            for kind in ("movie", "show"):
                table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
                if left not in table or right not in table or table[left] == table[right]:
                    continue
                if self.kid_age is not None and ({left, right} & KID_EXCLUDED_GENRES):
                    continue
                tastes = profile_genres[kind]
                if tastes and not ({left, right} & tastes):
                    continue
                media = _kind_to_tmdb(kind)
                noun = "Movies" if kind == "movie" else "Series"
                params = self._multi_genre_params(media, [left, right])
                params.update({"sort_by": "popularity.desc", "vote_count.gte": 30})
                append(94 - mood_index * 2, f"mood-{kind}-{left}-{right}", kind,
                       f"{title} — {noun}", params, SCORE_DEPTH + 2,
                       exploration=0.45, genres=[left, right])

        # Quality, recency, and under-the-radar lenses ensure the deeper page
        # is not merely the same popularity query with different labels.
        recent_cutoff = (self.today - datetime.timedelta(days=5 * 365)).isoformat()
        for kind in ("movie", "show"):
            media = _kind_to_tmdb(kind)
            noun = "Movies" if kind == "movie" else "Series"
            date_gte = "primary_release_date.gte" if media == "movie" \
                else "first_air_date.gte"
            for index, slug in enumerate(genre_lists[kind][:5]):
                label = tmdb.genre_label(slug)
                genre = self._multi_genre_params(media, [slug])

                acclaimed = {**genre, "sort_by": "vote_average.desc",
                              "vote_average.gte": 7.0, "vote_count.gte": 500}
                append(93 - index * 8, f"acclaimed-{kind}-{slug}", kind,
                       f"Acclaimed {label} {noun}", acclaimed,
                       SCORE_ACCLAIMED - index, exploration=0.05,
                       genres=[slug])

                recent = {**genre, "sort_by": "popularity.desc",
                          "vote_count.gte": 35, date_gte: recent_cutoff}
                append(89 - index * 8, f"recent-{kind}-{slug}", kind,
                       f"Recent {label} {noun}", recent,
                       SCORE_NEW_RELEASES - 8 - index, exploration=0.15,
                       genres=[slug])

                underseen = {**genre, "sort_by": "vote_average.desc",
                             "vote_average.gte": 6.8, "vote_count.gte": 50,
                             "vote_count.lte": 1800}
                append(85 - index * 8, f"underseen-{kind}-{slug}", kind,
                       f"Under-the-Radar {label} {noun}", underseen,
                       SCORE_GEMS - index, exploration=0.85, genres=[slug])

        # Favorite eras crossed with favorite genres. Use complete past decades
        # as exploration options only when history has no era signal.
        current_decade = (self.today.year // 10) * 10
        decades = [d for d, _ in self.profile.get("decades", ())
                   if 1900 <= d < current_decade]
        for fallback in (current_decade - 10, current_decade - 20,
                         current_decade - 30, current_decade - 40):
            if fallback not in decades:
                decades.append(fallback)
        for kind in ("movie", "show"):
            media = _kind_to_tmdb(kind)
            noun = "Movies" if kind == "movie" else "Series"
            lower_key = "primary_release_date.gte" if media == "movie" \
                else "first_air_date.gte"
            upper_key = "primary_release_date.lte" if media == "movie" \
                else "first_air_date.lte"
            for index, decade in enumerate(decades[:4]):
                slug = genre_lists[kind][index % min(3, len(genre_lists[kind]))]
                params = {
                    **self._multi_genre_params(media, [slug]),
                    "sort_by": "popularity.desc", "vote_count.gte": 80,
                    lower_key: f"{decade}-01-01",
                    upper_key: f"{decade + 9}-12-31",
                }
                append(79 - index * 6, f"era-{kind}-{decade}-{slug}", kind,
                       f"{decade}s {tmdb.genre_label(slug)} {noun}", params,
                       SCORE_DECADE - index, exploration=0.25, genres=[slug])

        # Honor demonstrated language taste, then offer a small amount of
        # bottom-of-page adjacent discovery tied to a favorite genre.
        profile_languages = [code for code, _ in self.profile.get("languages", ())
                             if code and code != "en"]
        discovery_languages = ["ko", "ja", "es", "fr", "hi", "de"]
        start = self.day % len(discovery_languages)
        languages = list(dict.fromkeys(
            profile_languages[:3]
            + [discovery_languages[(start + n) % len(discovery_languages)]
               for n in range(3)]
        ))
        for language_index, code in enumerate(languages):
            language = tmdb.LANG_NAMES.get(code, code.upper())
            for kind in ("movie", "show"):
                media = _kind_to_tmdb(kind)
                noun = "Movies" if kind == "movie" else "Series"
                slug = genre_lists[kind][language_index % min(3, len(genre_lists[kind]))]
                params = {
                    **self._multi_genre_params(media, [slug]),
                    "with_original_language": code,
                    "sort_by": "popularity.desc", "vote_count.gte": 20,
                }
                append(68 - language_index * 4,
                       f"language-{kind}-{code}-{slug}", kind,
                       f"{language} {tmdb.genre_label(slug)} Discoveries — {noun}",
                       params, SCORE_LANG - 5 - language_index,
                       exploration=(0.4 if code in profile_languages else 1.0),
                       genres=[slug])

        # Last-resort breadth is still labeled and filtered by two concrete
        # taste areas. OR makes these pools resilient after earlier rows have
        # claimed hundreds of titles.
        for kind in ("movie", "show"):
            media = _kind_to_tmdb(kind)
            noun = "Movies" if kind == "movie" else "Series"
            genres = genre_lists[kind]
            for index in range(min(6, len(genres) - 1)):
                left = genres[index]
                right = genres[(index + 2) % len(genres)]
                params = self._multi_genre_params(media, [left, right], "|")
                params.update({"sort_by": "popularity.desc", "vote_count.gte": 25})
                append(55 - index * 3, f"explore-{kind}-{left}-{right}", kind,
                       f"{tmdb.genre_label(left)} & {tmdb.genre_label(right)} "
                       f"Discoveries — {noun}", params,
                       SCORE_POPULAR - 2 - index, exploration=0.75,
                       genres=[left, right])

        specs.sort(key=lambda item: item[0], reverse=True)
        return [spec for _, spec in specs]

    async def _ensure_catalog_depth(self) -> None:
        """Fill up to the viewing-surface target with grounded row strategies."""
        if self._catalog_count() >= TARGET_CATALOG_ROWS:
            return
        attempted = 0
        for spec in self._depth_row_specs():
            if self._catalog_count() >= TARGET_CATALOG_ROWS:
                break
            attempted += 1
            metas = await self._resolve_discover(
                spec["media"], spec["params"], ROW_TARGET_ITEMS,
                pages=DISCOVER_PAGES,
            )
            # Rows below the floor may use a smaller niche pool; after the
            # floor, require a fuller row before it earns another vertical slot.
            min_items = 8 if self._catalog_count() < MIN_CATALOG_ROWS else 12
            self._add(spec["score"], spec["id"], spec["type"], spec["name"],
                      metas, min_items=min_items)
        logger.info(f"[{self.token[:8]}] depth planner attempted {attempted} "
                    f"strategies; surface has {self._catalog_count()} rows")

    async def run(self) -> None:
        self.recently_shown = await db.get_recently_shown(self.token)
        # Taste comes from what this service actually played for this viewer.
        # There is no Trakt account behind any of this any more: a free Trakt
        # account allows one connected application, and that slot belongs to
        # the client's own progress sync.
        wm, ws, rm, rs = await local_history.watched_lists(self.viewer_key)

        try:
            # Watchlists were Trakt's alone, so the optional watchlist
            # snapshots are simply never supplied any more.
            outcomes = await db.upsert_title_state_and_record_outcomes(
                self.token, wm, ws,
            )
            attributed = await db.attribute_outcomes(
                self.token,
                lookback_seconds=config.OUTCOME_ATTRIBUTION_HOURS * 3600,
            )
            if outcomes or attributed:
                logger.info(f"[{self.token[:8]}] outcomes: {len(outcomes)} new, "
                            f"{attributed} newly attributed")
        except Exception:
            # Recommendation availability must not depend on analytics state.
            logger.exception(f"[{self.token[:8]}] outcome ledger sync failed")

        self.profile = build_profile(wm, ws, rm, rs)
        logger.info(f"[{self.token[:8]}] profile: {len(wm)} movies, {len(ws)} shows"
                    f" watched{f', kid age {self.kid_age}' if self.kid_age is not None else ''}")

        # How much they liked each of those, and which of them were somebody
        # else's choice. Cache-only, so a failure here costs ranking quality
        # and never the build.
        try:
            self.taste = await taste.load(self.viewer_key)
            logger.info(f"[{self.token[:8]}] taste: {self.taste.summary()}")
        except Exception:
            logger.exception(f"[{self.token[:8]}] taste model failed")
            self.taste = None

        # The same history as a vector, which is what lets a candidate nothing
        # in the history is linked to still be scored. Returns None on a thin
        # history or an unpopulated feature store, and the row falls back to
        # seed similarity — a fingerprint built from two titles would rank the
        # whole catalogue with unearned confidence.
        if self.taste:
            try:
                context = taste.CONTEXT_SOLO if self.kid_age is None else None
                self.fingerprint = await fingerprint.for_viewer(
                    self.taste, context, self.rng)
                logger.info(
                    f"[{self.token[:8]}] fingerprint: "
                    f"{self.fingerprint.summary() if self.fingerprint else 'unavailable'}")
            except Exception:
                logger.exception(f"[{self.token[:8]}] fingerprint failed")
                self.fingerprint = None

        # thin history → blend in this user's hand-picked taste anchors and/or
        # real watch history pulled from elsewhere (e.g. Nuvio local history)
        if len(wm) + len(ws) < preseed.PRESEED_MAX_HISTORY:
            entries = preseed.load_for(self.user.get("name"))
            # Not `taste`: that name is the taste model module, and binding it
            # locally here made every reference to it in this function an
            # UnboundLocalError — including the model load above, which the
            # try/except then swallowed into a silent fallback.
            anchors = await preseed.taste_seeds(entries["taste"])
            history = await preseed.history_seeds(entries["history"])
            if anchors or history:
                preseed.apply_to_profile(self.profile, anchors + history)
                logger.info(f"[{self.token[:8]}] preseeded with {len(anchors)} "
                            f"taste + {len(history)} history titles for"
                            f" '{self.user.get('name')}'")

        # Build in approximately the same order the rows will be displayed so
        # depth-aware exposure handling reflects the actual surface.  The two
        # pinned rows register their depth explicitly because they do not live
        # in self.rows until final assembly.
        holiday_row = await self._holiday_row()
        if holiday_row:
            self.pinned_rows += 1
        top_picks = await self._top_picks()
        if top_picks:
            self.pinned_rows += 1
        await self._because_you_watched()
        await self._more_like_loved()
        await self._new_releases()
        await self._trending_row()
        await self._genre_rows()
        await self._person_rows()
        await self._acclaimed_row()
        await self._hidden_gems()
        await self._decade_rows()
        await self._language_row()
        await self._popular_row()
        # Gemini rows used to be required to create enough vertical depth.
        # Deterministic, grounded TMDB strategies now fill the surface even
        # when the model is disabled or rate-limited.
        await self._ensure_catalog_depth()

        self.rows.sort(key=lambda r: r[0], reverse=True)
        pinned = [c for c in (holiday_row, top_picks) if c]
        catalogs = pinned + [cat for _, cat in self.rows]
        for position, catalog in enumerate(catalogs):
            measurement = catalog.setdefault("measurement", {})
            measurement.setdefault("strategy", catalog["id"])
            measurement.setdefault(
                "candidate_source", self._candidate_source(catalog["id"]))
            measurement.setdefault("rank_score", float(len(catalogs) - position))
            measurement["score_components"] = {
                "row_position": position,
                "adventurousness": self.adventurousness,
                "preferred_media": self.user.get("preferred_media", "balanced"),
            }
        # What this surface actually looks like, recorded with it. Narrowing is
        # something you notice weeks late and cannot then attribute; a number
        # stored next to each build makes it a diff instead of a feeling.
        composition = await self._measure_breadth(catalogs)
        for warning in breadth.warnings(composition):
            logger.warning(f"[{self.token[:8]}] surface breadth: {warning}")
        logger.info(f"[{self.token[:8]}] surface: {composition}")

        await db.replace_catalogs(
            self.token, catalogs,
            policy_id="deep-home-v3-home-release",
            trigger=self.trigger,
            generation_metadata={
                "row_floor": MIN_CATALOG_ROWS,
                "row_target": TARGET_CATALOG_ROWS,
                "item_target": ROW_TARGET_ITEMS,
                "adventurousness": self.adventurousness,
                "preferred_media": self.user.get("preferred_media", "balanced"),
                "home_release_types": sorted(tmdb.HOME_RELEASE_TYPES),
                "home_release_fallback_days": config.HOME_RELEASE_FALLBACK_DAYS,
                "breadth": composition,
            },
        )
        holiday = active_holiday()
        # The "has anything changed since last build" marker is now the
        # viewer's own most recent play rather than a Trakt activity stamp.
        await db.mark_generated(
            self.token,
            last_activity=str(await local_history.last_play_at(self.viewer_key)),
            last_holiday=holiday["id"] if holiday else "")
        if len(catalogs) < MIN_CATALOG_ROWS:
            logger.warning(f"[{self.token[:8]}] only {len(catalogs)} catalogs; "
                           f"deterministic sources could not satisfy floor "
                           f"{MIN_CATALOG_ROWS}")
        logger.info(f"[{self.token[:8]}] stored {len(catalogs)} catalogs")

    # ── rows ─────────────────────────────────────────────────────────────

    async def _holiday_row(self) -> dict | None:
        """During a holiday window, a themed row pinned above everything else.
        Items are ranked by overlap with the user's own genre profile."""
        holiday = active_holiday()
        if not holiday:
            return None
        keyword_ids: list[int] = []
        for q in holiday["keywords"]:
            try:
                keyword_ids += await tmdb.search_keywords(q)
            except Exception:
                continue
        if not keyword_ids:
            return None
        kw = "|".join(str(k) for k in keyword_ids)

        user_genre_ids = {
            "movie": {tmdb.MOVIE_GENRES[g] for g in self._top_genres("movie", 6)},
            "tv": {tmdb.TV_GENRES[g] for g in self._top_genres("show", 6)},
        }

        async def gather(media: str) -> list[dict]:
            results: list[dict] = []
            params = {"with_keywords": kw, "sort_by": "popularity.desc",
                      "vote_count.gte": 20, "include_adult": "false"}
            params["primary_release_date.lte" if media == "movie"
                   else "first_air_date.lte"] = self.today.isoformat()
            if self.kid_age is not None and media == "movie":
                params["certification_country"] = "US"
                params["certification.lte"] = "PG" if self.kid_age < 13 else "PG-13"
            for page in range(1, DISCOVER_PAGES + 1):
                try:
                    results += await tmdb.discover(media, {**params, "page": page})
                except Exception:
                    break
            # rank to match what this user actually watches: genre overlap
            # with their profile first, then popularity
            results.sort(key=lambda r: (
                len(set(r.get("genre_ids") or []) & user_genre_ids[media]),
                r.get("popularity", 0)), reverse=True)
            return await self._resolve_ids(
                media, [r["id"] for r in results], ROW_TARGET_ITEMS)

        movies = await gather("movie")
        series = await gather("tv")
        combined = self._mix_media(movies, series)
        if len(combined) < 5:
            return None
        self.used_imdb.update(m["id"] for m in combined)
        logger.info(f"[{self.token[:8]}] holiday row active: {holiday['name']}")
        return {"id": f"nr-holiday-{holiday['id']}", "type": config.COMBINED_TYPE,
                "name": holiday["name"], "metas": combined}

    def _ranked_seeds(self, limit: int) -> list[tuple[dict, float]]:
        """This viewer's strongest titles, as (profile seed, 0..1 strength).

        The profile supplies the TMDB ids; the taste model supplies the order
        and the veto. Seeds it scores at or below zero — repeatedly opened and
        never watched — are dropped rather than merely demoted, because a
        single seed contributes a dozen candidates and one bad one is a
        visible chunk of the row.

        Movies and series are ranked in separate lists and allocated slots by
        the profile's own media share, because their engagement scores are not
        comparable: a series accumulates breadth one episode at a time while a
        film has only ever one thing to finish. Ranked together, series win
        essentially every slot — a live run produced ten series seeds out of
        ten, and therefore a row with no films in it at all.
        """
        pool = self.profile.get("seeds") or []
        if not pool or not self.taste:
            # No taste model means no plays to learn from; the profile's own
            # recency ordering is all there is.
            return [(seed, 0.5) for seed in pool[:limit]]
        by_media: dict[str, list[tuple[float, dict]]] = {"movie": [], "series": []}
        for seed in pool:
            imdb_id = seed.get("imdb")
            if not imdb_id:
                continue
            media_type = "movie" if seed.get("type") == "movie" else "series"
            signal = self.taste.signal_for(imdb_id, media_type)
            if signal is None:
                # A preseed anchor, or a title played before history was kept.
                # No evidence is not evidence against.
                by_media[media_type].append((UNMEASURED_SEED_RANK, seed))
                continue
            # Family context means "somebody else in the room chose this",
            # which is only true on an adult's profile. On a child's own
            # profile their children's television IS their taste, and
            # filtering it lost four of Skylar's seven seeds.
            if self.kid_age is None and signal.context != taste.CONTEXT_SOLO:
                continue
            if not self.taste.may_seed(signal):
                continue
            by_media[media_type].append((self.taste.seed_rank(signal), seed))
        for ranked in by_media.values():
            ranked.sort(key=lambda item: item[0], reverse=True)

        movie_slots = round(limit * self._movie_share())
        if by_media["movie"] and movie_slots < 1:
            movie_slots = 1
        movie_slots = min(movie_slots, len(by_media["movie"]))
        chosen = (by_media["movie"][:movie_slots]
                  + by_media["series"][:limit - movie_slots])
        if not chosen:
            return []
        # Slots are allocated per medium; strength is normalised globally. The
        # two must stay separate. Normalising per medium as well was tried and
        # inflated weak seeds: a film opened twice and abandoned at 23% scored
        # 0.87 simply for being the third-best film, and got a seed's full
        # dozen candidates. Now it earns a place in the row and nothing more —
        # its candidates carry its real, low strength and win slots only if
        # the medium is otherwise unrepresented.
        top = max(rank for rank, _ in chosen) or 1.0
        out = [(seed, rank / top) for rank, seed in chosen]
        out.sort(key=lambda item: item[1], reverse=True)
        return out

    def _pick_score(self, meta: dict, seed_strength: float, order: float,
                    affinity: dict[str, float],
                    lift: float | None = None) -> float:
        """A candidate's relevance to this viewer, 0..1.

        With a fingerprint the weights are rebalanced rather than added to:
        seed strength and TMDB's ordering are both statements about one seed,
        and a pool drawn from six different queries plus ten seeds needs a
        measure that spans all of them.
        """
        genres = [taste.genre_slug(g) for g in meta.get("genres") or ()]
        genre_fit = max((affinity.get(slug, 0.0) for slug in genres),
                        default=0.0)
        try:
            rating = float(meta.get("imdbRating") or 0) / 10.0
        except (TypeError, ValueError):
            rating = 0.0
        rating = max(0.0, min(1.0, rating))
        if lift is None:
            return (PICK_WEIGHT_SEED * seed_strength
                    + PICK_WEIGHT_ORDER * order
                    + PICK_WEIGHT_GENRE * genre_fit
                    + PICK_WEIGHT_RATING * rating)
        match = max(0.0, min(1.0, lift / LIFT_FULL_MATCH))
        remainder = 1.0 - PICK_WEIGHT_FINGERPRINT
        return (PICK_WEIGHT_FINGERPRINT * match
                + remainder * (0.35 * seed_strength + 0.20 * order
                               + 0.20 * genre_fit + 0.25 * rating))

    async def _measure_breadth(self, catalogs: list[dict]) -> dict:
        """Composition of the finished surface, lift-scored where possible."""
        lifts: dict[str, float] = {}
        if self.fingerprint:
            ids = {meta.get("id") for catalog in catalogs
                   for meta in catalog.get("metas") or () if meta.get("id")}
            try:
                store = await db.features_by_imdb(ids)
                lifts = {imdb_id: self.fingerprint.lift(tokens)
                         for imdb_id, tokens in store.items() if tokens}
            except Exception:
                logger.debug(f"[{self.token[:8]}] breadth scoring failed",
                             exc_info=True)
        return breadth.measure(catalogs, lifts)

    @staticmethod
    def _explore_score(meta: dict, lift: float | None) -> float:
        """How good this would be *as a departure*, or 0 if it is not one.

        A departure has to be two things at once. It must be genuinely outside
        what the fingerprint already vouches for — otherwise the reserved
        slots just fill with more of the same — and it must be well regarded,
        because a slot spent on something unasked-for is only worth spending
        on something good.

        It must also not be *too* far outside. Below the floor there is no
        thread back to anything the viewer likes, and a page of unconnected
        titles is not a broader catalogue, it is noise.
        """
        if lift is None or not (EXPLORE_LIFT_FLOOR <= lift <= EXPLORE_LIFT_CEILING):
            return 0.0
        try:
            rating = float(meta.get("imdbRating") or 0)
        except (TypeError, ValueError):
            return 0.0
        if rating < EXPLORE_MIN_RATING:
            return 0.0
        # Quality leads; distance from the core breaks ties, so the least
        # familiar of two equally good departures is preferred.
        quality = (rating - EXPLORE_MIN_RATING) / (10.0 - EXPLORE_MIN_RATING)
        distance = 1.0 - (lift - EXPLORE_LIFT_FLOOR) / (
            EXPLORE_LIFT_CEILING - EXPLORE_LIFT_FLOOR)
        return 0.75 * quality + 0.25 * distance

    async def _fingerprint_candidates(self, print_) -> list[tuple[str, list[dict]]]:
        """Titles swept from the catalogue by what this viewer's taste is made of.

        Each discover call OR-s a few of the fingerprint's strongest keywords
        together, so a query returns a neighbourhood rather than an
        intersection — `with_keywords=a|b|c` is broad on purpose. The
        fingerprint then re-ranks everything it finds, which is the division of
        labour that makes a wide sweep safe.

        Returned per query rather than as one list, because the diversity
        selector's quota is per source. Handing it two hundred candidates
        under a single label capped the entire sweep at one source's share:
        measured live, 319 candidates yielded four swept titles in the row.
        Each query is a different neighbourhood and competes as one.
        """
        keywords = [token.split(":", 1)[1]
                    for token in print_.top_features(
                        FINGERPRINT_KEYWORD_QUERIES
                        * FINGERPRINT_KEYWORDS_PER_QUERY, ("k",))]
        people = [token.split(":", 1)[1]
                  for token in print_.top_features(
                      FINGERPRINT_PEOPLE_QUERIES, ("p",))]
        # Sorting every sweep by popularity fills the row with whatever is
        # biggest that month — a live run returned Bridgerton, Modern Family
        # and two Harry Potters for a viewer whose taste is nature
        # documentaries and animation. Alternating with a rating sort behind a
        # vote floor reaches titles that match without being blockbusters.
        def sort_for(index: int) -> dict:
            if index % 2:
                return {"sort_by": "vote_average.desc", "vote_count.gte": 200}
            return {"sort_by": "popularity.desc"}

        queries: list[tuple[str, dict]] = []
        for index in range(0, len(keywords), FINGERPRINT_KEYWORDS_PER_QUERY):
            group = keywords[index:index + FINGERPRINT_KEYWORDS_PER_QUERY]
            if group:
                queries.append(("keywords",
                                {"with_keywords": "|".join(group),
                                 **sort_for(len(queries))}))
        for person in people:
            queries.append(("person", {"with_people": person,
                                       **sort_for(len(queries))}))
        if not queries:
            return []

        share = self._movie_share()
        out: list[tuple[str, list[dict]]] = []
        for index, (label, params) in enumerate(queries):
            # `with_people` is a movie-only filter on TMDB's discover; asking
            # for it on /discover/tv silently returns an unfiltered popularity
            # list, which is the opposite of a targeted sweep.
            medias = ("movie",) if label == "person" else (
                ("movie", "tv") if 0.15 < share < 0.85
                else ("movie" if share >= 0.85 else "tv",))
            for media in medias:
                try:
                    found = await self._resolve_discover(
                        media, params, FINGERPRINT_PER_QUERY,
                        pages=FINGERPRINT_DISCOVER_PAGES)
                except Exception:
                    logger.debug(f"[{self.token[:8]}] fingerprint sweep failed",
                                 exc_info=True)
                    continue
                if found:
                    out.append((f"fp-{label}-{index}-{media}", found))
        return out

    async def _top_picks(self) -> dict | None:
        """The opening row, built from this viewer's own strongest titles.

        This used to be Trakt's `/recommendations`, computed server-side from a
        connected account. With no account there is no such endpoint, so it is
        assembled from titles similar to what this person actually watched.

        The shape it replaced concatenated each seed's similars onto one list
        and truncated: the first seed to return a full row won the whole row,
        and every seed behind it was silently discarded. Observed live, two
        seeds produced all thirty items — eighteen horror and twelve nature
        documentaries — while four perfectly good seeds contributed nothing.

        So seeds no longer take turns; they compete. Every seed's candidates
        are scored on the same scale and then selected against soft diversity
        quotas, which means a seed earns slots by the quality of what it
        returns and no seed can run away with the row.
        """
        seeds = self._ranked_seeds(TOP_PICKS_SEED_COUNT)
        if not seeds:
            return None
        context = taste.CONTEXT_SOLO if self.kid_age is None else None
        affinity = self.taste.genre_affinity(context) if self.taste else {}
        print_ = self.fingerprint

        sources: list[tuple[str, float, list[dict]]] = []
        for seed, strength in seeds:
            try:
                sources.append((seed.get("imdb") or str(seed.get("tmdb") or ""),
                                strength,
                                await self._seed_similar(seed, TOP_PICKS_PER_SEED)))
            except Exception:
                logger.debug(f"[{self.token[:8]}] top-picks seed failed",
                             exc_info=True)
        swept_sources: set[str] = set()
        if print_:
            # A swept candidate answers to no seed, so it carries the median
            # seed strength: it should neither inherit the best seed's
            # authority nor be handicapped for having none. Its real claim on
            # a slot is its lift.
            strengths = sorted(s for _, s in seeds)
            median = strengths[len(strengths) // 2] if strengths else 0.5
            for label, found in await self._fingerprint_candidates(print_):
                swept_sources.add(label)
                sources.append((label, median, found))

        features_wanted = {m.get("id") for _, _, metas in sources
                           for m in metas if m.get("id")}
        lifts: dict[str, float] = {}
        if print_ and features_wanted:
            store = await db.features_by_imdb(features_wanted)
            lifts = {imdb_id: print_.lift(tokens)
                     for imdb_id, tokens in store.items() if tokens}

        candidates: list[taste.Candidate] = []
        seen: set[str] = set()
        for seed_id, strength, metas in sources:
            for position, meta in enumerate(metas):
                imdb_id = meta.get("id")
                if not imdb_id or imdb_id in seen:
                    continue
                genres = tuple(taste.genre_slug(g)
                               for g in meta.get("genres") or ())
                # Keeping kids' titles out of the seeds is not enough: a seed
                # an adult loves still recommends children's television
                # through shared genres, and a live run surfaced Huckleberry
                # Hound and a 1983 Dungeons & Dragons cartoon this way. Kid
                # profiles obviously keep theirs.
                if self.kid_age is None and taste.KIDS_GENRES & set(genres):
                    continue
                seen.add(imdb_id)
                order = 1.0 - position / float(max(1, len(metas)))
                candidates.append(taste.Candidate(
                    imdb_id=imdb_id,
                    media_type=("movie" if meta.get("type") == "movie"
                                else "series"),
                    score=self._pick_score(meta, strength, order, affinity,
                                           lifts.get(imdb_id)),
                    genres=genres,
                    seed_id=seed_id,
                    meta=meta,
                    explore_score=self._explore_score(meta, lifts.get(imdb_id)),
                ))
        if len(candidates) < 5:
            return None
        chosen = taste.select_diverse(
            candidates, ROW_TARGET_ITEMS, rng=self.rng,
            movie_share=self._movie_share(),
            explore_share=taste.EXPLORE_SHARE,
            target_genres=self.taste.genre_target(context) if self.taste else None)
        combined = [c.meta for c in chosen]
        if len(combined) < 5:
            return None
        contributing = len({c.seed_id for c in chosen})
        swept_in = sum(1 for c in chosen if c.seed_id in swept_sources)
        scored = sum(1 for c in chosen if c.imdb_id in lifts)
        explored = sum(1 for c in chosen if c.explore_score > 0)
        logger.info(f"[{self.token[:8]}] top picks: {len(combined)} items from "
                    f"{contributing}/{len(sources)} sources "
                    f"({len(candidates)} candidates; {swept_in} swept by "
                    f"fingerprint, {scored} lift-scored, {explored} exploratory)")
        self.used_imdb.update(m["id"] for m in combined)
        return {"id": TOP_PICKS_ID, "type": config.COMBINED_TYPE,
                "name": "Top Picks", "metas": combined}

    async def _because_you_watched(self) -> None:
        seeds = [s for s in self.profile["seeds"] if s.get("tmdb")]
        if not seeds:
            return
        # rotate through recent favorites so the rows change day to day
        pool = seeds[:14]
        start = self.day % len(pool)
        picked, seen_titles = [], set()
        for i in range(len(pool)):
            s = pool[(start + i) % len(pool)]
            if s["title"] not in seen_titles:
                picked.append(s)
                seen_titles.add(s["title"])
            if len(picked) == 4:
                break
        now = datetime.datetime.now().timestamp()
        for n, seed in enumerate(picked, 1):
            metas = await self._seed_similar(seed, ROW_TARGET_ITEMS)
            days_ago = (now - seed["last_watched"]) / 86400
            bonus = (12 if days_ago < 30 else 6 if days_ago < 90 else 0) \
                + (seed["rating"] / 2 if seed["rating"] else 0)
            verb = "Like" if seed.get("source") == "taste" else "Watched"
            self._add(SCORE_BYW + bonus, f"nr-byw-{n}", _kind_to_stremio(seed["type"]),
                      f"Because You {verb} {seed['title']}", metas)

    async def _seed_similar(self, seed: dict, limit: int) -> list[dict]:
        """Titles actually similar to a seed, from TMDB recommendations — but
        every candidate must share a genre with the seed, so a nature
        documentary can't pull in horror just because both are popular."""
        media = _kind_to_tmdb(seed["type"])
        seed_genres = set(seed.get("genres") or [])
        table = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
        seed_tmdb_genres = {table[g] for g in seed_genres if g in table}
        candidates: list[int] = []

        try:
            recs = await tmdb.tmdb_recommendations(media, seed["tmdb"])
        except Exception:
            recs = []
        for r in recs:
            release_date = (r.get("release_date") if media == "movie"
                            else r.get("first_air_date")) or ""
            if release_date and release_date[:10] > self.today.isoformat():
                continue
            if not seed_tmdb_genres or set(r.get("genre_ids") or []) & seed_tmdb_genres:
                candidates.append(r["id"])

        return await self._resolve_ids(media, candidates, limit)

    async def _more_like_loved(self) -> None:
        """Similar titles for the user's highest-rated recent watch."""
        for seed in self.profile["loved"]:
            if not seed.get("tmdb"):
                continue
            metas = await self._seed_similar(seed, ROW_TARGET_ITEMS)
            self._add(SCORE_RELATED + seed["rating"], "nr-related",
                      _kind_to_stremio(seed["type"]),
                      f"More Like {seed['title']}", metas)
            return

    async def _person_rows(self) -> None:
        """Creators/actors from the viewer's strongest movie anchors.

        Recurrence wins, but a thin profile can still get a grounded creator
        row from its best seed instead of losing two vertical slots entirely.
        """
        movie_seeds = []
        seen_tmdb: set[int] = set()
        for seed in self.profile["loved"] + self.profile["seeds"]:
            if seed.get("type") != "movie" or not seed.get("tmdb") \
                    or seed["tmdb"] in seen_tmdb:
                continue
            seen_tmdb.add(seed["tmdb"])
            movie_seeds.append(seed)
            if len(movie_seeds) == 8:
                break
        if not movie_seeds:
            return
        directors: dict[int, dict] = {}
        actors: dict[int, dict] = {}
        for seed_index, seed in enumerate(movie_seeds):
            try:
                credits = await tmdb.movie_credits(seed["tmdb"])
            except Exception:
                continue
            for c in credits.get("crew", []):
                if c.get("job") == "Director":
                    d = directors.setdefault(
                        c["id"], {"name": c["name"], "count": 0, "rank": seed_index})
                    d["count"] += 1
            for c in credits.get("cast", [])[:5]:
                a = actors.setdefault(
                    c["id"], {"name": c["name"], "count": 0, "rank": seed_index})
                a["count"] += 1

        async def add_row(people: dict, key: str, cat_id: str, label: str) -> None:
            if not people:
                return
            pid, person = max(
                people.items(), key=lambda item: (item[1]["count"], -item[1]["rank"]))
            metas = await self._resolve_discover(
                "movie", {key: pid, "sort_by": "popularity.desc"},
                ROW_TARGET_ITEMS)
            self._add(SCORE_PERSON + person["count"] * 2, cat_id, "movie",
                      f"{label} {person['name']}", metas)

        await add_row(directors, "with_crew", "nr-director", "Directed by")
        await add_row(actors, "with_cast", "nr-actor", "Starring")

    async def _genre_rows(self) -> None:
        # movies and series rows from the user's strongest genres, rotating daily
        specs: list[tuple[str, str]] = []
        m_genres, s_genres = self._top_genres("movie"), self._top_genres("show")
        for i in range(2):
            if len(m_genres) > i:
                specs.append(("movie", m_genres[(self.day + i) % len(m_genres)]))
            if len(s_genres) > i:
                specs.append(("show", s_genres[(self.day // 2 + i) % len(s_genres)]))
        seen = set()
        n = 0
        for kind, slug in specs:
            if (kind, slug) in seen:
                continue
            seen.add((kind, slug))
            n += 1
            media = _kind_to_tmdb(kind)
            table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
            sort = self.rng.choice(["popularity.desc", "vote_average.desc"])
            params = {**_genre_params(media, slug, table), "sort_by": sort,
                      "vote_count.gte": 300 if sort == "vote_average.desc" else 50}
            metas = await self._resolve_discover(media, params, ROW_TARGET_ITEMS)
            label = tmdb.genre_label(slug)
            noun = "Movies" if kind == "movie" else "Series"
            self._add(SCORE_GENRE + 15 * self._genre_strength(kind, slug),
                      f"nr-genre-{n}", _kind_to_stremio(kind),
                      f"{label} {noun}", metas)

    async def _new_releases(self) -> None:
        cutoff = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        m_genres = self._top_genres("movie", 3)
        s_genres = self._top_genres("show", 3)
        m_params = {"sort_by": "popularity.desc", "vote_count.gte": 50,
                    "primary_release_date.gte": cutoff}
        if m_genres:
            m_params["with_genres"] = "|".join(
                str(tmdb.MOVIE_GENRES[g]) for g in m_genres)
        s_params = {"sort_by": "popularity.desc", "vote_count.gte": 50,
                    "first_air_date.gte": cutoff}
        if s_genres:
            s_params["with_genres"] = "|".join(str(tmdb.TV_GENRES[g]) for g in s_genres)
        movies = await self._resolve_discover("movie", m_params, ROW_TARGET_ITEMS)
        series = await self._resolve_discover("tv", s_params, ROW_TARGET_ITEMS)
        self._add(SCORE_NEW_RELEASES, "nr-new", config.COMBINED_TYPE,
                  "New Releases", self._mix_media(movies, series))

    async def _trending_row(self) -> None:
        try:
            trend_m, trend_s = await asyncio.gather(
                tmdb.trending("movie"), tmdb.trending("tv"))
        except Exception:
            return
        movies = await self._resolve_feed("movie", trend_m, ROW_TARGET_ITEMS)
        series = await self._resolve_feed("tv", trend_s, ROW_TARGET_ITEMS)
        self._add(SCORE_TRENDING, "nr-trending", config.COMBINED_TYPE,
                  "Trending Now", self._mix_media(movies, series))

    async def _acclaimed_row(self) -> None:
        genres = self._top_genres("movie", 3)
        if not genres:
            return
        slug = genres[(self.day // 2) % len(genres)]
        params = {**_genre_params("movie", slug, tmdb.MOVIE_GENRES),
                  "sort_by": "vote_average.desc", "vote_count.gte": 2000}
        metas = await self._resolve_discover("movie", params, ROW_TARGET_ITEMS)
        self._add(SCORE_ACCLAIMED, "nr-acclaimed", "movie",
                  f"Critically Acclaimed {tmdb.genre_label(slug)}", metas)

    async def _hidden_gems(self) -> None:
        genres = self._top_genres("movie")
        if not genres:
            return
        slug = genres[(self.day // 3) % len(genres)]
        cutoff = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
        params = {**_genre_params("movie", slug, tmdb.MOVIE_GENRES),
                  "sort_by": "vote_average.desc",
                  "vote_average.gte": 7.0, "vote_count.gte": 100, "vote_count.lte": 2500,
                  "primary_release_date.lte": cutoff}
        metas = await self._resolve_discover("movie", params, ROW_TARGET_ITEMS)
        self._add(SCORE_GEMS, "nr-gems", "movie",
                  f"Hidden Gems: {tmdb.genre_label(slug)}", metas)

    async def _decade_rows(self) -> None:
        current = (datetime.date.today().year // 10) * 10
        weights = dict(self.profile["decades"])
        top = max(weights.values(), default=0)
        decades = [d for d, _ in self.profile["decades"] if d < current][:4]
        genres = self._top_genres("movie", 3)
        for n in range(2):
            if len(decades) <= n:
                break
            decade = decades[(self.day + n) % len(decades)] if len(decades) > 2 \
                else decades[n]
            params = {"sort_by": "popularity.desc", "vote_count.gte": 200,
                      "primary_release_date.gte": f"{decade}-01-01",
                      "primary_release_date.lte": f"{decade + 9}-12-31"}
            if genres:
                params["with_genres"] = "|".join(
                    str(tmdb.MOVIE_GENRES[g]) for g in genres)
            metas = await self._resolve_discover("movie", params, ROW_TARGET_ITEMS)
            strength = (weights.get(decade, 0) / top) if top else 0
            self._add(SCORE_DECADE + 8 * strength - n * 3, f"nr-decade-{n + 1}",
                      "movie", f"{decade}s Movies", metas)

    async def _language_row(self) -> None:
        langs = self.profile["languages"]
        total = sum(w for _, w in langs) or 1
        foreign = [(code, w) for code, w in langs if code and code != "en"]
        if not foreign or foreign[0][1] / total < 0.10:
            return
        code = foreign[0][0]
        name = tmdb.LANG_NAMES.get(code, code.upper())
        genres = self._top_genres("movie", 3)
        m_params = {"with_original_language": code, "sort_by": "popularity.desc",
                    "vote_count.gte": 50}
        if genres:
            m_params["with_genres"] = "|".join(str(tmdb.MOVIE_GENRES[g]) for g in genres)
        movies = await self._resolve_discover("movie", m_params, ROW_TARGET_ITEMS)
        series = await self._resolve_discover(
            "tv", {"with_original_language": code, "sort_by": "popularity.desc",
                   "vote_count.gte": 50}, ROW_TARGET_ITEMS)
        self._add(SCORE_LANG, "nr-lang", config.COMBINED_TYPE,
                  f"{name} Picks", self._mix_media(movies, series))

    async def _popular_row(self) -> None:
        try:
            pop_m, pop_s = await asyncio.gather(
                tmdb.popular("movie"), tmdb.popular("tv"))
        except Exception:
            return
        movies = await self._resolve_feed("movie", pop_m, ROW_TARGET_ITEMS)
        series = await self._resolve_feed("tv", pop_s, ROW_TARGET_ITEMS)
        self._add(SCORE_POPULAR, "nr-popular", config.COMBINED_TYPE,
                  "Popular Now", self._mix_media(movies, series))

_locks: dict[str, asyncio.Lock] = {}


async def generate_for_user(user: dict, trigger: str = "refresh") -> str | None:
    """Generate and store all catalogs for one user. Returns error string or None."""
    lock = _locks.setdefault(user["token"], asyncio.Lock())
    # Queue behind any in-flight run (so e.g. toggling kid mode twice quickly
    # still ends with catalogs matching the final settings), and re-read the
    # user row in case settings changed while we waited.
    async with lock:
        try:
            fresh = await db.get_user(user["token"])
            if not fresh:
                return "user deleted"
            await Generator(fresh, trigger=trigger).run()
            return None
        except Exception as e:
            logger.exception(f"[{user['token'][:8]}] generation failed")
            await db.mark_generated(user["token"], error=str(e))
            return str(e)
