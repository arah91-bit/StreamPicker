import unittest

from app import proxy, telemetry


class _Response:
    def __init__(self, status, content_range="", content_length=""):
        self.status_code = status
        self.headers = {}
        if content_range:
            self.headers["content-range"] = content_range
        if content_length:
            self.headers["content-length"] = content_length


class ProxyRangeSafetyTests(unittest.TestCase):
    def test_suffix_range_is_recognized(self):
        self.assertEqual((0, None, True), proxy._parse_range("bytes=-65536"))
        self.assertEqual(65536, proxy._suffix_length("bytes=-65536"))

    def test_nonzero_range_rejects_full_file_200(self):
        self.assertFalse(proxy._range_response_ok(
            _Response(200, content_length="1000"), "bytes=500-"))

    def test_nonzero_range_requires_exact_content_range_start(self):
        self.assertTrue(proxy._range_response_ok(
            _Response(206, "bytes 500-999/1000"), "bytes=500-"))
        self.assertFalse(proxy._range_response_ok(
            _Response(206, "bytes 0-499/1000"), "bytes=500-"))

    def test_suffix_validates_tail_length_and_end(self):
        self.assertTrue(proxy._range_response_ok(
            _Response(206, "bytes 900-999/1000"), "bytes=-100"))
        self.assertFalse(proxy._range_response_ok(
            _Response(206, "bytes 800-899/1000"), "bytes=-100"))


class StrongSignatureTests(unittest.TestCase):
    @staticmethod
    def stream(filename):
        return {"behaviorHints": {"filename": filename}}

    def test_long_shared_prefixes_do_not_collide(self):
        prefix = "Very.Long.Release.Name." + "A" * 100
        one = telemetry.signature(self.stream(prefix + ".CUT-ONE.mkv"))
        two = telemetry.signature(self.stream(prefix + ".CUT-TWO.mkv"))
        self.assertTrue(one.startswith("file:"))
        self.assertNotEqual(one, two)

    def test_legacy_session_signatures_are_scrubbed(self):
        entry = {"cands": [{"sig": "old-truncated"}],
                 "pool": [{"sig": "nzb:" + "a" * 64}],
                 "bufsig": "old-truncated"}
        self.assertEqual(1, proxy._scrub_legacy_sigs(entry))
        self.assertEqual("", entry["cands"][0]["sig"])
        self.assertNotIn("bufsig", entry)


if __name__ == "__main__":
    unittest.main()
