"""Streaming catalogs and Asian dramas as per-viewer opt-ins."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import dramas, main, profile_streaming, watching


class DramaRowTests(unittest.TestCase):
    def setUp(self):
        self.on = {"token": "t", "name": "Viewer", "asian_dramas_row": 1}
        self.off = {"token": "u", "name": "Phil", "asian_dramas_row": 0}
        self.state = patch.object(dramas.builder, "state", {
            "built_at": 1,
            "catalog_defs": [{"id": "ad_actor_bai_lu", "type": "series",
                              "name": "Bai Lu"}],
            "rows": {
                "ad_actor_bai_lu": [{"id": "tt1", "type": "series", "name": "One"}],
                "ad_cc_cn|Romance": [{"id": "tt2", "type": "series", "name": "Two"}],
            },
        })
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_a_viewer_who_opted_out_sees_no_drama_catalogs(self):
        self.assertEqual([], dramas.catalog_defs_for_user(self.off))

    def test_an_opted_in_viewer_gets_the_built_rows(self):
        ids = [d["id"] for d in dramas.catalog_defs_for_user(self.on)]
        self.assertIn("ad_actor_bai_lu", ids)

    def test_country_rows_are_hidden_from_home_but_addressable(self):
        defs = {d["id"]: d for d in dramas.catalog_defs_for_user(self.on)}
        row = defs["ad_cc_cn_romance"]
        self.assertFalse(row["showInHome"])
        self.assertEqual([{"name": "genre", "options": ["All"],
                           "isRequired": True}], row["extra"])

    def test_a_country_row_resolves_through_its_genre_extra(self):
        self.assertEqual(
            [{"id": "tt2", "type": "series", "name": "Two"}],
            dramas.get_metas(self.on, "ad_cc_cn_romance", "Romance"))

    def test_metas_are_withheld_from_an_opted_out_viewer(self):
        """The rows are shared, so the opt-in has to be enforced on read too —
        otherwise a guessed catalog id would serve them to anyone."""
        self.assertEqual([], dramas.get_metas(self.off, "ad_actor_bai_lu"))


class DramaCollectionExportTests(unittest.TestCase):
    """The collection has to name the addon that actually serves the rows.

    Nuvio resolves a collection source by the manifest id of an installed
    addon. Dramas were their own addon with one shared id, so one static export
    served the household; folded into Daily Picks, every viewer's manifest
    carries a different id, and the stale export named neither — which is why
    both drama collections rendered empty while the catalogs behind them were
    serving fine.
    """

    def setUp(self):
        self.user = {"token": "t", "name": "Viewer", "asian_dramas_row": 1}
        self.state = patch.object(dramas.builder, "state", {
            "built_at": 1,
            "actors": [{"id": 1879666, "name": "Bai Lu", "profile": "/a.jpg"},
                       {"id": 42, "name": "Nobody", "profile": None}],
            "rows": {
                "ad_p_1879666": [{"id": "tt1", "type": "series", "name": "One"}],
                "ad_cc_cn|Romance": [{"id": "tt2", "type": "series", "name": "Two"}],
            },
        })
        self.state.start()
        self.addCleanup(self.state.stop)

    def _export(self):
        return dramas.collection_export_for_user(self.user, "org.example.u1")

    def test_every_source_names_this_viewers_addon(self):
        addons = {source["addonId"]
                  for collection in self._export()
                  for folder in collection["folders"]
                  for source in folder["catalogSources"]}
        self.assertEqual({"org.example.u1"}, addons)

    def test_both_collections_are_exported(self):
        self.assertEqual(["asian-dramas", "asian-drama-stars"],
                         [c["id"] for c in self._export()])

    def test_folders_use_catalogsources_not_sources(self):
        """The Drama Stars export used `sources`, which intermediate Nuvio TV
        releases preferred and then failed to decode — a folder that looked
        valid and listed nothing."""
        for collection in self._export():
            for folder in collection["folders"]:
                self.assertIn("catalogSources", folder)
                self.assertNotIn("sources", folder)

    def test_an_actor_with_no_built_row_is_left_out(self):
        stars = self._export()[1]
        self.assertEqual(["Bai Lu"], [f["title"] for f in stars["folders"]])

    def test_a_country_with_no_built_rows_is_left_out(self):
        countries = self._export()[0]
        self.assertEqual(["Chinese Dramas"],
                         [f["title"] for f in countries["folders"]])

    def test_cover_art_is_served_from_this_host_not_the_retired_one(self):
        for collection in self._export():
            for folder in collection["folders"]:
                cover = folder.get("coverImageUrl") or ""
                if cover:
                    self.assertTrue(cover.startswith((dramas.recs_config.HOST_NAME + "/",
                                                      "https://image.tmdb.org/")))

    def test_an_opted_out_viewer_exports_nothing(self):
        self.user["asian_dramas_row"] = 0
        self.assertEqual([], self._export())


class StreamingProviderSelectionTests(unittest.TestCase):
    def test_no_stored_preference_means_every_service(self):
        self.assertEqual(
            [p["id"] for p in profile_streaming.kids_catalogs.PROVIDERS],
            [p["id"] for p in profile_streaming.providers_for_user({})])

    def test_an_empty_string_is_a_real_answer_of_none(self):
        """Distinct from NULL. A viewer who unticks every service must not be
        silently handed the full list back."""
        self.assertEqual([], profile_streaming.providers_for_user(
            {"streaming_providers": ""}))

    def test_stored_ids_are_returned_in_catalogue_order(self):
        chosen = profile_streaming.providers_for_user(
            {"streaming_providers": "disney,netflix"})
        self.assertEqual(["netflix", "disney"], [p["id"] for p in chosen])

    def test_a_retired_service_is_dropped_rather_than_carried(self):
        self.assertEqual(["netflix"], [p["id"] for p in
                                       profile_streaming.providers_for_user(
                                           {"streaming_providers": "netflix,mubi"})])

    def test_unticked_services_leave_the_manifest_immediately(self):
        """Filtered on read as well as at build time, so the switch does not
        wait for the next nightly build to take effect."""
        with patch.object(profile_streaming, "_targets", ["Alice"]):
            user = {"token": "t", "name": "Alice", "is_kid": 0,
                    "streaming_catalogs_row": 1,
                    "streaming_providers": ""}
            self.assertEqual([], profile_streaming.catalog_defs_for_user(user))


class StreamingProviderEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver")
        self.secret = main.config.require("SETUP_SECRET")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _post(self, provider, enabled, stored=None):
        update = AsyncMock()
        user = {"token": "t", "name": "A", "streaming_providers": stored}
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=user)),
            patch.object(main.db, "update_streaming_providers", update),
            patch.object(main.profile_streaming, "refresh_targets", AsyncMock()),
            patch.object(main, "_queue_profile_streaming_build", lambda _u: True),
        ):
            response = await self.client.post(
                f"/setup/{self.secret}/api/streaming-provider/t",
                json={"provider": provider, "enabled": enabled})
        return response, update

    async def test_unticking_one_stores_the_rest(self):
        response, update = await self._post("netflix", False)
        self.assertEqual(200, response.status_code)
        stored = update.await_args.args[1]
        self.assertNotIn("netflix", stored)
        self.assertIn("disney", stored)

    async def test_ticking_one_back_on_restores_it(self):
        _response, update = await self._post("netflix", True, stored="disney")
        self.assertEqual(["netflix", "disney"], update.await_args.args[1])

    async def test_an_unknown_service_is_rejected_rather_than_stored(self):
        response, update = await self._post("mubi", False)
        self.assertEqual(422, response.status_code)
        update.assert_not_awaited()


class StreamingCatalogFlagTests(unittest.TestCase):
    def test_the_flag_is_the_authority_not_the_name_match(self):
        """Un-ticking someone stops their rows immediately, without waiting for
        the next build to drop the profile from the state file."""
        with patch.object(profile_streaming, "_targets", ["Alice"]):
            user = {"token": "t", "name": "Alice", "is_kid": 0,
                    "streaming_catalogs_row": 0}
            self.assertEqual([], profile_streaming.catalog_defs_for_user(user))


class RowFamilyRegistryTests(unittest.TestCase):
    def test_every_switchable_row_maps_to_a_real_column(self):
        from app.recs import db
        for row_id, column in main.ROW_FAMILIES.items():
            self.assertIn(column, db.WATCHING_ROW_COLUMNS, row_id)

    def test_the_live_rows_and_the_families_do_not_collide(self):
        """ROW_FAMILIES is assembled by merging dicts, so a row id reused
        between the live rows and the families would silently swallow one of
        them — and the toggle for it would move the wrong column."""
        from app.recs import watchlist

        ids = ([*watching.ROWS, watchlist.WATCHLIST_ID,
                "streaming-catalogs", "asian-dramas"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(main.ROW_FAMILIES))


class ToggleEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver")
        self.secret = main.config.require("SETUP_SECRET")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _toggle(self, row, enabled=True):
        update = AsyncMock()
        with (
            patch.object(main.db, "get_user",
                         AsyncMock(return_value={"token": "t", "name": "A"})),
            patch.object(main.db, "update_watching_row", update),
            patch.object(main.profile_streaming, "refresh_targets", AsyncMock()),
            patch.object(main, "_queue_profile_streaming_build", lambda _u: True),
        ):
            response = await self.client.post(
                f"/setup/{self.secret}/api/watching/t",
                json={"row": row, "enabled": enabled})
        return response, update

    async def test_each_family_writes_its_own_column(self):
        for row, column in (("streaming-catalogs", "streaming_catalogs_row"),
                            ("asian-dramas", "asian_dramas_row"),
                            ("nr-continue-watching", "continue_watching_row")):
            response, update = await self._toggle(row)
            self.assertEqual(200, response.status_code, row)
            self.assertEqual(column, update.await_args.args[1], row)

    async def test_an_unknown_row_is_rejected_rather_than_ignored(self):
        response, update = await self._toggle("nr-not-a-row")
        self.assertEqual(422, response.status_code)
        update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
