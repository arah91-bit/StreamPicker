"""Titles as sparse feature vectors, and the vocabulary they share.

The taste model in `taste` answers "how much did they like this". This module
answers "what is this, exactly" — finely enough that liking can generalise
beyond the handful of titles TMDB happens to link to something already watched.

**Why not genres.** A genre vector has about twenty dimensions and almost no
discrimination: half of television is Drama. Measured on one real profile, a
fingerprint built from genre, decade and language ranked *Love Island* above
*Arrested Development*, because `l:en` and `d:2020` are true of nearly
everything and therefore say nothing. Keywords and people are where taste
actually lives — "wildlife", "anthology", "found family", a particular
showrunner — and there are tens of thousands of them.

**The features are free.** `tmdb.resolve_meta` already fetches the full detail
document for every title it resolves; `append_to_response` bundles keywords and
credits into that same request. Extraction costs one JSON parse, not one HTTP
call, which is what makes a vocabulary this wide affordable at all.

A feature is a `family:id` token. Families are weighted rather than treated
alike, because they differ in how much they narrow a title down — see
`FAMILY_WEIGHT`.
"""

from __future__ import annotations

import json
import logging
import math
import time

logger = logging.getLogger("nuvio-recs")

# How much each family of feature says about taste. Keywords lead because they
# describe what a title *is about*; people are nearly as strong and carry tone
# and craft. Genre, decade and language are near-constant across a catalogue,
# and were what drowned the first version of this — they are kept because they
# still separate a 1970s Japanese film from a 2024 American one, but quietly.
FAMILY_WEIGHT = {
    "k": 1.00,   # TMDB keyword
    "p": 0.90,   # person — top billing, or a directing/writing credit
    "n": 0.60,   # network
    "c": 0.50,   # production company
    "g": 0.35,   # genre
    "d": 0.15,   # decade
    "l": 0.10,   # original language
}
DEFAULT_FAMILY_WEIGHT = 0.5

# Cast beyond the top billing is noise: a long-running series lists hundreds of
# guest actors, and letting them all in makes every procedural look alike.
CAST_DEPTH = 10
# Crew jobs that shape what a title feels like. Everything else on a 700-person
# crew list is production apparatus.
CREW_JOBS = {"Director", "Creator", "Executive Producer", "Writer",
             "Screenplay", "Original Music Composer", "Series Composer"}
COMPANY_DEPTH = 3


def family_of(token: str) -> str:
    return token.split(":", 1)[0]


def weight_of(token: str) -> float:
    return FAMILY_WEIGHT.get(family_of(token), DEFAULT_FAMILY_WEIGHT)


def extract(detail: dict, media_type: str) -> list[str]:
    """Feature tokens for one TMDB detail document.

    Tolerant by design: a title with no keywords or no credits still yields
    genre, decade and language, and a title we cannot parse at all yields an
    empty list rather than an exception on the metadata path.
    """
    tokens: set[str] = set()
    try:
        for genre in detail.get("genres") or ():
            if genre.get("id"):
                tokens.add(f"g:{genre['id']}")

        # Movies return `keywords.keywords`; television returns
        # `keywords.results`. Same endpoint parameter, different shape.
        keywords = detail.get("keywords") or {}
        for keyword in (keywords.get("keywords")
                        or keywords.get("results") or ()):
            if keyword.get("id"):
                tokens.add(f"k:{keyword['id']}")

        credits = (detail.get("aggregate_credits")
                   or detail.get("credits") or {})
        for person in (credits.get("cast") or ())[:CAST_DEPTH]:
            if person.get("id"):
                tokens.add(f"p:{person['id']}")
        for person in credits.get("crew") or ():
            if not person.get("id"):
                continue
            jobs = {person.get("job")} | {
                role.get("job") for role in person.get("jobs") or ()}
            if jobs & CREW_JOBS or person.get("department") == "Directing":
                tokens.add(f"p:{person['id']}")

        for network in detail.get("networks") or ():
            if network.get("id"):
                tokens.add(f"n:{network['id']}")
        for company in (detail.get("production_companies") or ())[:COMPANY_DEPTH]:
            if company.get("id"):
                tokens.add(f"c:{company['id']}")

        language = detail.get("original_language")
        if language:
            tokens.add(f"l:{language}")
        date = str(detail.get("release_date")
                   or detail.get("first_air_date") or "")
        if len(date) >= 4 and date[:4].isdigit():
            tokens.add(f"d:{int(date[:4]) // 10 * 10}")
    except Exception:
        logger.debug("features: unusable TMDB detail", exc_info=True)
        return sorted(tokens)
    return sorted(tokens)


def encode(tokens: list[str]) -> str:
    return json.dumps(tokens, separators=(",", ":"))


def decode(blob: str | None) -> list[str]:
    if not blob:
        return []
    try:
        loaded = json.loads(blob)
    except (TypeError, ValueError):
        return []
    return [t for t in loaded if isinstance(t, str)] if isinstance(loaded, list) else []


class Vocabulary:
    """Inverse document frequency over every title we have features for.

    This is the half that the first attempt got wrong. IDF computed over the
    hundred titles in one viewer's history says `l:en` is rare; IDF computed
    over the whole catalogue says it is worthless, which is the truth. The
    corpus has to be the store, not the sample.
    """

    def __init__(self, document_frequency: dict[str, int], documents: int):
        self.document_frequency = document_frequency
        self.documents = max(1, documents)
        self._unseen = math.log(self.documents) + 1.0

    def idf(self, token: str) -> float:
        seen = self.document_frequency.get(token)
        if not seen:
            # Never observed in the corpus: as informative as a token can be,
            # but capped at the same ceiling a once-seen token would get.
            return self._unseen
        return math.log(self.documents / (1 + seen)) + 1.0

    def vector(self, tokens) -> dict[str, float]:
        """L2-normalised weighted vector for one title."""
        raw = {token: self.idf(token) * weight_of(token) for token in tokens}
        norm = math.sqrt(sum(value * value for value in raw.values()))
        if not norm:
            return {}
        return {token: value / norm for token, value in raw.items()}

    def __len__(self) -> int:
        return len(self.document_frequency)


# Document frequency is a full pass over tens of thousands of rows. That is
# nothing once per nightly build and far too much per request, and movie night
# reranks on every poll from every player. The corpus barely moves between
# builds, so a short-lived shared copy is both cheap and honest.
_VOCABULARY: tuple[float, Vocabulary] | None = None
VOCABULARY_TTL = 600.0


async def vocabulary(max_age: float = VOCABULARY_TTL) -> Vocabulary:
    """The IDF vocabulary for the whole feature store, cached briefly."""
    global _VOCABULARY
    from app.recs import db

    now = time.monotonic()
    if _VOCABULARY and now - _VOCABULARY[0] < max_age:
        return _VOCABULARY[1]
    frequency, documents = await db.feature_document_frequency()
    built = Vocabulary(frequency, documents)
    _VOCABULARY = (now, built)
    return built


def forget_vocabulary() -> None:
    """Drop the cached copy. For tests, and for anything that has just
    rewritten the store wholesale."""
    global _VOCABULARY
    _VOCABULARY = None
