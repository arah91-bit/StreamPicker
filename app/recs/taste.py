"""What a viewer actually likes, derived from what they actually played.

`profile.build_profile` answers "what has this person seen"; this module answers
"how much did they like it, and were they even watching it alone". It exists
because the first question turned out to be a poor proxy for the second.

The signal it replaces was `history._derived_rating`: two plays of anything, or
one play past 85% of the file, scored a flat 9 out of 10. For a series "two
plays" means two episodes, so 46 of one viewer's 77 titles scored an identical
9 — a rating that cannot discriminate, leaving pure recency as the tie-break.
That is why Top Picks collapsed onto whatever was watched most recently.

Three things are done differently here.

**Position, not bytes.** `played_titles` reads `watched_pct`, which is bytes
delivered over file size and is wrecked by seeking and read-ahead: one title
measured a maximum of 4% across 37 plays while `position_pct` on the same rows
reached 100% eleven times. `position_pct` is monotonic in playback position, so
it is the only column here that is allowed to mean "finished".

**Sessions, not plays.** A play row is one file open, and openings are cheap:
failed starts, re-opens and the picker's own retries all land in the table, so
25 rows can be one sitting. Plays are clustered into sessions (a gap over
`SESSION_GAP_SECONDS` starts a new one) and *coming back on another day* is
weighted far above volume within a day.

**Dislike is expressible.** Deliberately, and against the caution in
`history`'s docstring — with reason. That caution is right that failing to
finish something is ambiguous, so a bounce is only recorded when position data
was actually observed across repeated attempts and never once got past
`STARTED_PCT`. Absence of evidence stays neutral: rows imported from Trakt
carry no position at all and can never be scored badly for it.

Everything here is pure — plays in, model out, no I/O — so the scoring can be
tested against fixed histories rather than against a live database.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

# A gap longer than this starts a new viewing session. Three hours is long
# enough to keep a double feature together and short enough to separate
# "afternoon with the kids" from "after they went to bed".
SESSION_GAP_SECONDS = 3 * 3600

# Position thresholds, in percent of file. `position_pct` runs slightly ahead
# of what was actually seen (the player buffers) and has been observed above
# 100, so FINISHED sits well below the top and every reading is clamped.
FINISHED_PCT = 80.0
STARTED_PCT = 10.0

# Taste ages. A half-life rather than the original three-step recency ladder,
# which jumped discontinuously at 90 days and again at a year.
RECENCY_HALF_LIFE_DAYS = 120.0

# Engagement mixes three independent questions: how much of it did they take
# in, did they come back for more, and did they finish what they started.
WEIGHT_BREADTH = 0.40
WEIGHT_LOYALTY = 0.30
WEIGHT_FINISH = 0.30

# Recency scales the result rather than dominating it: a favourite from last
# year should still outrank something bounced off this morning.
RECENCY_FLOOR = 0.35

# Episodes at which a series counts as fully explored, for the log curve.
BREADTH_FULL_EPISODES = 10.0
# Sessions beyond the first at which loyalty saturates.
LOYALTY_FULL_RETURNS = 3.0

# Plays imported from Trakt record attendance only — no position, no duration.
# They are real history and must still count, but they cannot carry a claim
# about finishing, so their confidence is capped.
IMPORT_PICKER = "trakt-import"
IMPORTED_CONFIDENCE = 0.55

# TMDB's explicit Kids genre, and nothing else, marks a title as family
# viewing that happened to be played on an adult's profile.
#
# Two richer classifiers were tried against a real 570-play history and both
# were measurably worse, so neither is here:
#
#   * **Hour of day.** Kids' content really does own 08:00-18:00 on that
#     history while adult content owns the evening, but applied per title the
#     pattern misfires: Castlevania: Nocturne (TV-MA) and Kaiju No. 8 were
#     both classified as family purely because their few plays happened to
#     land in the afternoon.
#   * **Co-viewing.** Sharing a session with a known kids' title sounds
#     stronger and is weaker still, because in a household that plays
#     preschool television on a parent's profile all day, almost anything
#     shares a session boundary with it. Measured shares: Hell's Paradise
#     1.00, Castlevania: Nocturne 0.00 — the exact opposite of the truth.
#
# The genre tag catches every preschool title in that history and nothing
# else. Under-inclusive by design: wrongly leaving a title in an adult's
# profile costs one mediocre recommendation, while wrongly removing one
# silently deletes part of their taste.
KIDS_GENRES = {"kids"}

CONTEXT_SOLO = "solo"
CONTEXT_FAMILY = "family"

# Seed ranking tilts toward titles the proxy actually measured, without
# discarding imported history: at equal engagement a verified favourite leads
# an attendance-only one, but a strong imported title still outranks a weak
# measured one.
CONFIDENCE_TILT = 0.3


def genre_slug(name: str) -> str:
    """TMDB's label as `profile.build_profile` stores it, so the vocabularies
    of the two modules are interchangeable."""
    return str(name or "").lower().replace(" ", "-")


def _clamp_pct(value) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class TitleSignal:
    """One title, as this viewer engaged with it."""

    imdb_id: str
    media_type: str
    genres: tuple[str, ...] = ()
    plays: int = 0
    observed_plays: int = 0          # plays the proxy watched, not imported
    attempts: int = 0                # plays that reported a position at all
    finished: int = 0                # attempts that reached FINISHED_PCT
    episodes: int = 0                # distinct episodes touched
    finished_episodes: int = 0       # distinct episodes actually completed
    sessions: int = 0
    first_played_at: int = 0
    last_played_at: int = 0
    best_pct: float = 0.0
    engagement: float = 0.0
    confidence: float = 0.0
    context: str = CONTEXT_SOLO
    bounced: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.imdb_id, self.media_type)


def sessionise(timestamps: list[int],
               gap: int = SESSION_GAP_SECONDS) -> int:
    """How many separate sittings a set of play times represents."""
    if not timestamps:
        return 0
    ordered = sorted(timestamps)
    sessions = 1
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous > gap:
            sessions += 1
    return sessions


def _breadth(signal: TitleSignal) -> float:
    """How much of the title was taken in, 0..1."""
    if signal.media_type == "movie":
        if signal.finished:
            return 1.0
        if signal.best_pct >= STARTED_PCT:
            return 0.45
        # Attendance-only: an imported play says they watched it, and says
        # nothing about how much, so it sits mid-scale rather than at zero.
        return 0.25 if not signal.attempts else 0.1
    seen = signal.finished_episodes or signal.episodes
    if not seen:
        return 0.0
    return min(1.0, math.log1p(seen) / math.log1p(BREADTH_FULL_EPISODES))


def _loyalty(signal: TitleSignal) -> float:
    """Did they come back another day? 0..1."""
    if signal.sessions <= 1:
        return 0.0
    return min(1.0, (signal.sessions - 1) / LOYALTY_FULL_RETURNS)


def _finish_rate(signal: TitleSignal) -> float:
    """Share of measured attempts that reached the end.

    Returns a neutral 0.5 when nothing was measured. An imported title must not
    look like a failure merely because Trakt never told us where it stopped.
    """
    if signal.attempts < 1:
        return 0.5
    return signal.finished / float(signal.attempts)


def _recency(last_played_at: int, now: float) -> float:
    if not last_played_at:
        return RECENCY_FLOOR
    age_days = max(0.0, (now - last_played_at) / 86400.0)
    decay = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * decay


def _is_bounce(signal: TitleSignal) -> bool:
    """Repeatedly opened, position observed every time, never got going.

    The conjunction is the point. One abandoned play is ambiguous — a phone
    rang, a stream failed — so this needs at least two measured attempts, none
    of which reached even `STARTED_PCT`, and no episode ever finished.
    """
    return (signal.attempts >= 2 and signal.finished == 0
            and signal.best_pct < STARTED_PCT)


def _classify_context(signal: TitleSignal) -> str:
    """Family viewing or this viewer's own.

    Kids' television lands on a parent's profile because the parent's device
    plays it. Treating it as their taste is how a household ends up with
    preschool cartoons in an adult's recommendations. See `KIDS_GENRES` for
    why this is a single tag test and not something cleverer.
    """
    if KIDS_GENRES & set(signal.genres):
        return CONTEXT_FAMILY
    return CONTEXT_SOLO


@dataclass
class TasteModel:
    """Per-title engagement plus the aggregate vectors it implies."""

    signals: dict[tuple[str, str], TitleSignal] = field(default_factory=dict)
    generated_at: float = 0.0

    # ── per-title lookups ────────────────────────────────────────────────
    def signal_for(self, imdb_id: str,
                   media_type: str | None = None) -> TitleSignal | None:
        if media_type:
            return self.signals.get((imdb_id, media_type))
        for kind in ("series", "movie"):
            found = self.signals.get((imdb_id, kind))
            if found:
                return found
        return None

    def engagement_of(self, imdb_id: str,
                      media_type: str | None = None) -> float:
        signal = self.signal_for(imdb_id, media_type)
        return signal.engagement if signal else 0.0

    def context_of(self, imdb_id: str,
                   media_type: str | None = None) -> str:
        signal = self.signal_for(imdb_id, media_type)
        return signal.context if signal else CONTEXT_SOLO

    def solo_signals(self) -> list[TitleSignal]:
        return [s for s in self.signals.values() if s.context == CONTEXT_SOLO]

    # ── aggregate vectors ────────────────────────────────────────────────
    def genre_affinity(self, context: str = CONTEXT_SOLO) -> dict[str, float]:
        """Genre → 0..1 preference, from engagement rather than play counts.

        Weighted by engagement so a genre reached through one bounced title
        cannot outrank one reached through a series watched to the end.
        """
        weights: dict[str, float] = {}
        for signal in self.signals.values():
            if signal.context != context or signal.engagement <= 0:
                continue
            for slug in signal.genres:
                weights[slug] = weights.get(slug, 0.0) + signal.engagement
        if not weights:
            return {}
        top = max(weights.values())
        return {slug: value / top for slug, value in weights.items()}

    def media_share(self, context: str = CONTEXT_SOLO) -> float:
        """Share of positive engagement spent on movies, 0..1."""
        movie = series = 0.0
        for signal in self.signals.values():
            if signal.context != context or signal.engagement <= 0:
                continue
            if signal.media_type == "movie":
                movie += signal.engagement
            else:
                series += signal.engagement
        total = movie + series
        return movie / total if total > 0 else 0.5

    def seed_rank(self, signal: TitleSignal) -> float:
        """Engagement tilted toward evidence we gathered ourselves."""
        return signal.engagement * (
            (1.0 - CONFIDENCE_TILT) + CONFIDENCE_TILT * signal.confidence)

    def seed_order(self, context: str = CONTEXT_SOLO,
                   limit: int = 20) -> list[TitleSignal]:
        """Strongest titles first — the seed pool for similarity rows."""
        pool = [s for s in self.signals.values()
                if s.context == context and s.engagement > 0]
        pool.sort(key=lambda s: (self.seed_rank(s), s.last_played_at),
                  reverse=True)
        return pool[:limit]

    def summary(self) -> dict:
        """Compact, loggable description of what was learnt."""
        solo = self.solo_signals()
        return {
            "titles": len(self.signals),
            "solo_titles": len(solo),
            "family_titles": len(self.signals) - len(solo),
            "bounced": sum(1 for s in self.signals.values() if s.bounced),
            "measured": sum(1 for s in self.signals.values() if s.attempts),
            "movie_share": round(self.media_share(), 2),
        }


def build(plays: list[dict], genres_for=None,
          now: float | None = None) -> TasteModel:
    """The taste model for one viewer.

    `plays` are raw `play_history` rows. `genres_for(imdb_id)` returns that
    title's TMDB genre labels, or None when the title has never been resolved —
    an unresolved title still contributes engagement, just no genre vector.
    """
    now = time.time() if now is None else now
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in plays:
        imdb_id = row.get("imdb_id")
        media_type = row.get("media_type") or "series"
        if not imdb_id:
            continue
        grouped.setdefault((imdb_id, media_type), []).append(row)

    model = TasteModel(generated_at=now)
    for (imdb_id, media_type), rows in grouped.items():
        labels = (genres_for(imdb_id) if genres_for else None) or ()
        signal = TitleSignal(
            imdb_id=imdb_id, media_type=media_type,
            genres=tuple(genre_slug(g) for g in labels),
            plays=len(rows),
        )
        episodes: set[tuple] = set()
        finished_episodes: set[tuple] = set()
        timestamps: list[int] = []
        for row in rows:
            played_at = int(row.get("played_at") or 0)
            timestamps.append(played_at)
            if row.get("picker") != IMPORT_PICKER:
                signal.observed_plays += 1
            episode_key = (row.get("season"), row.get("episode"))
            episodes.add(episode_key)
            position = row.get("position_pct")
            if position is None:
                continue
            pct = _clamp_pct(position)
            signal.attempts += 1
            signal.best_pct = max(signal.best_pct, pct)
            if pct >= FINISHED_PCT:
                signal.finished += 1
                finished_episodes.add(episode_key)

        signal.episodes = len(episodes)
        signal.finished_episodes = len(finished_episodes)
        signal.sessions = sessionise(timestamps)
        signal.first_played_at = min(timestamps) if timestamps else 0
        signal.last_played_at = max(timestamps) if timestamps else 0
        signal.bounced = _is_bounce(signal)
        signal.context = _classify_context(signal)

        raw = (WEIGHT_BREADTH * _breadth(signal)
               + WEIGHT_LOYALTY * _loyalty(signal)
               + WEIGHT_FINISH * _finish_rate(signal))
        engagement = raw * _recency(signal.last_played_at, now)
        if signal.bounced:
            # Negative, not merely small: this has to be able to push a title
            # below one never watched at all, or a repeatedly abandoned title
            # keeps seeding rows on recency alone.
            engagement = -abs(engagement)
        signal.engagement = round(engagement, 4)

        measured = signal.attempts / float(signal.plays) if signal.plays else 0.0
        signal.confidence = round(
            IMPORTED_CONFIDENCE + (1.0 - IMPORTED_CONFIDENCE) * measured, 3)
        model.signals[signal.key] = signal
    return model


# ── diversified selection ────────────────────────────────────────────────
#
# Quotas are soft. Each is a target that costs an increasing amount to exceed
# rather than a wall that returns a short row: with one usable seed the row
# still fills, it just pays the pressure. That property is why this is a
# penalty ladder and not a filter — the previous implementation's hard `[:30]`
# was exactly a filter, and it silently discarded four of six seeds.

# Share of the row any single genre is expected to hold before it starts
# costing. A third leaves room for a genuine favourite to lead without
# letting it own the row.
GENRE_CAP_FRACTION = 1 / 3
# Below this, one seed with a deep recommendation list would still dominate.
SEED_CAP_MINIMUM = 2
# Weights are in the same units as the candidate score (0..1), so a weight of
# 0.5 means "at quota, give up half a point of relevance".
GENRE_PRESSURE = 0.55
SEED_PRESSURE = 0.45
MEDIA_PRESSURE = 0.30
# Daily reshuffle. Large enough to reorder near-equal candidates, small enough
# that it cannot lift a weak candidate over a strong one.
SELECTION_JITTER = 0.08


@dataclass
class Candidate:
    """One title in the running for a slot in a row."""

    imdb_id: str
    media_type: str
    score: float
    genres: tuple[str, ...] = ()
    seed_id: str = ""
    meta: dict = field(default_factory=dict)


def _pressure(count: int, quota: float) -> float:
    """Cost of having already taken `count` against a target of `quota`.

    Quadratic so that approaching the quota is nearly free and exceeding it
    gets expensive quickly.
    """
    if quota <= 0:
        return float(count)
    return (count / quota) ** 2


def select_diverse(candidates: list[Candidate], limit: int, *,
                   rng=None, movie_share: float = 0.5) -> list[Candidate]:
    """Greedily fill a row, spending relevance to buy variety.

    At every step the highest-scoring candidate wins *after* subtracting what
    it costs in sameness: how much of the row its genres already hold, how
    many slots its seed already took, and whether its medium is over its
    share. The result is a row that leads with the strongest pick and then
    widens, rather than one that exhausts the best seed before moving on.
    """
    if limit <= 0 or not candidates:
        return []
    genre_quota = max(1.0, limit * GENRE_CAP_FRACTION)
    seed_count = len({c.seed_id for c in candidates if c.seed_id})
    seed_quota = max(SEED_CAP_MINIMUM, limit / max(1, seed_count))
    movie_quota = max(1.0, limit * movie_share)
    series_quota = max(1.0, limit * (1.0 - movie_share))

    jittered: dict[str, float] = {}
    for candidate in candidates:
        jitter = rng.uniform(-SELECTION_JITTER, SELECTION_JITTER) if rng else 0.0
        jittered[candidate.imdb_id] = candidate.score + jitter

    taken: list[Candidate] = []
    taken_genres: dict[str, int] = {}
    taken_seeds: dict[str, int] = {}
    taken_media: dict[str, int] = {}
    remaining = {c.imdb_id: c for c in candidates}

    while remaining and len(taken) < limit:
        best_id, best_value = None, None
        for imdb_id, candidate in remaining.items():
            genre_load = max((taken_genres.get(g, 0) for g in candidate.genres),
                             default=0)
            quota = (movie_quota if candidate.media_type == "movie"
                     else series_quota)
            value = (jittered[imdb_id]
                     - GENRE_PRESSURE * _pressure(genre_load, genre_quota)
                     - SEED_PRESSURE * _pressure(
                         taken_seeds.get(candidate.seed_id, 0), seed_quota)
                     - MEDIA_PRESSURE * _pressure(
                         taken_media.get(candidate.media_type, 0), quota))
            if best_value is None or value > best_value:
                best_id, best_value = imdb_id, value
        candidate = remaining.pop(best_id)
        taken.append(candidate)
        for slug in candidate.genres:
            taken_genres[slug] = taken_genres.get(slug, 0) + 1
        taken_seeds[candidate.seed_id] = taken_seeds.get(candidate.seed_id, 0) + 1
        taken_media[candidate.media_type] = (
            taken_media.get(candidate.media_type, 0) + 1)
    return taken


async def load(viewer_key: str, limit: int = 4000) -> TasteModel:
    """The taste model for a viewer, from stored plays and cached metadata.

    The only impure entry point, and it reads caches exclusively — building a
    taste model must never fan out into TMDB.
    """
    from app.recs import db

    plays = await db.play_history(viewer_key, limit=limit)
    cached = await db.cached_genres_by_imdb(
        {row.get("imdb_id") for row in plays if row.get("imdb_id")})
    return build(plays, genres_for=cached.get)
