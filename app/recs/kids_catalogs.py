"""Public kids streaming catalog pack for Nuvio Collections.

This is intentionally file-backed: the imported Nuvio collection stays stable,
while the catalog rows behind it are rebuilt daily from TMDB watch-provider
availability and certification data. Serving never calls TMDB.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import random
import time
from pathlib import Path

from app.recs import config, db, preseed, tmdb
from app.recs import history as local_history
from app.recs.kids import effective_kid_age
from app.recs.profile import build_profile

logger = logging.getLogger("nuvio-recs")

STATE_PATH = config.CATALOGS_DIR / "kids-streaming-catalogs.json"
COLLECTION_PATH = config.CATALOGS_DIR / "nuvio-kids-streaming-collection.json"

# TMDB US watch-provider ids, verified from /watch/providers on 2026-07-08.
# Pipe means OR in TMDB discover. We use subscription/ad tiers, not rent/buy.
PROVIDERS = [
    {"id": "netflix", "title": "Netflix", "tmdb": "8|175|1796",
     "cover": "netflix-kids.png"},
    {"id": "prime", "title": "Prime Video", "tmdb": "9|2100",
     "cover": "prime-video-kids.png"},
    {"id": "disney", "title": "Disney+", "tmdb": "337",
     "cover": "disney-plus-kids.png"},
    {"id": "paramount", "title": "Paramount+", "tmdb": "2303|2616",
     "cover": "paramount-plus-kids.png"},
    {"id": "apple", "title": "Apple TV+", "tmdb": "350",
     "cover": "apple-tv-plus-kids.png"},
    {"id": "max", "title": "HBO Max", "tmdb": "1899",
     "cover": "hbo-max-kids.png"},
]

MOVIE_EXCLUDE = "27,53,80,10752"          # horror, thriller, crime, war
TV_EXCLUDE = "80,10763,10764,10767,10768" # crime, news, reality, talk, war

state: dict = {"built_at": 0, "catalog_defs": [], "rows": {}}

ROW_ORDER_VERSION = 3
LEAD_PICK_WINDOW = 24
TOP_SEEN_WINDOW = 12
ROW_JITTER = 14.0

_META_GENRE_SLUGS = {
    "action": {"action"},
    "action & adventure": {"action", "adventure"},
    "adventure": {"adventure"},
    "animation": {"animation"},
    "comedy": {"comedy"},
    "crime": {"crime"},
    "documentary": {"documentary"},
    "drama": {"drama"},
    "family": {"family"},
    "fantasy": {"fantasy"},
    "history": {"history"},
    "horror": {"horror"},
    "kids": {"kids"},
    "music": {"music"},
    "mystery": {"mystery"},
    "reality": {"reality"},
    "romance": {"romance"},
    "sci-fi & fantasy": {"science-fiction", "fantasy"},
    "science fiction": {"science-fiction"},
    "sci-fi": {"science-fiction"},
    "thriller": {"thriller", "suspense"},
    "war": {"war"},
    "war & politics": {"war"},
    "western": {"western"},
}

_TITLE_STOPWORDS = {
    "and", "for", "from", "into", "movie", "presents", "show", "the", "with",
}


def _recent(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _row_specs() -> list[dict]:
    """Rows shown inside every provider folder."""
    return [
        {
            "id": "movies", "title": "Movies", "media": "movie",
            "type": "movie", "max_age": 12,
            "params": {"with_genres": "16|10751|14|10402",
                       "without_genres": MOVIE_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 10},
        },
        {
            "id": "new-movies", "title": "New Movies", "media": "movie",
            "type": "movie", "max_age": 12,
            "params": {"with_genres": "16|10751|14|10402",
                       "without_genres": MOVIE_EXCLUDE,
                       "primary_release_date.gte": _recent(540),
                       "sort_by": "popularity.desc", "vote_count.gte": 5},
        },
        {
            "id": "animated-movies", "title": "Animated Movies",
            "media": "movie", "type": "movie", "max_age": 15,
            "params": {"with_genres": "16", "without_genres": MOVIE_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 10},
        },
        {
            "id": "adventure-movies", "title": "Family Adventure",
            "media": "movie", "type": "movie", "max_age": 12,
            "params": {"with_genres": "10751,12",
                       "without_genres": MOVIE_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 10},
        },
        {
            "id": "teen-movies", "title": "Teen Movies",
            "media": "movie", "type": "movie", "max_age": 15,
            "params": {"with_genres": "12|14|878|28|35|10751",
                       "without_genres": MOVIE_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 20},
        },
        {
            "id": "shows", "title": "Shows", "media": "tv",
            "type": "series", "max_age": 12,
            "params": {"with_genres": "16|10751|10762|35",
                       "without_genres": TV_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 5},
        },
        {
            "id": "preschool", "title": "Preschool & Little Kids",
            "media": "tv", "type": "series", "max_age": 6,
            "params": {"with_genres": "10762|16|10751",
                       "without_genres": TV_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 2},
        },
        {
            "id": "animated-shows", "title": "Animated Shows",
            "media": "tv", "type": "series", "max_age": 15,
            "params": {"with_genres": "16", "without_genres": TV_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 5},
        },
        {
            "id": "action-shows", "title": "Action Shows",
            "media": "tv", "type": "series", "max_age": 15,
            "params": {"with_genres": "10759", "without_genres": TV_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 5},
        },
        {
            "id": "comedy-shows", "title": "Comedy Shows",
            "media": "tv", "type": "series", "max_age": 15,
            "params": {"with_genres": "35|10751|10762",
                       "without_genres": TV_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 5},
        },
        {
            "id": "teen-shows", "title": "Teen Friendly",
            "media": "tv", "type": "series", "max_age": 15,
            "params": {"with_genres": "35|10759|10765|10751",
                       "without_genres": TV_EXCLUDE,
                       "sort_by": "popularity.desc", "vote_count.gte": 10},
        },
    ]


def _seed_int(*parts: str) -> int:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _seed_date() -> str:
    return dt.date.today().isoformat()


def _meta_key(meta: dict) -> str:
    return meta.get("id") or f"{meta.get('type', '')}:{meta.get('name', '')}"


def _title_key(title: str | None) -> str:
    return " ".join((title or "").casefold().replace("&", "and").split())


def _title_terms(title: str | None) -> set[str]:
    raw = _title_key(title).replace(":", " ").replace("-", " ")
    return {w for w in raw.split() if len(w) >= 4 and w not in _TITLE_STOPWORDS}


def _meta_genre_slugs(meta: dict) -> set[str]:
    out: set[str] = set()
    for genre in meta.get("genres") or []:
        out.update(_META_GENRE_SLUGS.get(str(genre).casefold(), set()))
    return out


def _profile_kind(stremio_type: str) -> str:
    return "movie" if stremio_type == "movie" else "show"


async def _taste_user(target_name: str) -> dict | None:
    target = target_name.strip().casefold()
    users = await db.all_users()
    for user in users:
        if (user.get("name") or "").strip().casefold() == target:
            return user
    for user in users:
        if (user.get("trakt_username") or "").strip().casefold() == target:
            return user
    return None


async def _ashton_user() -> dict | None:
    return await _taste_user(config.KIDS_TASTE_USER)


def _taste_hash(model: dict) -> str:
    payload = json.dumps(model, sort_keys=True, default=list)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _taste_model(profile: dict, user: dict, source: str,
                 activity: str = "") -> dict:
    genres: dict[str, dict[str, float]] = {"movie": {}, "show": {}}
    for kind in ("movie", "show"):
        table = dict(profile.get("genres", {}).get(kind, []))
        top = max(table.values(), default=0.0)
        if top:
            genres[kind] = {slug: weight / top for slug, weight in table.items()}

    seed_titles = {
        _title_key(s.get("title")) for s in profile.get("seeds", [])
        if s.get("title")
    }
    loved_titles = {
        _title_key(s.get("title")) for s in profile.get("loved", [])
        if s.get("title")
    }
    title_terms: set[str] = set()
    seed_imdb: set[str] = set()
    loved_imdb: set[str] = set()
    for seed in profile.get("seeds", []):
        title_terms.update(_title_terms(seed.get("title")))
        if seed.get("imdb"):
            seed_imdb.add(seed["imdb"])
    for seed in profile.get("loved", []):
        if seed.get("imdb"):
            loved_imdb.add(seed["imdb"])

    model = {
        "activity": activity,
        "age": effective_kid_age(user),
        "genres": genres,
        "loved_imdb": sorted(loved_imdb),
        "loved_titles": sorted(loved_titles),
        "seed_imdb": sorted(seed_imdb),
        "seed_titles": sorted(seed_titles),
        "source": source,
        "title_terms": sorted(title_terms),
        "user": user.get("name") or config.KIDS_TASTE_USER,
        "watched_imdb": sorted(profile.get("watched_imdb", set())),
    }
    model["hash"] = _taste_hash(model)
    return model


async def taste_model_for_user(target_name: str,
                               log_prefix: str = "streaming taste") -> dict | None:
    user = await _taste_user(target_name)
    if not user:
        logger.warning("%s: user %r not found", log_prefix, target_name)
        return None

    wm: list[dict] = []
    ws: list[dict] = []
    rm: list[dict] = []
    rs: list[dict] = []
    activity = user.get("last_activity") or ""
    source = "plays"
    try:
        from app.recs.profile_streaming import private_namespace_for_user
        viewer_key = private_namespace_for_user(user)
        wm, ws, rm, rs = await local_history.watched_lists(viewer_key)
        activity = str(await local_history.last_play_at(viewer_key))
    except Exception as exc:
        logger.warning("%s: local history unavailable for %s: %s",
                       log_prefix, user.get("name"), exc)
        source = "preseed"

    profile = build_profile(wm, ws, rm, rs)
    real_watches = len(wm) + len(ws)
    if real_watches < preseed.PRESEED_MAX_HISTORY:
        entries = preseed.load_for(user.get("name"))
        try:
            taste = await preseed.taste_seeds(entries["taste"])
            history = await preseed.history_seeds(entries["history"])
            if taste or history:
                preseed.apply_to_profile(profile, taste + history)
                source = f"{source}+preseed" if real_watches else "preseed"
        except Exception as exc:
            logger.warning("%s: preseed failed for %s: %s",
                           log_prefix, user.get("name"), exc)

    model = _taste_model(profile, user, source, activity)
    model["real_watches"] = real_watches
    logger.info("%s: %s profile from %s (%d Trakt watches)",
                log_prefix, model["user"], model["source"], real_watches)
    return model


async def _ashton_taste_model() -> dict | None:
    return await taste_model_for_user(config.KIDS_TASTE_USER, "kids taste")


def _taste_score(meta: dict, stremio_type: str, taste: dict | None) -> float:
    if not taste:
        return 0.0

    kind = _profile_kind(stremio_type)
    genres = taste.get("genres", {}).get(kind, {})
    meta_slugs = _meta_genre_slugs(meta)
    score = sum(genres.get(slug, 0.0) for slug in meta_slugs) * 14.0

    if (taste.get("age") or 99) <= 6 and meta_slugs & {"animation", "family", "kids"}:
        score += 5.0

    imdb_id = meta.get("id")
    title = _title_key(meta.get("name"))
    if imdb_id in set(taste.get("loved_imdb") or []):
        score += 14.0
    elif imdb_id in set(taste.get("seed_imdb") or []):
        score += 10.0
    elif imdb_id in set(taste.get("watched_imdb") or []):
        score += 5.0

    if title in set(taste.get("loved_titles") or []):
        score += 12.0
    elif title in set(taste.get("seed_titles") or []):
        score += 8.0

    shared_terms = _title_terms(meta.get("name")) & set(taste.get("title_terms") or [])
    if shared_terms:
        score += min(6.0, 1.5 * len(shared_terms))

    return score


def _rank_row_for_taste(row: list[dict], spec: dict,
                        taste: dict | None, seed_date: str) -> list[dict]:
    if not taste or len(row) < 2:
        return row
    rng = random.Random(_seed_int(
        str(ROW_ORDER_VERSION), seed_date, "taste", spec["id"]))
    ranked = sorted(
        enumerate(row),
        key=lambda item: (
            -_taste_score(item[1], spec["type"], taste),
            item[0] + rng.random() * 0.05,
        ),
    )
    return [meta for _, meta in ranked]


def _apply_taste_ranking(rows: dict[str, list[dict]], specs: list[dict],
                         taste: dict | None, seed_date: str) -> None:
    if not taste:
        return
    for provider in PROVIDERS:
        for spec in specs:
            cid = _catalog_id(provider, spec)
            ranked = _rank_row_for_taste(rows.get(cid, []), spec, taste, seed_date)
            rows[cid] = ranked if ranked else rows.get(cid, [])


def _diversify_row(
        provider: dict,
        spec: dict,
        row: list[dict],
        seed_date: str,
        first_seen: dict[str, int],
        top_seen: dict[str, int]) -> list[dict]:
    """Keep relevance from TMDB order, but avoid identical row openers."""
    if len(row) < 2:
        return row

    rng = random.Random(_seed_int(
        str(ROW_ORDER_VERSION), seed_date, provider["id"], spec["id"]))

    scores: dict[str, float] = {}
    for idx, meta in enumerate(row):
        key = _meta_key(meta)
        scores[key] = (
            idx
            + rng.random() * ROW_JITTER
            + first_seen.get(key, 0) * 22.0
            + top_seen.get(key, 0) * 7.0
        )

    lead_pool = row[:min(LEAD_PICK_WINDOW, len(row))]
    lead_candidates = [m for m in lead_pool if not first_seen.get(_meta_key(m))]
    if not lead_candidates:
        lead_candidates = [m for m in lead_pool if not top_seen.get(_meta_key(m))]
    if not lead_candidates:
        lead_candidates = lead_pool

    lead = min(lead_candidates, key=lambda m: scores[_meta_key(m)])
    lead_key = _meta_key(lead)
    rest = [m for m in row if _meta_key(m) != lead_key]
    rest.sort(key=lambda m: scores[_meta_key(m)])
    ordered = [lead, *rest]

    first_seen[lead_key] = first_seen.get(lead_key, 0) + 1
    for meta in ordered[:TOP_SEEN_WINDOW]:
        key = _meta_key(meta)
        top_seen[key] = top_seen.get(key, 0) + 1

    return ordered


def _diversify_rows(rows: dict[str, list[dict]],
                    specs: list[dict] | None = None,
                    seed_date: str | None = None) -> None:
    specs = specs or _row_specs()
    seed_date = seed_date or _seed_date()
    for provider in PROVIDERS:
        first_seen: dict[str, dict[str, int]] = {"movie": {}, "series": {}}
        top_seen: dict[str, dict[str, int]] = {"movie": {}, "series": {}}
        for spec in specs:
            cid = _catalog_id(provider, spec)
            row = rows.get(cid)
            if not row:
                continue
            media_type = spec["type"]
            rows[cid] = _diversify_row(
                provider, spec, row, seed_date,
                first_seen.setdefault(media_type, {}),
                top_seen.setdefault(media_type, {}),
            )


def _row_order_meta(taste: dict | None = None,
                    seed_date: str | None = None) -> dict:
    return {
        "version": ROW_ORDER_VERSION,
        "seed_date": seed_date or _seed_date(),
        "taste_user": config.KIDS_TASTE_USER,
        "taste_hash": (taste or {}).get("hash", "none"),
    }


def _ensure_row_order(taste: dict | None = None) -> bool:
    rows = state.get("rows") or {}
    if not rows:
        return False
    meta = _row_order_meta(taste)
    if state.get("row_order") == meta:
        return False
    specs = _row_specs()
    _apply_taste_ranking(rows, specs, taste, meta["seed_date"])
    _diversify_rows(rows, specs=specs, seed_date=meta["seed_date"])
    state["row_order"] = meta
    if taste:
        state["taste_profile"] = {
            "user": taste.get("user"),
            "source": taste.get("source"),
            "real_watches": taste.get("real_watches"),
            "hash": taste.get("hash"),
            "activity": taste.get("activity"),
        }
    return True


def _catalog_id(provider: dict, spec: dict) -> str:
    return f"ks_{provider['id']}_{spec['id']}"


def _ensure_dirs() -> None:
    config.CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
    config.CATALOG_ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> None:
    global state
    try:
        state = json.loads(STATE_PATH.read_text())
        age_h = (time.time() - state.get("built_at", 0)) / 3600
        logger.info("loaded kids catalogs: %d rows, %.1fh old",
                    len(state.get("rows", {})), age_h)
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def catalog_defs() -> list[dict]:
    defs = []
    for provider in PROVIDERS:
        for spec in _row_specs():
            defs.append({
                "type": spec["type"],
                "id": _catalog_id(provider, spec),
                "name": spec["title"],
                # Keeps backing rows out of the normal home catalog list while
                # still letting imported collections resolve them directly.
                "extra": [{"name": "genre", "options": ["All"],
                           "isRequired": True}],
            })
    return defs


async def _discover_row(provider: dict, spec: dict) -> list[dict]:
    media = spec["media"]
    params = {
        **spec["params"],
        "watch_region": "US",
        "with_watch_providers": provider["tmdb"],
        "with_watch_monetization_types": "flatrate|ads",
        "include_adult": "false",
    }
    if media == "movie":
        params["certification_country"] = "US"
        params["certification.lte"] = "PG-13" if spec["max_age"] >= 13 else "PG"

    results: list[dict] = []
    for page in range(1, config.KIDS_DISCOVER_PAGES + 1):
        try:
            page_items = await tmdb.discover(media, {**params, "page": page})
        except Exception as e:
            logger.warning("kids discover failed provider=%s row=%s page=%s: %s",
                           provider["id"], spec["id"], page, e)
            break
        if not page_items:
            break
        results.extend(page_items)
        if len(page_items) < 20:
            break

    return await tmdb.resolve_many(media, [r["id"] for r in results],
                                   set(), set(), config.KIDS_ROW_ITEMS,
                                   spec["max_age"])


def _source(catalog_id: str, spec: dict) -> dict:
    return {
        "provider": "addon",
        "addonId": config.KIDS_ADDON_ID,
        "type": spec["type"],
        "catalogId": catalog_id,
    }


def collection_export() -> list[dict]:
    rows = state.get("rows", {})
    folders = []
    for provider in PROVIDERS:
        sources = []
        for spec in _row_specs():
            cid = _catalog_id(provider, spec)
            # Keep the import clean on first build, but once imported the addon
            # still declares all catalog IDs so future empty rows do not break.
            if rows.get(cid):
                sources.append(_source(cid, spec))
        if not sources:
            continue
        folder = {
            "id": f"ks_f_{provider['id']}",
            "title": provider["title"],
            "coverImageUrl": f"{config.HOST_NAME}/kids/assets/{provider['cover']}",
            "focusGifUrl": None,
            "coverEmoji": None,
            "tileShape": "LANDSCAPE",
            "hideTitle": True,
            "catalogSources": sources,
            "_coverMode": "image",
        }
        folders.append(folder)
    return [{
        "id": "kids-streaming-services",
        "title": "Kids Streaming",
        "backdropImageUrl": None,
        "pinToTop": True,
        "focusGlowEnabled": True,
        "viewMode": "ROWS",
        "showAllTab": False,
        "folders": folders,
    }]


def _write_collection() -> None:
    COLLECTION_PATH.write_text(json.dumps(collection_export(), indent=2))


_build_lock = asyncio.Lock()


async def build() -> None:
    global state
    async with _build_lock:
        _ensure_dirs()
        started = time.time()
        logger.info("kids catalog build starting")
        rows: dict[str, list[dict]] = {}
        specs = _row_specs()
        for provider in PROVIDERS:
            results = await asyncio.gather(
                *(_discover_row(provider, spec) for spec in specs))
            for spec, row in zip(specs, results):
                cid = _catalog_id(provider, spec)
                rows[cid] = row
                logger.info("kids row %s: %d items", cid, len(row))

        taste = await _ashton_taste_model()
        row_order = _row_order_meta(taste)
        _apply_taste_ranking(rows, specs, taste, row_order["seed_date"])
        _diversify_rows(rows, specs, row_order["seed_date"])
        state = {
            "built_at": int(time.time()),
            "region": "US",
            "catalog_defs": catalog_defs(),
            "rows": rows,
            "providers": PROVIDERS,
            "row_order": row_order,
            "taste_profile": {
                "user": (taste or {}).get("user"),
                "source": (taste or {}).get("source"),
                "real_watches": (taste or {}).get("real_watches"),
                "hash": (taste or {}).get("hash"),
                "activity": (taste or {}).get("activity"),
            } if taste else None,
        }
        STATE_PATH.write_text(json.dumps(state))
        _write_collection()
        logger.info("kids catalog build done in %.0fs: %d rows",
                    time.time() - started, len(rows))


def get_metas(catalog_id: str) -> list[dict] | None:
    return state.get("rows", {}).get(catalog_id)


def _seconds_until_next_run() -> float:
    now = dt.datetime.now()
    nxt = now.replace(hour=config.KIDS_REFRESH_HOUR,
                      minute=config.KIDS_REFRESH_MINUTE,
                      second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


async def run() -> None:
    _ensure_dirs()
    load_state()
    taste = await _ashton_taste_model()
    if state.get("rows") and _ensure_row_order(taste):
        STATE_PATH.write_text(json.dumps(state))
    if state.get("rows"):
        _write_collection()
    if time.time() - state.get("built_at", 0) > config.KIDS_STALE_HOURS * 3600:
        try:
            await build()
        except Exception:
            logger.exception("startup kids catalog build failed")

    while True:
        await asyncio.sleep(_seconds_until_next_run())
        try:
            await build()
        except Exception:
            logger.exception("nightly kids catalog build failed")
