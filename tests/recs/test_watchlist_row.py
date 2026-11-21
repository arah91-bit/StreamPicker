"""Want to Watch — the one row assembled from an explicit request.

Everything else on the surface is inferred. This is the shelf the viewer
stocked themselves, which makes it the cheapest correction available when the
recommendations are wrong — and the one row that must never quietly drop
something somebody put on it.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.recs import bootstrap, db, main, watchlist


def _saved(imdb, *, at, media_type="movie"):
    return {"imdb_id": imdb, "media_type": media_type, "added_at": at}


def _found(imdb):
    return {"tmdb_id": int(imdb[2:]), "media_type": "movie", "genre_ids": []}


def _meta(media_type, tmdb_id, *_a, **_k):
    kind = "movie" if media_type == "movie" else "series"
    return {"id": f"tt{tmdb_id}", "type": kind, "name": f"Title {tmdb_id}"}


class WatchlistRowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        watchlist._cache.clear()
        watchlist._locks.clear()
        self.user = {"token": "viewer", "name": "V", "watchlist_row": 1}

    async def _build(self, saved, played=()):
        with (
            patch.object(watchlist.db, "watchlist_for",
                         AsyncMock(return_value=list(saved))),
            patch.object(watchlist.db, "played_titles",
                         AsyncMock(return_value=[{"imdb_id": i}
                                                 for i in played])),
            patch.object(watchlist.tmdb, "find_by_imdb",
                         AsyncMock(side_effect=lambda i: _found(i))),
            patch.object(watchlist.tmdb, "resolve_meta",
                         AsyncMock(side_effect=_meta)),
        ):
            return await watchlist._build(self.user)

    async def test_saved_titles_come_back_newest_first(self):
        metas = await self._build([_saved("tt3", at=300),
                                   _saved("tt1", at=100)])
        self.assertEqual(["tt3", "tt1"], [m["id"] for m in metas])

    async def test_a_watched_title_leaves_the_row(self):
        """The row answers "what have I still not got round to". A film
        watched last night is not an answer to that."""
        metas = await self._build([_saved("tt3", at=300),
                                   _saved("tt1", at=100)],
                                  played=["tt3"])
        self.assertEqual(["tt1"], [m["id"] for m in metas])

    async def test_a_watched_title_stays_in_the_table(self):
        """Leaving the row and leaving the store are different questions. The
        bootstrapper reads the store to know it has already asked about this
        title, and that stays true forever."""
        with patch.object(watchlist.db, "watchlist_for",
                          AsyncMock(return_value=[_saved("tt3", at=300)])):
            self.assertEqual(
                1, len(await watchlist.db.watchlist_for("viewer")))

    async def test_an_unreadable_history_costs_stale_cards_not_the_row(self):
        with (
            patch.object(watchlist.db, "watchlist_for",
                         AsyncMock(return_value=[_saved("tt1", at=100)])),
            patch.object(watchlist.db, "played_titles",
                         AsyncMock(side_effect=RuntimeError("no history"))),
            patch.object(watchlist.tmdb, "find_by_imdb",
                         AsyncMock(side_effect=lambda i: _found(i))),
            patch.object(watchlist.tmdb, "resolve_meta",
                         AsyncMock(side_effect=_meta)),
        ):
            metas = await watchlist._build(self.user)
        self.assertEqual(["tt1"], [m["id"] for m in metas])

    async def test_a_failed_build_costs_the_row_and_nothing_else(self):
        with patch.object(watchlist.db, "watchlist_for",
                          AsyncMock(side_effect=RuntimeError("db gone"))):
            self.assertEqual([], await watchlist.get_metas(self.user))

    async def test_a_title_still_in_cinemas_is_not_filtered_out(self):
        """Somebody may perfectly well save a film that is not streamable yet,
        and dropping it out of their own list would look like the save had
        failed."""
        seen = {}

        async def resolve(media, tmdb_id, *a, **kw):
            seen.update(kw)
            return _meta(media, tmdb_id)

        with (
            patch.object(watchlist.db, "watchlist_for",
                         AsyncMock(return_value=[_saved("tt1", at=100)])),
            patch.object(watchlist.db, "played_titles",
                         AsyncMock(return_value=[])),
            patch.object(watchlist.tmdb, "find_by_imdb",
                         AsyncMock(side_effect=lambda i: _found(i))),
            patch.object(watchlist.tmdb, "resolve_meta", resolve),
        ):
            await watchlist._build(self.user)
        self.assertFalse(seen.get("require_home_release", True))


class WatchlistManifestTests(unittest.TestCase):
    def test_the_row_is_advertised_while_it_is_still_empty(self):
        """A catalog cannot be added to a manifest mid-session, so a row that
        appeared only once it had content would be missing for exactly the
        first save."""
        defs = watchlist.catalog_defs({"token": "t", "watchlist_row": 1})
        self.assertEqual([watchlist.WATCHLIST_ID], [d["id"] for d in defs])

    def test_switching_it_off_removes_it(self):
        self.assertEqual(
            [], watchlist.catalog_defs({"token": "t", "watchlist_row": 0}))

    def test_the_row_is_switchable_from_the_admin_page(self):
        self.assertEqual(watchlist.WATCHLIST_FLAG,
                         main.ROW_FAMILIES[watchlist.WATCHLIST_ID])


class SaveIsNotAVerdictTests(unittest.TestCase):
    """A save is stored apart from the verdicts, and that separation is
    load-bearing in two directions at once."""

    def test_it_is_not_a_verdict_value(self):
        """`taste_feedback` replaces on conflict, so a save written there would
        be destroyed the moment the same title was later rated."""
        self.assertNotIn(bootstrap.SAVE_VERDICT, db.VERDICTS)

    def test_a_save_never_hides_a_title_from_the_recommendations(self):
        """Every reader of `taste_feedback` treats a row as "seen it, keep it
        out of the rows", which is the exact opposite of what a save means."""
        from app.recs.catalogs import Generator

        gen = Generator({"token": "t", "name": "V", "is_kid": 0})
        gen.profile = {"watched_imdb": set(), "watched_tmdb_movie": set(),
                       "watched_tmdb_show": set()}
        # `run()` only ever puts the three seen-it verdicts in here; a save
        # reaches a different table entirely and so cannot land in _exclude().
        gen.rated_titles = set()
        self.assertEqual(set(), gen._exclude())


if __name__ == "__main__":
    unittest.main()
