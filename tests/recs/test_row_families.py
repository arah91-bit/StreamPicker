"""Streaming catalogs and Asian dramas as per-viewer opt-ins."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import dramas, main, profile_streaming, watching


class DramaRowTests(unittest.TestCase):
    def setUp(self):
        self.on = {"token": "t", "name": "Tonya", "asian_dramas_row": 1}
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
        self.assertEqual(len(main.ROW_FAMILIES),
                         len(watching.ROWS) + 2)


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
