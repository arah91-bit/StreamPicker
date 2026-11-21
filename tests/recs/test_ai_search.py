"""Catalog-level behavior for the unified AI search pipeline."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import ai_search, config, main, tmdb
from app.recs.search_models import Candidate


class CatalogSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ai_search.clear_cache()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver')

    async def asyncTearDown(self):
        await self.client.aclose()
        ai_search.clear_cache()

    async def test_manifest_search_only(self):
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(main.db, 'get_user', AsyncMock(return_value={'token': 'viewer'})), patch.object(main.db, 'get_catalog_defs', AsyncMock(return_value=[])):
            response = await self.client.get('/viewer/manifest.json')
        item = next(c for c in response.json()['catalogs'] if c['id'] == ai_search.AI_SEARCH_ID)
        self.assertFalse(item['showInHome'])
        self.assertEqual(item['extra'], [{'name': 'search', 'isRequired': True}])

    async def test_decade_browse_uses_two_pages_and_excludes_wrong_year(self):
        rows1 = [{'id': i, 'title': f'Classic {i}', 'release_date': '1982-01-01', 'genre_ids': [27], 'vote_count': 300} for i in range(1, 5)]
        rows2 = [{'id': i, 'title': f'Classic {i}', 'release_date': '1985-01-01', 'genre_ids': [27], 'vote_count': 200} for i in range(5, 17)]
        rows2.append({'id': 99, 'title': 'Alien', 'release_date': '1979-01-01', 'genre_ids': [27], 'vote_count': 1000})
        async def resolve(_media, tid, **_kwargs):
            return {'id': f'tt{tid}', 'name': f'Classic {tid}', 'releaseInfo': '1982' if tid < 5 else '1985'}
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(main.db, 'get_user', AsyncMock(return_value={'token': 'viewer'})), patch.object(ai_search, '_query_ollama', AsyncMock(return_value={})), patch.object(tmdb, 'discover', AsyncMock(side_effect=[rows1, rows2])) as discover, patch.object(tmdb, 'resolve_meta', AsyncMock(side_effect=resolve)):
            response = await self.client.get('/viewer/catalog/all/nr-ai-search/search=scary%20movies%20from%20the%2080s.json')
        ids = [m['id'] for m in response.json()['metas']]
        self.assertEqual(len(ids), 16)
        self.assertNotIn('tt99', ids)
        self.assertEqual(discover.await_count, 2)
        self.assertEqual(discover.await_args_list[1].args[1]['page'], 2)

    async def test_model_failure_still_finds_title(self):
        row = {'id': 346364, 'title': 'It', 'release_date': '2017-09-06', 'genre_ids': [27]}
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(ai_search, '_query_ollama', AsyncMock(side_effect=httpx.ConnectError('down'))), patch.object(tmdb, 'search', AsyncMock(return_value=[row])), patch.object(tmdb, 'candidate_details', AsyncMock(return_value={})), patch.object(tmdb, 'resolve_meta', AsyncMock(return_value={'id': 'tt1396484', 'name': 'It'})):
            result = await ai_search.search('viewer', 'IT 2026')
        self.assertEqual([m['id'] for m in result], ['tt1396484'])

    async def test_short_title_skips_planner(self):
        row = {'id': 346364, 'title': 'It', 'release_date': '2017-09-06'}
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(ai_search, '_query_ollama', AsyncMock()) as planner, patch.object(tmdb, 'search', AsyncMock(return_value=[row])), patch.object(tmdb, 'candidate_details', AsyncMock(return_value={})), patch.object(tmdb, 'resolve_meta', AsyncMock(return_value={'id': 'tt1396484', 'name': 'It'})):
            result = await ai_search.search('viewer', 'IT')
        planner.assert_not_awaited()
        self.assertEqual(len(result), 1)

    async def test_eligibility_backfills_from_later_candidates(self):
        rows = [{'id': i, 'title': f'Classic {i}', 'release_date': '1982-01-01', 'genre_ids': [27], 'vote_count': 100-i} for i in range(1, 21)]
        async def resolve(_media, tid, **_kwargs):
            return None if tid <= 3 else {'id': f'tt{tid}', 'name': f'Classic {tid}', 'releaseInfo': '1982'}
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(ai_search, '_query_ollama', AsyncMock(return_value={})), patch.object(tmdb, 'discover', AsyncMock(side_effect=[rows, []])), patch.object(tmdb, 'resolve_meta', AsyncMock(side_effect=resolve)):
            result = await ai_search.search('viewer', 'scary movies from the 80s')
        self.assertEqual(len(result), 16)
        self.assertEqual(result[0]['id'], 'tt4')

    async def test_cache_hit_cancels_older_query(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def slow(*_args):
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(ai_search, '_run_search', slow):
            ai_search._cache[('v2', 'it', None, ai_search.active_model(), '')] = (ai_search.time.monotonic(), [{'id': 'cached'}])
            task = asyncio.create_task(ai_search.search('viewer', 'long query'))
            await started.wait()
            cached = await ai_search.search('viewer', 'IT')
            await task
        self.assertEqual(cached, [{'id': 'cached'}])
        self.assertTrue(cancelled.is_set())

    async def test_recovery_page_after_eligibility_drops(self):
        def rows(start, end):
            return [{'id': i, 'title': f'Classic {i}', 'release_date': '1982-01-01', 'genre_ids': [27], 'vote_count': 100-i} for i in range(start, end)]
        discover = AsyncMock(side_effect=[rows(1, 7), rows(7, 13), rows(13, 19)])
        async def resolve(_media, tid, **_kwargs):
            return None if tid < 7 else {'id': f'tt{tid}', 'name': f'Classic {tid}', 'releaseInfo': '1982'}
        with patch.object(config, 'AI_SEARCH_ENABLED', True), patch.object(tmdb, 'discover', discover), patch.object(tmdb, 'resolve_meta', AsyncMock(side_effect=resolve)):
            metas = await ai_search.search('viewer', 'scary movies from the 80s')
        self.assertEqual(len(metas), 12)
        self.assertEqual(discover.await_count, 3)
        params = discover.await_args_list[2].args[1]
        self.assertEqual(params['page'], 3)
        self.assertEqual(params['primary_release_date.gte'], '1980-01-01')
        self.assertEqual(params['with_genres'], '27')
