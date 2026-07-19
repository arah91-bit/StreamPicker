"""Anime numbering reconciliation: id/season backbone, cour-offset math, and the
release-string verdict.  A MockTransport stands in for anime-lists, Kitsu and
Jikan so the offset arithmetic and parsing are exercised without live calls.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app import anime


# A synthetic Attack-on-Titan-shaped franchise: one TVDB show (267440) whose
# TVDB season 3 is two Kitsu cours (12 + 10), so absolute #50 == S03E13.
FRIBB = [
    {"type": "TV", "imdb_id": ["tt2560140"], "tvdb_id": 267440,
     "kitsu_id": 7442, "mal_id": 16498, "anidb_id": 9541, "season": {"tvdb": 1}},
    {"type": "TV", "imdb_id": ["tt2560140"], "tvdb_id": 267440,
     "kitsu_id": 8671, "mal_id": 25777, "anidb_id": 10944, "season": {"tvdb": 2}},
    {"type": "TV", "imdb_id": ["tt2560140"], "tvdb_id": 267440,
     "kitsu_id": 13569, "mal_id": 35760, "anidb_id": 13241, "season": {"tvdb": 3}},
    {"type": "TV", "imdb_id": ["tt2560140"], "tvdb_id": 267440,
     "kitsu_id": 41982, "mal_id": 38524, "anidb_id": 14444, "season": {"tvdb": 3}},
    {"type": "TV", "imdb_id": ["tt2560140"], "tvdb_id": 267440,
     "kitsu_id": 42422, "mal_id": 40028, "anidb_id": 14977, "season": {"tvdb": 4}},
    {"type": "MOVIE", "imdb_id": ["tt3646944"], "tvdb_id": 267440,
     "kitsu_id": 8888, "mal_id": 9999, "anidb_id": 10583, "season": {"tvdb": 0}},
]

KITSU_COUNTS = {
    7442: (25, "2013-04-07"), 8671: (12, "2017-04-01"),
    13569: (12, "2018-07-23"), 41982: (10, "2019-04-29"),
    42422: (16, "2020-12-07"),
}
# Episode titles for the second season-3 cour (Kitsu 41982): its ep 1 is abs #50.
KITSU_TITLES = {41982: {1: "The Other Side of the Sea", 2: "Midnight Train"}}


def _resp(request, payload, status=200):
    return httpx.Response(status, json=payload, request=request)


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == anime.LISTS_URL:
        return _resp(request, FRIBB)
    if url.startswith(f"{anime.KITSU_BASE}/anime/") and "/episodes" in url:
        kid = int(request.url.path.rsplit("/", 2)[-2])
        offset = int(request.url.params.get("page[offset]", "0"))
        rows = []
        if offset == 0:
            for num, title in KITSU_TITLES.get(kid, {}).items():
                rows.append({"attributes": {"relativeNumber": num, "number": num,
                                            "canonicalTitle": title, "titles": {}}})
        return _resp(request, {"data": rows})
    if url.startswith(f"{anime.KITSU_BASE}/anime/"):
        kid = int(request.url.path.rsplit("/", 1)[-1])
        count, start = KITSU_COUNTS.get(kid, (None, ""))
        return _resp(request, {"data": {"attributes": {
            "episodeCount": count, "startDate": start}}})
    if url.startswith(anime.JIKAN_BASE):
        return _resp(request, {"data": {"episodes": None}}, status=503)
    raise AssertionError(f"unexpected request {url}")


class AnimeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patchers = [patch.object(anime, "ENABLED", True),
                          patch.object(anime, "JIKAN_ENABLED", True)]
        for p in self._patchers:
            p.start()
        anime._lists = anime._Lists()
        anime._show_cache.clear()
        anime._show_locks.clear()
        self._orig = anime._client
        anime._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True)

    async def asyncTearDown(self):
        await anime._client.aclose()
        anime._client = self._orig
        for p in reversed(self._patchers):
            p.stop()
        anime._lists = anime._Lists()
        anime._show_cache.clear()
        anime._show_locks.clear()


class ParsingTests(unittest.TestCase):
    def test_absolute_marker_numbers_only(self):
        self.assertEqual({50}, anime._abs_numbers("[Grp] Attack on Titan - 50 (1080p) [AB12].mkv"))
        self.assertEqual({99}, anime._abs_numbers("Show E099 720p.mkv"))
        self.assertEqual({5}, anime._abs_numbers("Show Ep 05 x265.mkv"))

    def test_title_numbers_years_and_resolutions_are_not_episodes(self):
        # "8" and "100" live in the title; 1080/2020 are metadata — none captured.
        self.assertEqual({9}, anime._abs_numbers("Kaiju No. 8 - 09 [1080p].mkv"))
        self.assertEqual({5}, anime._abs_numbers("Mob Psycho 100 II - 05 (2020) 720p.mkv"))
        self.assertEqual(set(), anime._abs_numbers("Some.Movie.2019.2160p.mkv"))

    def test_sxxexx_pairs_do_not_leak_into_absolute(self):
        self.assertEqual({(3, 13)}, anime._pairs("Attack on Titan S03E13 1080p.mkv"))
        self.assertEqual(set(), anime._abs_numbers("Attack on Titan S03E13 1080p.mkv"))


class AssessTests(unittest.TestCase):
    def _exp(self, **kw):
        base = dict(season=3, episode=13, absolute=50, relative=1, max_cour=25,
                    total=75, split_season=True, titles=("The Other Side of the Sea",))
        base.update(kw)
        return anime.Expectation(**base)

    def test_confirms_absolute_seasonal_relative_and_title(self):
        exp = self._exp()
        self.assertEqual(anime.CONFIRM, anime.assess(exp, "Attack on Titan - 50 (1080p).mkv"))
        self.assertEqual(anime.CONFIRM, anime.assess(exp, "Attack on Titan S03E13.mkv"))
        self.assertEqual(anime.CONFIRM, anime.assess(exp, "AoT Final Season Part 2 - 01.mkv"))
        self.assertEqual(anime.CONFIRM, anime.assess(
            exp, "AoT - The Other Side of the Sea [1080p].mkv"))

    def test_contradicts_wrong_absolute_and_wrong_other_season(self):
        exp = self._exp()
        self.assertEqual(anime.CONTRADICT, anime.assess(exp, "Attack on Titan - 60.mkv"))
        self.assertEqual(anime.CONTRADICT, anime.assess(exp, "Attack on Titan S04E01.mkv"))

    def test_split_season_sxxexx_is_neutral_not_contradiction(self):
        # S03E01 could be TVDB S3E1 or the second cour's ep 1 — don't hard-reject.
        self.assertEqual(anime.NEUTRAL, anime.assess(self._exp(), "Attack on Titan S03E01.mkv"))

    def test_clean_season_wrong_episode_contradicts(self):
        exp = self._exp(season=1, episode=5, absolute=5, relative=5, split_season=False, titles=())
        self.assertEqual(anime.CONTRADICT, anime.assess(exp, "Attack on Titan S01E06.mkv"))
        self.assertEqual(anime.CONFIRM, anime.assess(exp, "Attack on Titan - 05.mkv"))
        self.assertEqual(anime.CONTRADICT, anime.assess(exp, "Attack on Titan - 30.mkv"))

    def test_ambiguous_low_number_is_neutral(self):
        # "- 07" is neither the requested absolute nor relative, but small enough
        # to be a plausible cour number, so it must not hard-contradict.
        self.assertEqual(anime.NEUTRAL, anime.assess(self._exp(), "Attack on Titan - 07.mkv"))


class ResolutionTests(AnimeCase):
    async def test_offsets_map_absolute_to_split_tvdb_season(self):
        show = await anime.resolve("series", "tt2560140", season=3)
        self.assertIsNotNone(show)
        self.assertEqual(5, len(show.cours))          # movie/season-0 excluded

        # Season 3, episode 13 → second cour ep 1 → absolute 50.
        exp = show.expectation(3, 13)
        self.assertEqual(50, exp.absolute)
        self.assertEqual(1, exp.relative)
        self.assertTrue(exp.split_season)
        self.assertIn("The Other Side of the Sea", exp.titles)

        # Season 3, episode 1 → first cour → absolute 38, and not a split at ep 1.
        self.assertEqual(38, show.expectation(3, 1).absolute)
        # Season 1 episode 1 is absolute 1; season 4 episode 1 is absolute 60.
        self.assertEqual(1, show.expectation(1, 1).absolute)
        self.assertEqual(60, show.expectation(4, 1).absolute)
        # Out-of-range episode has no expectation.
        self.assertIsNone(show.expectation(3, 99))

    async def test_kitsu_id_request_resolves_same_show(self):
        show = await anime.resolve("series", "kitsu:41982:1", season=3)
        self.assertIsNotNone(show)
        self.assertEqual(267440, show.tvdb_id)
        self.assertEqual(50, show.expectation(3, 13).absolute)

    async def test_unmapped_id_returns_none(self):
        self.assertIsNone(await anime.resolve("series", "tt0000000", season=1))

    async def test_movie_and_disabled_short_circuit(self):
        self.assertIsNone(await anime.resolve("movie", "tt2560140"))
        with patch.object(anime, "ENABLED", False):
            self.assertIsNone(await anime.resolve("series", "tt2560140", season=1))


class PickerOverrideTests(unittest.TestCase):
    """The anime expectation reshapes the picker's per-stream identity verdict."""

    def setUp(self):
        from app import content_identity, picker
        self.picker = picker
        self.ci = content_identity
        self.profile = content_identity.IdentityProfile(
            media="series", imdb_id="tt2560140", aliases=("Attack on Titan",),
            season=3, episode=13)
        self.exp = anime.Expectation(
            season=3, episode=13, absolute=50, relative=1, max_cour=25,
            total=75, split_season=True, titles=())
        self._tokens = [picker._identity_profile_ctx.set(self.profile),
                        picker._anime_ctx.set(self.exp)]

    def tearDown(self):
        self.picker._identity_profile_ctx.reset(self._tokens[0])
        self.picker._anime_ctx.reset(self._tokens[1])

    def _assess(self, filename):
        s = {"behaviorHints": {"filename": filename}}
        return self.picker._assess_stream_identity(s, None, record=False)

    def test_absolute_number_promotes_compatible_to_strong(self):
        r = self._assess("[EMBER] Attack on Titan - 50 [1080p][HEVC].mkv")
        self.assertEqual(self.ci.STRONG, r.state)
        self.assertEqual(self.ci.EVIDENCE_ANIME, r.evidence)

    def test_wrong_absolute_is_contradicted(self):
        r = self._assess("[EMBER] Attack on Titan - 60 [1080p].mkv")
        self.assertEqual(self.ci.CONTRADICTION, r.state)

    def test_matching_sxxexx_stays_strong(self):
        r = self._assess("Attack on Titan S03E13 1080p WEB-DL.mkv")
        self.assertEqual(self.ci.STRONG, r.state)

    def test_without_anime_layer_absolute_release_is_contradicted(self):
        # Documents the bug the layer fixes: with no expectation, the ordinary
        # gate can't anchor the title before a bare "- 50", so it contradicts a
        # perfectly correct release.
        self.picker._anime_ctx.reset(self._tokens[1])
        self._tokens[1] = self.picker._anime_ctx.set(None)
        r = self._assess("[EMBER] Attack on Titan - 50 [1080p].mkv")
        self.assertEqual(self.ci.CONTRADICTION, r.state)


if __name__ == "__main__":
    unittest.main()
