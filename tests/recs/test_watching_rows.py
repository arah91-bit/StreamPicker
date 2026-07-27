import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import config, main, watching


def _play(imdb, *, pct, at, media_type="series", season=1, episode=1):
    """One play_history row."""
    return {"imdb_id": imdb, "media_type": media_type, "season": season,
            "episode": episode, "position_pct": pct, "played_at": at,
            "content_id": imdb if media_type == "movie"
                          else f"{imdb}:{season}:{episode}"}


def _found(imdb):
    return {"tmdb_id": int(imdb[2:]), "media_type": "movie" if imdb == "tt11"
            else "tv", "genre_ids": []}


def _meta(media_type, tmdb_id, *_a, **_k):
    kind = "movie" if media_type == "movie" else "series"
    return {"id": f"tt{tmdb_id}", "type": kind, "name": f"Title {tmdb_id}"}


class WatchingRowTests(unittest.IsolatedAsyncioTestCase):
    """Both rows now come from this service's own play history."""

    def setUp(self):
        watching._cache.clear()
        watching._locks.clear()
        self.user = {"token": "viewer", "name": "V",
                     "continue_watching_row": 1, "watch_history_row": 1}

    async def _build(self, plays):
        with (
            patch.object(watching.db, "play_history",
                         AsyncMock(return_value=plays)),
            patch.object(watching.tmdb, "find_by_imdb",
                         AsyncMock(side_effect=lambda i: _found(i))),
            patch.object(watching.tmdb, "episode_aired",
                         AsyncMock(return_value=True)),
            patch.object(watching.tmdb, "resolve_meta",
                         AsyncMock(side_effect=_meta)),
        ):
            return await watching._build(self.user)

    async def test_a_part_watched_title_is_offered_to_resume(self):
        rows = await self._build([_play("tt500", pct=41.0, at=200, episode=7)])
        entry = rows[watching.CONTINUE_ID][0]
        # The card addresses the SHOW: an episode-scoped catalog id has no meta
        # provider and fails to open.
        self.assertEqual("tt500", entry["id"])
        self.assertEqual("S1E7", entry["releaseInfo"])
        self.assertEqual("tt500:1:7", entry["behaviorHints"]["defaultVideoId"])

    async def test_a_finished_episode_offers_the_next_one(self):
        rows = await self._build([_play("tt500", pct=97.0, at=200, episode=7)])
        entry = rows[watching.CONTINUE_ID][0]
        self.assertEqual("S1E8", entry["releaseInfo"])

    async def test_a_finished_movie_is_not_offered(self):
        """There is no next episode of a film."""
        rows = await self._build(
            [_play("tt11", pct=99.0, at=200, media_type="movie")])
        self.assertEqual([], rows[watching.CONTINUE_ID])

    async def test_a_false_start_is_ignored(self):
        rows = await self._build([_play("tt500", pct=0.4, at=200)])
        self.assertEqual([], rows[watching.CONTINUE_ID])

    async def test_only_the_newest_play_of_a_title_counts(self):
        """play_history is append-only, so a show has many rows; the row must
        show where they are now, not every episode they ever watched."""
        rows = await self._build([
            _play("tt500", pct=30.0, at=300, episode=9),
            _play("tt500", pct=95.0, at=200, episode=8),
            _play("tt500", pct=95.0, at=100, episode=7),
        ])
        resume = rows[watching.CONTINUE_ID]
        self.assertEqual(1, len(resume))
        self.assertEqual("S1E9", resume[0]["releaseInfo"])

    async def test_rows_are_ordered_most_recent_first(self):
        rows = await self._build([
            _play("tt500", pct=30.0, at=300),
            _play("tt600", pct=30.0, at=100),
        ])
        self.assertEqual(["tt500", "tt600"],
                         [m["id"] for m in rows[watching.CONTINUE_ID]])

    async def test_history_collapses_a_binge_to_one_card_per_show(self):
        rows = await self._build([
            _play("tt500", pct=95.0, at=300, episode=3),
            _play("tt500", pct=95.0, at=200, episode=2),
            _play("tt11", pct=95.0, at=100, media_type="movie"),
        ])
        self.assertEqual(["tt500", "tt11"],
                         [m["id"] for m in rows[watching.HISTORY_ID]])

    async def test_each_row_is_opted_into_separately(self):
        self.user = {"token": "viewer", "name": "V",
                     "continue_watching_row": 1, "watch_history_row": 0}
        rows = await self._build([_play("tt500", pct=41.0, at=200)])
        self.assertEqual(1, len(rows[watching.CONTINUE_ID]))
        self.assertEqual([], rows[watching.HISTORY_ID])

    async def test_a_storage_failure_costs_the_row_not_the_home_screen(self):
        with patch.object(watching.db, "play_history",
                          AsyncMock(side_effect=RuntimeError("locked"))):
            metas = await watching.get_metas(self.user, watching.CONTINUE_ID)
        self.assertEqual([], metas)

    async def test_one_build_serves_both_rows(self):
        reads = AsyncMock(return_value=[])
        with (
            patch.object(watching.db, "play_history", reads),
            patch.object(watching.tmdb, "resolve_meta",
                         AsyncMock(side_effect=_meta)),
        ):
            await watching.get_metas(self.user, watching.CONTINUE_ID)
            await watching.get_metas(self.user, watching.HISTORY_ID)
        self.assertEqual(1, reads.await_count)

    async def test_a_disabled_viewer_gets_nothing_even_if_the_id_is_guessed(self):
        metas = await watching.get_metas(
            {"token": "v", "continue_watching_row": 0, "watch_history_row": 0},
            watching.CONTINUE_ID)
        self.assertEqual([], metas)


class WatchingManifestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        watching._cache.clear()
        watching._locks.clear()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        )
        self.stored = [
            {"id": "nr-top-picks", "type": "all", "name": "Top Picks"},
            {"id": "nr-trending", "type": "all", "name": "Trending"},
        ]

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _manifest(self, user):
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=user)),
            patch.object(main.db, "get_catalog_defs",
                         AsyncMock(return_value=self.stored)),
        ):
            return await self.client.get("/viewer/manifest.json")

    async def test_rows_are_pinned_above_the_nightly_slate(self):
        response = await self._manifest({"token": "viewer", "continue_watching_row": 1,
                     "watch_history_row": 1})

        ids = [c["id"] for c in response.json()["catalogs"]]
        self.assertEqual(ids[:2], [watching.CONTINUE_ID, watching.HISTORY_ID])
        self.assertEqual(ids[2:], ["nr-top-picks", "nr-trending"])

    async def test_rows_are_absent_until_the_viewer_opts_in(self):
        response = await self._manifest({"token": "viewer", "continue_watching_row": 0,
                                          "watch_history_row": 0})

        ids = [c["id"] for c in response.json()["catalogs"]]
        self.assertEqual(ids, ["nr-top-picks", "nr-trending"])

    async def test_only_the_enabled_row_is_advertised(self):
        response = await self._manifest({"token": "viewer", "continue_watching_row": 0,
                                         "watch_history_row": 1})

        ids = [c["id"] for c in response.json()["catalogs"]]
        self.assertEqual(ids, [watching.HISTORY_ID, "nr-top-picks", "nr-trending"])

    async def test_live_rows_bypass_the_stored_catalog_table_and_ledger(self):
        """They are the viewer's own backlog, so counting a resume as an
        assisted discovery would quietly distort recommendation measurement."""
        get_metas = AsyncMock(return_value=[{"id": "tt1", "type": "movie",
                                             "name": "One"}])
        stored = AsyncMock(return_value=None)
        delivery = AsyncMock()
        with (
            patch.object(main.db, "get_user", AsyncMock(
                return_value={"token": "viewer", "continue_watching_row": 1,
                     "watch_history_row": 1})),
            patch.object(main.db, "get_catalog_metas", stored),
            patch.object(main.db, "record_catalog_delivery", delivery),
            patch.object(main.watching, "get_metas", get_metas),
        ):
            response = await self.client.get(
                f"/viewer/catalog/all/{watching.CONTINUE_ID}.json")

        self.assertEqual(response.json()["metas"], [{"id": "tt1", "type": "movie",
                                                     "name": "One"}])
        stored.assert_not_awaited()
        delivery.assert_not_awaited()

    async def test_live_rows_paginate_like_every_other_row(self):
        page = [{"id": f"tt{i}", "type": "movie", "name": str(i)}
                for i in range(config.CATALOG_PAGE_SIZE + 5)]
        with (
            patch.object(main.db, "get_user", AsyncMock(
                return_value={"token": "viewer", "continue_watching_row": 1,
                     "watch_history_row": 1})),
            patch.object(main.watching, "get_metas",
                         AsyncMock(return_value=page)),
        ):
            first = await self.client.get(
                f"/viewer/catalog/all/{watching.CONTINUE_ID}.json")
            second = await self.client.get(
                f"/viewer/catalog/all/{watching.CONTINUE_ID}/skip=30.json")

        self.assertEqual(len(first.json()["metas"]), config.CATALOG_PAGE_SIZE)
        self.assertEqual(len(second.json()["metas"]), 5)


if __name__ == "__main__":
    unittest.main()


class CollectionFilenameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    def test_named_after_the_viewer(self):
        self.assertEqual(main.collection_filename({"name": "Phil"}),
                         "Phil collections.json")

    def test_a_nameless_viewer_still_gets_a_usable_filename(self):
        self.assertEqual(main.collection_filename({"name": ""}),
                         "daily-picks collections.json")

    def test_characters_that_would_break_a_header_or_path_are_dropped(self):
        self.assertEqual(
            main.collection_filename({"name": 'Toya/Tonya "T"'}),
            "ToyaTonya T collections.json")

    def test_a_name_of_pure_punctuation_still_yields_a_usable_file(self):
        self.assertEqual(main.collection_filename({"name": "///"}),
                         "daily-picks collections.json")

    async def test_the_download_is_served_under_that_name(self):
        with (
            patch.object(main.db, "get_user", AsyncMock(
                return_value={"token": "viewer", "name": "Skylar"})),
            patch.object(main.profile_streaming, "collection_export_for_user",
                         lambda _u: {"catalogs": []}),
        ):
            response = await self.client.get("/viewer/streaming-collection.json")

        self.assertEqual(response.status_code, 200)
        disposition = response.headers["content-disposition"]
        self.assertIn('filename="Skylar collections.json"', disposition)
        self.assertIn("filename*=UTF-8''Skylar%20collections.json", disposition)


class UnairedNextEpisodeTests(unittest.IsolatedAsyncioTestCase):
    """Adding one to an episode number is not proof the episode exists. Past a
    season finale, or for something only announced, offering it puts a card in
    the row for content nobody can play — which is how a phantom
    "Creature Commandos S2E1" appeared for a show with seven episodes."""

    def setUp(self):
        watching._cache.clear()
        watching._locks.clear()

    async def _resume(self, aired: bool):
        user = {"token": "v", "name": "V", "continue_watching_row": 1,
                "watch_history_row": 0}
        with (
            patch.object(watching.db, "play_history", AsyncMock(
                return_value=[_play("tt500", pct=97.0, at=200, episode=7)])),
            patch.object(watching.tmdb, "find_by_imdb",
                         AsyncMock(return_value=_found("tt500"))),
            patch.object(watching.tmdb, "episode_aired",
                         AsyncMock(return_value=aired)),
            patch.object(watching.tmdb, "resolve_meta",
                         AsyncMock(side_effect=_meta)),
        ):
            return (await watching._build(user))[watching.CONTINUE_ID]

    async def test_an_aired_next_episode_is_offered(self):
        self.assertEqual(1, len(await self._resume(True)))

    async def test_an_unaired_or_nonexistent_one_is_not(self):
        self.assertEqual([], await self._resume(False))
