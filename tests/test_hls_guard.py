"""Playlist-derived URLs may not reach the household's own services, and a
playlist fetch may not read a whole film into memory.

Field review (2026-10-15). Every URI inside an upstream playlist is fetched
server-side and relayed to a player, so a playlist could aim this process at
http://radarr:7878/… or 169.254.169.254 (blind SSRF). Separately, the master
playlist was read in full before the 2 MB size check rejected it.
"""

import base64
import unittest
from unittest.mock import patch

import httpx
from starlette.requests import Request

from app import hlsproxy


def _request(token: str, url: str, cand_key: str = "") -> Request:
    u = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    q = f"u={u}&s={hlsproxy._sign(token, url, cand_key)}"
    if cand_key:
        q += f"&c={cand_key}"
    return Request({"type": "http", "method": "GET", "path": "/",
                    "query_string": q.encode(), "headers": []})


class InternalHostTests(unittest.TestCase):
    def test_internal_targets(self):
        for host in ("radarr", "nzbdav", "localhost", "a.localhost",
                     "127.0.0.1", "10.0.0.5", "172.18.0.3", "192.168.1.20",
                     "169.254.169.254", "[::1]", "fd00::1", "100.64.0.1", ""):
            with self.subTest(host=host):
                self.assertTrue(hlsproxy._internal_host(host))

    def test_public_targets(self):
        for host in ("cdn.example.com", "8.8.8.8", "2606:4700::1111"):
            with self.subTest(host=host):
                self.assertFalse(hlsproxy._internal_host(host))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.hits.append(str(request.url))
            if request.url.path == "/bounce":
                return httpx.Response(302, headers={
                    "location": "http://127.0.0.1:8080/secret"})
            return httpx.Response(200, content=b"G" * 188,
                                  headers={"content-type": "video/mp2t"})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
            event_hooks={"request": [hlsproxy._guard_request]})
        self.addAsyncCleanup(client.aclose)
        for p in (patch.object(hlsproxy, "_client", client),
                  patch.object(hlsproxy, "_note_seg_failure"),
                  patch.object(hlsproxy, "_note_segment"),
                  patch.object(hlsproxy, "_prefetch_next")):
            p.start()
            self.addCleanup(p.stop)

    def _entry(self, master: str) -> dict:
        return {"picker": "fast", "id": "tt1",
                "cands": [{"key": "k", "u": master, "rh": {}, "sig": "s",
                           "lbl": "x"}]}

    async def _get(self, master: str, url: str):
        token = "tok-guard"
        try:
            return await hlsproxy.serve_resource(
                token, self._entry(master), _request(token, url, "k"))
        finally:
            hlsproxy._active.pop(token, None)

    async def test_a_playlist_cannot_point_at_an_internal_service(self):
        resp = await self._get("https://cdn.example.com/master.m3u8",
                               "http://radarr:7878/api/v3/system/status")
        self.assertEqual(502, resp.status_code)
        self.assertEqual([], self.hits, "the internal host was contacted")

    async def test_a_redirect_into_the_lan_is_refused_too(self):
        resp = await self._get("https://cdn.example.com/master.m3u8",
                               "https://cdn.example.com/bounce")
        self.assertEqual(502, resp.status_code)
        self.assertFalse(any("127.0.0.1" in h for h in self.hits))

    async def test_an_internal_addon_may_serve_its_own_segments(self):
        resp = await self._get("http://mediaflow:8888/master.m3u8",
                               "http://mediaflow:8888/seg1.ts")
        self.assertEqual(200, resp.status_code)

    async def test_public_segments_are_untouched(self):
        resp = await self._get("https://cdn.example.com/master.m3u8",
                               "https://edge.example.net/seg1.ts")
        self.assertEqual(200, resp.status_code)


class CappedFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_huge_playlist_url_is_not_read_in_full(self):
        served = 0

        async def body():
            nonlocal served
            for _ in range(1000):                # would be 64 MB
                served += 65536
                yield b"\0" * 65536

        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=body())))
        self.addAsyncCleanup(client.aclose)
        with patch.object(hlsproxy, "_client", client):
            got = await hlsproxy._fetch_capped(
                "https://cdn.example.com/x.m3u8", {}, 5.0, 256 * 1024)
        self.assertTrue(got.too_large)
        self.assertLess(served, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
