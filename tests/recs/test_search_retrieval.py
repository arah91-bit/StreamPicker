from unittest.mock import AsyncMock, patch
import unittest

from app.recs.search_intent import interpret
from app.recs.search_retrieval import Retriever
from app.recs import tmdb


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sparse_first_page_continues_and_preserves_filters(self):
        first = [{'id': i, 'title': f'Horror {i}', 'release_date': '1982-01-01', 'genre_ids': [27]} for i in range(1, 5)]
        second = [{'id': i, 'title': f'Horror {i}', 'release_date': '1985-01-01', 'genre_ids': [27]} for i in range(5, 9)]
        discover = AsyncMock(side_effect=[first, second])
        with patch.object(tmdb, 'discover', discover):
            r = Retriever(interpret('scary movies from the 80s'))
            out = await r.retrieve()
        assert len(out) == 8
        assert discover.await_count == 2
        for call in discover.await_args_list:
            params = call.args[1]
            assert params['with_genres'] == '27'
            assert params['primary_release_date.gte'] == '1980-01-01'
            assert params['primary_release_date.lte'] == '1989-12-31'



    async def test_weak_title_hit_not_treated_as_identity(self):
        with patch.object(tmdb, 'search', AsyncMock(return_value=[{'id': 1, 'title': 'It Ends', 'release_date': '2026-01-01'}])):
            r = Retriever(interpret('IT 2026'))
            assert await r.retrieve() == []

    async def test_setting_period_uses_keyword_without_release_date_filter(self):
        row = {'id': 11, 'title': 'A Film Set in 1985', 'release_date': '2020-01-01', 'genre_ids': [27]}
        with patch.object(tmdb, 'search_keyword_results', AsyncMock(return_value=[{'id': 208289, 'name': '1980s'}])), patch.object(tmdb, 'discover', AsyncMock(side_effect=[[row], []])) as discover:
            r = Retriever(interpret('horror set in the 80s'))
            candidates = await r.retrieve()
        assert len(candidates) == 1
        assert any(':setting:' in p for p in candidates[0].provenance)
        params = discover.await_args_list[0].args[1]
        assert params['with_keywords'] == '208289'
        assert 'primary_release_date.gte' not in params

    async def test_excluded_topic_stays_in_every_discover_page(self):
        row = {'id': 11, 'title': 'The Thing', 'release_date': '1982-01-01', 'genre_ids': [27]}
        with patch.object(tmdb, 'search_keyword_results', AsyncMock(return_value=[{'id': 3133, 'name': 'vampire'}])), patch.object(tmdb, 'discover', AsyncMock(side_effect=[[row], []])) as discover:
            await Retriever(interpret('80s horror without vampires')).retrieve()
        for call in discover.await_args_list:
            assert call.args[1]['without_keywords'] == '3133'
