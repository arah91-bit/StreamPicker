import os

# Every name here is DRAMAS_-prefixed because this package shares a process,
# and therefore an environment, with stream-picker and app/recs. The unprefixed
# spellings all belong to one of those: ADDON_NAME is the stream addon's,
# REFRESH_HOUR is Daily Picks' 04:30 nightly build, DATA_DIR/STATIC_DIR are
# deployment wiring. Reading them bare would silently retune the wrong service.
# The TMDB account is deliberately shared with Daily Picks, so the key is not
# redeclared here: tmdb.py asks app.recs.config.require for it per request.

ADDON_ID = os.environ.get("DRAMAS_ADDON_ID", "org.arahub.asiandramas")
ADDON_NAME = os.environ.get("DRAMAS_ADDON_NAME", "Asian Dramas")
STATIC_DIR = os.environ.get("DRAMAS_STATIC_DIR", "/catalogs/Asian Dramas/static")

DATA_DIR = os.environ.get("DRAMAS_DATA_DIR", "/catalogs/Asian Dramas/data")

# Whose watch history orders the actor rows is no longer configured
# here: it is whoever ticked Asian dramas on in the catalog builder.
# Actors that always come first, in this order, regardless of scores.
PINNED_ACTORS = [n.strip() for n in
                 os.environ.get("DRAMAS_PINNED_ACTORS", "Bai Lu").split(",") if n.strip()]
ACTOR_ROWS = int(os.environ.get("DRAMAS_ACTOR_ROWS", "10"))   # ranked rows after the pins
ROW_ITEMS = int(os.environ.get("DRAMAS_ROW_ITEMS", "200"))    # metas per catalog row
# TMDB discover pages to pull per country/genre before resolving to IMDb metas
# (20 results/page; ~18 pages ≈ 360 candidates to fill a 200-item row after the
# IMDb-less/adult drops). Niche country+genre combos just return fewer pages
# (discover_tv stops at the last real page), so cost scales with actual content.
DISCOVER_PAGES = int(os.environ.get("DRAMAS_DISCOVER_PAGES", "18"))

# Nightly rebuild time (container TZ). After nuvio-recs' 4:30 token refresh.
REFRESH_HOUR = int(os.environ.get("DRAMAS_REFRESH_HOUR", "5"))
REFRESH_MINUTE = int(os.environ.get("DRAMAS_REFRESH_MINUTE", "30"))
STALE_HOURS = int(os.environ.get("DRAMAS_STALE_HOURS", "26"))

# (ISO country, adjective) — one catalog row per country, ordered as shown:
# Chinese, Korean, Japanese lead (household preference); the rest by how much
# scripted-series content TMDB has for them.
COUNTRIES = [
    ("CN", "Chinese"),
    ("KR", "Korean"),
    ("JP", "Japanese"),
    ("TH", "Thai"),
    ("HK", "Hong Kong"),
    ("TW", "Taiwanese"),
    ("PH", "Filipino"),
    ("ID", "Indonesian"),
    ("MY", "Malaysian"),
    ("SG", "Singaporean"),
    ("VN", "Vietnamese"),
]
ASIAN_CC = {cc for cc, _ in COUNTRIES}
# Original-language codes counted as "Asian" for actor-row filtering.
ASIAN_LANGS = {"ko", "zh", "th", "ja", "cn",           # KR CN JP TH HK TW
               "tl", "fil", "id", "ms", "vi"}          # PH ID MY SG VN
