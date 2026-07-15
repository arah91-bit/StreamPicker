"""Duplicate-release recognition across addons, and what it means for probing.

Several user-added scraper addons carry the same files (same upstream catalogs)
under different URLs, usually without filename hints. These tests pin the
identity ladder (_release_ident), the dedup-aware slow probe wave, and the fast
race's duplicate-skip + per-pick host bench.
"""

import asyncio
import time
import unittest
from unittest.mock import patch

from app import picker, probe, sources


def _copy(name, url, *, title="", filename=None, size=None):
    s = {"name": name, "url": url, "behaviorHints": {}}
    if title:
        s["title"] = title
    if filename:
        s["behaviorHints"]["filename"] = filename
    if size:
        s["behaviorHints"]["videoSize"] = size
    return s


REMUX_TITLE = ("The Example Redemption (1994) 2160p UHD BluRay REMUX DV HDR "
               "10bit HEVC [Hindi DDP 2.0 + English DTS-HDMA 5.1]")


class ReleaseIdentTests(unittest.TestCase):
    def test_same_filename_across_addons_is_the_same_release(self):
        a = _copy("AddonA 4K", "https://a.example/1",
                  filename="Movie.2024.2160p.WEB-DL-GRP.mkv")
        b = _copy("AddonB 4K", "https://b.example/2",
                  filename="Movie.2024.2160p.WEB-DL-GRP.mkv")
        self.assertTrue(picker._release_ident(a))
        self.assertEqual(picker._release_ident(a), picker._release_ident(b))

    def test_videosize_off_by_one_across_addons_still_matches(self):
        # Observed in the wild: the same 4K remux served by three scraper
        # addons, byte-identical except one reports size+1.
        a = _copy("AddonA 2160p", "https://a.example/1", size=57_294_863_728)
        b = _copy("AddonB 4K", "https://b.example/2", size=57_294_863_729)
        self.assertTrue(picker._release_ident(a).startswith("size:"))
        self.assertEqual(picker._release_ident(a), picker._release_ident(b))

    def test_different_sizes_are_different_releases(self):
        a = _copy("AddonA 2160p", "https://a.example/1", size=57_294_863_728)
        b = _copy("AddonA 2160p", "https://a.example/2", size=18_071_074_897)
        self.assertNotEqual(picker._release_ident(a), picker._release_ident(b))

    def test_small_videosize_is_not_identifying(self):
        a = _copy("AddonA sample", "https://a.example/1", size=50_000_000)
        b = _copy("AddonB sample", "https://b.example/2", size=50_000_000)
        self.assertEqual("", picker._release_ident(a))
        self.assertEqual("", picker._release_ident(b))

    def test_identical_display_text_within_an_addon_matches(self):
        a = _copy("WebAddon 2160p", "https://w.example/1", title=REMUX_TITLE)
        b = _copy("WebAddon 2160p", "https://w.example/2", title=REMUX_TITLE)
        self.assertTrue(picker._release_ident(a).startswith("text:"))
        self.assertEqual(picker._release_ident(a), picker._release_ident(b))

    def test_text_differing_only_in_bitrate_is_a_different_file(self):
        a = _copy("Pengu 4K", "https://p.example/1",
                  title="Movie (1994) 4K BluRay HDR HEVC Direct ~53.8 Mbps")
        b = _copy("Pengu 4K", "https://p.example/2",
                  title="Movie (1994) 4K BluRay HDR HEVC Direct ~17.0 Mbps")
        self.assertNotEqual(picker._release_ident(a), picker._release_ident(b))

    def test_short_text_gives_no_identity(self):
        a = _copy("VS Zuri 2160p", "https://v.example/1", title="2160p VS")
        self.assertEqual("", picker._release_ident(a))

    def test_direct_nzb_key_wins_over_everything(self):
        s = _copy("NZB", "https://nzbdav.invalid/1", size=57_294_863_728)
        s["_nzb_release_key"] = "nzb:release-1"
        self.assertEqual("nzb:release-1", picker._release_ident(s))


class SlowSliceDedupTests(unittest.TestCase):
    def _pool(self, releases, copies):
        """`releases` distinct files, `copies` copies each (distinct URLs)."""
        pool = []
        for r in range(releases):
            for c in range(copies):
                pool.append(_copy(
                    f"Addon{c} 2160p", f"https://h{c}.example/{r}/{c}",
                    size=10_000_000_000 + r * 1_000_000))
        return pool

    def test_wave_covers_distinct_releases_not_copies(self):
        pool = self._pool(releases=10, copies=3)   # 30 candidates, 10 files
        wave = picker._slow_probe_slice(pool, max_probes=8, nzb_want=0)
        self.assertEqual(8, len(wave))
        idents = [picker._release_ident(s) for s in wave]
        self.assertEqual(len(idents), len(set(idents)))

    def test_best_copy_of_each_release_is_the_one_probed(self):
        pool = self._pool(releases=4, copies=2)
        wave = picker._slow_probe_slice(pool, max_probes=4, nzb_want=0)
        # first copy in quality order wins for each release
        self.assertEqual([s["url"] for s in pool if s["url"].endswith("/0")],
                         [s["url"] for s in wave])

    def test_duplicates_fill_the_wave_when_pool_is_small(self):
        pool = self._pool(releases=3, copies=3)    # only 3 distinct releases
        wave = picker._slow_probe_slice(pool, max_probes=6, nzb_want=0)
        self.assertEqual(6, len(wave))             # topped up with twin copies
        self.assertEqual(len({s["url"] for s in wave}), 6)
        idents = {picker._release_ident(s) for s in wave}
        self.assertEqual(3, len(idents))

    def test_copies_of_already_verified_releases_are_never_probed(self):
        pool = self._pool(releases=3, copies=3)
        verified_ident = picker._release_ident(pool[0])
        wave = picker._slow_probe_slice(pool, max_probes=9, nzb_want=0,
                                        skip_idents={verified_ident})
        self.assertTrue(wave)
        self.assertNotIn(verified_ident,
                         {picker._release_ident(s) for s in wave})

    def test_nzb_quota_still_honored_with_dedup(self):
        online = self._pool(releases=18, copies=1)
        direct = []
        for i in range(3):
            s = _copy("NZB", f"https://nzbdav.invalid/{i}",
                      filename=f"Movie.2024.1080p.WEB-DL-NZB{i}.mkv")
            s["_nzb_release_key"] = f"nzb:release-{i}"
            direct.append(s)
        wave = picker._slow_probe_slice(online + direct, max_probes=16,
                                        nzb_want=2)
        self.assertEqual(16, len(wave))
        self.assertEqual(2, sum(picker._is_direct_nzb(s) for s in wave))


class FastRaceDedupTests(unittest.IsolatedAsyncioTestCase):
    """The fast race must not spend its probe budget re-checking one release
    served by many addons, and must bench a host that keeps failing."""

    async def _race(self, streams, failing_urls, race_deadline=0.6):
        probed_urls: list[str] = []

        async def fake_get(source, media, media_id, wait):
            return list(streams) if source == sources.FAST else []

        async def fake_probe_race(candidates, need_bps_of, ttfb_max, want,
                                  concurrency=8, deadline=None,
                                  expect_secs=None):
            stream = candidates[0]
            probed_urls.append(stream["url"])
            await asyncio.sleep(0.02)
            if stream["url"] in failing_urls:
                return []
            return [(stream, probe.ProbeResult(True, ttfb=0.1,
                                               speed_bps=50_000_000))]

        with (patch("app.sources.has", side_effect=lambda s: s == sources.FAST),
              patch("app.sources.get", side_effect=fake_get),
              patch("app.sources.peek", return_value=[]),
              patch("app.picker.nzb_lane.in_progress", return_value=False),
              patch("app.probe.probe_race", side_effect=fake_probe_race),
              patch("app.reputation.blocked", return_value=False),
              patch.object(picker, "FAST_RACE_DEADLINE", race_deadline),
              patch.object(picker, "TOTAL_DEADLINE", race_deadline)):
            verified, _ = await picker._race_fast(
                "movie", "tt1234567", picker.PROFILES["full"], 7200,
                lambda _s: 1_000_000, time.monotonic())
        return verified, probed_urls

    async def test_verified_release_suppresses_its_other_copies(self):
        # The same 4K file from three addons (same size, different URLs) plus a
        # genuinely different release. Once one copy verifies, the other copies
        # must never be probed — but the distinct release must be.
        copies = [_copy(f"Addon{i} 2160p", f"https://h{i}.example/dup",
                        size=57_294_863_728 + (i % 2))       # off-by-one too
                  for i in range(3)]
        distinct = _copy("AddonX 2160p", "https://hx.example/other",
                         size=18_071_074_897)
        streams = copies + [distinct]

        verified, probed_urls = await self._race(streams, failing_urls=set())

        self.assertEqual(1, sum(u.endswith("/dup") for u in probed_urls))
        self.assertIn(distinct["url"], probed_urls)
        self.assertTrue(verified)

    async def test_failed_copy_lets_a_twin_on_another_host_try(self):
        # First copy fails (dead mirror) — a twin elsewhere may still work, so
        # exactly one more copy gets its chance and verifies.
        copies = [_copy(f"Addon{i} 2160p", f"https://h{i}.example/c{i}",
                        size=57_294_863_728) for i in range(3)]
        verified, probed_urls = await self._race(
            copies, failing_urls={copies[0]["url"]})

        self.assertEqual(copies[0]["url"], probed_urls[0])
        self.assertIn(copies[1]["url"], probed_urls)
        self.assertTrue(verified)

    async def test_host_with_repeated_failures_is_benched_for_the_pick(self):
        # Six distinct releases on one flaky host (all fail), one on a healthy
        # host. Probe completions can land in partial batches, so a couple of
        # extra flaky probes may dispatch before the third failure registers —
        # the invariant is that the bench eventually stops the bleeding (some
        # flaky candidates are never probed) and the healthy host still wins.
        flaky = [_copy("Flaky 2160p", f"https://flaky.example/{i}",
                       size=20_000_000_000 + i * 1_000_000_000)
                 for i in range(6)]
        healthy = _copy("Healthy 1080p", "https://healthy.example/ok",
                        size=8_000_000_000)
        streams = flaky + [healthy]

        with patch.object(picker, "PROBE_HOST_BENCH", 3):
            verified, probed_urls = await self._race(
                streams, failing_urls={s["url"] for s in flaky})

        flaky_probed = [u for u in probed_urls if "flaky.example" in u]
        self.assertGreaterEqual(len(flaky_probed), 3)
        self.assertLess(len(flaky_probed), 6)      # bench kicked in
        self.assertIn(healthy["url"], probed_urls)
        self.assertEqual(healthy["url"], verified[0][0]["url"])


if __name__ == "__main__":
    unittest.main()
