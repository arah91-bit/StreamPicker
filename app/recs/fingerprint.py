"""One viewer as a vector, and any title scored against it.

`taste` says how much a viewer liked each thing they watched. `features` says
what each thing is. This module multiplies the two: an engagement-weighted
centroid of everything they watched, in the shared feature space, which can
then be compared against *any* title — including titles nothing in their
history is linked to.

That last part is the point. The row this replaces could only reach what TMDB
already lists as similar to something watched, which capped it at about a
hundred candidates drawn from ten seeds. A fingerprint scores anything, so
candidates can be swept broadly and ranked precisely.

**The score is lift, not a probability.** Raw cosine against a sparse vector is
a small number with no natural interpretation — every real title lands between
0.01 and 0.15, and a reader cannot tell which of those is good. Dividing by
what an *average popular title* scores turns it into something legible: 1.0
means "no more like you than anything else", 3.0 means "strongly like you".

Calling it a probability would be a lie. There are no negative labels anywhere
in this system — nobody rates anything, and a title left unwatched is as likely
to be unheard-of as disliked. What exists instead is `outcome_events` and
`recommendation_attributions`, which record whether a recommendation was
eventually played. When enough of those accumulate they are real labels, and
these hand-set family weights can be fitted against them. Until then this is an
affinity measure that is honest about being one.
"""

from __future__ import annotations

import bisect
import logging
import math
import random

from app.recs import features as feature_lib

logger = logging.getLogger("nuvio-recs")

# Titles sampled from the store to establish what "average" scores. Large
# enough that the mean is stable, small enough that scoring them is trivial.
BASELINE_SAMPLE = 600
# A fingerprint built from almost nothing is worse than no fingerprint: it
# will confidently rank the whole catalogue off two titles.
MIN_TITLES = 4
# Below this many distinct features the vocabulary is too thin to discriminate.
MIN_DIMENSIONS = 40


# Weight of the disliked centroid when it exists. Rocchio-style: a title is
# scored by how much it looks like what they like *minus* how much it looks
# like what they turned down. Deliberately well below 1 — a handful of taps in
# a bootstrap session must be able to steer, never to veto.
NEGATIVE_WEIGHT = 0.45
# Below this many explicit dislikes the negative centroid is one person's
# passing mood rather than a direction.
MIN_NEGATIVE_TITLES = 3
# Ceiling on what a single explicit "I liked this" is worth. A statement rather
# than an inference, so a few of them count for as much as a well-watched
# title — but only a few.
BOOTSTRAP_LIKE_WEIGHT = 0.7

# What the whole bootstrap session may be worth, relative to everything the
# viewer has actually watched. 0.67 caps it at two fifths of the total.
#
# This exists because a flat per-tap weight does not survive contact with a
# thorough person. One profile rated 114 titles in a sitting, and at full
# weight that came to 78% of their fingerprint against 22% for seventy-five
# titles they had genuinely watched — one half-second tap counting the same as
# ninety minutes across nine evenings. Their nature documentaries, the thing
# their viewing says loudest, fell from 13.7 lift to 3.4.
#
# Stated preference is cheap to give and cheap to be wrong about; watching
# something is expensive and therefore honest. The two must not cost the same.
BOOTSTRAP_BUDGET_RATIO = 0.67
# ...but a viewer with little or no history is exactly who the bootstrapper is
# for, and a proportional cap would crush it there. This floor keeps a session
# decisive for them while the ratio restrains it for everybody else.
BOOTSTRAP_MIN_BUDGET = 4.0


def bootstrap_weight(play_weight: float, likes: int) -> float:
    """What each explicit like is worth, given how much watching there is."""
    if likes <= 0:
        return 0.0
    if play_weight <= 0:
        # Nothing watched: the taps are the entire signal, and holding them
        # back would defeat the point of asking.
        return BOOTSTRAP_LIKE_WEIGHT
    budget = max(play_weight * BOOTSTRAP_BUDGET_RATIO, BOOTSTRAP_MIN_BUDGET)
    return min(BOOTSTRAP_LIKE_WEIGHT, budget / likes)


class Fingerprint:
    """A viewer's centroid in feature space, with a baseline to read it against.

    Optionally a second, negative centroid. Nothing derived from plays can
    build one — "never watched" and "never heard of" are the same observation
    from here — so it exists only when somebody has explicitly said no to
    something in the bootstrapper.
    """

    def __init__(self, vector: dict[str, float], vocabulary,
                 baseline: list[float], titles: int,
                 negative: dict[str, float] | None = None,
                 negative_titles: int = 0):
        self.vector = vector
        self.vocabulary = vocabulary
        self.baseline = baseline
        self.titles = titles
        self.negative = negative or {}
        self.negative_titles = negative_titles
        self.baseline_mean = (sum(baseline) / len(baseline)) if baseline else 0.0

    @property
    def usable(self) -> bool:
        return (self.titles >= MIN_TITLES
                and len(self.vector) >= MIN_DIMENSIONS
                and self.baseline_mean > 0)

    def cosine(self, tokens) -> float:
        if not tokens or not self.vector:
            return 0.0
        vector = self.vocabulary.vector(tokens)
        score = sum(self.vector.get(token, 0.0) * value
                    for token, value in vector.items())
        if self.negative:
            score -= NEGATIVE_WEIGHT * sum(
                self.negative.get(token, 0.0) * value
                for token, value in vector.items())
        # Lift is a ratio, so the score has to stay non-negative or the whole
        # scale inverts for a strongly-rejected title.
        return max(0.0, score)

    def lift(self, tokens) -> float:
        """How much more like this viewer than an average popular title."""
        if not self.baseline_mean:
            return 0.0
        return self.cosine(tokens) / self.baseline_mean

    def percentile(self, tokens) -> float:
        """Where this title falls against the baseline sample, 0-100."""
        if not self.baseline:
            return 0.0
        return 100.0 * bisect.bisect_left(
            self.baseline, self.cosine(tokens)) / len(self.baseline)

    def top_features(self, limit: int = 40,
                     families: tuple[str, ...] = ("k", "p")) -> list[str]:
        """The strongest tokens, for turning a fingerprint into a query.

        Restricted to keywords and people by default because those are what
        TMDB's discover endpoint can actually filter on, and because the
        coarse families would return half the catalogue.
        """
        wanted = [(value, token) for token, value in self.vector.items()
                  if feature_lib.family_of(token) in families]
        wanted.sort(reverse=True)
        return [token for _, token in wanted[:limit]]

    def summary(self) -> dict:
        out = {
            "titles": self.titles,
            "dimensions": len(self.vector),
            "vocabulary": len(self.vocabulary),
            "baseline_mean": round(self.baseline_mean, 5),
        }
        if self.negative:
            out["disliked"] = self.negative_titles
        return out


def _centroid(weights: dict[str, float],
              features_by_imdb: dict[str, list[str]],
              vocabulary) -> tuple[dict[str, float], int]:
    vector: dict[str, float] = {}
    used = 0
    for imdb_id, weight in weights.items():
        if weight <= 0:
            continue
        tokens = features_by_imdb.get(imdb_id)
        if not tokens:
            continue
        used += 1
        for token, value in vocabulary.vector(tokens).items():
            vector[token] = vector.get(token, 0.0) + weight * value
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm:
        vector = {token: value / norm for token, value in vector.items()}
    return vector, used


def build(weights: dict[str, float], features_by_imdb: dict[str, list[str]],
          vocabulary, baseline_pool: list[list[str]] | None = None,
          rng: random.Random | None = None,
          disliked: dict[str, float] | None = None) -> Fingerprint:
    """The fingerprint for one viewer.

    `weights` maps IMDb id to that title's engagement — normally
    `taste.TitleSignal.engagement`, already filtered to the context being
    modelled, so a parent's fingerprint is not built from children's viewing.
    `disliked` is the same shape for titles explicitly turned down.
    """
    vector, used = _centroid(weights, features_by_imdb, vocabulary)
    negative, rejected = ({}, 0)
    if disliked:
        negative, rejected = _centroid(disliked, features_by_imdb, vocabulary)
        if rejected < MIN_NEGATIVE_TITLES:
            negative, rejected = {}, 0

    fingerprint = Fingerprint(vector, vocabulary, [], used, negative, rejected)
    if baseline_pool:
        rng = rng or random.Random(0)
        sample = (baseline_pool if len(baseline_pool) <= BASELINE_SAMPLE
                  else rng.sample(baseline_pool, BASELINE_SAMPLE))
        scores = sorted(fingerprint.cosine(tokens) for tokens in sample)
        fingerprint.baseline = scores
        fingerprint.baseline_mean = (sum(scores) / len(scores)) if scores else 0.0
    return fingerprint


async def for_viewer(model, context: str | None, rng=None,
                     user_token: str | None = None
                     ) -> tuple[Fingerprint | None, dict[str, list[str]]]:
    """(fingerprint, feature store) for a viewer.

    The store is returned alongside because the caller needs it to score every
    row on the surface, and loading it once per build rather than once per row
    is the difference between one query and twenty.

    The fingerprint is None when the history or the store is too thin to say
    anything — the caller keeps its existing behaviour rather than ranking on
    noise.
    """
    from app.recs import db

    store = await db.features_by_imdb()
    weights = {
        signal.imdb_id: signal.engagement
        for signal in model.signals.values()
        if (context is None or signal.context == context)
        and model.may_seed(signal)
    }
    disliked: dict[str, float] = {}
    if user_token:
        feedback = await db.feedback_for(user_token)
        # Sized against the viewing it is joining, so a long session refines a
        # rich history instead of replacing it — and still carries a thin one
        # on its own. See `bootstrap_weight`.
        fresh_likes = [i for i, e in feedback.items()
                       if e["verdict"] == "liked" and i not in weights
                       and i in store]
        played_weight = sum(w for i, w in weights.items()
                            if w > 0 and i in store)
        like_weight = bootstrap_weight(played_weight, len(fresh_likes))
        for imdb_id in fresh_likes:
            weights[imdb_id] = like_weight
        for imdb_id, entry in feedback.items():
            if entry["verdict"] == "disliked":
                disliked[imdb_id] = 1.0
    if len(weights) < MIN_TITLES or not store:
        return None, store
    vocabulary = await feature_lib.vocabulary()
    if not len(vocabulary):
        return None, store
    baseline_pool = [tokens for imdb_id, tokens in store.items()
                     if tokens and imdb_id not in weights]
    fingerprint = build(
        {k: v for k, v in weights.items() if k in store},
        store, vocabulary, baseline_pool, rng,
        disliked={k: v for k, v in disliked.items() if k in store})
    return (fingerprint if fingerprint.usable else None), store
