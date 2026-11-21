"""Validated records shared by the AI catalog search stages."""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt


@dataclass(frozen=True)
class DateClue:
    start: dt.date
    end: dt.date
    required: bool
    source: str

    def contains(self, value: str) -> bool:
        try:
            day = dt.date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return False
        return self.start <= day <= self.end


@dataclass(frozen=True)
class SearchIntent:
    query: str
    kind: str = "browse"
    media: tuple[str, ...] = ("movie", "tv")
    media_explicit: bool = False
    media_source: str = ""
    date: DateClue | None = None
    setting_period: str = ""
    genres: tuple[str, ...] = ()
    excluded_genres: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    excluded_topics: tuple[str, ...] = ()
    topic_relationship: str = "all"
    entities: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    preference: str = "recognition"
    ambiguities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"title_lookup", "remembered_title", "browse", "similar_titles"}:
            raise ValueError("invalid search intent")
        if not self.media or any(m not in {"movie", "tv"} for m in self.media):
            raise ValueError("invalid media")
        if self.preference not in {"recognition", "quality", "recency", "obscurity", "similarity"}:
            raise ValueError("invalid preference")
        if self.topic_relationship not in {"all", "any"}:
            raise ValueError("invalid topic relationship")
        if len(self.entities) > 4 or len(self.topics) > 4 or len(self.queries) > 3:
            raise ValueError("search intent exceeds limits")


@dataclass
class Candidate:
    media: str
    tmdb_id: int
    title: str
    original_title: str = ""
    date: str = ""
    genre_ids: tuple[int, ...] = ()
    overview: str = ""
    votes: int = 0
    rating: float = 0.0
    popularity: float = 0.0
    aliases: set[str] = field(default_factory=set)
    provenance: set[str] = field(default_factory=set)
    identity: int = 0
    assessment: str = "unassessed"
    grade: int = 0

    @property
    def ref(self) -> str:
        return f"{self.media}:{self.tmdb_id}"

    @classmethod
    def from_tmdb(cls, media: str, row: dict, source: str) -> Candidate | None:
        try:
            tid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            return None
        if tid <= 0 or row.get("adult"):
            return None
        return cls(
            media, tid, str(row.get("title") or row.get("name") or ""),
            str(row.get("original_title") or row.get("original_name") or ""),
            str(row.get("release_date") or row.get("first_air_date") or ""),
            tuple(int(v) for v in row.get("genre_ids", []) if isinstance(v, int)),
            str(row.get("overview") or "")[:500], int(row.get("vote_count") or 0),
            float(row.get("vote_average") or 0), float(row.get("popularity") or 0),
            provenance={source},
        )

    def merge(self, other: Candidate) -> None:
        self.provenance.update(other.provenance)
        self.aliases.update(other.aliases)
        self.identity = max(self.identity, other.identity)
        for attr in ("title", "original_title", "date", "overview", "genre_ids"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
