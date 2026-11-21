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

There is a fifth answer, "want to watch", and it does two jobs at once: it
stocks the row of the same name (`app/recs/watchlist.py`) and it counts as a
softer like. It is stored apart from the verdicts because it is a different
kind of claim — see the `taste_watchlist` comment in `db.py`.

The deck steers. Once enough answers are in, the sweep leans towards the
genres that are landing and away from the ones being turned down, which is
what makes a long session worth more than a short one. It leans rather than
narrows: `EXPLORE_SHARE` of the sweeps stay on what the viewer originally
picked, because a deck that only ever asks about the one thing it already
knows you like learns nothing and has nothing new to recommend.
"""

from __future__ import annotations

import logging
import random

from app.recs import config, db, taste, tmdb

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


def _slug_id_set(slugs: list[str], media: str) -> set[str]:
    """TMDB genre ids for these slugs, as a set.

    A set rather than the joined string the sweep actually sends, because the
    steering has to subtract the avoided ids from the wanted ones and TMDB's
    taxonomy is many-to-one — subtracting the strings would not catch it.
    """
    table = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
    return {str(table[s]) for s in slugs if s in table}


# The fifth answer. Stored in `taste_watchlist` rather than as a verdict, so it
# is not in `db.VERDICTS` and every verdict-shaped path has to name it
# explicitly — which is the point. See `db.py`.
SAVE_VERDICT = "watchlist"

# ── steering ─────────────────────────────────────────────────────────────
#
# What separates this from a plain popularity deck: the answers change what
# gets asked next. Every constant below exists to stop that becoming a tunnel.

# Answers needed before steering starts. Two likes is a mood, not a taste, and
# steering on it would spend the rest of the session confirming an accident.
MIN_STEER_ANSWERS = 6
# Positive answers a genre needs before it may pull the deck. A single liked
# film drags three genres with it, only one of which is usually the reason.
FAVOUR_MIN_HITS = 2
# Net score at which a genre is actively avoided. Two clear rejections and
# nothing on the other side — deliberately harder to earn than favour, because
# this one removes titles from the deck rather than adding them.
AVOID_MAX_NET = -2.0
# How many genres may be avoided at once, so the sweep cannot be filtered down
# to nothing by a run of dislikes.
MAX_AVOIDED = 3
# Sweeps that ignore the steering entirely and go back to what the viewer
# actually picked. A deck that only ever asks about the thing it already knows
# you like confirms itself, learns nothing, and leaves the recommender with
# one genre to work from.
EXPLORE_SHARE = 0.34
# How many of the favoured genres are in play. Always leaning on the single
# best one is the tunnel again, one level down.
FOCUS_CHOICES = 4


def _steerable(slug: str) -> bool:
    """Can this genre actually be asked for?

    The labels on a card are TMDB's display names, and its TV taxonomy uses
    compound ones — "Sci-Fi & Fantasy", "Action & Adventure", "War & Politics"
    — that exist in neither id table. Left in, one of those wins a focus slot,
    resolves to no genre id, and the sweep is skipped: a wasted round trip out
    of the six a deck gets, spent on the genre the viewer likes most.
    """
    return slug in tmdb.MOVIE_GENRES or slug in tmdb.TV_GENRES


async def steer(user_token: str) -> tuple[list[str], list[str]]:
    """(genres to lean towards, genres to steer clear of), from the answers.

    Genres rather than the full fingerprint on purpose. The fingerprint needs
    the feature store and a history to read against, and the viewer this page
    exists for has neither yet — while genre labels are already on every card
    the deck dealt, so this costs one cached read and works from the sixth tap.
    """
    positives: set[str] = set()
    negatives: set[str] = set()
    feedback = await db.feedback_for(user_token)
    for imdb_id, entry in feedback.items():
        if entry["verdict"] == "liked":
            positives.add(imdb_id)
        elif entry["verdict"] == "disliked":
            negatives.add(imdb_id)
    # A save is an appeal signal — the premise and the poster landed — so it
    # steers the same way a like does. It says nothing about how the thing
    # turned out, which is why it weighs less everywhere it is weighed at all.
    positives |= await db.watchlist_ids(user_token)
    if len(positives) + len(negatives) < MIN_STEER_ANSWERS:
        return [], []

    # Cache-only: every one of these titles was resolved when it was dealt, so
    # the meta is already local and a miss just means one card contributes no
    # genres.
    genres = await db.cached_genres_by_imdb(positives | negatives)
    score: dict[str, float] = {}
    hits: dict[str, int] = {}
    for imdb_id in positives:
        for label in genres.get(imdb_id) or ():
            slug = taste.genre_slug(label)
            score[slug] = score.get(slug, 0.0) + 1.0
            hits[slug] = hits.get(slug, 0) + 1
    for imdb_id in negatives:
        for label in genres.get(imdb_id) or ():
            slug = taste.genre_slug(label)
            score[slug] = score.get(slug, 0.0) - 1.0

    favoured = sorted((s for s, v in score.items()
                       if v > 0 and hits.get(s, 0) >= FAVOUR_MIN_HITS
                       and _steerable(s)),
                      key=lambda s: -score[s])
    avoided = sorted((s for s, v in score.items()
                      if v <= AVOID_MAX_NET and _steerable(s)),
                     key=lambda s: score[s])[:MAX_AVOIDED]
    return favoured, avoided


# How many differently-paged sweeps to try before giving up on filling a deck.
# One pass is not enough once a session is a few decks deep: every request
# picks a page at random, so two requests routinely landed on the same page
# and dealt the same titles again.
MAX_ATTEMPTS = 6


async def deck(user: dict, seen: set[str], size: int = DECK_SIZE,
               rng: random.Random | None = None,
               steering: tuple[list[str], list[str]] | None = None) -> list[dict]:
    """The next handful of cards for one viewer.

    `seen` is every title already ruled on, saved, played, or *dealt earlier in
    this session* — the last one matters. A card sitting unanswered in the
    phone's queue is not in the database anywhere, so a deck built only from
    stored verdicts happily dealt it again.

    Pages are tried until the deck fills rather than once, because a random
    page that has already been exhausted otherwise returns a deck of nothing.

    `steering` is looked up when not supplied. A failure there costs the
    session its tailoring and nothing else — an unsteered deck is the deck this
    page shipped with, so falling back to it is never worse than not answering.
    """
    rng = rng or random.Random()
    genres, countries = db.bootstrap_taste(user)
    if steering is None:
        try:
            steering = await steer(user["token"])
        except Exception:
            logger.debug("bootstrap: steering unavailable", exc_info=True)
            steering = ([], [])
    favoured, avoided = steering
    # Never steer away from something they asked for by name. The answers may
    # suggest their horror picks are not working out; the tick box is still the
    # more explicit instruction of the two.
    avoided = [s for s in avoided if s not in set(genres)]
    out: list[dict] = []
    tried: set[tuple[str, int]] = set()
    medias = ["movie", "tv"]
    rng.shuffle(medias)
    for attempt in range(MAX_ATTEMPTS):
        if len(out) >= size:
            break
        for media in medias:
            if len(out) >= size:
                break
            page = rng.randint(1, MAX_PAGE)
            if (media, page) in tried:
                continue
            tried.add((media, page))
            params = {
                "sort_by": "popularity.desc",
                "vote_count.gte": MIN_VOTES,
                "vote_average.gte": MIN_SCORE,
                "include_adult": "false",
                "page": page,
            }
            # Lean on one genre that is landing, or fall back to what they
            # picked. One at a time rather than all of them: TMDB treats a
            # `|`-joined list as "any of these", so pooling every favourite
            # into one sweep gives back the same popularity ordering the
            # unsteered deck already returns.
            wanted = genres
            if favoured and rng.random() >= EXPLORE_SHARE:
                wanted = [rng.choice(favoured[:FOCUS_CHOICES])]
            picked = _slug_id_set(wanted, media)
            if picked:
                params["with_genres"] = "|".join(sorted(picked))
            elif wanted:
                # They asked for something this medium does not have — TMDB's
                # TV taxonomy carries no Horror or Romance, for instance.
                # Skipping is the honest answer; sweeping unfiltered would
                # quietly hand back generic television and call it their pick.
                continue
            # Subtracted at the id level, not the slug level. TMDB's taxonomy
            # is many-to-one — `thriller` and `suspense` are both 53 on the
            # movie side, `thriller` and `crime` both 80 on the TV side — so
            # comparing slugs happily produces `with_genres=53&without_genres=53`
            # and a sweep that can only ever return nothing.
            shunned = _slug_id_set(avoided, media) - picked
            if shunned:
                params["without_genres"] = "|".join(sorted(shunned))
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
        # Carried so a tap on the poster can ask for a trailer without first
        # resolving the IMDb id back to the TMDB one it came from.
        "tmdb": item["id"],
        "media": media,
        "type": "movie" if media == "movie" else "series",
        "title": meta.get("name"),
        "year": (meta.get("releaseInfo") or "")[:4],
        "poster": meta.get("poster"),
        "genres": (meta.get("genres") or [])[:3],
        "rating": meta.get("imdbRating"),
        "overview": (meta.get("description") or "")[:220],
    }


async def already_known(user_token: str, viewer_key: str) -> set[str]:
    """Titles this viewer has already ruled on, saved, or played.

    The watchlist has to be in here as well as the verdicts. A save is an
    answer — the card left the screen and the viewer is done with it — but it
    is stored somewhere else entirely, so a deck built from verdicts alone
    deals it straight back.
    """
    known = set(await db.feedback_for(user_token))
    try:
        known |= await db.watchlist_ids(user_token)
    except Exception:
        logger.debug("bootstrap: could not read watchlist", exc_info=True)
    # Cards already put on screen, answered or not. The client sends back what
    # it is holding, but that is a page variable: one reload and the last deck
    # is dealt all over again, which is what people actually saw.
    try:
        known |= await db.dealt_recently(user_token, config.TASTE_DEALT_HOURS)
    except Exception:
        logger.debug("bootstrap: could not read dealt cards", exc_info=True)
    try:
        for row in await db.played_titles(viewer_key):
            known.add(row["imdb_id"])
    except Exception:
        logger.debug("bootstrap: could not read play history", exc_info=True)
    return known


# Enough directional verdicts to build a fingerprint from nothing else.
ENOUGH_RATED = 12
# Past that, rebuild every this-many further verdicts. There is no "done"
# button — people close the tab — so the rebuild cannot wait to be asked for.
# Stateless and deterministic on the count, which beats a timer that has to
# survive a phone going to sleep mid-session.
REBUILD_EVERY = 6


def should_rebuild(rated: int) -> bool:
    return rated >= ENOUGH_RATED and rated % REBUILD_EVERY == 0


async def progress(user_token: str) -> dict:
    """Counts for the progress readout, and whether it is worth rebuilding."""
    counts = await db.feedback_counts(user_token)
    rated = counts.get("liked", 0) + counts.get("disliked", 0)
    try:
        saved = await db.watchlist_count(user_token)
    except Exception:
        logger.debug("bootstrap: could not count the watchlist", exc_info=True)
        saved = 0
    # A save counts towards the total the same way a like and a dislike do.
    # The rule has always been that an answer counts if it says which way this
    # person's taste runs, and "I want to watch that" plainly does — it is the
    # premise, the poster and the genre all landing. What it does not say is
    # that they have seen it, which is why it is stored elsewhere and weighed
    # more lightly than a like. "It was fine" and "never seen it" still count
    # for nothing here: both are real answers and neither has a direction.
    directional = rated + saved
    return {
        "liked": counts.get("liked", 0),
        "disliked": counts.get("disliked", 0),
        "seen": counts.get("seen", 0),
        "saved": saved,
        "rated": directional,
        "enough": directional >= ENOUGH_RATED,
    }
