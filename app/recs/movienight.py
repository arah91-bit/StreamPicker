"""Movie night: get a room to agree on something, then stop.

Everybody opens their own link and votes yes or no. The moment one film has a
yes from every seat, the session ends for everybody at once and shows it.

Neither the ordering nor the pool is fixed.

The ordering is re-ranked from every vote cast so far, and the film shown next
is whichever is currently most likely to get a yes from *everyone*. Shared is
essential: if people were served unrelated films they would never vote on the
same thing and could never agree. So it is one ordering that moves, not one
ordering per person.

The pool moves too, and that matters more than it sounds. It used to be 150
films chosen before a single vote was cast, from the standing taste of
whichever seats happened to be known viewers — and never added to. A room whose
common ground was not among those 150 could not find it however it voted, and a
table of guests had its evening picked from nothing at all. So the pool is a
small window instead: ten to open with, and the next ten searched out from the
films the room has actually been agreeing on, fetched before anybody reaches
the end of the ones they have. See `topup`.

`rank` carries the argument for how it moves. The short version is that a film
with a single no is arithmetically dead and leaves the queue at once, and that
a film's score is whatever the least keen person in the room thinks of it.

Two further rules this must not break.

**A vote here is not taste.** "Not tonight, with these people" is a different
claim from "not for me", and letting it reach a fingerprint would teach the
recommender that somebody dislikes films they were simply not in the mood for
in company. Votes live in their own table, feed nothing, and are deleted with
the session.

**A guest is a guest.** Seats that are not a known viewer store no identity —
no name beyond a label typed by whoever set it up, no history, nothing that
outlives the evening.

Every seat is scored on two things at once: the fingerprint of whoever is
sitting there, if we know them, and what they have thumbed tonight. Standing
taste is the best guess available before anybody has said anything, and
steadily the weaker of the two afterwards, so tonight takes over as votes
accumulate. Guests have no fingerprint and are carried entirely by their
thumbs, which is why a room of strangers still converges.

A film's score is then whatever the *least* keen seat thinks of it. A film one
person loves and another cannot stand is exactly the film that will not end
the evening; an average hides that and a minimum does not.

The pool those scores range over has to be broad or none of this matters. A
sweep of pure popularity gives a room nothing to discover about itself — the
first version did precisely that and produced a wall of one month's
blockbusters, heavy on one franchise, with no way out however people voted.
`POOL_STRATEGIES` spans eras and moods, and a room with regulars in it also
gets sweeps built from their own strongest keywords.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time

from app.recs import db, features, fingerprint, taste, tmdb

logger = logging.getLogger("nuvio-recs")

# A session is an evening, not a record. Long enough to survive dinner and a
# restart, short enough that nothing lingers.
TTL_SECONDS = 12 * 3600
MAX_SEATS = 12
MIN_SEATS = 2
# How many films the sweeps score before the best of them are kept. Only the
# opening batch comes from here; everything after it is found from the room's
# own votes, so this no longer has to be big enough to contain the answer.
PLAYLIST_SIZE = 60

# ── how much of the pool exists at once ──────────────────────────────────
#
# The pool used to be 150 films chosen before anybody had voted, from the
# standing taste of whichever seats happened to be known viewers, and it was
# never added to. Votes only ever re-ordered it. Two things were wrong with
# that and neither is fixable by ranking: a room whose common ground was not
# among those 150 could never find it, and a guest contributed nothing to
# choosing them at all.
#
# So the pool is a small window that keeps moving instead. Ten is enough to
# vote on for a few minutes and small enough that the next ten can be
# recalculated from what the room has actually said by the time they are
# needed.
OPENING_BATCH = 10
TOPUP_BATCH = 10
# Fetch the next batch when a seat has this many cards left rather than none.
# A top-up costs a TMDB round trip per candidate, so waiting for the queue to
# empty would show somebody a spinner every ten votes — the point is that the
# seam never shows.
PREFETCH_WHEN_LEFT = 4
# A ceiling, so a long evening cannot grow the row without bound.
MAX_POOL = 240
# Films the room has agreed on that are used to search for more like them.
# Their yes counts are what makes this the *group's* signal rather than any one
# person's.
TOPUP_SEEDS = 3
# Recognisable, and worth agreeing on. A film nobody has heard of cannot get a
# room to a yes.
MIN_VOTES = 700
MIN_SCORE = 6.2

# Where the pool comes from. Ten pages of `popularity.desc` is what the first
# version used, and it produced exactly what that asks for: a wall of the
# current blockbusters, mostly one franchise. A room cannot find its taste in
# a pool that has none, so the sweep spans eras and moods as well as what is
# out this month. Every strategy carries the same recognisability floor.
POOL_STRATEGIES = (
    ("now", {"sort_by": "popularity.desc"}, 2),
    ("acclaimed", {"sort_by": "vote_average.desc", "vote_count.gte": 3000}, 2),
    ("2010s", {"sort_by": "vote_average.desc", "vote_count.gte": 2000,
               "primary_release_date.gte": "2010-01-01",
               "primary_release_date.lte": "2019-12-31"}, 1),
    ("2000s", {"sort_by": "vote_average.desc", "vote_count.gte": 1500,
               "primary_release_date.gte": "2000-01-01",
               "primary_release_date.lte": "2009-12-31"}, 1),
    ("90s", {"sort_by": "vote_average.desc", "vote_count.gte": 1200,
             "primary_release_date.gte": "1990-01-01",
             "primary_release_date.lte": "1999-12-31"}, 1),
    ("older", {"sort_by": "vote_average.desc", "vote_count.gte": 900,
               "primary_release_date.lte": "1989-12-31"}, 1),
    ("crowd", {"sort_by": "revenue.desc"}, 1),
)
# Keyword and people sweeps built from the known seats' fingerprints, so a
# room with regulars in it starts from what those people actually watch rather
# than from what is showing this month.
TASTE_SWEEPS = 4
TASTE_KEYWORDS_PER_SWEEP = 3

# How quickly tonight's thumbs take over from standing taste, as a saturating
# curve `n / (n + SESSION_HALFWAY)`: one vote is worth two fifths, two more
# than half, four nearly three quarters. A linear ramp over six votes was
# tried first and was far too slow — three thumbs down on the same franchise barely moved the queue,
# which is the single most obvious thing a person expects to work.
#
# It never reaches 1, so a known viewer's standing taste keeps a small say all
# evening. That is deliberate: a handful of noes in one sitting is thinner
# evidence than months of watching, however immediate it feels.
SESSION_HALFWAY = 1.5

# A fingerprint is a prior, not a verdict, so it is compressed toward the
# midpoint like the pool position is. Left at full range it simply outranks
# tonight: a seat whose standing taste scores a film 1.0 and a war film 0.04
# carries a 0.96 spread into the blend, while a film the room has actually
# refused only moves `tonight` a few tenths off centre. Measured, four
# explicit noes on comedies still could not stop a comedy being shown next.
STANDING_SPREAD = 0.6

# How much of the 0..1 range a film's position in the opening pool may claim.
# It has to be a tie-breaker, not a driver: at full spread the film at the top
# of the pool scored 1.0 for a seat with nothing to say about it, which beat a
# film mid-pool carrying a *perfect* session signal. Compressed around the
# midpoint it still orders an untouched pool correctly and stops deciding
# anything once people start voting.
PRIOR_SPREAD = 0.4

# ── chasing near-agreement was tried here, and measured worse ────────────
#
# The obvious next idea for this file is to promote a film the room has partly
# agreed to — two of three yeses means one tap ends the evening, so surely show
# that film next. It was built and simulated against this implementation, over
# rooms of 2-5 seats with hidden per-seat tastes, counting votes cast until a
# unanimous yes. Both as an additive bonus and as the cleaner formulation
# (score a film by the product of the scores of the seats *still to answer*,
# which makes proximity fall out of the arithmetic rather than a tuned weight):
#
#     room   this file   chasing near-agreement
#      2       4.2            4.6   votes to settle
#      3       5.4            6.6
#      4       6.3            7.5
#      5       7.4            8.5
#
# Consistently worse, and worse again when every seat votes off one snapshot
# instead of polling independently (9.1 -> 13.8 for three seats).
#
# The reason is worth keeping: a film with one yes and two probable noes gets
# promoted over films the whole room would have accepted, and then costs two
# more votes to die. Chasing agreement burns candidates two at a time, while
# ranking by what the *whole* room is predicted to accept converges on its own
# — everybody is looking at the same top film, so the first yes tends to be
# unanimous. The minimum below is already the convergence strategy.
#
# Also measured, and also a no-op: skipping the prediction for seats that have
# already voted. It changes nothing, because a seat that voted yes for a film
# is a seat whose predicted score for it was high anyway.


def new_session_id() -> str:
    return secrets.token_urlsafe(6)


def new_seat_key() -> str:
    return secrets.token_urlsafe(12)


async def _fingerprints(user_tokens: list[str]) -> list:
    """A fingerprint per known seat. Guests contribute none, by design."""
    out = []
    for token in user_tokens:
        try:
            user = await db.get_user(token)
            if not user:
                continue
            from app.recs.profile_streaming import private_namespace_for_user
            model = await taste.load(private_namespace_for_user(user))
            context = (taste.CONTEXT_SOLO
                       if not user.get("is_kid") else None)
            print_, _ = await fingerprint.for_viewer(
                model, context, user_token=token)
            if print_:
                out.append(print_)
        except Exception:
            logger.debug("movie night: no fingerprint for %s", token[:8],
                         exc_info=True)
    return out


def _sweeps(prints: list) -> list[tuple[str, dict, int]]:
    """Discover strategies for the pool: broad ones always, plus taste-shaped
    ones for every regular in the room."""
    out = list(POOL_STRATEGIES)
    for index, print_ in enumerate(prints):
        try:
            keywords = [t.split(":", 1)[1] for t in
                        print_.top_features(TASTE_SWEEPS
                                            * TASTE_KEYWORDS_PER_SWEEP, ("k",))]
        except Exception:
            continue
        for start in range(0, len(keywords), TASTE_KEYWORDS_PER_SWEEP):
            group = keywords[start:start + TASTE_KEYWORDS_PER_SWEEP]
            if group:
                out.append((f"taste{index}", {
                    "with_keywords": "|".join(group),
                    "sort_by": "vote_average.desc",
                    "vote_count.gte": 500,
                }, 1))
    return out


async def build_playlist(user_tokens: list[str],
                         kid_age: int | None = None) -> list[dict]:
    """The pool the evening is chosen from, best-first.

    Breadth is the point here, not precision — `rank` does the choosing, and
    it can only choose from what this collected. A pool swept entirely by
    popularity gives a room nothing to discover about itself, which is exactly
    what the first version did: a wall of this month's blockbusters, heavy on
    one franchise, and no way to escape it however people voted.
    """
    prints = await _fingerprints(user_tokens)
    store = await db.features_by_imdb() if prints else {}
    seen: set[str] = set()
    scored: list[tuple[float, dict]] = []
    for label, params, pages in _sweeps(prints):
        for page in range(1, pages + 1):
            query = {
                "vote_count.gte": MIN_VOTES,
                "vote_average.gte": MIN_SCORE,
                "include_adult": "false",
                "page": page,
                **params,
            }
            try:
                results = await tmdb.discover("movie", query)
            except Exception:
                logger.debug("movie night: %s sweep failed", label,
                             exc_info=True)
                continue
            for index, item in enumerate(results):
                try:
                    meta = await tmdb.resolve_meta("movie", item["id"],
                                                   max_age=kid_age)
                except Exception:
                    continue
                if not meta or meta["id"] in seen:
                    continue
                seen.add(meta["id"])
                rank_in_sweep = 1.0 - index / max(1, len(results))
                if prints:
                    tokens = store.get(meta["id"])
                    lifts = [p.lift(tokens) for p in prints] if tokens else []
                    # The weakest response decides. One person's enthusiasm
                    # cannot carry a film past whoever will veto it.
                    fit = min(lifts) if lifts else 0.0
                    score = 0.7 * min(1.0, fit / 2.5) + 0.3 * rank_in_sweep
                else:
                    score = rank_in_sweep
                scored.append((score, {
                    "id": meta["id"],
                    # Carried so a tap on the poster can fetch a trailer.
                    # Sessions created before this existed keep their stored
                    # playlist, so the page has to cope with it being absent.
                    "tmdb": item["id"],
                    "media": "movie",
                    "title": meta.get("name"),
                    "year": (meta.get("releaseInfo") or "")[:4],
                    "poster": meta.get("poster"),
                    "genres": (meta.get("genres") or [])[:3],
                    "rating": meta.get("imdbRating"),
                }))
    scored.sort(key=lambda item: item[0], reverse=True)
    logger.info("movie night pool: %d films from %d sweeps",
                min(len(scored), PLAYLIST_SIZE), len(_sweeps(prints)))
    return [meta for _, meta in scored[:PLAYLIST_SIZE]]


async def create(seats: list[dict], kid_age: int | None = None) -> dict:
    """Open a session. `seats` is [{label, user_token or None}, …]."""
    if not (MIN_SEATS <= len(seats) <= MAX_SEATS):
        raise ValueError(f"a session needs {MIN_SEATS}-{MAX_SEATS} people")
    known = [s["user_token"] for s in seats if s.get("user_token")]
    scored = await build_playlist(known, kid_age)
    if len(scored) < 5:
        raise RuntimeError("could not build a playlist")
    # Only the opening handful is stored. The rest of the evening is found from
    # what the room votes for, which is the whole point — these were chosen
    # before anybody had said anything, and for a table of guests they were
    # chosen from nothing at all.
    playlist = scored[:OPENING_BATCH]
    session_id = new_session_id()
    prepared = [{
        "seat": index,
        "seat_key": new_seat_key(),
        "user_token": seat.get("user_token"),
        "label": seat.get("label") or f"Guest {index + 1}",
    } for index, seat in enumerate(seats)]
    await db.create_match(session_id, prepared, playlist, TTL_SECONDS,
                          kid_age=kid_age)
    logger.info("movie night %s: %d seats (%d known), opening batch of %d",
                session_id, len(prepared), len(known), len(playlist))
    return {"session_id": session_id, "seats": prepared,
            "playlist_size": len(playlist)}


# ── topping the pool up from what the room has said ──────────────────────
#
# The group's fingerprint is not any one person's: it is the films that have
# collected yeses, weighted by how many seats gave them. Searching outward from
# those is what turns "here is a list, work through it" into "here is what you
# lot seem to be in the mood for".
#
# TMDB's own similarity is used rather than a keyword sweep because it is one
# request per seed and needs no vocabulary, and because a top-up has to land
# inside the few seconds a room is still voting on the previous batch.

# One in-flight top-up per session. Without this every seat's poll starts its
# own, and a room of five would fetch the same batch five times over.
_topping_up: dict[str, asyncio.Task] = {}
# When each session last tried. The in-flight guard alone is not enough: every
# seat polls every few seconds, so a room sitting at the end of its batch —
# which is exactly the state that asks for a top-up — would start a fresh one
# the instant the last finished, for as long as the tab was open. That matters
# most when there is nothing to find yet: a top-up with no agreed film to
# search from returns immediately and would spin.
_topped_up_at: dict[str, float] = {}
TOPUP_COOLDOWN = 12.0


def _group_seeds(votes: dict[int, dict[str, int]],
                 playlist: list[dict]) -> list[dict]:
    """The films the room agrees on most, best first.

    A yes from two people is a stronger steer than two yeses from one, so films
    are ordered by how much of the room wanted them. Anything carrying a no is
    excluded even if others liked it: it is dead as a candidate, and it is a
    poor description of what the room can agree on.
    """
    refused = {imdb_id for seat_votes in votes.values()
               for imdb_id, want in seat_votes.items() if not want}
    tally: dict[str, int] = {}
    for seat_votes in votes.values():
        for imdb_id, want in seat_votes.items():
            if want and imdb_id not in refused:
                tally[imdb_id] = tally.get(imdb_id, 0) + 1
    by_id = {m["id"]: m for m in playlist}
    ranked = sorted(tally.items(), key=lambda kv: -kv[1])
    return [by_id[i] for i, _ in ranked if i in by_id and by_id[i].get("tmdb")]


async def topup(session_id: str, playlist: list[dict], kid_age: int | None,
                limit: int = TOPUP_BATCH) -> int:
    """Find more films like the ones this room has been agreeing on.

    Returns how many were added. Never raises: a failed top-up costs the room
    some fresh options, not its evening.
    """
    if len(playlist) >= MAX_POOL:
        return 0
    try:
        votes = await db.match_votes_for(session_id)
        seeds = _group_seeds(votes, playlist)
        if not seeds:
            # Nothing agreed yet — nothing to search outward from. The opening
            # batch is still the best guess available.
            return 0
        have = {m["id"] for m in playlist}
        refused = {imdb_id for seat_votes in votes.values()
                   for imdb_id, want in seat_votes.items() if not want}
        found: list[dict] = []
        for seed in seeds[:TOPUP_SEEDS]:
            if len(found) >= limit:
                break
            try:
                results = await tmdb.tmdb_recommendations("movie", seed["tmdb"])
            except Exception:
                logger.debug("movie night %s: no similars for %s", session_id,
                             seed.get("title"), exc_info=True)
                continue
            for item in results:
                if len(found) >= limit:
                    break
                try:
                    meta = await tmdb.resolve_meta("movie", item["id"],
                                                   max_age=kid_age)
                except Exception:
                    continue
                # `max_age` is what keeps a children's session clean, and it is
                # only knowable because the session stores the age it was built
                # under. See the `kid_age` column.
                if not meta or meta["id"] in have or meta["id"] in refused:
                    continue
                have.add(meta["id"])
                found.append({
                    "id": meta["id"],
                    "tmdb": item["id"],
                    "media": "movie",
                    "title": meta.get("name"),
                    "year": (meta.get("releaseInfo") or "")[:4],
                    "poster": meta.get("poster"),
                    "genres": (meta.get("genres") or [])[:3],
                    "rating": meta.get("imdbRating"),
                })
        if not found:
            return 0
        added = await db.extend_match_playlist(session_id, found)
        logger.info("movie night %s: +%d films from %d agreed seed(s) (%s)",
                    session_id, added, min(len(seeds), TOPUP_SEEDS),
                    ", ".join(s.get("title") or "?" for s in seeds[:TOPUP_SEEDS]))
        return added
    except Exception:
        logger.exception("movie night %s: top-up failed", session_id)
        return 0


def _schedule_topup(session_id: str, playlist: list[dict],
                    kid_age: int | None) -> None:
    """Start a top-up without making the seat that triggered it wait.

    The page polls every few seconds, so the new films are there by the time
    anybody reaches the end of the ones they have — which is the difference
    between this feeling like one long queue and feeling like ten films
    followed by a spinner.
    """
    task = _topping_up.get(session_id)
    if task and not task.done():
        return
    now = time.monotonic()
    if now - _topped_up_at.get(session_id, 0.0) < TOPUP_COOLDOWN:
        return
    _topped_up_at[session_id] = now
    _topping_up[session_id] = asyncio.create_task(
        topup(session_id, playlist, kid_age))


# How hard a no counts against a film's *neighbours*, as opposed to itself.
#
# Weakly, and far more weakly than a yes, because of what a thumbs down in this
# room actually means. The commonest reason for one is "seen it" — which is
# frequently a film that person loved — and the next is "not in the mood
# tonight". Only the third possible reason, "I don't like this sort of thing",
# says anything about taste at all. A no is close to certain about *this film
# tonight* and nearly uninformative about everything else, so it kills the
# candidate outright and barely tints the queue.
#
# The weight itself is unchanged, because it was tuned against a room that had
# refused three or four of something and does that job well — somebody who
# usually loves comedies but is turning them down tonight has to stop being
# shown comedies. What was wrong was applying it in full to the *first*
# rejection. The ramp below is where the distinction lives.
SESSION_NEGATIVE = 0.55

# ...and it only reaches even that once several rejections agree with each
# other. This matters more than the weight above, because `_centroid`
# normalises: without it one no pushed against a whole cluster exactly as hard
# as five did, so a single "seen that one" steered the room away from the very
# thing that person likes best. A pattern of rejections is real evidence; one
# tap is a coin flip about which of the three reasons it was.
NEGATIVE_CONFIDENT_AT = 3


def _centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    if not vectors:
        return {}
    out: dict[str, float] = {}
    for vector in vectors:
        for token, value in vector.items():
            out[token] = out.get(token, 0.0) + value
    norm = math.sqrt(sum(v * v for v in out.values()))
    return {t: v / norm for t, v in out.items()} if norm else {}


def _affinity(candidate: dict[str, float], liked: dict[str, float],
              disliked: dict[str, float], negative_scale: float = 1.0) -> float:
    """How much this seat looks like it wants `candidate`.

    `negative_scale` is how much the rejections have earned the right to be
    believed — see `NEGATIVE_CONFIDENT_AT`. It exists because the centroid is
    normalised, so without it a single no weighs the same as a dozen.
    """
    score = sum(liked.get(t, 0.0) * v for t, v in candidate.items())
    if disliked:
        score -= SESSION_NEGATIVE * negative_scale * sum(
            disliked.get(t, 0.0) * v for t, v in candidate.items())
    return score


# Fingerprints do not change during an evening, and building one reads the
# whole feature store — far too much to repeat on every poll from every
# player. Held for the length of a session.
_SEAT_PRINTS: dict[str, tuple[float, dict[int, object]]] = {}
SEAT_PRINT_TTL = 1800.0


async def seat_prints(session_id: str) -> dict[int, object]:
    """{seat: fingerprint} for the known people in a session; guests absent."""
    cached = _SEAT_PRINTS.get(session_id)
    if cached and time.monotonic() - cached[0] < SEAT_PRINT_TTL:
        return cached[1]
    out: dict[int, object] = {}
    for seat in await db.match_seats(session_id):
        token = seat.get("user_token")
        if not token:
            continue
        for print_ in await _fingerprints([token]):
            out[seat["seat"]] = print_
    _SEAT_PRINTS[session_id] = (time.monotonic(), out)
    return out


def forget_seat_prints(session_id: str | None = None) -> None:
    if session_id is None:
        _SEAT_PRINTS.clear()
    else:
        _SEAT_PRINTS.pop(session_id, None)


async def rank(session_id: str, playlist: list[dict], seat_count: int,
               prints: dict[int, object] | None = None) -> list[dict]:
    """Re-order the remaining films by what the room has said so far.

    This is the difference between a deck and a matchmaker. A fixed list
    cannot converge — it just runs out — so the ordering is recomputed from
    every vote cast so far, and the film shown next is the one currently most
    likely to get a yes from *everybody*.

    Two rules do the work.

    **Anything with a single no is dead.** Winning takes a unanimous yes, so a
    film one person has already refused can never win. Leaving it in the queue
    spends everyone else's attention on an outcome that is arithmetically
    impossible.

    That is the *only* thing a no is taken to mean. It is a verdict on this
    film tonight and next to nothing about anything else, because the usual
    reason for one is having already seen it — often a film that person
    loved — and steering the room away from what somebody likes best is the
    opposite of the intent. See `SESSION_NEGATIVE`.

    **The least keen seat sets the score.** Same reason the initial deck used
    a minimum: the room is limited by whoever is hardest to please, and a film
    averaging well because one person adores it is exactly the film that will
    not end the evening.

    Those two rules *are* the convergence strategy, which is not obvious and
    is why the block above the function records what happened when it was
    replaced with something that chases near-agreement directly. Everybody is
    shown the same top film, so the first yes tends to be the unanimous one.
    """
    votes = await db.match_votes_for(session_id)
    rejected = {imdb_id for seat_votes in votes.values()
                for imdb_id, want in seat_votes.items() if not want}
    alive = [m for m in playlist if m["id"] not in rejected]
    if not alive:
        return []

    store = await db.features_by_imdb({m["id"] for m in alive}
                                      | {i for v in votes.values() for i in v})
    if not store:
        return alive
    vocab = await features.vocabulary()
    vectors = {imdb_id: vocab.vector(tokens)
               for imdb_id, tokens in store.items() if tokens}

    if prints is None:
        prints = await seat_prints(session_id)

    seat_taste: dict[int, tuple[dict, dict, float, float]] = {}
    for seat in range(seat_count):
        seat_votes = votes.get(seat, {})
        liked = _centroid([vectors[i] for i, want in seat_votes.items()
                           if want and i in vectors])
        refusals = [vectors[i] for i, want in seat_votes.items()
                    if not want and i in vectors]
        disliked = _centroid(refusals)
        # How far to believe those refusals as a statement about taste. One is
        # most likely "seen it"; three that look alike are a preference.
        negative_scale = min(1.0, len(refusals) / NEGATIVE_CONFIDENT_AT)
        # Tonight's thumbs take over from standing taste as they accumulate.
        # Before anybody has said anything their fingerprint is the best guess
        # available; several votes in, it is the weaker of the two.
        cast = len(seat_votes)
        takeover = cast / (cast + SESSION_HALFWAY) if cast else 0.0
        seat_taste[seat] = (liked, disliked, takeover, negative_scale)

    scored: list[tuple[float, dict]] = []
    for position, meta in enumerate(alive):
        vector = vectors.get(meta["id"])
        tokens = store.get(meta["id"])
        # Where the pool put it. The fallback for a guest who has not voted,
        # and for anything we have no features for. Compressed toward the
        # midpoint so it breaks ties without outvoting anybody.
        raw_prior = 1.0 - position / max(1, len(alive))
        prior = 0.5 + (raw_prior - 0.5) * PRIOR_SPREAD
        if not vector:
            scored.append((prior * 0.5, meta))
            continue
        worst = None
        for seat in range(seat_count):
            liked, disliked, takeover, negative_scale = seat_taste[seat]
            print_ = prints.get(seat)
            if print_ is not None and tokens:
                # What this person usually likes — the reason picking a known
                # viewer for a seat is worth doing at all.
                fit = min(1.0, print_.lift(tokens) / 2.5)
                standing = 0.5 + (fit - 0.5) * STANDING_SPREAD
            else:
                standing = prior
            if not liked and not disliked:
                seat_score = standing
            else:
                session = _affinity(vector, liked, disliked, negative_scale)
                # Centred on a half, not floored at zero. Clamping the low end
                # made "looks like something they refused" and "looks like
                # nothing they have seen" score identically, so a rejection
                # could not push a film below an unrelated one and the opening
                # order decided everything. A no has to be able to cost a film
                # its place, not merely fail to help it.
                tonight = max(0.0, min(1.0, 0.5 + session * 1.1))
                seat_score = takeover * tonight + (1 - takeover) * standing
            worst = seat_score if worst is None else min(worst, seat_score)
        scored.append((worst if worst is not None else prior, meta))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [meta for _, meta in scored]


async def vote(seat_row: dict, imdb_id: str, want: bool) -> dict:
    """Record one vote and settle the session if the room has agreed."""
    session_id = seat_row["session_id"]
    await db.record_match_vote(session_id, seat_row["seat"], imdb_id, want)
    if want:
        agreed = await db.match_unanimous(session_id, seat_row["seat_count"])
        if agreed:
            await db.set_match_winner(session_id, agreed)
    return await state(seat_row)


async def state(seat_row: dict) -> dict:
    """What this seat should be showing right now."""
    import json

    session_id = seat_row["session_id"]
    await db.touch_match_seat(session_id, seat_row["seat"])
    playlist = json.loads(seat_row["playlist"])
    votes = await db.match_votes_for(session_id)
    mine = votes.get(seat_row["seat"], {})

    # Re-read rather than trusting the row we were handed: another seat may
    # have settled it between that read and this one.
    fresh = await db.match_by_seat_key(seat_row["seat_key"])
    winner_id = (fresh or seat_row).get("winner_imdb")
    winner = next((m for m in playlist if m["id"] == winner_id), None) \
        if winner_id else None

    ranked = [] if winner else await rank(session_id, playlist,
                                          seat_row["seat_count"])
    remaining = [m for m in ranked if m["id"] not in mine]
    # Top up before this seat runs out, not after. Fired from whichever seat
    # notices first and shared by the room, so the batch is being fetched while
    # everybody is still voting on the current one.
    if not winner and len(remaining) <= PREFETCH_WHEN_LEFT:
        _schedule_topup(session_id, playlist, seat_row.get("kid_age"))
    seats = await db.match_seats(session_id)
    now = int(time.time())
    return {
        "winner": winner,
        "seat": seat_row["seat"],
        "label": seat_row["label"],
        "seats": seat_row["seat_count"],
        "voted": len(mine),
        "wanted": sum(1 for v in mine.values() if v),
        "playlist_size": len(playlist),
        "in_play": len(ranked),
        "next": remaining[:1],
        "exhausted": not remaining,
        # Whether more are on the way, so an empty moment can say "looking"
        # rather than "that's the lot" — they are different things and only one
        # of them is worth waiting through.
        "finding_more": bool((task := _topping_up.get(session_id))
                             and not task.done()),
        "others": [{
            "label": s["label"],
            "voted": len(votes.get(s["seat"], {})),
            "here": bool(s["last_seen_at"] and now - s["last_seen_at"] < 90),
        } for s in seats if s["seat"] != seat_row["seat"]],
    }
