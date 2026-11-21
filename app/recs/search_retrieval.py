"""Bounded TMDB retrieval, keeping metadata and provenance through ranking."""
from __future__ import annotations

import asyncio
import time
import re

from app.recs import tmdb
from app.recs.search_models import Candidate, SearchIntent

MAX_REQUESTS = 16
MAX_CANDIDATES = 60


def _norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


class Retriever:
    def __init__(self, intent: SearchIntent, deadline: float | None = None):
        self.intent = intent
        self.deadline = deadline
        self.calls = 0
        self.items: dict[str, Candidate] = {}
        self.errors = 0
        self._browse_params: dict[str, dict] = {}
        self._browse_sources: dict[str, str] = {}

    async def _call(self, fn, *args, **kwargs):
        remaining = self.deadline - time.monotonic() if self.deadline else 5.0
        if self.calls >= MAX_REQUESTS or remaining <= 0:
            return None
        self.calls += 1
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=min(5.0, remaining))
        except Exception:
            self.errors += 1
            return None

    def add(self, media: str, rows: list[dict] | None, source: str, identity: int = 0) -> None:
        for row in (rows or []):
            candidate = Candidate.from_tmdb(media, row, source)
            if candidate is None:
                continue
            candidate.identity = identity
            if candidate.ref in self.items:
                self.items[candidate.ref].merge(candidate)
            elif len(self.items) < MAX_CANDIDATES:
                self.items[candidate.ref] = candidate

    def _params(self, media: str, page: int) -> dict:
        intent = self.intent
        params: dict = {"sort_by": "vote_count.desc" if intent.date else "popularity.desc", "vote_count.gte": 10, "page": page}
        if intent.preference == "obscurity":
            params.update({"sort_by": "vote_average.desc", "vote_count.gte": 50, "vote_count.lte": 1000})
            if media == "movie":
                params["with_runtime.gte"] = 70
        if intent.date and intent.date.required:
            field = "primary_release_date" if media == "movie" else "first_air_date"
            params[f"{field}.gte"] = intent.date.start.isoformat()
            params[f"{field}.lte"] = intent.date.end.isoformat()
        mapping = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
        genres = [mapping[g] for g in intent.genres if g in mapping]
        if genres:
            params["with_genres"] = ",".join(map(str, genres))
        excluded = [mapping[g] for g in intent.excluded_genres if g in mapping]
        if excluded:
            params["without_genres"] = ",".join(map(str, excluded))
        return params

    async def browse(self, pages: int = 2) -> None:
        keyword_ids = []
        for topic in self.intent.topics[:2]:
            matches = await self._call(tmdb.search_keyword_results, topic, 4)
            exact = [int(k["id"]) for k in matches or [] if _norm(k.get("name", "")) in {_norm(topic), _norm(topic.rstrip("s"))}]
            if exact:
                keyword_ids.append(exact[0])
        excluded_keyword_ids = []
        for topic in self.intent.excluded_topics[:4]:
            term = topic.rstrip("s")
            matches = await self._call(tmdb.search_keyword_results, term, 4)
            exact = [int(k["id"]) for k in matches or [] if _norm(k.get("name", "")) == _norm(term)]
            if not exact:
                return
            excluded_keyword_ids.append(exact[0])
        setting_keyword = None
        if self.intent.setting_period:
            match = re.search(r"(?<!\d)(?:(18|19|20))?(\d{2})['’]?s\b", self.intent.setting_period.lower())
            if match:
                year = (match.group(1) or ("20" if int(match.group(2)) <= 20 else "19")) + match.group(2) + "s"
                matches = await self._call(tmdb.search_keyword_results, year, 4)
                setting_keyword = next((int(k["id"]) for k in matches or [] if _norm(k.get("name", "")) == _norm(year)), None)
            if setting_keyword is None:
                return
        for media in self.intent.media:
            mapping = tmdb.MOVIE_GENRES if media == "movie" else tmdb.TV_GENRES
            if any(g not in mapping for g in self.intent.genres):
                continue
            params = self._params(media, 1)
            if keyword_ids and len(keyword_ids) == len(self.intent.topics[:2]):
                params["with_keywords"] = ("," if self.intent.topic_relationship == "all" else "|").join(map(str, keyword_ids))
            if setting_keyword:
                params["with_keywords"] = str(setting_keyword) if "with_keywords" not in params else params["with_keywords"] + "," + str(setting_keyword)
            if excluded_keyword_ids:
                params["without_keywords"] = "|".join(map(str, excluded_keyword_ids))
            suffix = (":keywords:" if keyword_ids and len(keyword_ids) == len(self.intent.topics[:2]) else "") + (":setting:" if setting_keyword else "")
            self._browse_params[media] = dict(params)
            self._browse_sources[media] = suffix
            for page in range(1, pages + 1):
                params["page"] = page
                rows = await self._call(tmdb.discover, media, dict(params))
                self.add(media, rows, f"discover:{page}" + suffix)
                if not rows or len(self.items) >= MAX_CANDIDATES:
                    break

    async def browse_more(self) -> None:
        """One bounded recovery page using the exact original filters."""
        if self.intent.kind != "browse":
            return
        for media, original in self._browse_params.items():
            if self.calls >= MAX_REQUESTS or len(self.items) >= MAX_CANDIDATES:
                break
            params = {**original, "page": 3}
            rows = await self._call(tmdb.discover, media, params)
            self.add(media, rows, "discover:recovery" + self._browse_sources.get(media, ""))

    async def titles(self) -> None:
        intent = self.intent
        queries = list(dict.fromkeys([*intent.entities, *intent.queries]))[:3]
        original_queries = set(queries)
        supported_queries: set[str] = set()
        for query in queries:
            for media in intent.media:
                alias_checks = 0
                year = intent.date.start.year if intent.date and not intent.date.required and intent.date.start.year == intent.date.end.year else None
                year_rows = await self._call(tmdb.search, media, query, year=year) if year else []
                broad_rows = await self._call(tmdb.search, media, query)
                rows = list({int(r["id"]): r for r in [*(year_rows or [])[:8], *(broad_rows or [])[:8]] if r.get("id")}.values())
                for row in rows:
                    name = str(row.get("title") or row.get("name") or "")
                    original = str(row.get("original_title") or row.get("original_name") or "")
                    normalized = _norm(query)
                    actual = {_norm(name), _norm(original)}
                    identity = 4 if normalized in actual and query in original_queries else 1 if normalized in actual else 0
                    # A punctuation variant is useful as an expansion seed, but
                    # remains an unverified search hit until catalog evidence agrees.
                    if identity == 0 and name.casefold().startswith(query.casefold() + ":"):
                        identity = 3 if query in original_queries or query in supported_queries else 1
                    if identity == 0 and len(query) > 3 and alias_checks < 4:
                        alias_checks += 1
                        aliases = await self._call(tmdb.alternative_titles, media, int(row["id"]))
                        if any(_norm(a) == normalized for a in aliases or []):
                            identity = 2
                        elif any(_norm(a).startswith(normalized + " ") for a in aliases or []):
                            identity = 2
                    if identity == 0:
                        continue
                    self.add(media, [row], f"search:{query}", identity)
                    expand_prefix = name.casefold().startswith(query.casefold() + ":") and len(query) <= 12
                    expand_alias = identity == 2 and intent.kind == "remembered_title" and not intent.date
                    if (query in original_queries and (expand_prefix or expand_alias)
                            and name and _norm(name) != normalized and name not in queries and len(queries) < 3):
                        queries.append(name)
                        supported_queries.add(name)
        # Collection membership is strong catalog evidence, and does not depend
        # on title punctuation. Inspect at most six exact or near-exact seeds.
        seeds = sorted((c for c in self.items.values() if c.identity >= 3), key=lambda c: -c.identity)[:6] if not intent.date else []
        for seed in seeds:
            if self.calls >= MAX_REQUESTS:
                continue
            detail = await self._call(tmdb.candidate_details, seed.media, seed.tmdb_id)
            if detail:
                aliases = ((detail.get("alternative_titles") or {}).get("titles") or (detail.get("alternative_titles") or {}).get("results") or [])
                seed.aliases.update(str(a.get("title") or a.get("name") or "") for a in aliases if isinstance(a, dict))
            if seed.media != "movie":
                continue
            collection = (detail or {}).get("belongs_to_collection") or {}
            if not collection.get("id"):
                continue
            parts = await self._call(tmdb.collection_parts, int(collection["id"]))
            self.add("movie", parts, f"collection:{collection['id']}", 3)

    async def similar(self) -> None:
        await self.titles()
        seeds = sorted((c for c in self.items.values() if c.identity >= 3), key=lambda c: -c.identity)[:1]
        for seed in seeds:
            rows = await self._call(tmdb.similar_titles, seed.media, seed.tmdb_id)
            self.add(seed.media, rows, f"similar:{seed.ref}")
            self.items.pop(seed.ref, None)

    async def enrich_unknowns(self, limit: int = 6) -> None:
        """Resolve sparse search rows only when hard requirements need evidence."""
        if not (self.intent.date and self.intent.date.required or self.intent.genres):
            return
        count = 0
        for candidate in list(self.items.values()):
            if count >= limit or self.calls >= MAX_REQUESTS:
                break
            if candidate.date and candidate.genre_ids:
                continue
            detail = await self._call(tmdb.candidate_details, candidate.media, candidate.tmdb_id)
            count += 1
            if not detail:
                continue
            updated = Candidate.from_tmdb(candidate.media, detail, "detail")
            if updated:
                if not updated.genre_ids and isinstance(detail.get("genres"), list):
                    updated.genre_ids = tuple(int(g["id"]) for g in detail["genres"] if isinstance(g, dict) and g.get("id"))
                candidate.merge(updated)

    async def retrieve(self) -> list[Candidate]:
        if self.intent.kind == "browse":
            await self.browse()
        elif self.intent.kind == "similar_titles":
            await self.similar()
        else:
            await self.titles()
        await self.enrich_unknowns()
        return list(self.items.values())
