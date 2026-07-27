"""Taste-sorted streaming service collections for specific household profiles.

Each profile gets stable catalog IDs, but the row titles and discover filters are
rebuilt from that profile's taste model. Watched titles stay in the rows.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import random
import re
import time
from itertools import combinations
from urllib.parse import quote

from app.recs import config, db, kids_catalogs, tmdb
from app.recs.kids import effective_kid_age

logger = logging.getLogger("nuvio-recs")

STATE_PATH = config.CATALOGS_DIR / "profile-streaming-catalogs.json"
COLLECTION_DIR = config.CATALOGS_DIR / "Streaming Profiles"

ROW_ORDER_VERSION = 4
PRIVATE_CATALOG_PREFIX = "dp_streaming_"
PRIVATE_COLLECTION_VERSION = "v2"
PRIVATE_MANIFEST_VERSION = "1.5.0"

NORMAL_COVERS = {
    "netflix": "netflix-normal.png",
    "prime": "prime-video-normal.png",
    "disney": "disney-plus-normal.png",
    "paramount": "paramount-plus-normal.png",
    "apple": "apple-tv-plus-normal.png",
    "max": "hbo-max-normal.png",
}

KIDS_THUMBNAILS = {
    "netflix": "netflix.png",
    "prime": "prime video.png",
    "disney": "Disney+.png",
    "paramount": "Paramount+.png",
    "apple": "Apple TV.png",
    "max": "HBO Max.png",
}

state: dict = {"built_at": 0, "profiles": {}}
_build_lock = asyncio.Lock()
_import_files_lock = asyncio.Lock()

FALLBACK_MOVIE_GENRES = [
    "drama", "comedy", "action", "thriller", "science-fiction", "romance",
    "adventure", "mystery",
]
FALLBACK_SHOW_GENRES = [
    "drama", "comedy", "crime", "mystery", "science-fiction", "animation",
    "documentary", "reality",
]

GENRE_EXCLUDE = {
    "animation": {"movie": "27", "tv": ""},
    "anime": {"movie": "27", "tv": ""},
    "comedy": {"movie": "27,53,80,10752", "tv": "80,10768"},
    "family": {"movie": "27,53,80,10752", "tv": "80,10768"},
    "music": {"movie": "27,53,80", "tv": ""},
    "romance": {"movie": "27,53", "tv": ""},
}

GENRE_ADJECTIVES = {
    "animation": "Animated",
    "science-fiction": "Sci-Fi",
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "profile"


# Display names of the users currently opted into streaming catalogs, refreshed
# from the database by refresh_targets(). Kept as a module-level snapshot
# because _profile_targets() is called from synchronous serving paths that
# cannot await a query. Empty until the first refresh, which happens at startup
# before anything is served.
_targets: list[str] = []


async def refresh_targets() -> list[str]:
    """Re-read who has streaming catalogs switched on.

    Called at startup, before each build, and after the admin toggles someone,
    so a tick in the catalog builder takes effect without a restart.
    """
    global _targets
    try:
        users = await db.users_with_row("streaming_catalogs_row")
    except Exception:
        logger.exception("streaming catalogs: could not read opted-in users; "
                         "keeping the previous set")
        return _targets
    _targets = [str(u.get("name") or "").strip() for u in users
                if str(u.get("name") or "").strip()]
    return _targets


def _profile_targets() -> list[dict]:
    """One profile per opted-in viewer.

    Profiles remain identified by a slug of the display name rather than by
    token: the built state, the catalog ids, and the exported collection files
    are all keyed that way, and re-keying them would force everyone to re-import
    their collections for no behavioural gain.
    """
    seen: set[str] = set()
    out = []
    for target in _targets:
        pid = _slug(target)
        if pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "target": target, "title": target})
    return out


def _profile_for_id(profile_id: str) -> dict | None:
    for profile in _profile_targets():
        if profile["id"] == profile_id:
            return profile
    return None


def _catalog_id(profile: dict, provider: dict, spec: dict) -> str:
    return f"ps_{profile['id']}_{provider['id']}_{spec['id']}"


def private_namespace_for_user(user: dict) -> str:
    """Return an opaque, stable namespace for one private addon URL.

    Nuvio installs addons by URL but Collection sources resolve them by manifest
    ID.  A shared ID therefore lets one household profile accidentally resolve
    another profile's private addon.  Hashing the high-entropy route token gives
    each installed picker a stable identity without putting the token or profile
    name into an exported Collection.
    """
    token = str(user.get("token") or "").strip()
    if not token:
        raise ValueError("private Daily Picks users require a token")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"u{digest}"


def private_addon_id_for_user(user: dict) -> str:
    return f"{config.ADDON_ID}.{private_namespace_for_user(user)}"


def _private_catalog_id(namespace: str, provider: dict, spec: dict,
                        specs: list[dict]) -> str:
    """Stable, viewer-scoped catalog ID inside a private Daily Picks addon.

    The opaque namespace prevents Nuvio's cross-addon catalog fallback from
    selecting another viewer's identically named streaming slot.
    """
    if spec["id"] in {"trending-movies", "trending-shows"}:
        slot = spec["id"]
    else:
        same_type = [
            candidate for candidate in specs
            if candidate["type"] == spec["type"]
            and candidate["id"] not in {"trending-movies", "trending-shows"}
        ]
        try:
            ordinal = next(
                index for index, candidate in enumerate(same_type, 1)
                if candidate["id"] == spec["id"]
            )
        except StopIteration as exc:
            raise ValueError(f"spec is not part of its profile: {spec['id']}") from exc
        slot = f"{spec['type']}-{ordinal:02d}"
    return f"{PRIVATE_CATALOG_PREFIX}{namespace}_{provider['id']}_{slot}"


def _legacy_private_catalog_id(provider: dict, spec: dict,
                               specs: list[dict]) -> str:
    """Pre-v2 private ID, accepted briefly for cached-client compatibility."""
    return _private_catalog_id("", provider, spec, specs).replace(
        f"{PRIVATE_CATALOG_PREFIX}_", PRIVATE_CATALOG_PREFIX, 1)


def is_private_catalog_id(catalog_id: str) -> bool:
    return catalog_id.startswith(PRIVATE_CATALOG_PREFIX)


def profile_id_for_user(user: dict) -> str | None:
    """Resolve one DB user to one prebuilt streaming profile exactly.

    Profile generation historically accepted a substring username match.  That
    is unsafe on a request path because several household display names can
    share a prefix.  Private serving therefore uses an exact display-name
    match, plus the exact user identity persisted with the built taste model.
    Ambiguous or absent matches fail closed.
    """
    identities = {
        str(user.get("name") or "").strip().casefold()
    } - {""}
    if not identities:
        return None

    matches: set[str] = set()
    profile_states = state.get("profiles") or {}
    for profile in _profile_targets():
        profile_state = profile_states.get(profile["id"]) or {}
        profile_identities = {
            str(profile.get("target") or "").strip().casefold(),
            str(profile_state.get("target") or "").strip().casefold(),
            str(((profile_state.get("taste_profile") or {}).get("user")) or "")
            .strip().casefold(),
        }
        profile_identities.discard("")
        if identities & profile_identities:
            matches.add(profile["id"])
    return next(iter(matches)) if len(matches) == 1 else None


def _profile_state_matches_user_safety(user: dict, profile_id: str) -> bool:
    """Do not serve rows built under a stale kid/adult age policy."""
    profile_state = (state.get("profiles") or {}).get(profile_id) or {}
    if not profile_state:
        return False
    built_age = (profile_state.get("taste_profile") or {}).get("age")
    return built_age == effective_kid_age(user)


def _ensure_dirs() -> None:
    config.CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
    config.CATALOG_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTION_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> None:
    global state
    try:
        state = json.loads(STATE_PATH.read_text())
        logger.info("loaded profile streaming catalogs: %d profile(s)",
                    len(state.get("profiles", {})))
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_state() -> None:
    STATE_PATH.write_text(json.dumps(state))


def _seed_date() -> str:
    return dt.date.today().isoformat()


def _genre_label(slug: str, adjective: bool = False) -> str:
    if adjective:
        return GENRE_ADJECTIVES.get(slug, tmdb.genre_label(slug))
    return tmdb.genre_label(slug)


def _genre_title(kind: str, slugs: list[str]) -> str:
    labels = [_genre_label(slug, adjective=True) for slug in slugs]
    noun = "Movies" if kind == "movie" else "Shows"
    return f"{' '.join(labels)} {noun}"


def _genre_params(kind: str, slugs: list[str], rng: random.Random) -> dict:
    media = "movie" if kind == "movie" else "tv"
    table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
    genre_ids: list[str] = []
    for slug in slugs:
        genre_id = str(table.get(slug, ""))
        if genre_id and genre_id not in genre_ids:
            genre_ids.append(genre_id)

    sort_by = rng.choice(["popularity.desc", "vote_average.desc"])
    params = {
        "with_genres": ",".join(genre_ids),
        "sort_by": sort_by,
        "vote_count.gte": 300 if sort_by == "vote_average.desc" else 50,
    }
    exclude_ids: set[str] = set()
    for slug in slugs:
        for genre_id in (GENRE_EXCLUDE.get(slug, {}).get(media) or "").split(","):
            if genre_id:
                exclude_ids.add(genre_id)
    if exclude_ids:
        params["without_genres"] = ",".join(sorted(exclude_ids))
    return params


def _trending_specs() -> list[dict]:
    return [
        {
            "id": "trending-movies",
            "title": "Trending Movies",
            "media": "movie",
            "type": "movie",
            "params": {"sort_by": "popularity.desc", "vote_count.gte": 50},
        },
        {
            "id": "trending-shows",
            "title": "Trending Shows",
            "media": "tv",
            "type": "series",
            "params": {"sort_by": "popularity.desc", "vote_count.gte": 25},
        },
    ]


def _weighted_genres(taste: dict | None, kind: str, limit: int = 8) -> list[tuple[str, float]]:
    table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
    raw = ((taste or {}).get("genres") or {}).get(kind, {})
    genres = [
        (slug, float(weight))
        for slug, weight in raw.items()
        if slug in table
    ]
    if not genres:
        fallback = FALLBACK_MOVIE_GENRES if kind == "movie" else FALLBACK_SHOW_GENRES
        genres = [
            (slug, max(0.1, 1.0 - idx * 0.08))
            for idx, slug in enumerate(fallback)
            if slug in table
        ]
    genres.sort(key=lambda item: item[1], reverse=True)
    return genres[:limit]


def _candidate_specs(kind: str, taste: dict | None, seed: str) -> list[tuple[float, dict]]:
    rng = random.Random(f"profile-streaming:{kind}:{seed}")
    genres = _weighted_genres(taste, kind)
    table = tmdb.MOVIE_GENRES if kind == "movie" else tmdb.TV_GENRES
    media = "movie" if kind == "movie" else "tv"
    stremio_type = "movie" if kind == "movie" else "series"
    candidates: list[tuple[float, dict]] = []

    for idx, (slug, weight) in enumerate(genres):
        spec = {
            "title": _genre_title(kind, [slug]),
            "media": media,
            "type": stremio_type,
            "params": _genre_params(kind, [slug], rng),
        }
        candidates.append((weight * 100 - idx + rng.uniform(0, 18), spec))

    for left, right in combinations(genres[:6], 2):
        slugs = [left[0], right[0]]
        genre_ids = {table[slug] for slug in slugs if slug in table}
        if len(genre_ids) < 2:
            continue
        spec = {
            "title": _genre_title(kind, slugs),
            "media": media,
            "type": stremio_type,
            "params": _genre_params(kind, slugs, rng),
        }
        score = ((left[1] + right[1]) / 2.0) * 92 + rng.uniform(0, 24)
        candidates.append((score, spec))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def _row_specs_for_profile(
        profile: dict, taste: dict | None, seed_date: str | None = None) -> list[dict]:
    seed = f"{seed_date or _seed_date()}:{profile['id']}:{(taste or {}).get('hash', 'none')}"
    rng = random.Random(f"profile-streaming:pick:{seed}")
    target = max(1, config.PROFILE_STREAMING_TASTE_ROWS)
    movie_target = (target + 1) // 2
    show_target = target - movie_target
    movie_candidates = _candidate_specs("movie", taste, seed)
    show_candidates = _candidate_specs("show", taste, seed)
    selected: list[dict] = []
    used_titles: set[str] = set()

    def take(candidates: list[tuple[float, dict]], count: int) -> None:
        nonlocal selected
        for _, spec in candidates:
            if len([s for s in selected if s["media"] == spec["media"]]) >= count:
                break
            if spec["title"] in used_titles:
                continue
            used_titles.add(spec["title"])
            selected.append(spec)

    take(movie_candidates, movie_target)
    take(show_candidates, show_target)
    for _, spec in [*movie_candidates, *show_candidates]:
        if len(selected) >= target:
            break
        if spec["title"] in used_titles:
            continue
        used_titles.add(spec["title"])
        selected.append(spec)

    if len(selected) < target:
        fallback_candidates = [
            *_candidate_specs("movie", None, f"{seed}:fallback"),
            *_candidate_specs("show", None, f"{seed}:fallback"),
        ]
        for _, spec in fallback_candidates:
            if len(selected) >= target:
                break
            if spec["title"] in used_titles:
                continue
            used_titles.add(spec["title"])
            selected.append(spec)

    rng.shuffle(selected)
    for idx, spec in enumerate(selected[:target], 1):
        spec["id"] = f"taste-{idx:02d}"
    return [*_trending_specs(), *selected[:target]]


def _profile_specs(profile: dict) -> list[dict]:
    profile_state = (state.get("profiles") or {}).get(profile["id"]) or {}
    specs = profile_state.get("row_specs") or []
    if specs:
        return specs
    return _row_specs_for_profile(profile, None)


def _row_order_meta(profile: dict, taste: dict | None,
                    seed_date: str | None = None) -> dict:
    return {
        "version": ROW_ORDER_VERSION,
        "seed_date": seed_date or _seed_date(),
        "profile_id": profile["id"],
        "target": profile["target"],
        "taste_hash": (taste or {}).get("hash", "none"),
    }


async def _discover_raw(media: str, params: dict) -> list[dict]:
    results: list[dict] = []
    for page in range(1, config.PROFILE_STREAMING_DISCOVER_PAGES + 1):
        try:
            page_items = await tmdb.discover(media, {**params, "page": page})
        except Exception as exc:
            logger.warning("profile streaming discover failed media=%s page=%s: %s",
                           media, page, exc)
            break
        if not page_items:
            break
        results.extend(page_items)
        if len(page_items) < 20:
            break
    return results


async def _discover_row(
        provider: dict, spec: dict, taste: dict | None, seed: str,
        max_age: int | None = None) -> list[dict]:
    params = {
        **spec["params"],
        "watch_region": "US",
        "with_watch_providers": provider["tmdb"],
        "with_watch_monetization_types": "flatrate|ads",
        "include_adult": "false",
    }
    if max_age is not None and spec["media"] == "movie":
        params["certification_country"] = "US"
        params["certification.lte"] = "PG-13" if max_age >= 13 else "PG"
    results = await _discover_raw(spec["media"], params)
    metas = await tmdb.resolve_many(
        spec["media"], [r["id"] for r in results],
        set(), set(), config.PROFILE_STREAMING_ROW_ITEMS, max_age)

    if len(metas) < 6 and "," in str(params.get("with_genres", "")):
        fallback_params = dict(params)
        fallback_params["with_genres"] = str(params["with_genres"]).replace(",", "|")
        fallback_results = await _discover_raw(spec["media"], fallback_params)
        metas = await tmdb.resolve_many(
            spec["media"], [r["id"] for r in fallback_results],
            set(), set(), config.PROFILE_STREAMING_ROW_ITEMS, max_age)

    return kids_catalogs._rank_row_for_taste(metas, spec, taste, seed)


def _diversify_profile_rows(
        profile: dict, rows: dict[str, list[dict]], specs: list[dict],
        seed_date: str) -> None:
    for provider in kids_catalogs.PROVIDERS:
        first_seen: dict[str, dict[str, int]] = {"movie": {}, "series": {}}
        top_seen: dict[str, dict[str, int]] = {"movie": {}, "series": {}}
        for spec in specs:
            cid = _catalog_id(profile, provider, spec)
            row = rows.get(cid)
            if not row:
                continue
            rows[cid] = kids_catalogs._diversify_row(
                provider, spec, row, seed_date,
                first_seen.setdefault(spec["type"], {}),
                top_seen.setdefault(spec["type"], {}),
            )


def catalog_defs() -> list[dict]:
    """Legacy public-helper definitions kept only for migration compatibility."""
    defs = []
    for profile in _profile_targets():
        specs = _profile_specs(profile)
        for provider in kids_catalogs.PROVIDERS:
            for spec in specs:
                defs.append({
                    "type": spec["type"],
                    "id": _catalog_id(profile, provider, spec),
                    "name": spec["title"],
                    # TV honors the explicit collection-only flag; the required
                    # one-value genre below is the corresponding Mobile guard.
                    "showInHome": False,
                    "extra": [{"name": "genre", "options": ["All"],
                               "isRequired": True}],
                })
    return defs


def catalog_defs_for_user(user: dict) -> list[dict]:
    """Collection-only descriptors for this user's private Daily Picks addon."""
    # The flag is the authority. Checking it here as well as in the target list
    # means un-ticking someone stops serving their rows immediately, without
    # waiting for the next build to drop the profile from the state file.
    if not user.get("streaming_catalogs_row"):
        return []
    profile_id = profile_id_for_user(user)
    profile = _profile_for_id(profile_id) if profile_id else None
    if not profile or not _profile_state_matches_user_safety(user, profile_id):
        return []
    profile_state = (state.get("profiles") or {}).get(profile_id) or {}
    rows = profile_state.get("rows") or {}
    specs = _profile_specs(profile)
    namespace = private_namespace_for_user(user)
    defs = []
    for provider in kids_catalogs.PROVIDERS:
        for spec in specs:
            internal_id = _catalog_id(profile, provider, spec)
            # A stable collection slot must survive a temporarily empty daily
            # result so an existing import resolves again when the slot refills.
            if internal_id not in rows:
                continue
            defs.append({
                "type": spec["type"],
                "id": _private_catalog_id(namespace, provider, spec, specs),
                "name": f"{provider['title']} · {spec['title']}",
                "showInHome": False,
                "pageSize": max(1, config.CATALOG_PAGE_SIZE),
                # TV clients honor showInHome. Mobile clients currently hide
                # collection backing catalogs by excluding descriptors with a
                # required extra, so keep the one-value genre convention too.
                "extra": [
                    {"name": "genre", "options": ["All"],
                     "isRequired": True},
                    {"name": "skip", "isRequired": False},
                ],
            })
    return defs


def manifest_export() -> dict:
    return {
        "id": config.PROFILE_STREAMING_ADDON_ID,
        "version": "1.0.2",
        "name": config.PROFILE_STREAMING_ADDON_NAME,
        "description": "Hidden backing addon for imported streaming profile "
                       "collections. Rows are only exposed through those "
                       "collections.",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": catalog_defs(),
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }


def _source(catalog_id: str, spec: dict,
            addon_id: str = config.PROFILE_STREAMING_ADDON_ID,
            genre: str | None = None) -> dict:
    source = {
        "provider": "addon",
        "addonId": addon_id,
        "type": spec["type"],
        "catalogId": catalog_id,
    }
    if genre:
        source["genre"] = genre
    return source


def _cover_image_url(profile: dict, provider: dict) -> str:
    profile_state = (state.get("profiles") or {}).get(profile["id"]) or {}
    taste_age = (profile_state.get("taste_profile") or {}).get("age")
    if isinstance(taste_age, int) or profile["id"] == _slug(config.KIDS_TASTE_USER):
        filename = quote(KIDS_THUMBNAILS[provider["id"]], safe="")
        return f"{config.HOST_NAME}/catalogs/kids-thumbnails/{filename}"
    return (
        f"{config.HOST_NAME}/catalogs/assets/"
        f"{NORMAL_COVERS.get(provider['id'], provider['cover'])}"
    )


def _profile_max_age(profile: dict, taste: dict | None) -> int | None:
    age = (taste or {}).get("age")
    if isinstance(age, int):
        return age
    return 3 if profile["id"] == _slug(config.KIDS_TASTE_USER) else None


def collection_export(profile_id: str, *,
                      addon_id: str = config.PROFILE_STREAMING_ADDON_ID,
                      private_ids: bool = False,
                      private_namespace: str | None = None,
                      collection_title: str | None = None) -> list[dict]:
    profile = _profile_for_id(profile_id)
    if not profile:
        return []
    profile_state = (state.get("profiles") or {}).get(profile_id) or {}
    rows = profile_state.get("rows") or {}
    specs = _profile_specs(profile)
    if private_ids and not private_namespace:
        raise ValueError("private collection exports require a namespace")
    folders = []
    for provider in kids_catalogs.PROVIDERS:
        sources = []
        for spec in specs:
            internal_id = _catalog_id(profile, provider, spec)
            has_row = internal_id in rows if private_ids else bool(rows.get(internal_id))
            if has_row:
                catalog_id = _private_catalog_id(
                    str(private_namespace), provider, spec, specs) \
                    if private_ids else internal_id
                sources.append(_source(
                    catalog_id, spec, addon_id,
                    genre="All" if private_ids else None,
                ))
        if not sources:
            continue
        folder = {
            "id": (
                f"dp_{PRIVATE_COLLECTION_VERSION}_f_"
                f"{private_namespace}_{provider['id']}"
                if private_ids
                else f"ps_f_{profile['id']}_{provider['id']}"
            ),
            "title": provider["title"],
            "coverImageUrl": _cover_image_url(profile, provider),
            "focusGifUrl": None,
            "coverEmoji": None,
            "tileShape": "LANDSCAPE",
            "hideTitle": True,
            "_coverMode": "image",
        }
        # catalogSources is the stable format understood by old and current
        # Nuvio clients.  Omitting the newer sources field is intentional:
        # intermediate TV releases gave it precedence even when they failed to
        # decode its entries, leaving a valid-looking folder with no rows.
        folder["catalogSources"] = sources
        folders.append(folder)
    return [{
        "id": (
            f"daily-picks-streaming-{PRIVATE_COLLECTION_VERSION}-"
            f"{private_namespace}"
            if private_ids
            else f"profile-streaming-{profile['id']}"
        ),
        "title": collection_title or f"{profile['title']} Streaming",
        "backdropImageUrl": None,
        "pinToTop": not private_ids,
        "focusGlowEnabled": True,
        "viewMode": "ROWS",
        "showAllTab": False,
        "folders": folders,
    }]


def collection_export_for_user(user: dict) -> list[dict]:
    profile_id = profile_id_for_user(user)
    if not profile_id or not _profile_state_matches_user_safety(user, profile_id):
        return []
    display_name = str(user.get("name") or "").strip()
    namespace = private_namespace_for_user(user)
    return collection_export(
        profile_id,
        addon_id=private_addon_id_for_user(user),
        private_ids=True,
        private_namespace=namespace,
        collection_title=f"{display_name} Streaming" if display_name else None,
    )


def _collection_filename(profile: dict) -> str:
    safe = re.sub(r"[^A-Z0-9]+", "_", profile["title"].upper()).strip("_")
    return f"NUVIO_COLLECTION_IMPORT_{safe}_STREAMING.json"


def _write_text_atomic(path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content)
    temporary.replace(path)


def _write_readme() -> None:
    lines = [
        "# Daily Picks Streaming Collections",
        "",
        "Each JSON file is folded into its matching private Daily Picks addon.",
        "Open the Daily Picks setup page and install that viewer's **Private Daily",
        "Picks addon** URL under Nuvio Addons. Do not install a streaming helper.",
        "",
        "Then import the matching `NUVIO_COLLECTION_IMPORT_*_STREAMING.json` file",
        "under Nuvio Collections. These files are viewer-specific even though they",
        "contain no private addon token.",
        "",
        "Rows keep watched titles in place. Trending Movies and Trending Shows",
        "stay first, followed by daily taste-generated categories.",
        f"Scheduled refresh: one profile per day at "
        f"{config.PROFILE_STREAMING_REFRESH_HOUR:02d}:"
        f"{config.PROFILE_STREAMING_REFRESH_MINUTE:02d} local time.",
        "",
    ]
    _write_text_atomic(COLLECTION_DIR / "README.md", "\n".join(lines))


async def write_import_files() -> None:
    """Write only one-addon, viewer-scoped Collection imports.

    The old static pack advertised a second helper addon. Leaving those files
    beside the folded imports made it easy to combine a v2 private picker with a
    v1 helper-backed Collection: Nuvio could preview its folders but had no
    installed addon capable of resolving their rows. Generate from DB users so
    every file carries the same opaque addon/catalog identity as that user's
    private manifest.
    """
    async with _import_files_lock:
        _ensure_dirs()
        for temporary in COLLECTION_DIR.glob(".*.tmp"):
            temporary.unlink()
        helper_path = COLLECTION_DIR / "HELPER_ADDON_MANIFEST_INSTALL_THIS_FIRST.json"
        helper_path.unlink(missing_ok=True)

        expected_files: set[str] = set()
        for user in await db.all_users():
            profile_id = profile_id_for_user(user)
            profile = _profile_for_id(profile_id) if profile_id else None
            if not profile or not _profile_state_matches_user_safety(user, profile_id):
                continue
            payload = collection_export_for_user(user)
            if not payload:
                continue
            filename = _collection_filename(profile)
            if filename in expected_files:
                filename = filename.removesuffix(".json") + (
                    f"_{private_namespace_for_user(user).upper()}.json"
                )
            _write_text_atomic(
                COLLECTION_DIR / filename, json.dumps(payload, indent=2))
            expected_files.add(filename)

        # Delete stale helper-era/user files only after every replacement is in
        # place, so a failed generation cannot leave the directory half-empty.
        for path in COLLECTION_DIR.glob("NUVIO_COLLECTION_IMPORT_*_STREAMING*.json"):
            if path.name not in expected_files:
                path.unlink()
        _write_readme()


async def build_profile(profile: dict) -> None:
    taste = await kids_catalogs.taste_model_for_user(
        profile["target"], "profile streaming taste")
    row_order = _row_order_meta(profile, taste)
    specs = _row_specs_for_profile(profile, taste, row_order["seed_date"])
    seed = f"{row_order['seed_date']}:{profile['id']}"
    max_age = _profile_max_age(profile, taste)

    rows: dict[str, list[dict]] = {}
    for provider in kids_catalogs.PROVIDERS:
        discovered = await asyncio.gather(
            *(_discover_row(provider, spec, taste, seed, max_age) for spec in specs))
        for spec, row in zip(specs, discovered):
            rows[_catalog_id(profile, provider, spec)] = row

    _diversify_profile_rows(profile, rows, specs, row_order["seed_date"])

    profiles = state.setdefault("profiles", {})
    profiles[profile["id"]] = {
        "built_at": int(time.time()),
        "target": profile["target"],
        "title": profile["title"],
        "row_order": row_order,
        "row_specs": specs,
        "taste_profile": {
            "user": (taste or {}).get("user"),
            "source": (taste or {}).get("source"),
            "age": (taste or {}).get("age"),
            "real_watches": (taste or {}).get("real_watches"),
            "hash": (taste or {}).get("hash"),
            "activity": (taste or {}).get("activity"),
        } if taste else None,
        "rows": rows,
    }
    state["built_at"] = int(time.time())
    state["catalog_defs"] = catalog_defs()
    _save_state()
    logger.info("profile streaming: built %s (%d rows, %d specs)",
                profile["target"], len(rows), len(specs))


async def build(profile_id: str | None = None) -> None:
    # Pick up anyone the admin has just ticked (or un-ticked) before deciding
    # which profiles exist.
    await refresh_targets()
    async with _build_lock:
        _ensure_dirs()
        if profile_id:
            profile = _profile_for_id(profile_id)
            if not profile:
                raise RuntimeError(f"unknown profile streaming id: {profile_id}")
            await build_profile(profile)
            await write_import_files()
            return
        for profile in _profile_targets():
            await build_profile(profile)
        await write_import_files()


def get_metas(catalog_id: str) -> list[dict] | None:
    for profile_state in (state.get("profiles") or {}).values():
        rows = profile_state.get("rows") or {}
        if catalog_id in rows:
            return rows[catalog_id]
    return None


def get_metas_for_user(user: dict, ctype: str,
                       catalog_id: str) -> list[dict] | None:
    """Return a private collection row only when the token owns its profile."""
    if not is_private_catalog_id(catalog_id):
        return None
    profile_id = profile_id_for_user(user)
    profile = _profile_for_id(profile_id) if profile_id else None
    if not profile or not _profile_state_matches_user_safety(user, profile_id):
        return None
    rows = ((state.get("profiles") or {}).get(profile_id) or {}).get("rows") or {}
    specs = _profile_specs(profile)
    namespace = private_namespace_for_user(user)
    for provider in kids_catalogs.PROVIDERS:
        for spec in specs:
            current_id = _private_catalog_id(namespace, provider, spec, specs)
            legacy_id = _legacy_private_catalog_id(provider, spec, specs)
            if catalog_id not in {current_id, legacy_id}:
                continue
            if spec["type"] != ctype:
                return None
            return rows.get(_catalog_id(profile, provider, spec))
    return None


def _profile_for_date(day: dt.date) -> dict | None:
    profiles = _profile_targets()
    if not profiles:
        return None
    return profiles[day.toordinal() % len(profiles)]


def _needs_build(profile: dict) -> bool:
    profile_state = (state.get("profiles") or {}).get(profile["id"]) or {}
    if not profile_state:
        return True
    if not profile_state.get("row_specs"):
        return True
    row_order = profile_state.get("row_order") or {}
    return row_order.get("version") != ROW_ORDER_VERSION


def _seconds_until_next_run() -> float:
    now = dt.datetime.now()
    nxt = now.replace(hour=config.PROFILE_STREAMING_REFRESH_HOUR,
                      minute=config.PROFILE_STREAMING_REFRESH_MINUTE,
                      second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


async def run() -> None:
    _ensure_dirs()
    load_state()
    await refresh_targets()
    stale = [p for p in _profile_targets() if _needs_build(p)]
    try:
        if stale:
            async with _build_lock:
                for profile in stale:
                    await build_profile(profile)
                await write_import_files()
        else:
            await write_import_files()
    except Exception:
        # Serving uses the last good in-memory/file-backed state. A transient
        # export failure must not kill the long-running daily scheduler.
        logger.exception("startup profile streaming reconciliation failed")

    while True:
        delay = _seconds_until_next_run()
        logger.info("next profile streaming refresh in %.1fh", delay / 3600)
        await asyncio.sleep(delay)
        profile = _profile_for_date(dt.date.today())
        if not profile:
            continue
        try:
            async with _build_lock:
                await build_profile(profile)
                await write_import_files()
        except Exception:
            logger.exception("profile streaming refresh failed for %s",
                             profile["target"])
