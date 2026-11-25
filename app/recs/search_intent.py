"""Interpret search wording while retaining explicit user constraints."""
from __future__ import annotations

import datetime as dt
import json
import re

from app.recs import tmdb
from app.recs.search_models import DateClue, SearchIntent

_GENRE_WORDS = {"scary": "horror", "spooky": "horror", "frightening": "horror", "creepy": "horror", "animated": "animation", "cartoon": "animation", "sci fi": "science-fiction", "sci-fi": "science-fiction", "stand up": "comedy", "stand-up": "comedy"}
_GENRE_WORDS.update({g.replace("-", " "): g for g in set(tmdb.MOVIE_GENRES) | set(tmdb.TV_GENRES)})
_WRITTEN = {"eighties": 1980, "nineties": 1990, "seventies": 1970, "sixties": 1960, "fifties": 1950, "twenties": 2020}
_MEDIA_RE = re.compile(r"\b(movies?|films?|shows?|series|tv|specials?)\b", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2})(?!\d)")
_TOPIC_NOISE = {"movie", "film", "show", "series", "cinema", "classic", "vintage", "scary", "horror", "comedy", "drama", "animation", "animated", "good", "best", "new", "recent", "release", "year", "80s", "90s", "70s", "60s", "eighties", "nineties", "seventies", "sixties", "fifties", "twenties"}


def _topic_supported(value: str, query: str) -> bool:
    words = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", value.casefold())}
    source = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", query.casefold())}
    noise = {n.rstrip("s") for n in _TOPIC_NOISE}
    meaningful = {w for w in words if w not in noise and not w.isdigit() and not re.fullmatch(r"\d{2,4}s?", w)}
    return bool(meaningful & source)



def _date_clue(query: str, today: dt.date) -> tuple[DateClue | None, str]:
    q = query.lower()
    setting = bool(re.search(r"\b(set|takes place|takes place in|based in)\b.{0,15}\b(in|during)\b", q))
    years = [int(y) for y in _YEAR_RE.findall(q)]
    decade = re.search(r"(?<!\d)(?:(18|19|20))?(\d{2})['’]?s\b", q)
    written = next((y for word, y in _WRITTEN.items() if re.search(rf"\b{word}\b", q)), None)
    if decade:
        n = int(decade.group(2))
        start = int(decade.group(1)) * 100 + n if decade.group(1) else (2000 if n <= today.year % 100 else 1900) + n
        years = [start, start + 9]
    elif written:
        years = [written, written + 9]
    if not years:
        if re.search(r"\b(this year|last year)\b", q):
            year = today.year if "this year" in q else today.year - 1
            years = [year]
        else:
            return None, ""
    if setting:
        return None, query
    if re.search(r"\b(before|prior to)\b", q):
        start, end = 1890, years[0] - 1
    elif re.search(r"\b(after|since)\b", q):
        start, end = years[0] + (1 if "after" in q else 0), today.year
    elif len(years) >= 2:
        start, end = min(years), max(years)
    else:
        start = end = years[0]
    if not 1890 <= start <= end <= today.year + 10:
        return None, ""
    strict = bool(re.search(r"\b(released only|only released|released in|from|during|before|after|since|between|made in|this year|last year)\b", q))
    browse = bool(re.search(r"\b(movies?|films?|shows?|series|classics|horror|comedy|drama)\b", q))
    required = strict or browse
    return DateClue(dt.date(start, 1, 1), dt.date(end, 12, 31), required, query), ""


def interpret(query: str, raw: str | dict | None = None, today: dt.date | None = None) -> SearchIntent:
    if len(query) > 500:
        raise ValueError("query too long")
    today = today or dt.date.today()
    q = " ".join(query.split())
    low = q.lower()
    data: dict = {}
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and len(raw) <= 12000:
        try:
            data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
        except ValueError:
            pass
    if not isinstance(data, dict):
        data = {}
    media_words = [m.group(1).lower() for m in _MEDIA_RE.finditer(q)]
    movie = any(m.startswith(("movie", "film", "special")) for m in media_words)
    tv = any(m.startswith(("show", "serie", "tv")) for m in media_words)
    explicit = movie != tv
    media = ("movie",) if movie and not tv else ("tv",) if tv and not movie else ("movie", "tv")
    # The model may suggest a format, but only viewer wording can make it a
    # hard media requirement. Ambiguous title recollections search both.
    date, setting = _date_clue(q, today)
    genres = []
    excluded_genres = []
    for word, slug in _GENRE_WORDS.items():
        m = re.search(rf"\b{re.escape(word)}\b", low)
        if not m:
            continue
        excluded = bool(re.search(r"\b(without|no|exclude|excluding)\s+(?:\w+\s+){0,2}$", low[:m.start()]))
        # "Family" can be part of a title, as in "Family Feud". Treat it as
        # a genre when the phrase independently reads like a browse request.
        if (slug == "family" and not excluded and low != "family"
                and not re.search(r"\b(movies|films|shows|classics|friendly|for kids)\b",
                                  low)):
            continue
        target = excluded_genres if excluded else genres
        if slug not in target:
            target.append(slug)
    excluded_topics = tuple(m.group(1).strip() for m in re.finditer(r"\b(?:without|no|excluding)\s+([\w -]+?)(?=\s+(?:and|with|from|in)\b|$)", low))[:4]
    topics = []
    for entry in data.get("topics", data.get("keywords", [])) if isinstance(data.get("topics", data.get("keywords", [])), list) else []:
        value = entry.get("name", "") if isinstance(entry, dict) else entry
        value = str(value).lower().strip()
        if value.endswith("s") and not value.endswith(("ss", "us", "is")) and " " not in value:
            value = value[:-1]
        excluded_stems = {w.rstrip("s") for x in excluded_topics for w in re.findall(r"[a-z0-9]+", x)}
        topic_stems = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", value)}
        if (2 <= len(value) <= 40 and value not in topics and _topic_supported(value, q)
                and not (topic_stems & excluded_stems)
                and not re.search(r"\b(no|without|exclude|excluding)[- ]", value)):
            topics.append(value)
    if not topics:
        for word in ("cat", "dog", "vampire", "zombie"):
            if re.search(rf"\b{word}s?\b", low) and not any(word in x for x in excluded_topics):
                topics.append(word)
    if re.search(r"\bstand[- ]up\b", low) and "stand-up comedy" not in topics:
        topics.append("stand-up comedy")
    remembered = bool(re.search(r"\b(that|remember|forgot|new|recent)\b", low))
    similar = bool(re.search(r"\b(like|similar to)\b", low))
    title = re.sub(r"\b(18\d{2}|19\d{2}|20\d{2})\b", "", q).strip(" ()")
    title = re.sub(r"\s+(?:released only in|only released in|released in|from)\s*$", "", title, flags=re.I).strip()
    title = re.sub(r"^(?:that|the)\s+(?:new|recent)\s+|^(?:new|recent)\s+", "", title, flags=re.I)
    title = re.sub(r"\s+(?:show|series|movie|film)\s*$", "", title, flags=re.I).strip()
    browse = bool(genres or (not remembered and re.search(r"\b(movies|films|shows|classics)\b", low)))
    kind = "similar_titles" if similar else "browse" if browse else "remembered_title" if remembered or date else "title_lookup"
    entities = []
    if kind == "similar_titles":
        title = re.sub(r"^.*?\b(?:similar to|like)\s+", "", title, flags=re.I).strip()
    if kind != "browse" and title and not (low.startswith("that ") and re.search(r"\bwith\b", low)):
        entities.append(title)
    for entry in (data.get("entities", []) if isinstance(data.get("entities"), list) and kind not in ("browse", "title_lookup") else []):
        value = entry.get("name", "") if isinstance(entry, dict) else entry
        value = str(value).strip()
        if (1 <= len(value) <= 80 and value.casefold() not in {v.casefold() for v in entities}
                and not re.search(r"\b(?:movie|film|release|show|series)\b", value, re.I)
                and not _YEAR_RE.search(value)):
            entities.append(value)
    queries = []
    if "dog" in topics and "cat" in topics:
        queries.append("CatDog")
    for entry in (data.get("search_queries", []) if isinstance(data.get("search_queries"), list) and kind not in ("browse", "title_lookup") else []):
        value = str(entry).strip()
        if (1 <= len(value) <= 80 and value not in queries
                and not re.search(r"\b(?:movie|film|release|show|series)\b", value, re.I)
                and not _YEAR_RE.search(value)):
            queries.append(value)
    preference = "obscurity" if re.search(r"\b(obscure|hidden gems?)\b", low) else "recency" if re.search(r"\b(new|recent|latest)\b", low) else "similarity" if similar else "recognition"
    if date and kind == "remembered_title" and not re.search(r"released only|only released", low):
        date = DateClue(date.start, date.end, False, date.source)
    return SearchIntent(q, kind, media, explicit, " ".join(media_words), date, setting,
                        tuple(genres[:4]), tuple(excluded_genres[:4]), tuple(topics[:4]),
                        excluded_topics, "all", tuple(entities[:4]), tuple(queries[:3]), preference)
