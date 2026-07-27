import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import config, main, profile_streaming

SETUP_SECRET = config.require("SETUP_SECRET")


class MainSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_root_is_a_public_service_landing_page(self):
        response = await self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("Daily Picks", response.text)
        self.assertIn("Service online", response.text)
        self.assertNotIn(SETUP_SECRET, response.text)

    async def test_setup_page_stays_hidden_behind_the_exact_secret(self):
        wrong = await self.client.get("/setup/not-the-secret")
        correct = await self.client.get(f"/setup/{SETUP_SECRET}")

        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(correct.status_code, 200)
        self.assertIn("Download collection file", correct.text)
        self.assertNotIn("Copy collection URL", correct.text)

    async def test_account_delete_reconciles_private_collection_files(self):
        user = {"token": "viewer", "name": "Viewer"}
        delete_user = AsyncMock()
        write_imports = AsyncMock()
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=user)),
            patch.object(main.db, "delete_user", delete_user),
            patch.object(main.profile_streaming, "write_import_files", write_imports),
        ):
            response = await self.client.post(
                f"/setup/{SETUP_SECRET}/api/delete/viewer")

        self.assertEqual(response.status_code, 200)
        delete_user.assert_awaited_once_with("viewer")
        write_imports.assert_awaited_once_with()

    async def test_admin_user_list_includes_privacy_safe_measurement_summary(self):
        user = {
            "token": "viewer", "name": "Viewer", "trakt_username": "view",
            "is_kid": 0, "kid_age": None, "kid_birthdate": None,
            "preferred_media": "balanced", "adventurousness": 30,
            "last_generated_at": 100, "last_served_at": 110,
            "last_error": None,
        }
        summary = {
            "window_days": 30, "window_start": 1, "as_of": 2,
            "generations": 3, "sessions": 4, "delivered_rows": 5,
            "outcome_events": 2, "attributed_outcomes": 1,
            "winning_sessions": 1, "assisted_pick_rate": 0.25,
        }
        with (
            patch.object(main.db, "all_users", AsyncMock(return_value=[user])),
            patch.object(main.db, "get_catalog_defs", AsyncMock(return_value=[])),
            patch.object(main.db, "get_recommendation_summary",
                         AsyncMock(return_value=summary)),
        ):
            response = await self.client.get(
                f"/setup/{SETUP_SECRET}/api/users")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["users"][0]["measurement"], summary)
        self.assertNotIn("content_id", response.json()["users"][0]["measurement"])
        self.assertTrue(response.json()["users"][0]["manifest_url"].endswith(
            f"/viewer/manifest.json?v={profile_streaming.PRIVATE_MANIFEST_VERSION}"
        ))

    async def test_manifest_advertises_optional_skip_for_every_catalog(self):
        catalogs = [
            {"id": "top", "type": "movie", "name": "Top picks"},
            {"id": "shows", "type": "series", "name": "Series"},
        ]
        with (
            patch.object(main.db, "get_user", AsyncMock(
                return_value={"token": "viewer"})),
            patch.object(main.db, "get_catalog_defs", AsyncMock(
                return_value=catalogs)),
        ):
            response = await self.client.get("/viewer/manifest.json")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"],
                         profile_streaming.PRIVATE_MANIFEST_VERSION)
        self.assertEqual(
            body["id"],
            profile_streaming.private_addon_id_for_user({"token": "viewer"}),
        )
        self.assertEqual(
            [catalog["extra"] for catalog in body["catalogs"]],
            [[{"name": "skip", "isRequired": False}]] * 2,
        )

    async def test_catalog_skip_pages_are_bounded_and_empty_probe_is_not_delivery(self):
        metas = [{"id": f"tt{n:07d}", "type": "movie"} for n in range(65)]
        get_user = AsyncMock(return_value={"token": "viewer"})
        get_metas = AsyncMock(return_value=metas)
        record_delivery = AsyncMock(return_value={})
        with (
            patch.object(main.db, "get_user", get_user),
            patch.object(main.db, "get_catalog_metas", get_metas),
            patch.object(main.db, "record_catalog_delivery", record_delivery),
            patch.object(config, "CATALOG_PAGE_SIZE", 30),
        ):
            first = await self.client.get(
                "/viewer/catalog/movie/row.json")
            second = await self.client.get(
                "/viewer/catalog/movie/row/skip=30.json")
            invalid = await self.client.get(
                "/viewer/catalog/movie/row/skip=not-a-number.json")
            empty = await self.client.get(
                "/viewer/catalog/movie/row/skip=90.json")

        self.assertEqual(
            [meta["id"] for meta in first.json()["metas"]],
            [meta["id"] for meta in metas[:30]],
        )
        self.assertEqual(
            [meta["id"] for meta in second.json()["metas"]],
            [meta["id"] for meta in metas[30:60]],
        )
        self.assertEqual(invalid.json()["metas"], metas[:30])
        self.assertEqual(empty.json(), {"metas": []})
        self.assertEqual(record_delivery.await_count, 3)

    async def test_delivery_ledger_failure_does_not_break_catalog_serving(self):
        with (
            patch.object(main.db, "get_user", AsyncMock(
                return_value={"token": "viewer"})),
            patch.object(main.db, "get_catalog_metas", AsyncMock(
                return_value=[{"id": "tt1"}])),
            patch.object(main.db, "record_catalog_delivery", AsyncMock(
                side_effect=RuntimeError("ledger unavailable"))),
            patch.object(main.db, "mark_served", AsyncMock()) as mark_served,
        ):
            response = await self.client.get(
                "/viewer/catalog/movie/row.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"metas": [{"id": "tt1"}]})
        mark_served.assert_awaited_once_with("viewer")

    async def test_preference_update_persists_then_regenerates_with_new_user(self):
        old_user = {
            "token": "viewer",
            "preferred_media": "balanced",
            "adventurousness": 30,
        }
        new_user = {
            **old_user,
            "preferred_media": "series",
            "adventurousness": 72,
        }
        get_user = AsyncMock(side_effect=[old_user, new_user])
        update = AsyncMock()
        generate = AsyncMock()
        with (
            patch.object(main.db, "get_user", get_user),
            patch.object(main.db, "update_preferences", update),
            patch.object(main, "generate_for_user", generate),
        ):
            response = await self.client.post(
                f"/setup/{SETUP_SECRET}/api/preferences/viewer",
                json={"preferred_media": "series", "adventurousness": 72},
            )
            # The endpoint deliberately schedules regeneration in the
            # background; give that task one loop turn to start.
            await asyncio.sleep(0)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "ok",
            "preferred_media": "series",
            "adventurousness": 72,
        })
        update.assert_awaited_once_with("viewer", "series", 72)
        generate.assert_awaited_once_with(new_user, trigger="taste-settings")

    async def test_invalid_preference_is_422_and_does_not_regenerate(self):
        generate = AsyncMock()
        with (
            patch.object(main.db, "get_user", AsyncMock(
                return_value={"token": "viewer"})),
            patch.object(main.db, "update_preferences", AsyncMock(
                side_effect=ValueError("preferred_media must be valid"))),
            patch.object(main, "generate_for_user", generate),
        ):
            response = await self.client.post(
                f"/setup/{SETUP_SECRET}/api/preferences/viewer",
                json={"preferred_media": "invalid", "adventurousness": 72},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"],
                         "preferred_media must be valid")
        generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
