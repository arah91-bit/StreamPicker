"""Custom addons: any player /stream addon can be plugged in and becomes a
first-class source. Tests cover parsing/registration, the saved-value
validation, and the connection test wiring."""

import asyncio
import json
import os
import tempfile
import unittest

os.environ.setdefault("ADDON_SECRET", "test-secret")
os.environ["CONFIG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="sp-addons-"), "config.json")

from app import config, connections, sources


class SourceRegistrationTests(unittest.TestCase):
    def setUp(self):
        self._extras = list(sources.EXTRAS)
        self._meta = list(sources.EXTRA_META)

    def tearDown(self):
        # undo whatever _load_extras registered during a test
        for key in list(sources.EXTRAS):
            if key not in self._extras:
                sources._BASES.pop(key, None)
                sources._REQ_TIMEOUT.pop(key, None)
        sources.EXTRAS[:] = self._extras
        sources.EXTRA_META[:] = self._meta
        os.environ.pop("EXTRA_ADDONS", None)

    def test_manifest_url_becomes_a_registered_source(self):
        os.environ["EXTRA_ADDONS"] = json.dumps([
            {"name": "AIOStreams", "url": "https://aio.example/abc/manifest.json"},
            {"name": "Usenet Ultimate", "url": "https://uu.example/xyz"},
        ])
        sources._load_extras()
        # the /manifest.json suffix is stripped to the base the picker fetches
        aio = next(m for m in sources.EXTRA_META if m["name"] == "AIOStreams")
        self.assertEqual("https://aio.example/abc", aio["url"])
        self.assertTrue(sources.has(aio["key"]))
        # both extras are searched, and they slot in before the slow NZB lane
        keys = sources.search_all()
        self.assertIn(aio["key"], keys)
        if sources.has(sources.NZB):
            self.assertLess(keys.index(aio["key"]), keys.index(sources.NZB))

    def test_duplicate_names_get_distinct_keys(self):
        os.environ["EXTRA_ADDONS"] = json.dumps([
            {"name": "Same", "url": "https://a.example"},
            {"name": "Same", "url": "https://b.example"},
        ])
        sources._load_extras()
        new = [m for m in sources.EXTRA_META if m["name"] == "Same"]
        self.assertEqual(2, len(new))
        self.assertNotEqual(new[0]["key"], new[1]["key"])

    def test_bad_entries_ignored_not_crashing(self):
        os.environ["EXTRA_ADDONS"] = json.dumps([
            {"name": "no url"}, {"url": "ftp://nope"}, "garbage",
            {"name": "ok", "url": "https://ok.example"},
        ])
        sources._load_extras()
        names = [m["name"] for m in sources.EXTRA_META]
        self.assertIn("ok", names)
        self.assertNotIn("no url", names)


class AddonConfigValidationTests(unittest.TestCase):
    def setUp(self):
        try:
            os.unlink(os.environ["CONFIG_FILE"])
        except FileNotFoundError:
            pass

    def test_saved_addons_normalized(self):
        config.save({"EXTRA_ADDONS": json.dumps([
            {"name": "AIO", "url": "https://x.example/cfg/manifest.json/"}])})
        stored = json.loads(config.pending("EXTRA_ADDONS"))
        self.assertEqual("https://x.example/cfg", stored[0]["url"])
        self.assertEqual("AIO", stored[0]["name"])

    def test_invalid_json_rejected(self):
        with self.assertRaises(ValueError):
            config.save({"EXTRA_ADDONS": "{not json"})

    def test_non_http_url_rejected(self):
        with self.assertRaises(ValueError):
            config.save({"EXTRA_ADDONS": json.dumps(
                [{"name": "x", "url": "ftp://bad"}])})

    def test_empty_clears(self):
        config.save({"EXTRA_ADDONS": json.dumps(
            [{"name": "x", "url": "https://ok.example"}])})
        config.save({"EXTRA_ADDONS": ""})
        self.assertEqual("", config.pending("EXTRA_ADDONS"))


class AddonConnectionTestTests(unittest.TestCase):
    def test_service_registered(self):
        self.assertIn("addon", connections._TESTS)

    def test_no_url_fails_without_network(self):
        r = asyncio.run(connections.test("addon", {}))
        self.assertFalse(r["ok"])

    def test_non_http_url_fails_without_network(self):
        r = asyncio.run(connections.test("addon", {"url": "ftp://x.example"}))
        self.assertFalse(r["ok"])
        self.assertIn("http", r["detail"].lower())


if __name__ == "__main__":
    unittest.main()
