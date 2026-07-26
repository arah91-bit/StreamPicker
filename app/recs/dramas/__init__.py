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

from app.recs.dramas import builder, config

logger = logging.getLogger("nuvio-recs")

CATALOG_PREFIX = "ad_"
COUNTRY_PREFIX = "ad_cc_"


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


def catalog_defs_for_user(user: dict) -> list[dict]:
    """Drama descriptors for one viewer's personal addon, or nothing."""
    if not enabled_for(user):
        return []
    return list(builder.state.get("catalog_defs") or []) \
        + _country_row_catalog_defs()


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


async def run() -> None:
    """Nightly build loop, carried over from the standalone service."""
    await builder.run()
