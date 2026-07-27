import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import config, kids_catalogs, main, profile_streaming


def _spec(title: str = "Action Movies") -> dict:
    return {
        "id": "taste-01",
        "title": title,
        "media": "movie",
        "type": "movie",
        "params": {"with_genres": "28"},
    }


def _metas(prefix: str, count: int = 65) -> list[dict]:
    return [
        {"id": f"tt{prefix}{index:04d}", "type": "movie", "name": f"{prefix}-{index}"}
        for index in range(count)
    ]


class ProfileStreamingFoldInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_state = profile_streaming.state
        # Streaming catalogs are opted into per user now; the module keeps a
        # snapshot of the enabled display names refreshed from the database.
        self.targets = patch.object(
            profile_streaming, "_targets", ["Alpha", "Beta"])
        self.targets.start()
        provider = kids_catalogs.PROVIDERS[0]
        alpha_spec = _spec("Alice Action Movies")
        beta_spec = _spec("Bob Adventure Movies")
        profile_streaming.state = {
            "built_at": 1,
            "profiles": {
                "alpha": {
                    "target": "Alpha",
                    "title": "Alpha",
                    "taste_profile": {"user": "Alice", "age": None},
                    "row_specs": [alpha_spec],
                    "rows": {
                        f"ps_alpha_{provider['id']}_{alpha_spec['id']}": _metas("a"),
                    },
                },
                "beta": {
                    "target": "Beta",
                    "title": "Beta",
                    "taste_profile": {"user": "Bob", "age": 12},
                    "row_specs": [beta_spec],
                    "rows": {
                        f"ps_beta_{provider['id']}_{beta_spec['id']}": _metas("b"),
                    },
                },
            },
        }
        self.alice = {
            "token": "alice-secret-token",
            "name": "Alice",
            "is_kid": 0,
            "streaming_catalogs_row": 1,
        }
        self.bob = {
            "token": "bob-secret-token",
            "name": "Bob",
            "is_kid": 1,
            "kid_age": 12,
            "kid_birthdate": None,
            "streaming_catalogs_row": 1,
        }
        self.namespace = profile_streaming.private_namespace_for_user(self.alice)
        self.addon_id = profile_streaming.private_addon_id_for_user(self.alice)
        self.private_id = (
            f"dp_streaming_{self.namespace}_netflix_movie-01"
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        profile_streaming.state = self.original_state
        self.targets.stop()

    async def test_identity_mapping_is_exact_and_ambiguous_matches_fail_closed(self):
        self.assertEqual(profile_streaming.profile_id_for_user(self.alice), "alpha")
        self.assertEqual(profile_streaming.profile_id_for_user(self.bob), "beta")
        self.assertIsNone(profile_streaming.profile_id_for_user({
            "name": "my-alpha-viewer",
        }))

        profile_streaming.state["profiles"]["beta"]["taste_profile"]["user"] = "Alice"
        self.assertIsNone(profile_streaming.profile_id_for_user(self.alice))

    async def test_builder_identity_lookup_does_not_use_name_substrings(self):
        users = [
            {"name": "alpha-household"},
            {"name": "Alpha"},
        ]
        with patch.object(kids_catalogs.db, "all_users", AsyncMock(return_value=users)):
            exact = await kids_catalogs._taste_user("alpha")
            missing = await kids_catalogs._taste_user("house")

        self.assertEqual(exact["name"], "Alpha")
        self.assertIsNone(missing)

    async def test_private_defs_are_generic_collection_only_and_legacy_is_hidden(self):
        defs = profile_streaming.catalog_defs_for_user(self.alice)

        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["id"], self.private_id)
        self.assertNotIn("alice", defs[0]["id"].casefold())
        self.assertNotIn("alpha", defs[0]["id"].casefold())
        self.assertIs(defs[0]["showInHome"], False)
        self.assertEqual(defs[0]["extra"], [
            {"name": "genre", "options": ["All"], "isRequired": True},
            {"name": "skip", "isRequired": False},
        ])
        self.assertTrue(all(
            definition["showInHome"] is False
            for definition in profile_streaming.manifest_export()["catalogs"]
        ))

    async def test_stale_adult_rows_are_withheld_after_enabling_kid_mode(self):
        newly_kid = {**self.alice, "is_kid": 1, "kid_age": 10,
                     "kid_birthdate": None}

        self.assertEqual(profile_streaming.catalog_defs_for_user(newly_kid), [])
        self.assertEqual(profile_streaming.collection_export_for_user(newly_kid), [])
        self.assertIsNone(profile_streaming.get_metas_for_user(
            newly_kid, "movie", self.private_id))

    async def test_private_slots_keep_their_media_type_when_daily_specs_shuffle(self):
        provider = kids_catalogs.PROVIDERS[0]
        profile = profile_streaming.state["profiles"]["alpha"]
        day_one_specs = [
            _spec("Day One Movie"),
            {**_spec("Day One Series"), "id": "taste-02",
             "media": "tv", "type": "series"},
        ]
        profile["row_specs"] = day_one_specs
        profile["rows"] = {
            f"ps_alpha_{provider['id']}_taste-01": [
                {"id": "tt-day1-movie", "type": "movie"}],
            f"ps_alpha_{provider['id']}_taste-02": [
                {"id": "tt-day1-series", "type": "series"}],
        }
        day_one = {
            definition["id"]: definition["type"]
            for definition in profile_streaming.catalog_defs_for_user(self.alice)
        }

        profile["row_specs"] = [
            {**_spec("Day Two Series"), "id": "taste-01",
             "media": "tv", "type": "series"},
            {**_spec("Day Two Movie"), "id": "taste-02"},
        ]
        profile["rows"] = {
            f"ps_alpha_{provider['id']}_taste-01": [
                {"id": "tt-day2-series", "type": "series"}],
            f"ps_alpha_{provider['id']}_taste-02": [
                {"id": "tt-day2-movie", "type": "movie"}],
        }
        day_two = {
            definition["id"]: definition["type"]
            for definition in profile_streaming.catalog_defs_for_user(self.alice)
        }

        expected = {
            f"dp_streaming_{self.namespace}_netflix_movie-01": "movie",
            f"dp_streaming_{self.namespace}_netflix_series-01": "series",
        }
        self.assertEqual(day_one, expected)
        self.assertEqual(day_two, expected)
        self.assertEqual(
            profile_streaming.get_metas_for_user(
                self.alice, "movie",
                f"dp_streaming_{self.namespace}_netflix_movie-01"),
            [{"id": "tt-day2-movie", "type": "movie"}],
        )

    async def test_temporarily_empty_private_slot_stays_declared_and_imported(self):
        provider = kids_catalogs.PROVIDERS[0]
        internal_id = f"ps_alpha_{provider['id']}_taste-01"
        profile_streaming.state["profiles"]["alpha"]["rows"][internal_id] = []

        defs = profile_streaming.catalog_defs_for_user(self.alice)
        collection = profile_streaming.collection_export_for_user(self.alice)
        source_ids = [
            source["catalogId"]
            for folder in collection[0]["folders"]
            for source in folder["catalogSources"]
        ]

        self.assertEqual([definition["id"] for definition in defs], [self.private_id])
        self.assertEqual(source_ids, [self.private_id])
        self.assertEqual(profile_streaming.get_metas_for_user(
            self.alice, "movie", self.private_id), [])

    async def test_manifest_has_visible_home_rows_and_hidden_streaming_descriptors(self):
        home_defs = [
            {"id": "daily-one", "type": "movie", "name": "Daily One"},
            {"id": "daily-two", "type": "series", "name": "Daily Two"},
        ]
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=self.alice)),
            patch.object(main.db, "get_catalog_defs", AsyncMock(return_value=home_defs)),
        ):
            response = await self.client.get("/alice-secret-token/manifest.json")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], self.addon_id)
        self.assertEqual(body["version"],
                         profile_streaming.PRIVATE_MANIFEST_VERSION)
        visible = [c for c in body["catalogs"] if c.get("showInHome") is not False]
        hidden = [c for c in body["catalogs"] if c.get("showInHome") is False]
        self.assertEqual([c["id"] for c in visible], ["daily-one", "daily-two"])
        self.assertEqual([c["id"] for c in hidden], [self.private_id])

    async def test_private_catalog_is_scoped_paginated_and_never_hits_home_ledger(self):
        get_daily = AsyncMock(return_value=None)
        delivery = AsyncMock()
        mark_served = AsyncMock()
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=self.alice)),
            patch.object(main.db, "get_catalog_metas", get_daily),
            patch.object(main.db, "record_catalog_delivery", delivery),
            patch.object(main.db, "mark_served", mark_served),
            patch.object(config, "CATALOG_PAGE_SIZE", 30),
        ):
            first = await self.client.get(
                f"/alice-secret-token/catalog/movie/{self.private_id}.json")
            second = await self.client.get(
                f"/alice-secret-token/catalog/movie/{self.private_id}/"
                "genre=All&skip=30.json")
            wrong_type = await self.client.get(
                f"/alice-secret-token/catalog/series/{self.private_id}.json")

        self.assertEqual(first.json()["metas"], _metas("a")[:30])
        self.assertEqual(second.json()["metas"], _metas("a")[30:60])
        self.assertEqual(wrong_type.json(), {"metas": []})
        get_daily.assert_not_awaited()
        delivery.assert_not_awaited()
        mark_served.assert_not_awaited()

    async def test_viewer_scoped_catalog_id_cannot_resolve_for_another_token(self):
        async def get_user(token: str):
            return self.alice if token == self.alice["token"] else self.bob

        bob_private_id = (
            f"dp_streaming_{profile_streaming.private_namespace_for_user(self.bob)}_"
            "netflix_movie-01"
        )

        with patch.object(main.db, "get_user", side_effect=get_user):
            alice = await self.client.get(
                f"/{self.alice['token']}/catalog/movie/{self.private_id}.json")
            bob_using_alice_id = await self.client.get(
                f"/{self.bob['token']}/catalog/movie/{self.private_id}.json")
            bob = await self.client.get(
                f"/{self.bob['token']}/catalog/movie/{bob_private_id}.json")

        self.assertEqual(alice.json()["metas"][0]["id"], "tta0000")
        self.assertEqual(bob_using_alice_id.json(), {"metas": []})
        self.assertEqual(bob.json()["metas"][0]["id"], "ttb0000")
        self.assertNotEqual(alice.json()["metas"], bob.json()["metas"])

    async def test_private_collection_uses_picker_addon_and_contains_no_token(self):
        with patch.object(main.db, "get_user", AsyncMock(return_value=self.alice)):
            response = await self.client.get(
                f"/{self.alice['token']}/streaming-collection.json")

        self.assertEqual(response.status_code, 200)
        # Named per viewer so a household's downloads stay distinguishable.
        self.assertIn('filename="Alice collections.json"',
                      response.headers["content-disposition"])
        payload = response.json()
        collection = payload[0]
        self.assertEqual(collection["title"], "Alice Streaming")
        self.assertIs(collection["pinToTop"], False)
        self.assertEqual(
            collection["id"],
            f"daily-picks-streaming-v2-{self.namespace}",
        )
        self.assertTrue(all(
            folder["id"].startswith(f"dp_v2_f_{self.namespace}_")
            for folder in collection["folders"]
        ))
        self.assertTrue(all("sources" not in folder for folder in collection["folders"]))
        sources = [
            source
            for folder in collection["folders"]
            for source in folder["catalogSources"]
        ]
        self.assertTrue(sources)
        self.assertTrue(all(source["addonId"] == self.addon_id for source in sources))
        self.assertTrue(all(source["catalogId"] == self.private_id for source in sources))
        self.assertTrue(all(source["genre"] == "All" for source in sources))
        serialized = json.dumps(payload)
        self.assertNotIn(config.PROFILE_STREAMING_ADDON_ID, serialized)
        self.assertNotIn(self.alice["token"], serialized)
        self.assertNotIn("alpha-viewer", serialized)

    async def test_private_addon_and_catalog_namespaces_differ_by_token(self):
        alice_defs = profile_streaming.catalog_defs_for_user(self.alice)
        bob_defs = profile_streaming.catalog_defs_for_user(self.bob)

        self.assertNotEqual(
            profile_streaming.private_addon_id_for_user(self.alice),
            profile_streaming.private_addon_id_for_user(self.bob),
        )
        self.assertNotEqual(alice_defs[0]["id"], bob_defs[0]["id"])

    async def test_private_namespace_is_stable_opaque_and_requires_a_token(self):
        renamed = {**self.alice, "name": "Renamed"}

        self.assertEqual(
            profile_streaming.private_namespace_for_user(self.alice),
            profile_streaming.private_namespace_for_user(renamed),
        )
        self.assertRegex(self.namespace, r"^u[0-9a-f]{12}$")
        self.assertNotIn(self.alice["token"], self.namespace)
        with self.assertRaises(ValueError):
            profile_streaming.private_namespace_for_user({"token": "  "})

    async def test_collection_sources_all_resolve_in_its_one_private_manifest(self):
        home_defs = [{"id": "home", "type": "movie", "name": "Home"}]
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=self.alice)),
            patch.object(main.db, "get_catalog_defs", AsyncMock(return_value=home_defs)),
        ):
            manifest = (await self.client.get(
                f"/{self.alice['token']}/manifest.json")).json()
            collection = (await self.client.get(
                f"/{self.alice['token']}/streaming-collection.json")).json()

        hidden = {
            (definition["type"], definition["id"])
            for definition in manifest["catalogs"]
            if definition.get("showInHome") is False
        }
        sources = [
            source
            for folder in collection[0]["folders"]
            for source in folder["catalogSources"]
        ]
        self.assertTrue(sources)
        self.assertTrue(all(source["addonId"] == manifest["id"] for source in sources))
        self.assertTrue(all(
            (source["type"], source["catalogId"]) in hidden
            for source in sources
        ))
        self.assertTrue(all(source["genre"] == "All" for source in sources))
        self.assertEqual(len(sources), len({
            (source["type"], source["catalogId"]) for source in sources
        }))
        self.assertEqual(collection, json.loads(json.dumps(collection)))

    async def test_legacy_private_catalog_route_remains_a_scoped_transition_alias(self):
        legacy_id = "dp_streaming_netflix_movie-01"

        self.assertEqual(
            profile_streaming.get_metas_for_user(self.alice, "movie", legacy_id),
            _metas("a"),
        )
        self.assertEqual(
            profile_streaming.get_metas_for_user(self.bob, "movie", legacy_id),
            _metas("b"),
        )
        self.assertIsNone(
            profile_streaming.get_metas_for_user(self.alice, "series", legacy_id)
        )

    async def test_static_import_pack_contains_folded_private_collections_only(self):
        alice_clone = {
            **self.alice,
            "token": "alice-second-secret-token",
            "name": "Alice",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            collection_dir = Path(temp_dir)
            helper = collection_dir / "HELPER_ADDON_MANIFEST_INSTALL_THIS_FIRST.json"
            helper.write_text("legacy helper")
            stale = collection_dir / "NUVIO_COLLECTION_IMPORT_STALE_STREAMING.json"
            stale.write_text("legacy collection")

            with (
                patch.object(profile_streaming, "COLLECTION_DIR", collection_dir),
                patch.object(profile_streaming.db, "all_users", AsyncMock(
                    return_value=[self.alice, alice_clone, self.bob])),
            ):
                await profile_streaming.write_import_files()

            alice_path = collection_dir / "NUVIO_COLLECTION_IMPORT_ALPHA_STREAMING.json"
            bob_path = collection_dir / "NUVIO_COLLECTION_IMPORT_BETA_STREAMING.json"
            alice_collection = json.loads(alice_path.read_text())
            bob_collection = json.loads(bob_path.read_text())
            import_paths = sorted(collection_dir.glob(
                "NUVIO_COLLECTION_IMPORT_*_STREAMING*.json"))
            imports = [json.loads(path.read_text()) for path in import_paths]
            alice_sources = [
                source
                for folder in alice_collection[0]["folders"]
                for source in folder["catalogSources"]
            ]
            bob_sources = [
                source
                for folder in bob_collection[0]["folders"]
                for source in folder["catalogSources"]
            ]

            self.assertFalse(helper.exists())
            self.assertFalse(stale.exists())
            self.assertEqual(len(imports), 3)
            self.assertTrue(alice_sources)
            self.assertTrue(bob_sources)
            self.assertTrue(all(
                source["addonId"] == self.addon_id for source in alice_sources
            ))
            self.assertNotEqual(
                alice_sources[0]["addonId"], bob_sources[0]["addonId"]
            )
            self.assertEqual(len({
                collection[0]["folders"][0]["catalogSources"][0]["addonId"]
                for collection in imports
            }), 3)
            self.assertTrue(all(
                "sources" not in folder for folder in alice_collection[0]["folders"]
            ))
            serialized = "\n".join(json.dumps(collection) for collection in imports)
            self.assertNotIn(config.PROFILE_STREAMING_ADDON_ID, serialized)
            self.assertNotIn("ps_alpha_", serialized)
            for user in (self.alice, alice_clone, self.bob):
                self.assertNotIn(user["token"], serialized)
            readme = (collection_dir / "README.md").read_text()
            self.assertIn("do not install a streaming helper", readme.casefold())

    async def test_manual_refresh_queues_home_and_streaming_rebuilds(self):
        generate = AsyncMock()
        build = AsyncMock()
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=self.alice)),
            patch.object(main, "generate_for_user", generate),
            patch.object(main.profile_streaming, "build", build),
        ):
            response = await self.client.post(f"/{self.alice['token']}/refresh")
            await asyncio.sleep(0)

        self.assertEqual(response.status_code, 200)
        generate.assert_awaited_once_with(self.alice, trigger="manual-refresh")
        build.assert_awaited_once_with("alpha")

    async def test_unknown_token_fails_before_private_catalog_or_collection_lookup(self):
        with patch.object(main.db, "get_user", AsyncMock(return_value=None)):
            catalog = await self.client.get(
                f"/unknown/catalog/movie/{self.private_id}.json")
            collection = await self.client.get(
                "/unknown/streaming-collection.json")

        self.assertEqual(catalog.status_code, 404)
        self.assertEqual(collection.status_code, 404)

    async def test_public_refresh_trigger_is_no_longer_available(self):
        with patch.object(main.db, "get_user", AsyncMock(return_value=None)):
            response = await self.client.get("/streaming-profiles/refresh")

        self.assertEqual(response.status_code, 404)

    async def test_public_streaming_helper_and_legacy_collections_are_retired(self):
        manifest = await self.client.get("/streaming-profiles/manifest.json")
        catalog = await self.client.get(
            "/streaming-profiles/catalog/movie/ps_alpha_netflix_taste-01.json")
        collection = await self.client.get(
            "/streaming-profiles/collection/alpha.json")

        self.assertEqual(manifest.status_code, 410)
        self.assertEqual(catalog.status_code, 410)
        self.assertEqual(collection.status_code, 410)
        self.assertIn("folded", manifest.json()["detail"])


if __name__ == "__main__":
    unittest.main()
