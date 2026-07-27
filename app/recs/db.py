"""SQLite storage. Every table is keyed by the user's token, so one user's
data can never be read while serving another user's request."""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.recs import config

_conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    token TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_generated_at INTEGER,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS catalogs (
    user_token TEXT NOT NULL,
    position INTEGER NOT NULL,
    catalog_id TEXT NOT NULL,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    metas TEXT NOT NULL,
    generated_at INTEGER NOT NULL,
    PRIMARY KEY (user_token, catalog_id, type)
);
CREATE TABLE IF NOT EXISTS meta_cache (
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    imdb_id TEXT,
    meta TEXT,
    cert TEXT,
    home_release_date TEXT,
    home_release_verified INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, media_type)
);
CREATE TABLE IF NOT EXISTS recommendation_exposure (
    user_token TEXT NOT NULL,
    imdb_id TEXT NOT NULL,
    last_shown_at INTEGER NOT NULL,
    show_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_token, imdb_id)
);
CREATE INDEX IF NOT EXISTS idx_recommendation_exposure_recent
    ON recommendation_exposure (user_token, last_shown_at);
-- Plays observed through this service's own addon. This is the successor to
-- Trakt as a history source: Trakt limits a free account to one connected
-- application, and that slot is wanted for the client's own sync, so the
-- recommendation engine has to learn what people watch from what it serves.
--
-- `viewer_key` is the opaque per-viewer namespace (a hash of the addon token),
-- never the token itself. `watched_pct` is bytes-served / file-size, NOT a
-- playback position: a seek-heavy session or a player that buffers far ahead
-- overstates it. Treat it as a coarse "did they finish this" signal only.
CREATE TABLE IF NOT EXISTS play_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_key TEXT NOT NULL,
    content_id TEXT NOT NULL,
    imdb_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    season INTEGER,
    episode INTEGER,
    played_at INTEGER NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    megabytes REAL NOT NULL DEFAULT 0,
    watched_pct REAL,
    -- Where the player's read head actually was, in bytes, when it stopped:
    -- the offset of the last range request plus what was delivered against it.
    -- Unlike watched_pct (total bytes delivered / file size, which a seek-heavy
    -- session inflates) this is monotonic in playback position, so it is the
    -- one to resume from. Still runs ahead of what the viewer SAW by whatever
    -- the player had buffered.
    position_bytes INTEGER,
    total_bytes INTEGER,
    position_pct REAL,
    picker TEXT,
    UNIQUE (viewer_key, content_id, played_at)
);
CREATE INDEX IF NOT EXISTS idx_play_history_recent
    ON play_history (viewer_key, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_play_history_title
    ON play_history (viewer_key, imdb_id);

CREATE TABLE IF NOT EXISTS storage_migrations (
    id TEXT PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

-- An append-only record of exactly what a recommendation build contained.
-- `catalogs` remains the replaceable serving cache; these rows are the
-- measurement ledger and are deliberately never updated by application code.
CREATE TABLE IF NOT EXISTS recommendation_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_token TEXT NOT NULL,
    generated_at INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    variant TEXT,
    trigger TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    catalog_count INTEGER NOT NULL,
    item_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recommendation_generations_user_time
    ON recommendation_generations (user_token, generated_at DESC);

CREATE TABLE IF NOT EXISTS recommendation_generation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    user_token TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    catalog_type TEXT NOT NULL,
    catalog_name TEXT NOT NULL,
    row_position INTEGER NOT NULL,
    item_position INTEGER NOT NULL,
    content_id TEXT,
    media_type TEXT,
    title TEXT,
    strategy TEXT,
    candidate_source TEXT,
    seed_content_id TEXT,
    rank_score REAL,
    score_components TEXT,
    meta TEXT NOT NULL,
    UNIQUE (generation_id, row_position, item_position)
);
CREATE INDEX IF NOT EXISTS idx_generation_items_content
    ON recommendation_generation_items (user_token, content_id, generation_id);
CREATE INDEX IF NOT EXISTS idx_generation_items_catalog
    ON recommendation_generation_items
       (generation_id, catalog_type, catalog_id);

-- Sessions are inferred from bursts of catalog deliveries. A delivery means
-- the server returned a row; it intentionally does not claim the user
-- scrolled to, saw, or considered every card in that row.
CREATE TABLE IF NOT EXISTS recommendation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_token TEXT NOT NULL,
    generation_id INTEGER,
    started_at INTEGER NOT NULL,
    last_delivery_at INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recommendation_sessions_user_time
    ON recommendation_sessions (user_token, last_delivery_at DESC);

CREATE TABLE IF NOT EXISTS recommendation_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    user_token TEXT NOT NULL,
    generation_id INTEGER,
    catalog_id TEXT NOT NULL,
    catalog_type TEXT NOT NULL,
    row_position INTEGER,
    requested_at INTEGER NOT NULL,
    item_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recommendation_deliveries_session
    ON recommendation_deliveries (session_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_recommendation_deliveries_generation_catalog
    ON recommendation_deliveries
       (generation_id, catalog_type, catalog_id, requested_at);

-- Current title state makes polling a stateful diff. This lets us tell a
-- first movie watch from a rewatch and a first series episode from a later
-- continuation without persisting any raw payloads.
CREATE TABLE IF NOT EXISTS title_state (
    user_token TEXT NOT NULL,
    content_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    episode_count INTEGER NOT NULL DEFAULT 0,
    last_watched_at INTEGER,
    rating INTEGER,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY (user_token, content_id, media_type)
);
CREATE TABLE IF NOT EXISTS title_state_syncs (
    user_token TEXT PRIMARY KEY,
    first_observed_at INTEGER NOT NULL,
    last_observed_at INTEGER NOT NULL,
    sync_count INTEGER NOT NULL DEFAULT 1
);

-- Outcome events are append-only and idempotent by event_key. Attribution is
-- kept separately so observing an outcome never rewrites history.
CREATE TABLE IF NOT EXISTS outcome_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_token TEXT NOT NULL,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    content_id TEXT,
    media_type TEXT,
    season INTEGER,
    episode INTEGER,
    previous_value INTEGER,
    current_value INTEGER,
    occurred_at INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    UNIQUE (user_token, event_key)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_user_time
    ON outcome_events (user_token, occurred_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_content
    ON outcome_events (user_token, content_id, media_type);

CREATE TABLE IF NOT EXISTS recommendation_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outcome_event_id INTEGER NOT NULL UNIQUE,
    generation_id INTEGER NOT NULL,
    generation_item_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    delivery_id INTEGER NOT NULL,
    attribution_model TEXT NOT NULL,
    lookback_seconds INTEGER NOT NULL,
    attributed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recommendation_attributions_generation
    ON recommendation_attributions (generation_id, session_id);
"""


MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN is_kid INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN kid_age INTEGER",
    "ALTER TABLE meta_cache ADD COLUMN cert TEXT",
    # Earliest date on which a legitimate home-viewing source should exist.
    # "?" means TMDB had neither a home release nor a usable fallback date.
    "ALTER TABLE meta_cache ADD COLUMN home_release_date TEXT",
    # 1 = an explicit TMDB Digital/Physical/TV date; 0 = conservative
    # old-title fallback or unknown. Fallback approvals remain recheckable.
    "ALTER TABLE meta_cache ADD COLUMN home_release_verified INTEGER",
    # birthdate anchor so kid ages advance in real time (kid_age is only the
    # age at the moment it was set)
    "ALTER TABLE users ADD COLUMN kid_birthdate TEXT",
    # snapshot of Trakt activity + active holiday at last generation, so the
    # nightly refresh can skip users with nothing new
    "ALTER TABLE users ADD COLUMN last_activity TEXT",
    "ALTER TABLE users ADD COLUMN last_holiday TEXT",
    # A catalog request is the closest server-side signal that the user opened
    # Daily Picks. The next nightly run uses it to decide whether a reshuffle is
    # worth the API work.
    "ALTER TABLE users ADD COLUMN last_served_at INTEGER",
    # Cold-start preferences. The setup UI can expose these independently;
    # defaults preserve all existing profiles.
    "ALTER TABLE users ADD COLUMN preferred_media TEXT NOT NULL DEFAULT 'balanced'",
    "ALTER TABLE users ADD COLUMN adventurousness INTEGER NOT NULL DEFAULT 30",
    # Link each replaceable serving row to its immutable build snapshot.
    "ALTER TABLE catalogs ADD COLUMN generation_id INTEGER",
    # Opt-in for the live Continue Watching / Watch History rows. Off by
    # default: they pin above every recommendation row and put the viewer's own
    # backlog on top of the surface, which is not what every profile wants.
    # Independent flags — wanting a resume row is not the same as wanting a
    # permanent record of what you watched sitting on the home screen.
    "ALTER TABLE users ADD COLUMN continue_watching_row INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN watch_history_row INTEGER NOT NULL DEFAULT 0",
    # Streaming catalogs used to be an env list of display names that
    # profile_streaming string-matched back to users — fragile, because two
    # household names can share a prefix. It is a per-user flag now; the
    # backfill below preserves whoever the env list currently names.
    "ALTER TABLE users ADD COLUMN streaming_catalogs_row INTEGER NOT NULL DEFAULT 0",
    # Asian Dramas, formerly its own shared-secret addon and container.
    "ALTER TABLE users ADD COLUMN asian_dramas_row INTEGER NOT NULL DEFAULT 0",
    # Resume position, added after the table shipped with delivered-bytes only.
    "ALTER TABLE play_history ADD COLUMN position_bytes INTEGER",
    "ALTER TABLE play_history ADD COLUMN total_bytes INTEGER",
    "ALTER TABLE play_history ADD COLUMN position_pct REAL",
    # Trakt is gone: no account to connect, so no grant to store, and the
    # watchlist it was the only source of no longer exists either. Dropped
    # rather than left NULL-able so nothing can quietly start writing to a
    # credential column again.
    "ALTER TABLE users DROP COLUMN trakt_username",
    "ALTER TABLE users DROP COLUMN access_token",
    "ALTER TABLE users DROP COLUMN refresh_token",
    "ALTER TABLE users DROP COLUMN expires_at",
    "ALTER TABLE title_state DROP COLUMN trakt_id",
    "ALTER TABLE title_state DROP COLUMN in_watchlist",
    "ALTER TABLE title_state_syncs DROP COLUMN movie_watchlist_initialized",
    "ALTER TABLE title_state_syncs DROP COLUMN series_watchlist_initialized",
    "ALTER TABLE outcome_events DROP COLUMN trakt_id",
]

# Applied before SCHEMA, unlike MIGRATIONS. `CREATE TABLE IF NOT EXISTS` would
# otherwise create an empty table under the new name, leaving every existing
# row stranded in the old one and the rename permanently failing.
RENAMES = [
    ("trakt_title_state", "title_state"),
    ("trakt_state_syncs", "title_state_syncs"),
    ("trakt_outcome_events", "outcome_events"),
]


# Serializes session inference and outcome/state upserts on the shared SQLite
# connection. Catalog generation already has a per-user lock in catalogs.py.
_ledger_lock = asyncio.Lock()

# These are the outcome classes that represent a new viewing choice. Rewatches
# and series continuations remain measurable outcomes, but are intentionally
# not treated as a new pick when summarizing sessions.
PICK_OUTCOME_TYPES = (
    "first_movie_watch",
    "first_series_episode",
)


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(config.DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    for old_name, new_name in RENAMES:
        try:
            await _conn.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")
        except aiosqlite.OperationalError:
            pass  # fresh database, or already renamed
    await _conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            await _conn.execute(stmt)
        except aiosqlite.OperationalError:
            pass  # column already exists
    # backfill birthdate for kid users created before the anchor existed
    await _conn.execute(
        "UPDATE users SET kid_birthdate = date('now', '-' || kid_age || ' years')"
        " WHERE is_kid = 1 AND kid_age IS NOT NULL AND kid_birthdate IS NULL")
    # Streaming catalogs moved from an env list of display names to a per-user
    # flag. Run once, so that later un-ticking someone in the UI is not undone
    # on the next restart by the (now vestigial) env list.
    seeded = await _conn.execute(
        "INSERT INTO storage_migrations (id, applied_at) VALUES (?,?)"
        " ON CONFLICT(id) DO NOTHING",
        ("streaming-catalogs-flag-v1", int(time.time())),
    )
    if seeded.rowcount and config.PROFILE_STREAMING_USERS:
        names = [n.strip().casefold() for n in config.PROFILE_STREAMING_USERS]
        placeholders = ",".join("?" * len(names))
        await _conn.execute(
            "UPDATE users SET streaming_catalogs_row = 1 WHERE"
            f" lower(trim(name)) IN ({placeholders})",
            names)
    # Every pre-ledger exposure was created at generation time, so none can be
    # defended as a delivered row. Clear that phantom history exactly once;
    # the marker preserves real delivery exposure across later restarts.
    migration = await _conn.execute(
        "INSERT INTO storage_migrations (id, applied_at) VALUES (?,?)"
        " ON CONFLICT(id) DO NOTHING",
        ("delivery-only-exposure-v1", int(time.time())),
    )
    if migration.rowcount:
        await _conn.execute("DELETE FROM recommendation_exposure")
    await _conn.commit()


async def close() -> None:
    if _conn:
        await _conn.close()


def conn() -> aiosqlite.Connection:
    assert _conn is not None, "db.init() not called"
    return _conn


# ── users ────────────────────────────────────────────────────────────────

async def create_user(token: str, name: str, is_kid: bool = False,
                      kid_age: int | None = None,
                      kid_birthdate: str | None = None) -> None:
    await conn().execute(
        "INSERT INTO users (token, name, created_at, is_kid, kid_age,"
        " kid_birthdate) VALUES (?,?,?,?,?,?)",
        (token, name, int(time.time()), int(is_kid), kid_age, kid_birthdate),
    )
    await conn().commit()


async def update_kid(token: str, is_kid: bool, kid_age: int | None,
                     kid_birthdate: str | None) -> None:
    """Toggle kid mode / re-anchor the age. Passing kid_age=None keeps the
    existing birthdate (so toggling off and back on remembers the age)."""
    if kid_age is None:
        await conn().execute("UPDATE users SET is_kid=? WHERE token=?",
                             (int(is_kid), token))
    else:
        await conn().execute(
            "UPDATE users SET is_kid=?, kid_age=?, kid_birthdate=? WHERE token=?",
            (int(is_kid), kid_age, kid_birthdate, token))
    await conn().commit()


WATCHING_ROW_COLUMNS = ("continue_watching_row", "watch_history_row",
                        "streaming_catalogs_row", "asian_dramas_row")


async def update_watching_row(token: str, column: str, enabled: bool) -> None:
    """Toggle one opt-in row family for one user."""
    if column not in WATCHING_ROW_COLUMNS:
        raise ValueError(f"unknown watching row: {column}")
    # The column name is validated against a fixed allowlist above, never
    # interpolated from caller input.
    await conn().execute(f"UPDATE users SET {column}=? WHERE token=?",
                         (int(enabled), token))
    await conn().commit()


async def record_play(event: dict) -> None:
    """Persist one play. Idempotent on (viewer, content, timestamp) so a retry
    or a duplicate telemetry sink cannot inflate someone's history."""
    await conn().execute(
        "INSERT INTO play_history (viewer_key, content_id, imdb_id, media_type,"
        " season, episode, played_at, seconds, megabytes, watched_pct,"
        " position_bytes, total_bytes, position_pct, picker)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(viewer_key, content_id, played_at) DO NOTHING",
        (event["viewer_key"], event["content_id"], event["imdb_id"],
         event["media_type"], event.get("season"), event.get("episode"),
         event["played_at"], event.get("seconds") or 0.0,
         event.get("megabytes") or 0.0, event.get("watched_pct"),
         event.get("position_bytes"), event.get("total_bytes"),
         event.get("position_pct"), event.get("picker") or ""),
    )
    await conn().commit()


async def play_history(viewer_key: str, limit: int = 500) -> list[dict]:
    """Most recent plays first — the chronological log."""
    async with conn().execute(
            "SELECT * FROM play_history WHERE viewer_key=?"
            " ORDER BY played_at DESC LIMIT ?", (viewer_key, limit)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def played_titles(viewer_key: str, min_pct: float = 0.0) -> list[dict]:
    """One row per title, rolled up — the shape a taste profile wants.

    `plays` counts distinct play events, which for a series is roughly episodes
    watched. `best_pct` is the furthest any single play got, so a title watched
    once to the end outranks one abandoned five times.
    """
    async with conn().execute(
            "SELECT imdb_id, media_type, COUNT(*) AS plays,"
            " MAX(COALESCE(watched_pct, 0)) AS best_pct,"
            " MAX(played_at) AS last_played_at,"
            " SUM(COALESCE(seconds, 0)) AS total_seconds"
            " FROM play_history WHERE viewer_key=?"
            " GROUP BY imdb_id, media_type"
            " HAVING best_pct >= ?"
            " ORDER BY last_played_at DESC", (viewer_key, min_pct)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def play_history_counts() -> dict[str, int]:
    """{viewer_key: play count} — for the admin view of coverage."""
    async with conn().execute(
            "SELECT viewer_key, COUNT(*) AS n FROM play_history"
            " GROUP BY viewer_key") as cur:
        return {r["viewer_key"]: r["n"] for r in await cur.fetchall()}


async def users_with_row(column: str) -> list[dict]:
    """Every user opted into one row family."""
    if column not in WATCHING_ROW_COLUMNS:
        raise ValueError(f"unknown watching row: {column}")
    async with conn().execute(
            f"SELECT * FROM users WHERE {column}=1 ORDER BY created_at") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_preferences(token: str, preferred_media: str,
                             adventurousness: int) -> None:
    """Update lightweight cold-start controls used by ranking policies."""
    if preferred_media not in {"balanced", "movies", "series"}:
        raise ValueError("preferred_media must be balanced, movies, or series")
    if not 0 <= adventurousness <= 100:
        raise ValueError("adventurousness must be between 0 and 100")
    await conn().execute(
        "UPDATE users SET preferred_media=?, adventurousness=? WHERE token=?",
        (preferred_media, adventurousness, token),
    )
    await conn().commit()


async def get_user(token: str) -> dict | None:
    async with conn().execute("SELECT * FROM users WHERE token = ?", (token,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def all_users() -> list[dict]:
    async with conn().execute("SELECT * FROM users ORDER BY created_at") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def delete_user(token: str) -> None:
    # Delete private measurement data as part of the same account-erasure
    # operation. Generations are immutable during normal operation, not a
    # reason to retain data after its owner disconnects the addon.
    for table in (
        "recommendation_attributions",
        "recommendation_deliveries",
        "recommendation_sessions",
        "recommendation_generation_items",
        "recommendation_generations",
        "outcome_events",
        "title_state",
        "title_state_syncs",
    ):
        if table == "recommendation_attributions":
            await conn().execute(
                "DELETE FROM recommendation_attributions WHERE outcome_event_id IN"
                " (SELECT id FROM outcome_events WHERE user_token=?)",
                (token,),
            )
        else:
            await conn().execute(
                f"DELETE FROM {table} WHERE user_token=?", (token,))
    await conn().execute("DELETE FROM catalogs WHERE user_token=?", (token,))
    await conn().execute(
        "DELETE FROM recommendation_exposure WHERE user_token=?", (token,))
    await conn().execute("DELETE FROM users WHERE token=?", (token,))
    await conn().commit()


async def update_tokens(token: str, access_token: str, refresh_token: str, expires_at: int) -> None:
    await conn().execute(
        "UPDATE users SET access_token=?, refresh_token=?, expires_at=? WHERE token=?",
        (access_token, refresh_token, expires_at, token),
    )
    await conn().commit()


async def mark_generated(token: str, error: str | None = None,
                         last_activity: str | None = None,
                         last_holiday: str | None = None) -> None:
    await conn().execute(
        "UPDATE users SET last_generated_at=?, last_error=?, last_activity=?,"
        " last_holiday=? WHERE token=?",
        (int(time.time()), error, last_activity, last_holiday, token),
    )
    await conn().commit()


async def mark_served(token: str) -> None:
    """Record that at least one personalized catalog was requested.

    Nuvio normally requests many rows together. Debouncing this write to once
    per hour preserves the signal without turning every row fetch into a WAL
    write.
    """
    now = int(time.time())
    await conn().execute(
        "UPDATE users SET last_served_at=? WHERE token=?"
        " AND (last_served_at IS NULL OR last_served_at < ?"
        " OR last_served_at < COALESCE(last_generated_at, 0))",
        (now, token, now - 3600),
    )
    await conn().commit()


# ── catalogs + immutable generation ledger ───────────────────────────────


def _normal_media_type(value: str | None) -> str | None:
    if not value:
        return None
    value = value.lower()
    if value in {"movie", "movies"}:
        return "movie"
    if value in {"show", "shows", "tv", "series"}:
        return "series"
    return value


def _measurement_value(item: dict, catalog: dict, key: str) -> Any:
    """Read optional ranking provenance without imposing it on served metas.

    Generators may place fields under a ``measurement`` mapping on the item or
    catalog. Top-level values are also accepted for easy incremental adoption.
    Existing catalogs contain neither and simply snapshot with null provenance.
    """
    item_measurement = item.get("measurement") or item.get("_measurement")
    catalog_measurement = catalog.get("measurement") or catalog.get("_measurement")
    if isinstance(item_measurement, dict) and key in item_measurement:
        return item_measurement[key]
    if key in item:
        return item[key]
    if isinstance(catalog_measurement, dict) and key in catalog_measurement:
        return catalog_measurement[key]
    return catalog.get(key)


def _served_meta(meta: dict) -> dict:
    """Remove ledger-only annotations from protocol metadata responses."""
    return {key: value for key, value in meta.items()
            if key not in {"measurement", "_measurement"}}


async def replace_catalogs(
    user_token: str,
    catalogs: list[dict],
    *,
    policy_id: str = "daily-picks-v1",
    variant: str | None = None,
    trigger: str | None = None,
    generation_metadata: dict | None = None,
    generated_at: int | None = None,
) -> int:
    """Atomically replace the serving cache and append its exact snapshot.

    ``catalogs`` keeps its longstanding shape of
    ``[{id, type, name, metas: [...]}]``. Optional ``measurement`` mappings on
    catalogs/items can supply ``strategy``, ``candidate_source``,
    ``seed_content_id``, ``rank_score``, and ``score_components``. The return
    value is the immutable generation id; existing callers may ignore it.
    """
    now = int(time.time()) if generated_at is None else int(generated_at)
    item_count = sum(len(cat["metas"]) for cat in catalogs)
    try:
        cur = await conn().execute(
            "INSERT INTO recommendation_generations"
            " (user_token, generated_at, policy_id, variant, trigger, metadata,"
            " catalog_count, item_count) VALUES (?,?,?,?,?,?,?,?)",
            (user_token, now, policy_id, variant, trigger,
             json.dumps(generation_metadata or {}, sort_keys=True),
             len(catalogs), item_count),
        )
        generation_id = int(cur.lastrowid)

        await conn().execute("DELETE FROM catalogs WHERE user_token=?", (user_token,))
        snapshot_items: list[tuple] = []
        for row_pos, cat in enumerate(catalogs):
            served_metas = [_served_meta(meta) for meta in cat["metas"]]
            metas_json = json.dumps(served_metas)
            await conn().execute(
                "INSERT INTO catalogs"
                " (user_token, position, catalog_id, type, name, metas, generated_at,"
                " generation_id) VALUES (?,?,?,?,?,?,?,?)",
                (user_token, row_pos, cat["id"], cat["type"], cat["name"],
                 metas_json, now, generation_id),
            )
            for item_pos, (meta, served_meta) in enumerate(
                    zip(cat["metas"], served_metas)):
                score_components = _measurement_value(
                    meta, cat, "score_components")
                snapshot_items.append((
                    generation_id, user_token, cat["id"], cat["type"],
                    cat["name"], row_pos, item_pos, meta.get("id"),
                    _normal_media_type(meta.get("type") or cat["type"]),
                    meta.get("name") or meta.get("title"),
                    _measurement_value(meta, cat, "strategy"),
                    _measurement_value(meta, cat, "candidate_source"),
                    _measurement_value(meta, cat, "seed_content_id"),
                    _measurement_value(meta, cat, "rank_score"),
                    json.dumps(score_components, sort_keys=True)
                    if score_components is not None else None,
                    json.dumps(served_meta),
                ))
        if snapshot_items:
            await conn().executemany(
                "INSERT INTO recommendation_generation_items"
                " (generation_id, user_token, catalog_id, catalog_type, catalog_name,"
                " row_position, item_position, content_id, media_type, title, strategy,"
                " candidate_source, seed_content_id, rank_score, score_components, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                snapshot_items,
            )

        # A generated placement is not an exposure. Serving records depth-aware
        # exposure separately in record_catalog_delivery().
        await conn().execute(
            "DELETE FROM recommendation_exposure WHERE last_shown_at < ?",
            (now - 90 * 86400,),
        )
        await conn().commit()
        return generation_id
    except Exception:
        await conn().rollback()
        raise


async def get_recently_shown(user_token: str, days: int = 14) -> dict[str, int]:
    """IMDb id -> latest depth-weighted catalog-delivery time.

    Despite the legacy table/function name, generation alone does not enter
    this result. That avoids rotating titles the client merely had available
    but never requested during a browsing session.
    """
    cutoff = int(time.time()) - days * 86400
    async with conn().execute(
        "SELECT imdb_id, last_shown_at FROM recommendation_exposure"
        " WHERE user_token=? AND last_shown_at>=?",
        (user_token, cutoff),
    ) as cur:
        recent = {r["imdb_id"]: r["last_shown_at"] for r in await cur.fetchall()}
    return recent


async def get_catalog_defs(user_token: str) -> list[dict]:
    async with conn().execute(
        "SELECT catalog_id, type, name FROM catalogs WHERE user_token=? ORDER BY position",
        (user_token,),
    ) as cur:
        return [{"id": r["catalog_id"], "type": r["type"], "name": r["name"]}
                for r in await cur.fetchall()]


async def get_catalog_metas(user_token: str, ctype: str, catalog_id: str) -> list | None:
    async with conn().execute(
        "SELECT metas FROM catalogs WHERE user_token=? AND type=? AND catalog_id=?",
        (user_token, ctype, catalog_id),
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row["metas"]) if row else None


async def get_generation(generation_id: int) -> dict | None:
    """Return one build record without exposing the user's OAuth tokens."""
    async with conn().execute(
        "SELECT * FROM recommendation_generations WHERE id=?", (generation_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result["metadata"])
    return result


async def get_generation_items(generation_id: int) -> list[dict]:
    async with conn().execute(
        "SELECT * FROM recommendation_generation_items WHERE generation_id=?"
        " ORDER BY row_position, item_position",
        (generation_id,),
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    for row in rows:
        row["meta"] = json.loads(row["meta"])
        row["score_components"] = (
            json.loads(row["score_components"])
            if row["score_components"] is not None else None
        )
    return rows


async def latest_generation(user_token: str) -> dict | None:
    async with conn().execute(
        "SELECT * FROM recommendation_generations WHERE user_token=?"
        " ORDER BY id DESC LIMIT 1",
        (user_token,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    result = dict(row)
    result["metadata"] = json.loads(result["metadata"])
    return result


# ── inferred recommendation sessions + row deliveries ───────────────────

async def record_catalog_delivery(
    user_token: str,
    ctype: str,
    catalog_id: str,
    *,
    requested_at: int | None = None,
    session_gap_seconds: int = 30 * 60,
) -> dict:
    """Record a served row and infer the surrounding browsing session.

    Requests for the same generation separated by less than ``session_gap``
    share a session. Each HTTP response remains a separate delivery event;
    callers must not describe those items as viewed impressions.
    """
    if session_gap_seconds < 1:
        raise ValueError("session_gap_seconds must be positive")
    at = int(time.time()) if requested_at is None else int(requested_at)
    async with _ledger_lock:
        async with conn().execute(
            "SELECT generation_id, position, metas FROM catalogs"
            " WHERE user_token=? AND type=? AND catalog_id=?",
            (user_token, ctype, catalog_id),
        ) as cur:
            catalog = await cur.fetchone()
        if not catalog:
            raise ValueError("catalog is not present in the user's current generation")

        generation_id = catalog["generation_id"]
        async with conn().execute(
            "SELECT * FROM recommendation_sessions WHERE user_token=?"
            " ORDER BY last_delivery_at DESC, id DESC LIMIT 1",
            (user_token,),
        ) as cur:
            previous = await cur.fetchone()
        same_generation = previous and previous["generation_id"] == generation_id
        continues = (
            same_generation
            and previous["started_at"] <= at
            and previous["last_delivery_at"] >= at - session_gap_seconds
        )
        if continues:
            session_id = int(previous["id"])
            await conn().execute(
                "UPDATE recommendation_sessions SET"
                " last_delivery_at=MAX(last_delivery_at, ?),"
                " request_count=request_count + 1 WHERE id=?",
                (at, session_id),
            )
            is_new_session = False
        else:
            cur = await conn().execute(
                "INSERT INTO recommendation_sessions"
                " (user_token, generation_id, started_at, last_delivery_at, request_count)"
                " VALUES (?,?,?,?,1)",
                (user_token, generation_id, at, at),
            )
            session_id = int(cur.lastrowid)
            is_new_session = True

        try:
            item_count = len(json.loads(catalog["metas"]))
        except (TypeError, json.JSONDecodeError):
            item_count = 0
        cur = await conn().execute(
            "INSERT INTO recommendation_deliveries"
            " (session_id, user_token, generation_id, catalog_id, catalog_type,"
            " row_position, requested_at, item_count) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, user_token, generation_id, catalog_id, ctype,
             catalog["position"], at, item_count),
        )
        delivery_id = int(cur.lastrowid)

        # Nuvio prefetches rows, so depth tempers the freshness penalty: rows
        # 0-5 count strongly, 6-11 weakly, and deeper rows do not count at all.
        # Encoding the weak signal as an older effective timestamp preserves
        # compatibility with the generator's existing timestamp-only API.
        row_position = catalog["position"]
        effective_exposure_at = (
            at if row_position < 6 else at - 5 * 86400 if row_position < 12 else None
        )
        if effective_exposure_at is not None:
            try:
                delivered_ids = {
                    meta.get("id") for meta in json.loads(catalog["metas"])
                    if meta.get("id")
                }
            except (TypeError, json.JSONDecodeError):
                delivered_ids = set()
            if delivered_ids:
                await conn().executemany(
                    "INSERT INTO recommendation_exposure"
                    " (user_token, imdb_id, last_shown_at, show_count)"
                    " VALUES (?,?,?,1)"
                    " ON CONFLICT(user_token, imdb_id) DO UPDATE SET"
                    " last_shown_at=MAX(recommendation_exposure.last_shown_at,"
                    " excluded.last_shown_at),"
                    " show_count=recommendation_exposure.show_count + 1",
                    ((user_token, content_id, effective_exposure_at)
                     for content_id in delivered_ids),
                )
            await conn().execute(
                "DELETE FROM recommendation_exposure WHERE last_shown_at < ?",
                (at - 90 * 86400,),
            )

        # Preserve the existing refresh-queue behavior while making this the
        # one integration call the serving route needs.
        await conn().execute(
            "UPDATE users SET last_served_at=? WHERE token=?"
            " AND (last_served_at IS NULL OR last_served_at < ?"
            " OR last_served_at < COALESCE(last_generated_at, 0))",
            (at, user_token, at - 3600),
        )
        await conn().commit()
    return {
        "session_id": session_id,
        "delivery_id": delivery_id,
        "generation_id": generation_id,
        "is_new_session": is_new_session,
    }


async def get_session(session_id: int) -> dict | None:
    async with conn().execute(
        "SELECT * FROM recommendation_sessions WHERE id=?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_session_deliveries(session_id: int) -> list[dict]:
    async with conn().execute(
        "SELECT * FROM recommendation_deliveries WHERE session_id=?"
        " ORDER BY requested_at, id",
        (session_id,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


# ── Trakt state, outcomes, and exact recommendation attribution ─────────

def _epoch(value: int | float | str | datetime | None,
           default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("a timestamp is required")
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _outcome_key(event: dict) -> str:
    explicit = event.get("event_key") or event.get("event_id")
    if explicit is not None:
        return f"{event['event_type']}:{explicit}"
    identity = {
        "event_type": event["event_type"],
        "content_id": event.get("content_id") or event.get("imdb_id"),
        "media_type": _normal_media_type(event.get("media_type")),
        "season": event.get("season"),
        "episode": event.get("episode"),
        "previous_value": event.get("previous_value"),
        "current_value": event.get("current_value"),
        "occurred_at": _epoch(event.get("occurred_at")),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _insert_outcome_no_commit(user_token: str, event: dict,
                                    observed_at: int) -> int:
    content_id = event.get("content_id") or event.get("imdb_id")
    if not content_id:
        raise ValueError("an outcome needs content_id/imdb_id")
    if not event.get("event_type"):
        raise ValueError("an outcome needs event_type")
    occurred_at = _epoch(event.get("occurred_at"), observed_at)
    event_key = _outcome_key({**event, "occurred_at": occurred_at})
    await conn().execute(
        "INSERT INTO outcome_events"
        " (user_token, event_key, event_type, content_id, media_type,"
        " season, episode, previous_value, current_value, occurred_at, observed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(user_token, event_key) DO NOTHING",
        (user_token, event_key, event["event_type"], content_id,
         _normal_media_type(event.get("media_type")),
         event.get("season"), event.get("episode"), event.get("previous_value"),
         event.get("current_value"), occurred_at, observed_at),
    )
    async with conn().execute(
        "SELECT id FROM outcome_events WHERE user_token=? AND event_key=?",
        (user_token, event_key),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row["id"])


async def record_outcome(
    user_token: str,
    event_type: str,
    content_id: str | None,
    media_type: str | None,
    occurred_at: int | float | str | datetime,
    *,
    event_key: str | None = None,
    event_id: str | int | None = None,
    season: int | None = None,
    episode: int | None = None,
    previous_value: int | None = None,
    current_value: int | None = None,
    observed_at: int | None = None,
) -> int:
    """Append one idempotent, payload-free outcome event."""
    event = {
        "event_type": event_type,
        "content_id": content_id,
        "media_type": media_type,
        "occurred_at": occurred_at,
        "event_key": event_key,
        "event_id": event_id,
        "season": season,
        "episode": episode,
        "previous_value": previous_value,
        "current_value": current_value,
    }
    return (await record_outcomes(
        user_token, [event], observed_at=observed_at))[0]


async def record_outcomes(
    user_token: str,
    events: list[dict],
    *,
    observed_at: int | None = None,
) -> list[int]:
    """Append a batch of normalized events, returning ids in input order."""
    observed = int(time.time()) if observed_at is None else int(observed_at)
    async with _ledger_lock:
        try:
            ids = [await _insert_outcome_no_commit(user_token, event, observed)
                   for event in events]
            await conn().commit()
            return ids
        except Exception:
            await conn().rollback()
            raise


def _content_id(item: dict) -> str | None:
    """The IMDb id, which is what every surface here addresses titles by."""
    return (item.get("ids") or {}).get("imdb")


def _watched_states(watched_movies: list[dict], watched_shows: list[dict]) -> dict:
    states: dict[tuple[str, str], dict] = {}
    for entry in watched_movies:
        item = entry.get("movie") or {}
        content_id = _content_id(item)
        if not content_id:
            continue
        states[(content_id, "movie")] = {
            "content_id": content_id,
            "media_type": "movie",
            "play_count": max(0, int(entry.get("plays") or 0)),
            "episode_count": 0,
            "last_watched_at": _epoch(entry.get("last_watched_at"), 0) or None,
        }
    for entry in watched_shows:
        item = entry.get("show") or {}
        content_id = _content_id(item)
        if not content_id:
            continue
        episode_count = 0
        episode_plays = 0
        last_watched = _epoch(entry.get("last_watched_at"), 0)
        for season in entry.get("seasons") or []:
            for episode in season.get("episodes") or []:
                plays = max(0, int(episode.get("plays") or 0))
                if plays:
                    episode_count += 1
                    episode_plays += plays
                last_watched = max(
                    last_watched,
                    _epoch(episode.get("last_watched_at"), 0),
                )
        states[(content_id, "series")] = {
            "content_id": content_id,
            "media_type": "series",
            "play_count": episode_plays,
            "episode_count": episode_count,
            "last_watched_at": last_watched or None,
        }
    return states


async def upsert_title_state_and_record_outcomes(
    user_token: str,
    watched_movies: list[dict],
    watched_shows: list[dict],
    *,
    observed_at: int | None = None,
) -> list[dict]:
    """Diff watch snapshots, persist state, and append semantic outcomes.

    The first call establishes a baseline and emits no outcomes. Later calls
    distinguish ``first_movie_watch``, ``movie_rewatch``,
    ``first_series_episode`` and ``series_continuation``.
    """
    observed = int(time.time()) if observed_at is None else int(observed_at)
    states = _watched_states(watched_movies, watched_shows)

    async with _ledger_lock:
        try:
            async with conn().execute(
                "SELECT * FROM title_state WHERE user_token=?", (user_token,)
            ) as cur:
                prior_rows = await cur.fetchall()
            prior = {(row["content_id"], row["media_type"]): dict(row)
                     for row in prior_rows}
            async with conn().execute(
                "SELECT * FROM title_state_syncs WHERE user_token=?",
                (user_token,),
            ) as cur:
                sync_row = await cur.fetchone()
            is_baseline = sync_row is None

            emitted: list[dict] = []
            for key, state in states.items():
                old = prior.get(key)
                old_plays = int(old["play_count"]) if old else 0
                old_episodes = int(old["episode_count"]) if old else 0
                occurred = state["last_watched_at"] or observed
                event: dict | None = None
                if not is_baseline and state["media_type"] == "movie" \
                        and state["play_count"] > old_plays:
                    event = {
                        "event_type": "first_movie_watch" if old_plays == 0
                        else "movie_rewatch",
                        "previous_value": old_plays,
                        "current_value": state["play_count"],
                    }
                elif not is_baseline and state["media_type"] == "series" \
                        and state["episode_count"] > old_episodes:
                    event = {
                        "event_type": "first_series_episode" if old_episodes == 0
                        else "series_continuation",
                        "previous_value": old_episodes,
                        "current_value": state["episode_count"],
                    }
                if event:
                    event.update({
                        "content_id": state["content_id"],
                        "media_type": state["media_type"],
                        "occurred_at": occurred,
                        "event_key": (
                            f"state:{state['media_type']}:{state['content_id']}:"
                            f"{event['event_type']}:{event['current_value']}"
                        ),
                    })
                    event_id = await _insert_outcome_no_commit(
                        user_token, event, observed)
                    emitted.append({"id": event_id, **event})

                await conn().execute(
                    "INSERT INTO title_state"
                    " (user_token, content_id, media_type, play_count,"
                    " episode_count, last_watched_at, rating, observed_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(user_token, content_id, media_type) DO UPDATE SET"
                    " play_count=excluded.play_count,"
                    " episode_count=excluded.episode_count,"
                    " last_watched_at=COALESCE(excluded.last_watched_at,"
                    " title_state.last_watched_at),"
                    " rating=COALESCE(excluded.rating, title_state.rating),"
                    " observed_at=excluded.observed_at",
                    (user_token, state["content_id"], state["media_type"],
                     state["play_count"], state["episode_count"],
                     state["last_watched_at"], None, observed),
                )

            await conn().execute(
                "INSERT INTO title_state_syncs"
                " (user_token, first_observed_at, last_observed_at, sync_count)"
                " VALUES (?,?,?,1) ON CONFLICT(user_token) DO UPDATE SET"
                " last_observed_at=excluded.last_observed_at,"
                " sync_count=title_state_syncs.sync_count + 1",
                (user_token, observed, observed),
            )
            await conn().commit()
            return emitted
        except Exception:
            await conn().rollback()
            raise


async def attribute_outcomes(
    user_token: str,
    *,
    lookback_seconds: int = 72 * 3600,
    as_of: int | None = None,
    event_types: tuple[str, ...] | None = None,
    attribution_model: str = "last_delivered_exact",
) -> int:
    """Attribute exact-title outcomes to the latest eligible row delivery.

    This deliberately refuses fuzzy title matching and only considers a row
    the server actually delivered before the outcome. It returns the number of
    newly attributed events; outcome events themselves remain unchanged.
    """
    if lookback_seconds < 1:
        raise ValueError("lookback_seconds must be positive")
    now = int(time.time()) if as_of is None else int(as_of)
    params: list[Any] = [user_token, now, lookback_seconds]
    type_clause = ""
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        type_clause = f" AND o.event_type IN ({placeholders})"
        params.extend(event_types)
    query = (
        "SELECT o.id AS outcome_event_id, gi.id AS generation_item_id,"
        " gi.generation_id, d.session_id, d.id AS delivery_id, d.requested_at"
        " FROM outcome_events o"
        " JOIN recommendation_generation_items gi"
        "   ON gi.user_token=o.user_token AND gi.content_id=o.content_id"
        "  AND (gi.media_type=o.media_type OR gi.media_type IS NULL"
        "       OR o.media_type IS NULL)"
        " JOIN recommendation_deliveries d"
        "   ON d.generation_id=gi.generation_id"
        "  AND d.catalog_id=gi.catalog_id AND d.catalog_type=gi.catalog_type"
        " LEFT JOIN recommendation_attributions a ON a.outcome_event_id=o.id"
        " WHERE o.user_token=? AND a.id IS NULL AND o.content_id IS NOT NULL"
        " AND o.observed_at<=? AND d.requested_at<=o.occurred_at"
        " AND o.occurred_at-d.requested_at<=?" + type_clause +
        " ORDER BY o.id, d.requested_at DESC, d.id DESC, gi.item_position"
    )
    async with _ledger_lock:
        async with conn().execute(query, params) as cur:
            candidates = await cur.fetchall()
        chosen: dict[int, Any] = {}
        for row in candidates:
            chosen.setdefault(int(row["outcome_event_id"]), row)
        for row in chosen.values():
            await conn().execute(
                "INSERT INTO recommendation_attributions"
                " (outcome_event_id, generation_id, generation_item_id, session_id,"
                " delivery_id, attribution_model, lookback_seconds, attributed_at)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(outcome_event_id) DO NOTHING",
                (row["outcome_event_id"], row["generation_id"],
                 row["generation_item_id"], row["session_id"], row["delivery_id"],
                 attribution_model, lookback_seconds, now),
            )
        await conn().commit()
    return len(chosen)


async def get_outcomes(user_token: str, *,
                       unattributed_only: bool = False) -> list[dict]:
    where = " AND a.id IS NULL" if unattributed_only else ""
    async with conn().execute(
        "SELECT o.*, a.generation_id AS attributed_generation_id,"
        " a.generation_item_id AS attributed_generation_item_id,"
        " a.session_id AS attributed_session_id,"
        " a.delivery_id AS attributed_delivery_id,"
        " a.attribution_model, a.lookback_seconds, a.attributed_at"
        " FROM outcome_events o LEFT JOIN recommendation_attributions a"
        " ON a.outcome_event_id=o.id WHERE o.user_token=?" + where +
        " ORDER BY o.occurred_at, o.id",
        (user_token,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def get_recommendation_summary(
    user_token: str,
    window_days: int = 30,
    *,
    as_of: int | None = None,
) -> dict:
    """Return privacy-safe aggregate recommendation measurements.

    All counts use the inclusive UTC epoch window ``[window_start, as_of]``.
    Session-related counts use sessions that started in that window;
    deliveries and attributed outcomes must also occur inside it. A winning
    session is one associated through exact-title attribution with at least one
    new-pick outcome in :data:`PICK_OUTCOME_TYPES`; continuations and rewatches
    remain in ``outcome_events``/``attributed_outcomes`` but do not make a
    session a winner. ``assisted_pick_rate`` is ``None`` when there are no
    sessions, otherwise ``winning_sessions / sessions``. This is an observed
    association metric, not an estimate of recommendation causality.

    No title, account token, external content id, or session id is returned.
    """
    if isinstance(window_days, bool) or not isinstance(window_days, int) \
            or window_days < 1:
        raise ValueError("window_days must be a positive integer")
    end = int(time.time()) if as_of is None else int(as_of)
    start = end - window_days * 86400
    pick_placeholders = ",".join("?" for _ in PICK_OUTCOME_TYPES)
    query = (
        "WITH"
        " window_generations AS ("
        "   SELECT id FROM recommendation_generations"
        "   WHERE user_token=? AND generated_at>=? AND generated_at<=?"
        " ),"
        " window_sessions AS ("
        "   SELECT id FROM recommendation_sessions"
        "   WHERE user_token=? AND started_at>=? AND started_at<=?"
        " ),"
        " window_deliveries AS ("
        "   SELECT d.id FROM recommendation_deliveries d"
        "   JOIN window_sessions s ON s.id=d.session_id"
        "   WHERE d.user_token=? AND d.requested_at>=? AND d.requested_at<=?"
        " ),"
        " window_outcomes AS ("
        "   SELECT id, event_type FROM outcome_events"
        "   WHERE user_token=? AND occurred_at>=? AND occurred_at<=?"
        " ),"
        " window_attributions AS ("
        "   SELECT a.outcome_event_id, a.session_id, o.event_type"
        "   FROM recommendation_attributions a"
        "   JOIN window_outcomes o ON o.id=a.outcome_event_id"
        "   JOIN window_sessions s ON s.id=a.session_id"
        " )"
        " SELECT"
        "  (SELECT COUNT(*) FROM window_generations) AS generations,"
        "  (SELECT COUNT(*) FROM window_sessions) AS sessions,"
        "  (SELECT COUNT(*) FROM window_deliveries) AS delivered_rows,"
        "  (SELECT COUNT(*) FROM window_outcomes) AS outcome_events,"
        "  (SELECT COUNT(*) FROM window_attributions) AS attributed_outcomes,"
        "  (SELECT COUNT(DISTINCT session_id) FROM window_attributions"
        f"   WHERE event_type IN ({pick_placeholders})) AS winning_sessions"
    )
    params: tuple[Any, ...] = (
        user_token, start, end,
        user_token, start, end,
        user_token, start, end,
        user_token, start, end,
        *PICK_OUTCOME_TYPES,
    )
    async with conn().execute(query, params) as cur:
        row = await cur.fetchone()
    assert row is not None
    sessions = int(row["sessions"])
    winning_sessions = int(row["winning_sessions"])
    return {
        "window_days": window_days,
        "window_start": start,
        "as_of": end,
        "generations": int(row["generations"]),
        "sessions": sessions,
        "delivered_rows": int(row["delivered_rows"]),
        "outcome_events": int(row["outcome_events"]),
        "attributed_outcomes": int(row["attributed_outcomes"]),
        "winning_sessions": winning_sessions,
        "assisted_pick_rate": (
            winning_sessions / sessions if sessions else None
        ),
    }


# ── meta cache (shared, content metadata only — nothing user-specific) ──

async def cache_get_meta(tmdb_id: int, media_type: str) -> dict | None:
    async with conn().execute(
        "SELECT imdb_id, meta, cert, home_release_date, home_release_verified,"
        " updated_at"
        " FROM meta_cache WHERE tmdb_id=? AND media_type=?",
        (tmdb_id, media_type),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {"imdb_id": row["imdb_id"], "cert": row["cert"],
            "home_release_date": row["home_release_date"],
            "home_release_verified": row["home_release_verified"],
            "updated_at": row["updated_at"],
            "meta": json.loads(row["meta"]) if row["meta"] else None}


async def cache_put_meta(tmdb_id: int, media_type: str, imdb_id: str | None,
                         meta: dict | None, cert: str | None = None,
                         home_release_date: str | None = None,
                         home_release_verified: bool | None = None) -> None:
    await conn().execute(
        "INSERT OR REPLACE INTO meta_cache"
        " (tmdb_id, media_type, imdb_id, meta, cert, home_release_date,"
        " home_release_verified, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (tmdb_id, media_type, imdb_id, json.dumps(meta) if meta else None, cert,
         home_release_date,
         None if home_release_verified is None else int(home_release_verified),
         int(time.time())),
    )
    await conn().commit()
