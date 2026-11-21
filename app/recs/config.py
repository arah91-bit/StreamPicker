import os
from pathlib import Path


# Credentials with no sensible default: TMDB_API_KEY and SETUP_SECRET (the
# secret path segment gating the setup/configure flow — without one, anyone
# who finds the public URL could add viewers). They are deliberately *not*
# module constants: reading them here with os.environ[...] made every module
# under app.recs — and so the whole test suite, which never calls TMDB —
# unimportable without a real deployment's secrets. require() keeps the check
# just as hard, one step later.
def require(name: str) -> str:
    """The named credential, or a hard error saying which one is missing.

    Call this where the value is about to be used for real: sent to TMDB,
    or compared against a caller's secret. A deployment that forgot one then
    fails on the request that needed it, with a message naming it, instead of
    at some unrelated import.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set, and Daily Picks cannot serve this without "
            "it. Set it in the environment and restart.")
    return value


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENABLED = (
    os.environ.get("GEMINI_ENABLED", "true").lower() == "true" and bool(GEMINI_API_KEY)
)

HOST_NAME = os.environ.get("HOST_NAME", "http://localhost:8010").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "/data/nuvio-recs.db")

ADDON_ID = os.environ.get("RECS_ADDON_ID",
                          os.environ.get("ADDON_ID", "org.arahub.dailypicks"))
# RECS_-prefixed because this package now shares a process — and therefore an
# environment — with stream-picker, whose own ADDON_NAME is "Auto Stream".
# Reading the bare name would let one addon silently rename the other.
ADDON_NAME = os.environ.get("RECS_ADDON_NAME", "Daily Picks")

# Local time (container TZ) at which the nightly refresh starts.
REFRESH_HOUR = int(os.environ.get("REFRESH_HOUR", "4"))
REFRESH_MINUTE = int(os.environ.get("REFRESH_MINUTE", "30"))
# Delay between users during the nightly refresh, to spread API load.
STAGGER_SECONDS = int(os.environ.get("STAGGER_SECONDS", "90"))
# Regenerate on startup if a user's catalogs are older than this many hours.
STALE_HOURS = int(os.environ.get("STALE_HOURS", "26"))

# Daily Picks is a full home viewing surface, not a compact recommendation
# widget.  The generator aims for this much vertical depth while keeping the
# opening rows highest-confidence and every title unique in the current slate.
CATALOG_MIN_ROWS = int(os.environ.get("CATALOG_MIN_ROWS", "18"))
CATALOG_TARGET_ROWS = int(os.environ.get("CATALOG_TARGET_ROWS", "22"))
CATALOG_ROW_ITEMS = int(os.environ.get("CATALOG_ROW_ITEMS", "30"))
CATALOG_PAGE_SIZE = int(os.environ.get("CATALOG_PAGE_SIZE", "30"))
CATALOG_TOP_ZONE_ROWS = int(os.environ.get("CATALOG_TOP_ZONE_ROWS", "6"))
CATALOG_MIDDLE_ZONE_ROWS = int(os.environ.get("CATALOG_MIDDLE_ZONE_ROWS", "12"))

# ── Top Picks rotation ───────────────────────────────────────────────────────
# A title gets a turn of TOP_PICKS_TURN_DAYS days on which its viewer
# actually loaded the row and saw it — calendar days do not count, so someone
# who visits once a fortnight is not cycled through titles they never saw —
# and then rests for TOP_PICKS_REST_DAYS calendar days.
TOP_PICKS_ITEMS = int(os.environ.get("TOP_PICKS_ITEMS", "50"))
TOP_PICKS_TURN_DAYS = int(os.environ.get("TOP_PICKS_TURN_DAYS", "3"))
TOP_PICKS_REST_DAYS = int(os.environ.get("TOP_PICKS_REST_DAYS", "21"))
# How far into a delivered page a card counts as seen. Nuvio shows six or
# seven cards across; twelve is one press of the remote further.
TOP_PICKS_SEEN_POSITIONS = int(os.environ.get("TOP_PICKS_SEEN_POSITIONS", "12"))
# Every Nth card is drawn from a genre the viewer has also watched but that
# the cards around it do not already cover. 0 turns the variety slots off.
TOP_PICKS_VARIETY_EVERY = int(os.environ.get("TOP_PICKS_VARIETY_EVERY", "5"))
# A seed watched recently counts for more than one watched long ago, measured
# from the viewer's own latest play (an occasional visitor's last visit is
# their "recent"). Never below the floor, so a much-loved older title still
# beats something barely watched last night.
SEED_RECENCY_HALF_LIFE_DAYS = float(os.environ.get(
    "SEED_RECENCY_HALF_LIFE_DAYS", "30"))
SEED_RECENCY_FLOOR = float(os.environ.get("SEED_RECENCY_FLOOR", "0.5"))

# ── Rebuilding only for people who use it ───────────────────────────────────
# The nightly job skips a profile nobody has opened and nobody has watched on
# since its last build. Opening the app with a slate older than
# OPEN_REFRESH_AFTER_HOURS rebuilds it in the background; the request is
# answered from the slate already stored. Starting a title the viewer has
# never played rebuilds NEW_WATCH_REBUILD_DELAY_SECONDS later, unless the
# slate is younger than NEW_WATCH_MIN_GAP_HOURS.
OPEN_REFRESH_AFTER_HOURS = float(os.environ.get("OPEN_REFRESH_AFTER_HOURS", "20"))
NEW_WATCH_REBUILD_DELAY_SECONDS = int(os.environ.get(
    "NEW_WATCH_REBUILD_DELAY_SECONDS", "600"))
NEW_WATCH_MIN_GAP_HOURS = float(os.environ.get("NEW_WATCH_MIN_GAP_HOURS", "2"))
# A play shorter than this is an index read or a false start, not a watch.
NEW_WATCH_MIN_SECONDS = int(os.environ.get("NEW_WATCH_MIN_SECONDS", "120"))

# ── Continue Watching / Watch History (opt-in, pinned above everything) ──
# These two rows are built on the request path rather than by the nightly job,
# because a resume row that is hours stale is worse than no resume row. They
# are deliberately not subject to CATALOG_ROW_ITEMS: a recommendation row is a
# curated slate that gets worse as it grows, while these are just the viewer's
# own backlog, where more depth is strictly more useful.
CONTINUE_WATCHING_ROW_ITEMS = int(os.environ.get(
    "CONTINUE_WATCHING_ROW_ITEMS", "60"))
WATCH_HISTORY_ROW_ITEMS = int(os.environ.get("WATCH_HISTORY_ROW_ITEMS", "150"))
# Seconds a built pair of rows is reused. Nuvio fires every row of the home
# screen at once and paginates on scroll, so without this one home-screen open
# would repeat the same work many times over. Short enough that
# finishing an episode is reflected on the next visit.
WATCHING_CACHE_SECONDS = int(os.environ.get("WATCHING_CACHE_SECONDS", "90"))
# Ceiling on per-show `next_episode` lookups in one build. This is the only
# fan-out in the row (one lookup per show), so it bounds worst-case latency
# for a viewer with a very large shelf of part-watched series.
WATCHING_PROGRESS_LOOKUPS = int(os.environ.get("WATCHING_PROGRESS_LOOKUPS", "40"))
# Cards in the Want to Watch row. Like the two above it this is a backlog
# rather than a curated slate, so depth is useful — but it is also one TMDB
# lookup per card on a cold build, which is what bounds it.
WATCHLIST_ROW_ITEMS = int(os.environ.get("WATCHLIST_ROW_ITEMS", "60"))

# ── trailers on the taste page ───────────────────────────────────────────
# Tapping a poster plays the trailer without leaving the page. Served as a
# plain MP4 through our own proxy rather than as a YouTube embed, because an
# embed cannot be made ad-free — see app/recs/trailers.py.
TRAILER_EXTRACT_TIMEOUT = int(os.environ.get("TRAILER_EXTRACT_TIMEOUT", "20"))
# How long a card the bootstrapper already showed stays out of the deck, even
# unanswered. Long enough to cover a session and a page reload; short enough
# that a good question is not retired for ever. See `taste_dealt`.
TASTE_DEALT_HOURS = int(os.environ.get("TASTE_DEALT_HOURS", "24"))
# Generous: this covers the whole body transfer, not just the connect.
TRAILER_STREAM_TIMEOUT = int(os.environ.get("TRAILER_STREAM_TIMEOUT", "60"))
# Address series entries at the episode (tt1234:3:7) instead of the show
# (tt1234). OFF by default because it is broken in practice: a catalog id is
# what the client fetches meta for, and nothing serves meta for an episode id,
# so tapping such a card fails with "could not load details from any addon".
# The episode is conveyed by releaseInfo + behaviorHints.defaultVideoId
# instead. Left switchable so a client that does resolve episode ids can be
# tried without a code change.
WATCHING_EPISODE_IDS = os.environ.get(
    "WATCHING_EPISODE_IDS", "false").lower() not in ("0", "false", "no", "off")
# A movie past this much progress is treated as finished, not resumable.
WATCHING_DONE_THRESHOLD = float(os.environ.get("WATCHING_DONE_THRESHOLD", "92"))
# ...and below this it is treated as a false start (trailer, wrong pick).
WATCHING_MIN_THRESHOLD = float(os.environ.get("WATCHING_MIN_THRESHOLD", "2"))

# A theatrical date does not prove that a clean home-viewing source exists.
# Recent movies must have a TMDB Digital/Physical/TV release. When TMDB has no
# historical home-release rows at all, this conservative age fallback keeps
# classics from disappearing because of incomplete crowd-sourced metadata.
HOME_RELEASE_FALLBACK_DAYS = int(os.environ.get(
    "HOME_RELEASE_FALLBACK_DAYS", "365"))
HOME_RELEASE_RECHECK_HOURS = int(os.environ.get(
    "HOME_RELEASE_RECHECK_HOURS", "24"))

# Later viewing activity is matched back to delivered recommendation snapshots
# inside this window.  It is an assisted-discovery signal, not a claim of
# causal attribution.
OUTCOME_ATTRIBUTION_HOURS = int(os.environ.get("OUTCOME_ATTRIBUTION_HOURS", "72"))

# Catalog type used for combined movie+series rows. Stremio-protocol clients
# that refuse to render "all" can be accommodated by setting this to "movie".
COMBINED_TYPE = os.environ.get("COMBINED_TYPE", "all")

# File-backed public catalog pack used by Nuvio Collections imports. The host
# path /srv/docker/nuvio-recs/Catalogs is mounted here in docker-compose.
CATALOGS_DIR = Path(os.environ.get("CATALOGS_DIR", "/catalogs"))
CATALOG_ASSETS_DIR = CATALOGS_DIR / "assets"

KIDS_ADDON_ID = os.environ.get("KIDS_ADDON_ID", "org.arahub.kidsstreaming")
KIDS_ADDON_NAME = os.environ.get("KIDS_ADDON_NAME", "Kids Streaming")
KIDS_TASTE_USER = os.environ.get("KIDS_TASTE_USER", "Ashton")
KIDS_REFRESH_HOUR = int(os.environ.get("KIDS_REFRESH_HOUR", "5"))
KIDS_REFRESH_MINUTE = int(os.environ.get("KIDS_REFRESH_MINUTE", "10"))
KIDS_STALE_HOURS = int(os.environ.get("KIDS_STALE_HOURS", "26"))
KIDS_DISCOVER_PAGES = int(os.environ.get("KIDS_DISCOVER_PAGES", "8"))
KIDS_ROW_ITEMS = int(os.environ.get("KIDS_ROW_ITEMS", "100"))

PROFILE_STREAMING_ADDON_ID = os.environ.get(
    "PROFILE_STREAMING_ADDON_ID", "org.arahub.tastesortedstreaming")
PROFILE_STREAMING_ADDON_NAME = os.environ.get(
    "PROFILE_STREAMING_ADDON_NAME", "Taste Sorted Streaming")
PROFILE_STREAMING_USERS = [
    p.strip() for p in os.environ.get(
        "PROFILE_STREAMING_USERS",
        "Skylar,Toya,Tonya,Hayden,arah91,Ashton",
    ).split(",")
    if p.strip()
]
PROFILE_STREAMING_REFRESH_HOUR = int(os.environ.get(
    "PROFILE_STREAMING_REFRESH_HOUR", "3"))
PROFILE_STREAMING_REFRESH_MINUTE = int(os.environ.get(
    "PROFILE_STREAMING_REFRESH_MINUTE", "47"))
PROFILE_STREAMING_TASTE_ROWS = int(os.environ.get(
    "PROFILE_STREAMING_TASTE_ROWS", "15"))
PROFILE_STREAMING_DISCOVER_PAGES = int(os.environ.get(
    "PROFILE_STREAMING_DISCOVER_PAGES", "5"))
PROFILE_STREAMING_ROW_ITEMS = int(os.environ.get(
    "PROFILE_STREAMING_ROW_ITEMS", "100"))

# ── Local AI Search (Ollama + TMDB verification) ────────────────────────────
# Off by default so environments without Ollama do not advertise an empty
# search catalog. When enabled, Nuvio's Search screen queries this catalog with
# plain-English phrases and resolves candidate titles through TMDB.
AI_SEARCH_ENABLED = (
    os.environ.get("AI_SEARCH_ENABLED", "false").lower() == "true"
)
AI_SEARCH_OLLAMA_URL = os.environ.get(
    "AI_SEARCH_OLLAMA_URL", "http://ollama:11434").rstrip("/")
AI_SEARCH_MODEL = os.environ.get("AI_SEARCH_MODEL", "qwen3:4b-instruct")
AI_SEARCH_TIMEOUT = float(os.environ.get("AI_SEARCH_TIMEOUT", "35"))
AI_SEARCH_KEEP_ALIVE = os.environ.get("AI_SEARCH_KEEP_ALIVE", "30m")
AI_SEARCH_RESULTS = int(os.environ.get("AI_SEARCH_RESULTS", "16"))
AI_SEARCH_CACHE_TTL = int(os.environ.get("AI_SEARCH_CACHE_TTL", "1800"))
AI_SEARCH_MIN_CHARS = int(os.environ.get("AI_SEARCH_MIN_CHARS", "4"))
