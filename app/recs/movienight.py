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

The deck blends whatever tastes it knows. With known viewers it ranks by the
*worst* fingerprint response rather than the average, because a film one
person loves and another cannot stand is exactly the film that will not end
the evening — an average hides that, a minimum does not.
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
DISCOVER_PAGES = 10


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


async def build_playlist(user_tokens: list[str],
                         kid_age: int | None = None) -> list[dict]:
    """The shared deck, most-likely-to-be-agreed first.

    Ranked on the least enthusiastic fingerprint rather than the average: the
    point is to end the evening, and a film that splits the room will not.
    """
    prints = await _fingerprints(user_tokens)
    store = await db.features_by_imdb() if prints else {}
    seen: set[str] = set()
    scored: list[tuple[float, dict]] = []
    for page in range(1, DISCOVER_PAGES + 1):
        try:
            results = await tmdb.discover("movie", {
                "sort_by": "popularity.desc",
                "vote_count.gte": MIN_VOTES,
                "vote_average.gte": MIN_SCORE,
                "include_adult": "false",
                "page": page,
            })
        except Exception:
            logger.debug("movie night: discover failed", exc_info=True)
            break
        for index, item in enumerate(results):
            try:
                meta = await tmdb.resolve_meta("movie", item["id"],
                                               max_age=kid_age)
            except Exception:
                continue
            if not meta or meta["id"] in seen:
                continue
            seen.add(meta["id"])
            popularity = 1.0 - (page - 1 + index / max(1, len(results))) \
                / DISCOVER_PAGES
            if prints:
                tokens = store.get(meta["id"])
                lifts = [p.lift(tokens) for p in prints] if tokens else []
                # The weakest response decides. One person's enthusiasm cannot
                # carry a film past the person who will veto it.
                fit = min(lifts) if lifts else 0.0
                score = 0.7 * min(1.0, fit / 2.5) + 0.3 * popularity
            else:
                score = popularity
            scored.append((score, {
                "id": meta["id"],
                "title": meta.get("name"),
                "year": (meta.get("releaseInfo") or "")[:4],
                "poster": meta.get("poster"),
                "genres": (meta.get("genres") or [])[:3],
                "rating": meta.get("imdbRating"),
            }))
        if len(scored) >= PLAYLIST_SIZE * 2:
            break
    scored.sort(key=lambda item: item[0], reverse=True)
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


# How much a seat's votes *this evening* outweigh their standing taste. Their
# fingerprint says what they usually like; a thumb tonight says what they want
# tonight, which is the more useful of the two once there is any of it.
SESSION_WEIGHT = 0.75
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

    seat_taste: dict[int, tuple[dict, dict]] = {}
    for seat in range(seat_count):
        seat_votes = votes.get(seat, {})
        liked = _centroid([vectors[i] for i, want in seat_votes.items()
                           if want and i in vectors])
        disliked = _centroid([vectors[i] for i, want in seat_votes.items()
                              if not want and i in vectors])
        seat_taste[seat] = (liked, disliked)

    scored: list[tuple[float, dict]] = []
    for position, meta in enumerate(alive):
        vector = vectors.get(meta["id"])
        # Position in the opening deck is the standing-taste prior, and the
        # only signal available for a seat that has not voted yet.
        prior = 1.0 - position / max(1, len(alive))
        if not vector:
            scored.append((prior * 0.5, meta))
            continue
        worst = None
        for seat in range(seat_count):
            liked, disliked = seat_taste[seat]
            if not liked and not disliked:
                seat_score = prior
            else:
                session = _affinity(vector, liked, disliked)
                # Centred on a half, not floored at zero. Clamping the low end
                # made "looks like something they refused" and "looks like
                # nothing they have seen" score identically, so a rejection
                # could not push a film below an unrelated one and the opening
                # order decided everything. A no has to be able to cost a film
                # its place, not merely fail to help it.
                seat_score = (
                    SESSION_WEIGHT * max(0.0, min(1.0, 0.5 + session * 1.1))
                    + (1 - SESSION_WEIGHT) * prior)
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
