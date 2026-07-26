"""Nightly catalog build.

Country rows: TMDB discover per (country, genre), popularity-sorted so each
day's ordering follows what's currently hot; "New" sorts by air date instead.

Actor rows: ranked by what Tonya and Toya actually watch — every Asian drama
in their Trakt history votes for its top-billed cast, weighted by how recently
it was watched — blended with the casts of currently-trending dramas so the
lineup moves with the times. Pinned actors (Bai Lu) always come first.

Everything lands in /data/catalogs.json; serving never calls an API.
"""

import asyncio
import datetime as dt
import hashlib
import json
import logging
import random
import time
from pathlib import Path

from app.recs.dramas import config, tmdb, trakt

logger = logging.getLogger("asian-dramas")

STATE_PATH = Path(config.DATA_DIR) / "catalogs.json"

# Scripted live-action dramas only: drop animation (16 — otherwise the
# Japanese row is wall-to-wall anime, some of it adult), talk (10767),
# reality (10764), news (10763), documentary (99).
NON_DRAMA = "16,10767,10764,10763,99"
NON_DRAMA_IDS = {16, 10767, 10764, 10763, 99}

# Genre dropdown; "Trending" is also the default row order. Combined tabs
# (Historical Romance, Rom-Com, …) are just multiple TMDB filters ANDed together.
GENRES = ["Trending", "New", "Romance", "Comedy", "Rom-Com", "Fantasy",
          "Romance Fantasy", "Action", "Mystery", "Crime",
          "Historical", "Historical Romance", "Historical Fantasy",
          "Historical Comedy"]

# TMDB's TV "Romance" genre (10749) barely tags Asian dramas (CN: 9 titles!),
# so romance uses the "romance" keyword instead — far better coverage (KR 907,
# CN 2222). Comedy/Action/Mystery/Crime/Fantasy are proper TV genres that tag
# fine, so they stay genre-based.
_ROMANCE_KW = "9840"

# Each genre → discover filters merged onto the base params. `_new` swaps to
# air-date sort; `_hist` and `_romance` add their keyword groups (multiple groups
# are comma-joined, which TMDB treats as AND — so Historical Romance = the
# historical-keyword OR-group AND the romance keyword). `with_genres` with a
# comma is ANDed by TMDB.
_GENRE_SPEC: dict[str, dict] = {
    "Trending": {},
    "New": {"_new": True},
    "Romance": {"_romance": True},
    "Comedy": {"with_genres": "35"},
    "Rom-Com": {"_romance": True, "with_genres": "35"},
    "Fantasy": {"with_genres": "10765"},
    "Romance Fantasy": {"_romance": True, "with_genres": "10765"},
    "Action": {"with_genres": "10759"},
    "Mystery": {"with_genres": "9648"},
    "Crime": {"with_genres": "80"},
    "Historical": {"_hist": True},
    "Historical Romance": {"_hist": True, "_romance": True},
    "Historical Fantasy": {"_hist": True, "with_genres": "10765"},
    "Historical Comedy": {"_hist": True, "with_genres": "35"},
}

# Live state served by main.py: {"built_at", "catalog_defs", "rows"}
state: dict = {"built_at": 0, "catalog_defs": [], "rows": {}}

ROW_ORDER_VERSION = 4
LEAD_PICK_WINDOW = 32
TOP_SEEN_WINDOW = 14
ROW_JITTER = 16.0


def _seed_int(*parts: str) -> int:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _seed_date() -> str:
    return dt.date.today().isoformat()


def _meta_key(meta: dict) -> str:
    name = (meta.get("name") or "").strip().casefold()
    return name or meta.get("id") or meta.get("type", "")


def _diversify_row(
        group_id: str,
        row_id: str,
        row: list[dict],
        seed_date: str,
        first_seen: dict[str, int],
        top_seen: dict[str, int]) -> list[dict]:
    """Keep TMDB relevance, but avoid identical openers across related rows."""
    if len(row) < 2:
        if row:
            key = _meta_key(row[0])
            first_seen[key] = first_seen.get(key, 0) + 1
            top_seen[key] = top_seen.get(key, 0) + 1
        return row

    rng = random.Random(_seed_int(
        str(ROW_ORDER_VERSION), seed_date, group_id, row_id))

    scores: dict[str, float] = {}
    for idx, meta in enumerate(row):
        key = _meta_key(meta)
        scores[key] = (
            idx
            + rng.random() * ROW_JITTER
            + first_seen.get(key, 0) * 28.0
            + top_seen.get(key, 0) * 8.0
        )

    lead_pool = row[:min(LEAD_PICK_WINDOW, len(row))]
    lead_candidates = [m for m in row if not first_seen.get(_meta_key(m))]
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


def _diversify_country_rows(rows: dict[str, list[dict]], seed_date: str) -> None:
    for cc, _ in config.COUNTRIES:
        cid = f"ad_cc_{cc.lower()}"
        first_seen: dict[str, int] = {}
        top_seen: dict[str, int] = {}
        # Sparse rows have fewer valid lead choices, so reserve their best
        # options before broad rows like Trending or Comedy claim them.
        genres = sorted(
            GENRES,
            key=lambda g: (len(rows.get(f"{cid}|{g}", [])) or 9999,
                           GENRES.index(g)),
        )
        for genre in genres:
            key = f"{cid}|{genre}"
            row = rows.get(key)
            if not row:
                continue
            rows[key] = _diversify_row(
                cid, genre, row, seed_date, first_seen, top_seen)
        if rows.get(f"{cid}|Trending"):
            rows[cid] = rows[f"{cid}|Trending"]


def _diversify_actor_rows(rows: dict[str, list[dict]], seed_date: str) -> None:
    first_seen: dict[str, int] = {}
    top_seen: dict[str, int] = {}
    for actor in state.get("actors", []):
        cid = f"ad_p_{actor['id']}"
        row = rows.get(cid)
        if not row:
            continue
        rows[cid] = _diversify_row(
            "actors", cid, row, seed_date, first_seen, top_seen)


def _row_order_meta(seed_date: str | None = None) -> dict:
    return {"version": ROW_ORDER_VERSION, "seed_date": seed_date or _seed_date()}


def _diversify_rows(rows: dict[str, list[dict]], seed_date: str) -> None:
    _diversify_country_rows(rows, seed_date)
    _diversify_actor_rows(rows, seed_date)


def _ensure_row_order() -> bool:
    rows = state.get("rows") or {}
    if not rows:
        return False
    meta = _row_order_meta()
    if state.get("row_order") == meta:
        return False
    _diversify_rows(rows, meta["seed_date"])
    state["row_order"] = meta
    return True


def load_state() -> None:
    global state
    try:
        state = json.loads(STATE_PATH.read_text())
        if _ensure_row_order():
            STATE_PATH.write_text(json.dumps(state))
        age_h = (time.time() - state.get("built_at", 0)) / 3600
        logger.info(f"loaded catalogs.json: {len(state['rows'])} rows, {age_h:.1f}h old")
    except (FileNotFoundError, json.JSONDecodeError):
        pass


# TMDB has no "History" genre for TV, so historical dramas are matched by an OR
# of period/historical keywords. The old 3-keyword set badly under-counted (only
# ~45 Korean matches); this broader curated set captures sageuk, wuxia/xianxia,
# and general period tags — ~196 Korean, ~1300 Chinese.
_HISTORICAL_KW_QUERIES = [
    "costume drama", "historical drama", "period drama", "wuxia", "xianxia",
    "sageuk", "joseon dynasty", "ancient china", "historical", "feudal japan",
    "samurai",
]


async def _historical_keywords() -> str | None:
    ids: list[int] = []
    for q in _HISTORICAL_KW_QUERIES:
        ids += await tmdb.search_keywords(q, limit=1)
    return "|".join(map(str, dict.fromkeys(ids))) or None


def _discover_params(cc: str, genre: str, hist_kw: str | None) -> dict | None:
    params = {
        "with_origin_country": cc,
        "without_genres": NON_DRAMA,
        "sort_by": "popularity.desc",
        "include_null_first_air_dates": "false",
    }
    spec = dict(_GENRE_SPEC.get(genre, {}))
    kw_groups: list[str] = []   # each an OR-group; comma-joined = AND across them
    if spec.pop("_new", False):
        params["sort_by"] = "first_air_date.desc"
        params["first_air_date.lte"] = dt.date.today().isoformat()
        params["vote_count.gte"] = 2
    if spec.pop("_hist", False):
        if not hist_kw:
            return None
        kw_groups.append(hist_kw)
    if spec.pop("_romance", False):
        kw_groups.append(_ROMANCE_KW)
    if kw_groups:
        params["with_keywords"] = ",".join(kw_groups)
    params.update(spec)
    return params


async def _country_row(cc: str, genre: str, hist_kw: str | None) -> list[dict]:
    params = _discover_params(cc, genre, hist_kw)
    if params is None:
        return []
    results = await tmdb.discover_tv(params, pages=config.DISCOVER_PAGES)
    return await tmdb.resolve_many([r["id"] for r in results], config.ROW_ITEMS)


# ── actor ranking ────────────────────────────────────────────────────────

def _recency_weight(last_watched_at: str | None) -> float:
    if not last_watched_at:
        return 0.3
    try:
        watched = dt.datetime.fromisoformat(last_watched_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.3
    days = max(0.0, (dt.datetime.now(dt.timezone.utc) - watched).days)
    return 0.5 ** (days / 120)  # half-life ~4 months


def _is_asian_show(show: dict) -> bool:
    country = (show.get("country") or "").upper()
    lang = (show.get("language") or "").lower()
    return country in config.ASIAN_CC or lang in config.ASIAN_LANGS


async def _watched_asian_shows() -> dict[int, float]:
    """{tmdb show id: recency weight (summed across users)}"""
    weights: dict[int, float] = {}
    by_viewer = await trakt.watched_shows_by_viewer()
    for name, items in by_viewer.items():
        n = 0
        for it in items:
            show = it.get("show") or {}
            tmdb_id = (show.get("ids") or {}).get("tmdb")
            if not tmdb_id or not _is_asian_show(show):
                continue
            weights[tmdb_id] = weights.get(tmdb_id, 0) \
                + _recency_weight(it.get("last_watched_at"))
            n += 1
        logger.info(f"{name}: {n} watched asian dramas")
    return weights


async def _rank_actors() -> list[dict]:
    """Ordered [{'id', 'name'}]: pinned first, then watch/trending score."""
    scores: dict[int, float] = {}
    names: dict[int, str] = {}
    profiles: dict[int, str | None] = {}

    def vote(cast: list[dict], weight: float, top: int) -> None:
        for i, person in enumerate(cast[:top]):
            pid = person.get("id")
            if not pid:
                continue
            scores[pid] = scores.get(pid, 0) + weight * (top - i) / top
            names[pid] = person.get("name") or ""
            profiles.setdefault(pid, person.get("profile_path"))

    # Watch-history votes (the primary signal).
    watched = await _watched_asian_shows()
    recent = sorted(watched.items(), key=lambda kv: -kv[1])[:60]
    credits = await asyncio.gather(
        *(tmdb.aggregate_credits(sid) for sid, _ in recent))
    for (sid, weight), cast in zip(recent, credits):
        vote(cast, weight, top=8)

    # Trending votes (keeps rows alive when watch data is thin, and lets the
    # lineup follow what's hot).
    for cc, _ in config.COUNTRIES:
        shows = await tmdb.discover_tv(
            {"with_origin_country": cc, "without_genres": NON_DRAMA,
             "sort_by": "popularity.desc"}, pages=1)
        casts = await asyncio.gather(
            *(tmdb.aggregate_credits(s["id"]) for s in shows[:8]))
        for cast in casts:
            vote(cast, weight=0.15, top=5)

    pinned: list[dict] = []
    pinned_ids: set[int] = set()
    for name in config.PINNED_ACTORS:
        person = await tmdb.search_person(name)
        if person:
            pinned.append({"id": person["id"], "name": person.get("name") or name,
                           "profile": person.get("profile_path")})
            pinned_ids.add(person["id"])
        else:
            logger.warning(f"pinned actor not found on TMDB: {name}")

    ranked = [{"id": pid, "name": names[pid], "profile": profiles.get(pid)}
              for pid, _ in sorted(scores.items(), key=lambda kv: -kv[1])
              if pid not in pinned_ids and names.get(pid)]
    return pinned + ranked[:config.ACTOR_ROWS]


def _credit_sort_key(c: dict) -> str:
    return c.get("first_air_date") or "0000"


async def _actor_row(person_id: int) -> list[dict]:
    credits = await tmdb.person_tv_credits(person_id)
    keep = []
    for c in credits:
        if NON_DRAMA_IDS & set(c.get("genre_ids") or []):
            continue
        origin = {o.upper() for o in (c.get("origin_country") or [])}
        if not (origin & config.ASIAN_CC
                or (c.get("original_language") or "").lower() in config.ASIAN_LANGS):
            continue
        if (c.get("episode_count") or 99) < 2:  # skip one-episode guest spots
            continue
        keep.append(c)
    keep.sort(key=_credit_sort_key, reverse=True)  # newest work first
    return await tmdb.resolve_many([c["id"] for c in keep], config.ROW_ITEMS)


# ── full build ───────────────────────────────────────────────────────────

_build_lock = asyncio.Lock()


async def build() -> None:
    async with _build_lock:
        started = time.time()
        logger.info("build starting")
        tmdb.load_cache()
        defs: list[dict] = []
        rows: dict[str, list[dict]] = {}

        actors = await _rank_actors()
        # Imported Nuvio collections reference actor catalogs by id and only
        # resolve while the id is still declared in the manifest, so carry
        # actors from earlier builds even after they fall out of the ranking
        # (capped; oldest hangers-on drop first).
        current = {a["id"] for a in actors}
        actors += [a for a in state.get("actors", []) if a["id"] not in current]
        actors = actors[:25]
        kept_actors: list[dict] = []
        for a in actors:
            cid = f"ad_p_{a['id']}"
            row = await _actor_row(a["id"])
            if not row:
                continue
            kept_actors.append(a)
            # isRequired hides the catalog from Nuvio's home page (it filters
            # out catalogs with required extras) while collections can still
            # fetch it — these rows exist to feed the imported collection.
            defs.append({"type": "series", "id": cid, "name": a["name"],
                         "extra": [{"name": "genre", "options": ["All"],
                                    "isRequired": True}]})
            rows[cid] = row

        hist_kw = await _historical_keywords()
        for cc, adj in config.COUNTRIES:
            cid = f"ad_cc_{cc.lower()}"
            results = await asyncio.gather(
                *(_country_row(cc, g, hist_kw) for g in GENRES))
            for genre, row in zip(GENRES, results):
                rows[f"{cid}|{genre}"] = row
            rows[cid] = rows[f"{cid}|Trending"]
            defs.append({
                "type": "series", "id": cid, "name": f"{adj} Dramas",
                "genres": GENRES,
                "extra": [{"name": "genre", "options": GENRES,
                           "isRequired": True}],
            })

        row_order = _row_order_meta()
        state.update(actors=kept_actors)
        _diversify_rows(rows, row_order["seed_date"])
        state.update(built_at=int(time.time()), catalog_defs=defs, rows=rows,
                     row_order=row_order)
        STATE_PATH.write_text(json.dumps(state))
        tmdb.save_cache()
        logger.info(f"build done in {time.time() - started:.0f}s: "
                    f"{len(kept_actors)} actor rows, {len(defs)} catalogs")


# ── scheduler ────────────────────────────────────────────────────────────

def _seconds_until_next_run() -> float:
    now = dt.datetime.now()
    nxt = now.replace(hour=config.REFRESH_HOUR, minute=config.REFRESH_MINUTE,
                      second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


async def run() -> None:
    load_state()
    if time.time() - state.get("built_at", 0) > config.STALE_HOURS * 3600:
        try:
            await build()
        except Exception:
            logger.exception("startup build failed")
    while True:
        await asyncio.sleep(_seconds_until_next_run())
        try:
            await build()
        except Exception:
            logger.exception("nightly build failed")
