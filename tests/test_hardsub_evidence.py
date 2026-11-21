"""Burned-in subtitles, judged by the file rather than its name.

Check subtitle evidence from the media tracks and allow clean candidates time to arrive.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app import picker, probe, vprobe

FFMPEG = shutil.which("ffmpeg")


def _make(path: str, *extra: str, subs: bool = False) -> None:
    """A tiny real media file: 2 s of video + audio, optionally an English
    subtitle track. MP4 output keeps ffmpeg's default (index at EOF)."""
    args = [FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10",
            "-f", "lavfi", "-i", "sine=duration=2"]
    if subs:
        srt = path + ".srt"
        with open(srt, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,500\nHello\n")
        args += ["-i", srt, "-map", "0", "-map", "1", "-map", "2",
                 "-metadata:s:s:0", "language=eng"]
    args += ["-c:v", "mpeg4", "-c:a", "aac", *extra, path]
    subprocess.run(args, check=True, capture_output=True, timeout=60)


@unittest.skipUnless(FFMPEG and vprobe.enabled(), "needs ffmpeg/ffprobe")
class TrackSniffTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _details(self, data: bytes):
        return asyncio.run(vprobe.media_details_of(data))

    def test_soft_subtitles_are_listed_by_language(self):
        path = os.path.join(self.dir.name, "soft.mkv")
        _make(path, "-c:s", "srt", subs=True)
        with open(path, "rb") as f:
            d = self._details(f.read())
        self.assertEqual(["en"], d.subs)

    def test_no_subtitle_track_is_a_measured_empty_list(self):
        path = os.path.join(self.dir.name, "none.mkv")
        _make(path)
        with open(path, "rb") as f:
            d = self._details(f.read())
        self.assertEqual([], d.subs, "[] = looked and found none")

    def test_unreadable_bytes_are_unknown_not_empty(self):
        self.assertIsNone(self._details(b"\0" * 4096).subs,
                          "None = never read; must not be mistaken for 'none'")


@unittest.skipUnless(FFMPEG and vprobe.enabled(), "needs ffmpeg/ffprobe")
class Mp4IndexTests(unittest.IsolatedAsyncioTestCase):
    """An MP4 that keeps its index after the media data tells a head sniff
    nothing; one bounded range read of the index tells everything."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _file(self, name: str, **kw) -> bytes:
        path = os.path.join(self.dir.name, name)
        _make(path, **kw)
        with open(path, "rb") as f:
            return f.read()

    @staticmethod
    def _head(data: bytes) -> bytes:
        """The first bytes up to just inside mdat — what a 2 MB sniff of a
        real episode sees: ftyp, the mdat header, no moov."""
        for kind, off, size in probe._mp4_boxes(data):
            if kind == b"mdat":
                return data[:off + 64]
        raise AssertionError("no mdat in test file")

    async def test_index_is_located_and_fetched_from_the_end(self):
        data = self._file("tail.mp4")
        head = self._head(data)
        self.assertIsNone((await vprobe.media_details_of(head)).subs)
        start = probe._mp4_index_offset(head, len(data))
        self.assertEqual(b"moov", data[start + 4:start + 8])

        class Resp:
            status_code = 206

            def __init__(self, rng):
                a = int(rng.split("=")[1].split("-")[0])
                self.body = data[a:]
                self.headers = {"content-range":
                                f"bytes {a}-{len(data) - 1}/{len(data)}"}

            async def aiter_bytes(self):
                yield self.body

        class Ctx:
            def __init__(self, rng):
                self.r = Resp(rng)

            async def __aenter__(self):
                return self.r

            async def __aexit__(self, *a):
                return False

        class Client:
            def stream(self, method, url, headers=None, **kw):
                return Ctx(headers["Range"])

        with patch.object(probe, "_client", Client()):
            index = await probe._mp4_index("https://cdn/x.mp4", head,
                                           len(data), None)
        d = await vprobe.media_details_of(index)
        self.assertTrue(d.video, "the stitched ftyp+moov must parse")
        self.assertEqual([], d.subs)

    async def test_index_already_in_the_head_needs_no_fetch(self):
        data = self._faststart()
        self.assertIsNone(probe._mp4_index_offset(data[:4096], len(data)))

    def _faststart(self) -> bytes:
        path = os.path.join(self.dir.name, "fs.mp4")
        _make(path, "-movflags", "+faststart")
        with open(path, "rb") as f:
            return f.read()

    def test_an_implausibly_large_tail_is_not_fetched(self):
        head = (b"\0\0\0\x20ftypisom" + b"\0" * 20
                + (1 << 20).to_bytes(4, "big") + b"mdat")
        self.assertIsNone(probe._mp4_index_offset(
            head, (1 << 20) + 32 + probe._MP4_INDEX_MAX + 1))


class CleanLevelTests(unittest.TestCase):
    def setUp(self):
        self.token = picker._original_lang.set("zh")
        self.addCleanup(picker._original_lang.reset, self.token)

    @staticmethod
    def _s(filename, **kw):
        return dict({"name": "x", "behaviorHints": {"filename": filename}}, **kw)

    def test_an_explicit_tag_is_believed_over_soft_tracks(self):
        s = self._s("Show.S01E01.1080p.WEB.HC-GRP.mkv", _subs=["en"])
        self.assertEqual(0, picker._clean_level(s))

    def test_a_cjk_prefix_alone_is_a_hint_soft_tracks_override(self):
        name = "示例.Sample.Series.S01E13.2025.1080p.DSNP.WEB-DL-ColorTV.mkv"
        self.assertEqual(0, picker._clean_level(self._s(name)))
        self.assertEqual(0, picker._clean_level(self._s(name, _subs=[])))
        self.assertEqual(3, picker._clean_level(self._s(name, _subs=["en", "zh"])))
        self.assertEqual(0, picker._clean_level(self._s(name, _subs=["zh", "zh"])),
                         "Chinese soft tracks are no evidence against Chinese burn-in")
        self.assertEqual(2, picker._clean_level(self._s(name, _subs=[""])),
                         "an untagged track might be English")

    def test_only_english_soft_subtitles_count(self):
        name = "Example.Show.S01E17.2026.2160p.HQ.WEB-DL.H265.DDP5.1-BlackTV.mkv"
        self.assertEqual(1, picker._clean_level(self._s(name, _subs=["zh", "zh"])))
        self.assertEqual(2, picker._clean_level(self._s(
            name, _subs=["zh"], _audio_langs=["zh", "en"])))
        self.assertEqual(3, picker._clean_level(self._s(name, _subs=["zh", "en"])))
        self.assertEqual(2, picker._clean_level(self._s(name, _subs=["", "zh"])))
        self.assertEqual(0, picker._soft_subs(self._s(name, _subs=["zh", "zh"])))
        self.assertEqual(1, picker._soft_subs(self._s(name, _subs=["zh", "en"])))

    def test_no_subtitle_track_in_a_foreign_title_is_demoted(self):
        zmweb = self._s("Sample.Series.2025.S01E13.2160p.WEB-DL.HEVC.AAC5.1-ZmWeb.mp4",
                        _subs=[])
        self.assertEqual(1, picker._clean_level(zmweb))

    def test_english_audio_needs_no_subtitles(self):
        dub = self._s("Show.S01E01.1080p.Dual-Audio.mkv", _subs=[],
                      _audio_langs=["ja", "en"])
        self.assertEqual(2, picker._clean_level(dub))

    def test_unmeasured_is_not_evidence(self):
        self.assertEqual(2, picker._clean_level(self._s("Show.S01E01.1080p.mp4")))

    def test_an_english_title_without_subtitles_is_fine(self):
        token = picker._original_lang.set("en")
        try:
            self.assertEqual(2, picker._clean_level(
                self._s("Movie.2024.1080p.WEB-DL.mkv", _subs=[])))
        finally:
            picker._original_lang.reset(token)

    def test_kill_switch(self):
        with patch.object(picker, "HARDSUB_DEMOTE", False):
            self.assertEqual(2, picker._clean_level(
                self._s("Show.HC.HardSub.mkv")))

    def test_clean_1080p_outranks_burned_in_2160p(self):
        zmweb = {"name": "2160p", "url": "https://a/z",
                 "behaviorHints": {"filename":
                                   "Sample.Series.S01E13.2160p.WEB-DL.HEVC-ZmWeb.mp4"},
                 "_subs": [], "_effres": 2160, "_qbps": 20_000_000}
        dsnp = {"name": "1080p", "url": "https://a/d",
                "behaviorHints": {"filename":
                                  "Sample.Series.S01E13.1080p.DSNP.WEB-DL-CHIOS.mkv"},
                "_subs": ["en", "en", "zh"], "_effres": 1080, "_qbps": 8_000_000}
        ranked = sorted([zmweb, dsnp], key=picker._quality_key, reverse=True)
        self.assertEqual("https://a/d", ranked[0]["url"])

    def test_chinese_only_subtitles_do_not_outrank_english_ones(self):
        """A Chinese-only soft track must not outrank an English track."""
        domestic = {"name": "2160p", "url": "https://a/blacktv",
                    "behaviorHints": {"filename":
                                      "Example.Show.S01E17.2026.2160p.HQ.WEB-DL."
                                      "H265.DDP5.1-BlackTV.mkv"},
                    "_subs": ["zh", "zh"], "_audio_langs": ["zh"],
                    "_effres": 2160, "_qbps": 20_000_000}
        netflix = {"name": "1080p", "url": "https://a/nf",
                   "behaviorHints": {"filename":
                                     "Example.Show.S01E17.2026.1080p.NF.WEB-DL."
                                     "DDP2.0.H.264-ANDY.mkv"},
                   "_subs": ["en", "zh", "zh"], "_audio_langs": ["zh"],
                   "_effres": 1080, "_qbps": 6_000_000}
        ranked = sorted([domestic, netflix], key=picker._quality_key, reverse=True)
        self.assertEqual("https://a/nf", ranked[0]["url"])


class SoftSubtitleTiebreakTests(unittest.TestCase):
    """soft > none applies to English titles too — but only between copies
    of the same quality: picture quality comes first when subtitles are
    optional."""

    def _v(self, name, res, qbps, subs):
        s = {"name": f"{res}p", "url": f"https://x/{name}",
             "behaviorHints": {"filename": f"Movie.2024.{res}p.WEB-DL-{name}.mkv"},
             "_effres": res, "_qbps": qbps, "_subs": subs}
        return s, probe.ProbeResult(True, ttfb=0.5, speed_bps=30e6)

    def setUp(self):
        self.token = picker._original_lang.set("en")
        self.addCleanup(picker._original_lang.reset, self.token)

    def test_same_quality_prefers_switchable_subtitles(self):
        plain = self._v("PLAIN", 1080, 8_000_000, [])
        soft = self._v("SOFT", 1080, 8_000_000, ["en"])
        for key in (picker._verified_key, picker._verified_quality_key):
            with self.subTest(key=key.__name__):
                best = max([plain, soft], key=key)
                self.assertEqual("https://x/SOFT", best[0]["url"])

    def test_better_picture_still_wins_for_an_english_title(self):
        plain4k = self._v("UHD", 2160, 30_000_000, [])
        soft1080 = self._v("SOFT", 1080, 8_000_000, ["en"])
        best = max([plain4k, soft1080], key=picker._verified_quality_key)
        self.assertEqual("https://x/UHD", best[0]["url"])


class RaceKeepsLookingForCleanTests(unittest.IsolatedAsyncioTestCase):
    HARDSUB = {"name": "2160p ZmWeb", "url": "https://z.example/v",
               "behaviorHints": {"filename":
                                 "Example.Show.2024.2160p.WEB-DL.HEVC-ZmWeb.mp4"}}
    CLEAN = {"name": "1080p CHIOS", "url": "https://c.example/v",
             "behaviorHints": {"filename":
                               "Example.Show.2024.1080p.DSNP.WEB-DL-CHIOS.mkv"}}

    async def _race(self, orig: str):
        probed = []

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            stream = candidates[0]
            probed.append(stream["url"])
            if stream["url"] == self.HARDSUB["url"]:
                return [(stream, probe.ProbeResult(
                    True, ttfb=0.01, speed_bps=30_000_000, sub_langs=()))]
            await asyncio.sleep(0.05)
            return [(stream, probe.ProbeResult(
                True, ttfb=0.01, speed_bps=20_000_000, sub_langs=("en",)))]

        token = picker._original_lang.set(orig)
        try:
            with (patch("app.picker.sources.search_all",
                        return_value=[picker.sources.FAST]),
                  patch("app.sources.get",
                        return_value=[dict(self.HARDSUB), dict(self.CLEAN)]),
                  patch("app.sources.peek", return_value=[]),
                  patch("app.picker.nzb_lane.in_progress", return_value=False),
                  patch("app.probe.probe_race", side_effect=fake_probe_race),
                  patch("app.reputation.blocked", return_value=False),
                  patch.object(picker, "PROBE_BATCH", 1)):
                verified, _ = await picker._race_fast(
                    "movie", "tt1234567", picker.PROFILES["full"], 7200,
                    lambda _s: 1_000_000, time.monotonic())
        finally:
            picker._original_lang.reset(token)
        return verified, probed

    async def test_a_verified_hardsub_does_not_end_the_race(self):
        verified, probed = await self._race("zh")
        self.assertEqual([self.HARDSUB["url"], self.CLEAN["url"]], probed)
        self.assertEqual(self.CLEAN["url"], verified[0][0]["url"],
                         "the clean release must lead, burned-in 4K or not")

    async def test_an_english_title_still_stops_on_its_best(self):
        verified, probed = await self._race("en")
        self.assertEqual([self.HARDSUB["url"]], probed)
        self.assertEqual(self.HARDSUB["url"], verified[0][0]["url"])


class RaceKeepsLookingForAudioTests(unittest.IsolatedAsyncioTestCase):
    """The household's other complaint: a kids' show whose first pick only
    had French audio. Ranking already put a confirmed foreign dub last; the
    race, judging on resolution alone, could still stop on one as its only
    verified row."""

    DUB = {"name": "1080p", "url": "https://d.example/v",
           "behaviorHints": {"filename": "Kids.Show.S01E03.1080p.WEB-DL.mkv"}}
    ENGLISH = {"name": "720p", "url": "https://e.example/v",
               "behaviorHints": {"filename": "Kids.Show.S01E03.720p.WEB-DL.mkv"}}

    async def test_a_verified_foreign_dub_does_not_end_the_race(self):
        probed = []

        async def fake_probe_race(candidates, *_a, outcomes=None, **_k):
            stream = candidates[0]
            probed.append(stream["url"])
            langs = ("fr",) if stream["url"] == self.DUB["url"] else ("en",)
            if stream["url"] != self.DUB["url"]:
                await asyncio.sleep(0.05)
            return [(stream, probe.ProbeResult(
                True, ttfb=0.01, speed_bps=20_000_000, audio_langs=langs,
                sub_langs=()))]

        tokens = (picker._accept_langs.set(frozenset({"en"})),
                  picker._original_lang_known.set(True),
                  picker._original_lang.set("en"))
        try:
            with (patch("app.picker.sources.search_all",
                        return_value=[picker.sources.FAST]),
                  patch("app.sources.get",
                        return_value=[dict(self.DUB), dict(self.ENGLISH)]),
                  patch("app.sources.peek", return_value=[]),
                  patch("app.picker.nzb_lane.in_progress", return_value=False),
                  patch("app.probe.probe_race", side_effect=fake_probe_race),
                  patch("app.reputation.blocked", return_value=False),
                  patch.object(picker, "PROBE_BATCH", 1)):
                verified, _ = await picker._race_fast(
                    "series", "tt1234567:1:3", picker.PROFILES["full"], 1320,
                    lambda _s: 1_000_000, time.monotonic())
        finally:
            picker._original_lang.reset(tokens[2])
            picker._original_lang_known.reset(tokens[1])
            picker._accept_langs.reset(tokens[0])
        self.assertEqual([self.DUB["url"], self.ENGLISH["url"]], probed)
        self.assertEqual(self.ENGLISH["url"], verified[0][0]["url"],
                         "English audio leads, even at a lower resolution")


if __name__ == "__main__":
    unittest.main()
