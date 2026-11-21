"""Asian Dramas rows, folded in from the standalone asian-dramas service.

It used to be its own container and its own shared-secret addon, which meant
everyone who wanted it installed a second thing and everyone who did not still
saw it exist. It is a per-viewer checkbox now: the rows are built once nightly
and appear only in the manifests of viewers who ticked it on.

This module is the whole public surface — `builder`, `tmdb` and `config` are
carried over from that service almost unchanged, so the row-building logic did
not have to be re-derived.
"""

import logging
import re
from pathlib import Path

from app.recs import config as recs_config
from app.recs.dramas import builder, config

logger = logging.getLogger("nuvio-recs")

CATALOG_PREFIX = "ad_"
COUNTRY_PREFIX = "ad_cc_"
ACTOR_PREFIX = "ad_p_"

# Where main.py mounts config.STATIC_DIR. The flag stills and focus GIFs used
# to be served from the standalone service's own hostname, which is why the
# collection files it exported still point at a host that now 404s.
STATIC_ROUTE = "/catalogs/dramas"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _country_row_catalog_id(country_catalog_id: str, genre: str) -> str:
    return f"{country_catalog_id}_{_slug(genre)}"


def _country_row_key(catalog_id: str) -> str | None:
    """Map a country+genre catalog id back to its key in the built rows."""
    for cc, _ in config.COUNTRIES:
        country_id = f"{COUNTRY_PREFIX}{cc.lower()}"
        prefix = f"{country_id}_"
        if not catalog_id.startswith(prefix):
            continue
        genre_slug = catalog_id[len(prefix):]
        for genre in builder.GENRES:
            if _slug(genre) == genre_slug:
                return f"{country_id}|{genre}"
    return None


def _country_row_catalog_defs() -> list[dict]:
    defs = []
    for cc, _adj in config.COUNTRIES:
        country_id = f"{COUNTRY_PREFIX}{cc.lower()}"
        for genre in builder.GENRES:
            defs.append({
                "type": "series",
                "id": _country_row_catalog_id(country_id, genre),
                "name": genre,
                # Hidden from the home screen but addressable from an imported
                # Nuvio collection — same contract the standalone addon had.
                "showInHome": False,
                "extra": [{"name": "genre", "options": ["All"],
                           "isRequired": True}],
            })
    return defs


def is_drama_catalog_id(catalog_id: str) -> bool:
    return catalog_id.startswith(CATALOG_PREFIX)


def enabled_for(user: dict) -> bool:
    return bool(user.get("asian_dramas_row"))


def collection_only(catalog: dict) -> dict:
    """A drama descriptor carrying *both* client guards.

    The two clients hide collection-backing catalogs differently: Mobile drops
    descriptors with a required extra, TV honours `showInHome=false` and
    nothing else. A descriptor with only the required extra therefore looks
    correct on a phone and arrives as a loose row on the television — which is
    exactly what Drama Stars did, for every viewer opted into dramas.

    Applied here rather than trusted from `builder.state` because that state is
    written nightly and persists: a viewer would otherwise keep the stray rows
    until the next build, however early the bug was fixed upstream of this.
    """
    return {**catalog, "showInHome": False}


def catalog_defs_for_user(user: dict) -> list[dict]:
    """Drama descriptors for one viewer's personal addon, or nothing."""
    if not enabled_for(user):
        return []
    stored = list(builder.state.get("catalog_defs") or [])
    return [collection_only(c) for c in stored] + _country_row_catalog_defs()


def get_metas(user: dict, catalog_id: str, genre: str | None = None) -> list[dict]:
    """Rows are shared — one nightly build serves every opted-in viewer — but
    the opt-in is still checked here so a guessed catalog id returns nothing."""
    if not enabled_for(user):
        return []
    rows = builder.state.get("rows") or {}
    row_key = _country_row_key(catalog_id)
    if row_key:
        return rows.get(row_key) or []
    key = f"{catalog_id}|{genre}" if genre else catalog_id
    return rows.get(key) or rows.get(catalog_id) or []


def _flag_urls(cc: str) -> tuple[str | None, str | None]:
    """(still, focus GIF) for one country, whichever of them exist on disk.

    Globbed rather than tabulated so dropping a new flag into the static
    directory is the whole change; five countries have a GIF and six do not.
    """
    flags = Path(config.STATIC_DIR) / "flags"
    still = gif = None
    for suffix in (".jpg", ".png", ".webp"):
        candidate = flags / f"{cc.lower()}{suffix}"
        if candidate.exists():
            still = f"{recs_config.HOST_NAME}{STATIC_ROUTE}/flags/{candidate.name}"
            break
    animated = flags / f"{cc.lower()}.gif"
    if animated.exists():
        gif = f"{recs_config.HOST_NAME}{STATIC_ROUTE}/flags/{animated.name}"
    return still, gif


def _source(catalog_id: str, addon_id: str) -> dict:
    return {
        "provider": "addon",
        "addonId": addon_id,
        "type": "series",
        "catalogId": catalog_id,
    }


def collection_export_for_user(user: dict, addon_id: str) -> list[dict]:
    """The Asian Dramas and Drama Stars collections, bound to one viewer.

    Nuvio resolves a collection source by the *manifest id* of an installed
    addon. Dramas used to be their own addon with one shared id, so a single
    static export served everybody; folded into Daily Picks, every viewer's
    manifest carries a different id, and an export naming any other one
    resolves to nothing at all. So this is generated per viewer, from the live
    build state, rather than written once to disk.
    """
    if not enabled_for(user):
        return []
    rows = builder.state.get("rows") or {}
    collections = []

    countries = []
    for cc, adjective in config.COUNTRIES:
        country_id = f"{COUNTRY_PREFIX}{cc.lower()}"
        sources = [
            _source(_country_row_catalog_id(country_id, genre), addon_id)
            for genre in builder.GENRES
            if rows.get(f"{country_id}|{genre}")
        ]
        if not sources:
            continue
        still, gif = _flag_urls(cc)
        countries.append({
            "id": f"ad_f_{cc.lower()}",
            "title": f"{adjective} Dramas",
            "tileShape": "landscape",
            # catalogSources for the same reason profile_streaming uses it:
            # intermediate Nuvio TV releases preferred the newer `sources`
            # field and then failed to decode it, leaving a folder that looked
            # valid and listed nothing. The old Drama Stars export used
            # `sources`, which is one more reason it showed empty.
            "catalogSources": sources,
            "coverImageUrl": still,
            "focusGifUrl": gif,
            "focusGifEnabled": bool(gif),
        })
    if countries:
        collections.append({
            "id": "asian-dramas",
            "title": "Asian Dramas",
            "pinToTop": True,
            "viewMode": "ROWS",
            "showAllTab": False,
            "folders": countries,
        })

    stars = []
    for actor in builder.state.get("actors") or []:
        catalog_id = f"{ACTOR_PREFIX}{actor['id']}"
        if not rows.get(catalog_id):
            continue
        profile_path = actor.get("profile")
        stars.append({
            "id": f"ad_f_p_{actor['id']}",
            "title": actor["name"],
            "tileShape": "poster",
            "catalogSources": [_source(catalog_id, addon_id)],
            "coverImageUrl": (f"https://image.tmdb.org/t/p/w500{profile_path}"
                              if profile_path else None),
        })
    if stars:
        collections.append({
            "id": "asian-drama-stars",
            "title": "Drama Stars",
            "pinToTop": True,
            "viewMode": "TABBED_GRID",
            "showAllTab": False,
            "folders": stars,
        })
    return collections


async def run() -> None:
    """Nightly build loop, carried over from the standalone service."""
    await builder.run()
