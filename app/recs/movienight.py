"""Movie night: get a room to agree on something, then stop.

Everybody opens their own link and votes yes or no. The moment one film has a
yes from every seat, the session ends for everybody at once and shows it.

The ordering is shared but not fixed. A static list cannot converge — it just
runs out — so the queue is re-ranked from every vote cast so far, and the film
shown next is whichever is currently most likely to get a yes from *everyone*.
Shared is still essential: if people were served unrelated films they would
never vote on the same thing and could never agree. So it is one ordering that
moves, not one ordering per person.

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
PLAYLIST_SIZE = 150
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
    playlist = await build_playlist(known, kid_age)
    if len(playlist) < 5:
        raise RuntimeError("could not build a playlist")
    session_id = new_session_id()
    prepared = [{
        "seat": index,
        "seat_key": new_seat_key(),
        "user_token": seat.get("user_token"),
        "label": seat.get("label") or f"Guest {index + 1}",
    } for index, seat in enumerate(seats)]
    await db.create_match(session_id, prepared, playlist, TTL_SECONDS)
    logger.info("movie night %s: %d seats (%d known), %d films",
                session_id, len(prepared), len(known), len(playlist))
    return {"session_id": session_id, "seats": prepared,
            "playlist_size": len(playlist)}


# A no counts against a film's neighbours as well as itself, but not as hard
# as a yes counts for them: rejecting one war film is not rejecting the genre.
SESSION_NEGATIVE = 0.55


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
              disliked: dict[str, float]) -> float:
    score = sum(liked.get(t, 0.0) * v for t, v in candidate.items())
    if disliked:
        score -= SESSION_NEGATIVE * sum(
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

    **The least keen seat sets the score.** Same reason the initial deck used
    a minimum: the room is limited by whoever is hardest to please, and a film
    averaging well because one person adores it is exactly the film that will
    not end the evening.
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

    seat_taste: dict[int, tuple[dict, dict, float]] = {}
    for seat in range(seat_count):
        seat_votes = votes.get(seat, {})
        liked = _centroid([vectors[i] for i, want in seat_votes.items()
                           if want and i in vectors])
        disliked = _centroid([vectors[i] for i, want in seat_votes.items()
                              if not want and i in vectors])
        # Tonight's thumbs take over from standing taste as they accumulate.
        # Before anybody has said anything their fingerprint is the best guess
        # available; several votes in, it is the weaker of the two.
        cast = len(seat_votes)
        takeover = cast / (cast + SESSION_HALFWAY) if cast else 0.0
        seat_taste[seat] = (liked, disliked, takeover)

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
            liked, disliked, takeover = seat_taste[seat]
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
                session = _affinity(vector, liked, disliked)
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
        "others": [{
            "label": s["label"],
            "voted": len(votes.get(s["seat"], {})),
            "here": bool(s["last_seen_at"] and now - s["last_seen_at"] < 90),
        } for s in seats if s["seat"] != seat_row["seat"]],
    }
