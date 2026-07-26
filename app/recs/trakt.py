"""Trakt API client. Every call that touches user data takes that user's
access token explicitly — there is no module-level user state."""

import logging
import time
from typing import Any

import httpx

from app.recs import config, db

logger = logging.getLogger("nuvio-recs")

BASE = "https://api.trakt.tv"
SYNC_PAGE_LIMIT = 250
# Kept as an alias for callers/tests that imported the old public constant.
WATCHED_PAGE_LIMIT = SYNC_PAGE_LIMIT
# /sync/playback is a small working set (what you have not finished), not a
# paginated archive, so one request is the whole answer.
PLAYBACK_LIMIT = 100

_client = httpx.AsyncClient(
    base_url=BASE,
    timeout=30,
    headers={
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": config.TRAKT_CLIENT_ID,
    },
)


def _auth(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


# ── OAuth device flow ────────────────────────────────────────────────────

async def create_device_code() -> dict[str, Any]:
    r = await _client.post("/oauth/device/code", json={"client_id": config.TRAKT_CLIENT_ID})
    r.raise_for_status()
    return r.json()


async def poll_device_token(device_code: str) -> dict[str, Any] | None:
    """Returns token payload once the user has approved, None while pending."""
    r = await _client.post(
        "/oauth/device/token",
        json={
            "code": device_code,
            "client_id": config.TRAKT_CLIENT_ID,
            "client_secret": config.TRAKT_CLIENT_SECRET,
        },
    )
    if r.status_code == 200:
        return r.json()
    if r.status_code == 400:  # authorization pending
        return None
    r.raise_for_status()
    return None


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    r = await _client.post(
        "/oauth/token",
        json={
            "refresh_token": refresh_token,
            "client_id": config.TRAKT_CLIENT_ID,
            "client_secret": config.TRAKT_CLIENT_SECRET,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        },
    )
    r.raise_for_status()
    return r.json()


async def ensure_fresh_token(user: dict) -> str:
    """Return a valid access token for this user, refreshing and persisting
    if it expires within a day."""
    if user["expires_at"] - time.time() > 86400:
        return user["access_token"]
    tokens = await refresh_access_token(user["refresh_token"])
    expires_at = int(time.time()) + int(tokens.get("expires_in", 7776000))
    await db.update_tokens(user["token"], tokens["access_token"],
                           tokens["refresh_token"], expires_at)
    user["access_token"] = tokens["access_token"]
    return tokens["access_token"]


# ── user data ────────────────────────────────────────────────────────────

async def last_activities(access_token: str) -> dict:
    """Timestamps of the user's most recent watches/ratings/watchlist changes.
    One cheap call that tells us whether anything changed since last refresh."""
    r = await _client.get("/sync/last_activities", headers=_auth(access_token))
    r.raise_for_status()
    return r.json()


def latest_activity(acts: dict) -> str:
    """Max ISO timestamp across all activity types (ISO strings sort safely)."""
    latest = ""
    for section in acts.values():
        if isinstance(section, dict):
            for key, val in section.items():
                if key.endswith("_at") and isinstance(val, str) and val > latest:
                    latest = val
    return latest


async def get_username(access_token: str) -> str | None:
    r = await _client.get("/users/settings", headers=_auth(access_token))
    if r.status_code != 200:
        return None
    return (r.json().get("user") or {}).get("username")


async def _get(path: str, access_token: str, params: dict | None = None) -> Any:
    r = await _client.get(path, headers=_auth(access_token), params=params or {})
    r.raise_for_status()
    return r.json()


async def _get_sync_pages(path: str, access_token: str,
                          params: dict | None = None) -> list[dict]:
    """Load a complete list-valued sync endpoint.

    Trakt can return fewer items than the requested limit for expensive
    response modes, so a short page is not an end condition. An empty page is
    the authoritative terminator. The repeated-page guard also keeps this
    finite when a sync endpoint or proxy ignores ``page`` and returns the
    same unpaginated response each time.

    ``params`` contains endpoint-specific options such as ``extended=full``;
    pagination values are owned here so callers cannot accidentally override
    them and truncate a user's snapshot.
    """
    items: list[dict] = []
    previous_page: list[dict] | None = None
    base_params = dict(params or {})
    base_params.pop("page", None)
    base_params.pop("limit", None)
    page = 1
    while True:
        response = await _client.get(
            path,
            headers=_auth(access_token),
            params={**base_params, "page": page, "limit": SYNC_PAGE_LIMIT},
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            actual = type(batch).__name__
            raise TypeError(
                f"Trakt sync endpoint {path} returned {actual}, not a list")
        if not batch or batch == previous_page:
            break
        items.extend(batch)
        previous_page = batch
        page += 1
    return items


async def _get_watched_pages(path: str, access_token: str,
                             params: dict | None = None) -> list[dict]:
    """Backward-compatible wrapper around complete sync pagination."""
    return await _get_sync_pages(path, access_token, params)


async def watched_movies(access_token: str) -> list[dict]:
    return await _get_watched_pages(
        "/sync/watched/movies", access_token, {"extended": "full"})


async def watched_shows(access_token: str) -> list[dict]:
    # Do not use extended=noseasons: the season/episode arrays are the state
    # used to recognize first episodes and subsequent series continuation.
    return await _get_watched_pages(
        "/sync/watched/shows", access_token, {"extended": "full"})


async def ratings(access_token: str, media: str) -> list[dict]:
    return await _get_sync_pages(f"/sync/ratings/{media}", access_token)


async def watchlist(access_token: str, media: str) -> list[dict]:
    return await _get_sync_pages(
        f"/sync/watchlist/{media}", access_token, {"extended": "full"})


async def recommendations(access_token: str, media: str, limit: int = 50) -> list[dict]:
    """Trakt's personalized recommendations — computed by Trakt per account,
    so they are siloed at the source. media: 'movies' | 'shows'."""
    return await _get(
        f"/recommendations/{media}", access_token,
        {"limit": limit, "extended": "full", "ignore_collected": "false",
         "ignore_watchlisted": "true"},
    )


async def playback(access_token: str) -> list[dict]:
    """Titles the user is part-way through, newest pause first.

    This is the authoritative resume signal: each entry carries a real playback
    `progress` percentage, which bytes-served telemetry cannot substitute for.
    Movies and episodes come back in one call; entries are
    {progress, paused_at, type: 'movie'|'episode', movie|episode+show}.
    """
    return await _get("/sync/playback", access_token,
                      {"extended": "full", "limit": PLAYBACK_LIMIT})


async def history(access_token: str, limit: int = 200) -> list[dict]:
    """Most recently watched first. Unlike /sync/watched (a per-title rollup),
    this is the chronological event log, which is what a history row shows."""
    return await _get("/sync/history", access_token,
                      {"extended": "full", "limit": limit})


async def show_progress(access_token: str, show_id: int | str) -> dict | None:
    """Watched progress for one show, including `next_episode` — the episode to
    play when the previous one was finished. Returns None if Trakt has no
    progress for it, so a single missing show never fails the whole row.

    `extended=full` is required, not cosmetic: without it `next_episode` has no
    `first_aired`, and Trakt happily names an announced-but-unaired episode as
    next. Offering one produces a card for something that does not exist.
    """
    try:
        return await _get(f"/shows/{show_id}/progress/watched", access_token,
                          {"hidden": "false", "specials": "false",
                           "count_specials": "false", "extended": "full"})
    except (httpx.HTTPError, ValueError):
        logger.debug("no watched progress for show %s", show_id, exc_info=True)
        return None


async def trending(media: str, limit: int = 40) -> list[dict]:
    r = await _client.get(f"/{media}/trending", params={"limit": limit, "extended": "full"})
    r.raise_for_status()
    return r.json()


async def popular(media: str, limit: int = 40) -> list[dict]:
    r = await _client.get(f"/{media}/popular", params={"limit": limit, "extended": "full"})
    r.raise_for_status()
    return r.json()


async def related(media: str, item_id: str | int, limit: int = 30) -> list[dict]:
    """Trakt's own 'related' titles for a movie/show. media: 'movies' | 'shows'."""
    r = await _client.get(f"/{media}/{item_id}/related",
                          params={"limit": limit, "extended": "full"})
    r.raise_for_status()
    return r.json()
