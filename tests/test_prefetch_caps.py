"""The next-episode prefetch must rank for, and cache under, the screen that
is actually playing.

Field-review case (2026-10-15). The household's own player sends
X-Display-Caps (with a device id) on every stream request, so its answers are
cached under a caps-tagged key. The prefetch is started from inside the proxy,
on a media request that carries no such header, and three call sites built
their keys by hand without the tag. Every next episode prefetched for those
televisions was ranked for no display in particular and stored where nothing
would ever read it.
"""

import unittest
from unittest import mock

from app import display_caps, picker, probe, proxy

HEADER = "hdr=hdr10,hlg; dv=0; maxlum=215; audio=ac3,eac3; override=sdr; dev=tv-lounge"


def _verified(filename: str) -> dict:
    stream = {"name": "[TB+] Show", "title": filename,
              "url": "https://stream.invalid/" + filename,
              "behaviorHints": {"filename": filename}}
    picker._annotate_quality([stream], 2_400)
    return picker._mark(stream, 1, probe.ProbeResult(True, ttfb=0.1,
                                                      speed_bps=20_000_000))


class HeaderRoundTripTests(unittest.TestCase):
    def test_header_parses_back_to_the_same_caps(self):
        caps = display_caps.parse(HEADER)
        self.assertEqual(caps, display_caps.parse(caps.header()))
        self.assertEqual(caps.cache_token(),
                         display_caps.parse(caps.header()).cache_token())

    def test_partial_caps_round_trip(self):
        for raw in ("dev=abc", "dv=1", "maxlum=600", "audio=truehd",
                    "hdr=hdr10plus", "override=hdr"):
            caps = display_caps.parse(raw)
            with self.subTest(raw=raw):
                self.assertEqual(caps, display_caps.parse(caps.header()))


class SessionCarriesCapsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (mock.patch.object(proxy, "_sessions", {}),
                  mock.patch.object(proxy, "_persist")):
            p.start()
            self.addCleanup(p.stop)

    def test_a_minted_token_remembers_the_screen(self):
        token = display_caps.current_caps.set(display_caps.parse(HEADER))
        try:
            t = proxy._mint([{"u": "https://x/f.mkv"}], [], "series",
                            "tt1:1:1", "fast")
        finally:
            display_caps.current_caps.reset(token)
        self.assertEqual(display_caps.parse(HEADER),
                         display_caps.parse(proxy._sessions[t][1]["caps"]))

    def test_a_stock_player_token_carries_nothing(self):
        t = proxy._mint([{"u": "https://x/f.mkv"}], [], "series",
                        "tt1:1:1", "fast")
        self.assertNotIn("caps", proxy._sessions[t][1])

    async def test_the_prefetch_runs_as_that_screen(self):
        seen = []

        async def prefetch(media, media_id, label):
            seen.append(display_caps.current())

        with mock.patch.object(picker, "note_playback"), \
             mock.patch.object(picker, "prefetch_next", new=prefetch):
            await proxy._fire_prefetch("tt1:1:1", "fast", "", HEADER)
            await proxy._fire_prefetch("tt1:1:1", "fast", "", "")
        self.assertEqual([display_caps.parse(HEADER), None], seen)


class PrefetchKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefetch_is_cached_where_the_same_screen_will_look(self):
        nxt = "tt9999999:1:2"
        stream = _verified("Show.S01E02.1080p.WEB-DL-GOOD.mkv")

        async def a_pick(media, media_id, profile):
            return [dict(stream)]

        token = display_caps.current_caps.set(display_caps.parse(HEADER))
        try:
            with mock.patch.object(picker, "_cache", {}), \
                    mock.patch.object(picker, "_stale_cache", {}), \
                    mock.patch.object(picker, "_background", {}), \
                    mock.patch.object(picker, "_prefetching", set()), \
                    mock.patch.object(picker, "_next_episode",
                                      new=mock.AsyncMock(return_value=nxt)), \
                    mock.patch.object(picker.reputation, "blocked",
                                      return_value=False), \
                    mock.patch.object(picker.reputation, "cooled",
                                      return_value=False), \
                    mock.patch.object(picker, "pick", new=a_pick), \
                    mock.patch.object(picker, "pick_slow", new=a_pick):
                await picker.prefetch_next("series", "tt9999999:1:1", "fast")
                fast_key = picker._result_key("full", "series", nxt)
                slow_key = picker._result_key("full", "series", nxt, slow=True)
                tag = display_caps.parse(HEADER).cache_token()

                self.assertEqual({fast_key, slow_key}, set(picker._cache))
                self.assertTrue(fast_key.startswith(tag + "|"))
                self.assertIsNotNone(picker._cached_candidate(fast_key),
                                     "the next open from this screen hits")
        finally:
            display_caps.current_caps.reset(token)

    def test_playback_touch_finds_the_caps_tagged_entry(self):
        media_id = "tt9999999:1:1"
        stream = _verified("Show.S01E01.1080p.WEB-DL-GOOD.mkv")
        sig = picker.telemetry.signature(stream)
        token = display_caps.current_caps.set(display_caps.parse(HEADER))
        try:
            key = picker._result_key("full", "series", media_id)
            old = 1.0
            with mock.patch.object(picker, "_cache", {key: (old, [stream])}), \
                    mock.patch.object(picker.time, "monotonic",
                                      return_value=100.0), \
                    mock.patch.object(picker.reputation, "blocked",
                                      return_value=False), \
                    mock.patch.object(picker.reputation, "cooled",
                                      return_value=False):
                picker.note_playback("series", media_id, "fast", sig)
                self.assertEqual(100.0, picker._cache[key][0])
        finally:
            display_caps.current_caps.reset(token)


if __name__ == "__main__":
    unittest.main()
