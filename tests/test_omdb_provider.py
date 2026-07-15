"""Persistent, quota-safe OMDb provider and dashboard-secret regressions."""

import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app import config, connections, omdb


def movie_payload(imdb_id: str, **changes) -> dict:
    payload = {
        "Title": "Ghost in the Shell",
        "Year": "1995",
        "Runtime": "83 min",
        "Country": "Japan, United Kingdom",
        "Language": "Japanese, English",
        "imdbID": imdb_id,
        "Type": "movie",
        "Response": "True",
        # These deliberately must not enter the normalized cache.
        "Plot": "raw plot must not be persisted",
        "Poster": "https://poster.example/private-path",
    }
    payload.update(changes)
    return payload


def episode_payload(series_id: str, season: int, episode: int,
                    **changes) -> dict:
    payload = {
        "Title": "Pilot",
        "Released": "24 Mar 2005",
        "Runtime": "23 min",
        "Country": "United States",
        "Language": "English",
        "imdbID": "tt10000001",
        "seriesID": series_id,
        "Season": str(season),
        "Episode": str(episode),
        "Type": "episode",
        "Response": "True",
    }
    payload.update(changes)
    return payload


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class FakeClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.responder, BaseException):
            raise self.responder
        result = self.responder(url, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result


class MutableClock:
    def __init__(self, value=1_700_000_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class OMDbProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sp-omdb-")
        self.path = str(Path(self.tmp.name) / "omdb.sqlite3")
        self.providers = []

    async def asyncTearDown(self):
        for provider in reversed(self.providers):
            await provider.close()
        self.tmp.cleanup()

    def provider(self, client, **kwargs):
        provider = omdb.OMDbProvider(
            kwargs.pop("api_key", "secret-omdb-key-123"),
            path=self.path, client=client, **kwargs)
        self.providers.append(provider)
        return provider

    async def test_exact_id_lookup_normalizes_selected_fields_only(self):
        client = FakeClient(lambda _url, kw: FakeResponse(
            movie_payload(kw["params"]["i"])))
        provider = self.provider(client)

        record = await provider.lookup("movie", "tt0113568")

        self.assertEqual("tt0113568", record.imdb_id)
        self.assertEqual("Ghost in the Shell", record.title)
        self.assertEqual(1995, record.year)
        self.assertEqual(83, record.runtime_minutes)
        self.assertEqual(4980.0, record.runtime_seconds)
        self.assertEqual(("Japan", "United Kingdom"), record.countries)
        self.assertEqual(("Japanese", "English"), record.languages)
        self.assertEqual(1, len(client.calls))
        url, call = client.calls[0]
        self.assertEqual(omdb.API_URL, url)
        self.assertEqual({"apikey": "secret-omdb-key-123",
                          "i": "tt0113568", "r": "json"}, call["params"])
        self.assertNotIn("t", call["params"])
        self.assertNotIn("s", call["params"])

        columns = {row[1] for row in sqlite3.connect(self.path).execute(
            "PRAGMA table_info(title_cache)")}
        self.assertEqual(
            {"imdb_id", "item_type", "title", "year", "runtime_minutes",
             "countries", "languages", "fetched_at"}, columns)
        for candidate in Path(self.tmp.name).glob("omdb.sqlite3*"):
            raw = candidate.read_bytes()
            self.assertNotIn(b"secret-omdb-key-123", raw)
            self.assertNotIn(b"raw plot must not be persisted", raw)
            self.assertNotIn(b"poster.example", raw)
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)

    async def test_positive_cache_survives_provider_restart(self):
        first_client = FakeClient(lambda _url, kw: FakeResponse(
            movie_payload(kw["params"]["i"])))
        first = self.provider(first_client)
        wanted = await first.lookup("movie", "tt0113568")
        await first.close()
        self.providers.remove(first)

        second_client = FakeClient(RuntimeError("network must not be used"))
        second = self.provider(second_client)
        cached = await second.lookup("movie", "tt0113568")

        self.assertEqual(wanted, cached)
        self.assertEqual([], second_client.calls)

    async def test_stale_positive_is_returned_when_refresh_fails(self):
        clock = MutableClock()
        first_client = FakeClient(lambda _url, kw: FakeResponse(
            movie_payload(kw["params"]["i"])))
        first = self.provider(first_client, clock=clock, positive_ttl=10)
        wanted = await first.lookup("movie", "tt0113568")
        await first.close()
        self.providers.remove(first)

        clock.value += 20
        failing = FakeClient(RuntimeError("request URL contained a secret"))
        second = self.provider(failing, clock=clock, positive_ttl=10)
        with self.assertLogs("stream-picker", level="WARNING") as captured:
            stale = await second.lookup("movie", "tt0113568")

        self.assertEqual(wanted, stale)
        self.assertEqual(1, len(failing.calls))
        self.assertNotIn("secret-omdb-key-123", "\n".join(captured.output))
        self.assertNotIn("request URL contained", "\n".join(captured.output))

    async def test_singleflight_survives_one_waiter_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()

        class BlockingClient(FakeClient):
            async def get(inner_self, url, **kwargs):
                inner_self.calls.append((url, kwargs))
                started.set()
                await release.wait()
                return FakeResponse(movie_payload(kwargs["params"]["i"]))

        client = BlockingClient(lambda *_: None)
        provider = self.provider(client)
        one = asyncio.create_task(provider.lookup("movie", "tt0113568"))
        two = asyncio.create_task(provider.lookup("movie", "tt0113568"))
        await started.wait()
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await one
        release.set()

        self.assertEqual("tt0113568", (await two).imdb_id)
        self.assertEqual(1, len(client.calls))

    async def test_utc_budget_is_persistent_and_resets_next_day(self):
        clock = MutableClock()
        client = FakeClient(lambda _url, kw: FakeResponse(
            movie_payload(kw["params"]["i"])))
        first = self.provider(client, clock=clock, daily_budget=1)

        self.assertIsNotNone(await first.lookup("movie", "tt0113568"))
        self.assertIsNone(await first.lookup("movie", "tt0133093"))
        self.assertEqual(1, len(client.calls))
        self.assertEqual({"utc_day": first._day(), "used": 1, "limit": 1,
                          "remaining": 0}, first.quota_status())
        await first.close()
        self.providers.remove(first)

        # A restart on the same day cannot evade the durable counter.
        second = self.provider(client, clock=clock, daily_budget=1)
        self.assertIsNone(await second.lookup("movie", "tt0133093"))
        self.assertEqual(1, len(client.calls))
        clock.value += 86400
        self.assertIsNotNone(await second.lookup("movie", "tt0133093"))
        self.assertEqual(2, len(client.calls))

    async def test_id_and_type_mismatches_are_never_cached(self):
        wrong_id = FakeClient(lambda _url, _kw: FakeResponse(
            movie_payload("tt9999999")))
        first = self.provider(wrong_id)
        self.assertIsNone(await first.lookup("movie", "tt0113568"))
        count = sqlite3.connect(self.path).execute(
            "SELECT count(*) FROM title_cache").fetchone()[0]
        self.assertEqual(0, count)
        await first.close()
        self.providers.remove(first)

        wrong_type = FakeClient(lambda _url, kw: FakeResponse(
            movie_payload(kw["params"]["i"], Type="series")))
        second = self.provider(wrong_type)
        self.assertIsNone(await second.lookup("movie", "tt0113568"))
        count = sqlite3.connect(self.path).execute(
            "SELECT count(*) FROM title_cache").fetchone()[0]
        self.assertEqual(0, count)

    async def test_reflected_api_key_is_never_persisted(self):
        secret = "secret-omdb-key-123"
        client = FakeClient(lambda _url, kw: FakeResponse(movie_payload(
            kw["params"]["i"], Title=f"reflected {secret}")))
        provider = self.provider(client, api_key=secret)

        self.assertIsNone(await provider.lookup("movie", "tt0113568"))
        count = sqlite3.connect(self.path).execute(
            "SELECT count(*) FROM title_cache").fetchone()[0]
        self.assertEqual(0, count)
        for candidate in Path(self.tmp.name).glob("omdb.sqlite3*"):
            self.assertNotIn(secret.encode(), candidate.read_bytes())

    async def test_exact_episode_has_separate_cache_and_exact_runtime(self):
        series_id = "tt0386676"

        def answer(_url, kw):
            params = kw["params"]
            if "Season" in params:
                return FakeResponse(episode_payload(
                    params["i"], int(params["Season"]),
                    int(params["Episode"])))
            return FakeResponse(movie_payload(
                params["i"], Title="The Office", Year="2005–2013",
                Runtime="22 min", Type="series"))

        client = FakeClient(answer)
        provider = self.provider(client)
        show, episode = await asyncio.gather(
            provider.lookup("series", series_id),
            provider.lookup_episode(series_id, 1, 1),
        )

        self.assertEqual("The Office", show.title)
        self.assertEqual(2005, show.year)
        self.assertEqual(23, episode.runtime_minutes)
        self.assertEqual(1380.0, episode.runtime_seconds)
        self.assertEqual("tt10000001", episode.episode_imdb_id)
        self.assertEqual(2, len(client.calls))
        episode_params = next(c[1]["params"] for c in client.calls
                              if "Season" in c[1]["params"])
        self.assertEqual(
            {"apikey": "secret-omdb-key-123", "i": series_id,
             "Season": "1", "Episode": "1", "r": "json"},
            episode_params)
        self.assertNotIn("t", episode_params)
        self.assertNotIn("s", episode_params)

        # Both exact records are now free persistent hits.
        await provider.close()
        self.providers.remove(provider)
        no_network = FakeClient(RuntimeError("must stay cached"))
        restarted = self.provider(no_network)
        self.assertEqual(show, await restarted.lookup("series", series_id))
        self.assertEqual(
            episode, await restarted.lookup_episode(series_id, 1, 1))
        self.assertEqual([], no_network.calls)

    async def test_episode_response_must_bind_series_type_and_numbers(self):
        cases = [
            {"seriesID": "tt9999999"},
            {"Type": "movie"},
            {"Season": "2"},
            {"Episode": "2"},
            {"imdbID": "not-an-imdb-id"},
        ]
        for index, changes in enumerate(cases):
            path = str(Path(self.tmp.name) / f"bad-episode-{index}.sqlite3")
            client = FakeClient(lambda _url, _kw, c=changes: FakeResponse(
                episode_payload("tt0386676", 1, 1, **c)))
            provider = omdb.OMDbProvider(
                "secret-omdb-key-123", path=path, client=client)
            self.providers.append(provider)
            self.assertIsNone(await provider.lookup_episode("tt0386676", 1, 1))
            count = sqlite3.connect(path).execute(
                "SELECT count(*) FROM episode_cache").fetchone()[0]
            self.assertEqual(0, count, changes)


class OMDbConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_dashboard_schema_masks_key_and_bounds_budget(self):
        self.assertTrue(config.is_secret("OMDB_API_KEY"))
        self.assertEqual("750", config.default("OMDB_DAILY_BUDGET"))
        self.assertEqual(900, config._SPECS["OMDB_DAILY_BUDGET"]["max"])
        self.assertIn("omdb", {row["id"] for row in config.CONNECTIONS})

    async def test_connection_test_is_one_exact_id_call(self):
        secret = "dashboard-omdb-secret"
        client = FakeClient(lambda _url, kw: FakeResponse(
            movie_payload(kw["params"]["i"])))
        old = connections._client
        connections._client = client
        try:
            result = await connections._omdb({"OMDB_API_KEY": secret})
        finally:
            connections._client = old

        self.assertTrue(result["ok"])
        self.assertNotIn(secret, str(result))
        self.assertEqual(1, len(client.calls))
        params = client.calls[0][1]["params"]
        self.assertEqual({"apikey": secret, "i": "tt0133093", "r": "json"},
                         params)
        self.assertNotIn("t", params)
        self.assertNotIn("s", params)

    async def test_connection_test_never_reflects_api_error(self):
        secret = "dashboard-omdb-secret"
        client = FakeClient(lambda *_: FakeResponse({
            "Response": "False",
            "Error": f"Invalid API key! {secret}",
        }))
        old = connections._client
        connections._client = client
        try:
            result = await connections._omdb({"OMDB_API_KEY": secret})
        finally:
            connections._client = old
        self.assertFalse(result["ok"])
        self.assertNotIn(secret, str(result))


if __name__ == "__main__":
    unittest.main()
