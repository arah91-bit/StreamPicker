"""Native Jellyfin library identity, authentication and playback proxy tests.

No test talks to the operator's server or contains a real credential.  A
MockTransport acts like Jellyfin at its HTTP boundary so request paths,
headers, query parameters, pagination and byte-range behavior are all covered.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app import library


USER_ID = "user00001"
MOVIE_ID = "movie0001"
SERIES_ID = "series001"
EPISODE_ID = "episode001"
SOURCE_ID = "source0001"
SESSION_ID = "session001"
ACCESS_TOKEN = "synthetic-access-token-never-persist"
PASSWORD = "synthetic-jellyfin-password"


class AsyncBytes(httpx.AsyncByteStream):
    """One-shot response body used to exercise httpx's real streaming path."""

    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


def response(request: httpx.Request, status: int = 200, payload=None,
             *, content: bytes | None = None, headers=None) -> httpx.Response:
    if content is None:
        content = json.dumps(payload if payload is not None else {}).encode()
        headers = {"content-type": "application/json", **(headers or {})}
        return httpx.Response(status, content=content, headers=headers,
                              request=request)
    return httpx.Response(status, stream=AsyncBytes(content), headers=headers,
                          request=request)


def auth_payload(token: str = ACCESS_TOKEN) -> dict:
    return {"User": {"Id": USER_ID}, "AccessToken": token}


def movie_item(imdb: str = "tt0113568", **changes) -> dict:
    item = {"Id": MOVIE_ID, "Type": "Movie", "Name": "Synthetic Movie",
            "ProviderIds": {"IMDB": imdb}, "ProductionYear": 1995}
    item.update(changes)
    return item


def series_item(imdb: str = "tt0386676", **changes) -> dict:
    item = {"Id": SERIES_ID, "Type": "Series", "Name": "Synthetic Show",
            "ProviderIds": {"Imdb": imdb}, "ProductionYear": 2005}
    item.update(changes)
    return item


def media_source(source_id: str = SOURCE_ID, **changes) -> dict:
    source = {
        "Id": source_id,
        "Container": "mkv",
        "Path": "/media/Synthetic.Movie.1995.2160p.mkv",
        "Size": "123456789",
        "MediaStreams": [
            {"Type": "Video", "Height": 2160, "BitRate": "28700000"},
            {"Type": "Audio", "BitRate": 768000},
        ],
    }
    source.update(changes)
    return source


def hls_source(source_id: str = SOURCE_ID, codec: str = "mpeg2video",
               **changes) -> dict:
    source = {
        "Id": source_id,
        "Container": "mkv",
        "Path": "/media/Legacy.Show.S01E01.mkv",
        "Size": "734003200",
        "MediaStreams": [
            {"Type": "Video", "Codec": codec, "Height": 576,
             "BitRate": "4000000"},
            {"Type": "Audio", "Codec": "ac3", "Channels": 2},
        ],
    }
    source.update(changes)
    return source


def request(method: str = "GET", headers=None, query: str = "") -> Request:
    raw_headers = [(str(k).lower().encode("latin-1"),
                    str(v).encode("latin-1"))
                   for k, v in (headers or {}).items()]
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "https", "path": "/library/test", "raw_path": b"",
        "query_string": query.encode("latin-1"), "headers": raw_headers,
        "client": ("192.0.2.10", 54321),
        "server": ("picker.example", 443),
    })


def proxy_urls(playlist_text: str) -> list[str]:
    """Every StreamPicker-proxied URL a rewritten playlist points at."""
    found = []
    for line in playlist_text.splitlines():
        for token in ('URI="', ""):
            if token and token in line:
                inner = line.split(token, 1)[1].split('"', 1)[0]
                if inner.startswith("https://picker.example/library/"):
                    found.append(inner)
        stripped = line.strip()
        if stripped.startswith("https://picker.example/library/"):
            found.append(stripped)
    return found


class NativeJellyfinCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls: list[httpx.Request] = []
        self._patchers = [
            patch.object(library, "JELLYFIN_URL", "http://jellyfin.invalid:8096"),
            patch.object(library, "JELLYFIN_USERNAME", "synthetic-user"),
            patch.object(library, "JELLYFIN_PASSWORD", PASSWORD),
            patch.object(library, "PUBLIC_URL", "https://picker.example"),
            patch.object(library, "_ADDON_SECRET", b"synthetic-addon-secret"),
            patch.object(library, "INDEX_TTL", 300.0),
            patch.object(library, "NEG_TTL", 60.0),
            patch.object(library, "STREAM_TOKEN_TTL", 3600.0),
        ]
        for p in self._patchers:
            p.start()
        library._access_token = ""
        library._user_id = ""
        library._index = None
        library._cache.clear()
        self._original_client = library._client
        self._original_auth_lock = library._auth_lock
        self._original_index_lock = library._index_lock
        # Locks created while another IsolatedAsyncioTestCase loop was active
        # must not carry loop affinity between cases.
        library._auth_lock = asyncio.Lock()
        library._index_lock = asyncio.Lock()
        self.client: httpx.AsyncClient | None = None

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        library._client = self._original_client
        library._auth_lock = self._original_auth_lock
        library._index_lock = self._original_index_lock
        for p in reversed(self._patchers):
            p.stop()
        library._access_token = ""
        library._user_id = ""
        library._index = None
        library._cache.clear()

    def install(self, handler) -> None:
        async def recording(req: httpx.Request) -> httpx.Response:
            self.calls.append(req)
            result = handler(req)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(recording),
            timeout=None, follow_redirects=True)
        library._client = self.client

    def assert_token_header(self, req: httpx.Request,
                            expected: str = ACCESS_TOKEN) -> None:
        self.assertEqual(expected, req.headers.get("x-emby-token"))
        self.assertNotIn("api_key", req.url.params)
        self.assertNotIn(expected, str(req.url))


class AuthenticationTests(NativeJellyfinCase):
    async def test_authentication_uses_client_identity_and_json_credentials(self):
        def handler(req):
            self.assertEqual("/Users/AuthenticateByName", req.url.path)
            body = json.loads(req.content)
            self.assertEqual({"Username": "synthetic-user", "Pw": PASSWORD}, body)
            authorization = req.headers.get("authorization", "")
            self.assertIn('Client="StreamPicker"', authorization)
            self.assertIn("DeviceId=", authorization)
            self.assertNotIn(PASSWORD, str(req.url))
            self.assertNotIn(PASSWORD, authorization)
            return response(req, payload=auth_payload())

        self.install(handler)
        self.assertEqual((ACCESS_TOKEN, USER_ID), await library._authenticate())
        self.assertEqual(1, len(self.calls))

    async def test_concurrent_authentication_is_single_flight(self):
        count = 0

        async def handler(req):
            nonlocal count
            count += 1
            await asyncio.sleep(0.02)
            return response(req, payload=auth_payload())

        self.install(handler)
        sessions = await asyncio.gather(
            *(library._authenticate() for _ in range(20)))
        self.assertEqual([(ACCESS_TOKEN, USER_ID)] * 20, sessions)
        self.assertEqual(1, count)

    async def test_api_401_reauthenticates_once_and_retries_with_new_token(self):
        logins = 0
        api_tokens = []

        def handler(req):
            nonlocal logins
            if req.url.path == "/Users/AuthenticateByName":
                logins += 1
                return response(req, payload=auth_payload(f"token-{logins}"))
            api_tokens.append(req.headers.get("x-emby-token"))
            return response(req, 401 if len(api_tokens) == 1 else 200,
                            {"Version": "synthetic"})

        self.install(handler)
        result = await library._api("GET", "/System/Info")
        self.assertEqual(200, result.status_code)
        self.assertEqual(2, logins)
        self.assertEqual(["token-1", "token-2"], api_tokens)

    async def test_concurrent_401s_share_one_session_refresh(self):
        library._access_token = "expired-token"
        library._user_id = USER_ID
        logins = 0
        expired_calls = 0

        async def handler(req):
            nonlocal logins, expired_calls
            if req.url.path == "/Users/AuthenticateByName":
                logins += 1
                await asyncio.sleep(0.02)
                return response(req, payload=auth_payload("fresh-token"))
            if req.headers.get("x-emby-token") == "expired-token":
                expired_calls += 1
                # Let both requests observe the same expired generation before
                # either starts the refresh.
                while expired_calls < 2:
                    await asyncio.sleep(0)
                return response(req, 401, {"error": "expired"})
            self.assertEqual("fresh-token", req.headers.get("x-emby-token"))
            return response(req, payload={"ok": True})

        self.install(handler)
        results = await asyncio.gather(
            library._api("GET", "/System/One"),
            library._api("GET", "/System/Two"),
        )
        self.assertEqual([200, 200], [r.status_code for r in results])
        self.assertEqual(1, logins, "one expired token generation should cause "
                         "one shared reauthentication")

    async def test_auth_failure_returns_no_stream_and_does_not_log_credentials(self):
        def handler(req):
            return response(req, 403, {"error": PASSWORD})

        self.install(handler)
        with self.assertLogs("stream-picker", level="WARNING") as captured:
            self.assertEqual([], await library.streams("movie", "tt0113568"))
        text = "\n".join(captured.output)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(ACCESS_TOKEN, text)
        self.assertIn("HTTPStatusError", text)


class ResolutionTests(NativeJellyfinCase):
    async def test_movie_uses_exact_provider_index_and_mints_trusted_stream(self):
        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            self.assert_token_header(req)
            if req.url.path == f"/Users/{USER_ID}/Items":
                self.assertEqual("Movie,Series",
                                 req.url.params["IncludeItemTypes"])
                self.assertIn("ProviderIds", req.url.params["Fields"])
                self.assertNotIn("AnyProviderIdEquals", req.url.params)
                return response(req, payload={
                    "Items": [movie_item()], "TotalRecordCount": 1})
            if req.url.path == f"/Items/{MOVIE_ID}/PlaybackInfo":
                self.assertEqual(USER_ID, req.url.params["UserId"])
                self.assertEqual({"UserId": USER_ID}, json.loads(req.content))
                return response(req, payload={
                    "PlaySessionId": SESSION_ID,
                    "MediaSources": [
                        media_source(),
                        media_source("source0002", Container="mkv?unsafe"),
                    ],
                })
            self.fail(f"unexpected Jellyfin request {req.method} {req.url}")

        self.install(handler)
        found = await library.streams("movie", "TT0113568")
        self.assertEqual(1, len(found))
        stream = found[0]
        self.assertEqual("📚 Library 4K", stream["name"])
        self.assertEqual("Synthetic.Movie.1995.2160p.mkv", stream["title"])
        self.assertEqual(123456789, stream["behaviorHints"]["videoSize"])
        self.assertEqual(28700000, stream["_vbitrate"])
        self.assertEqual("strong", stream["_library_identity_confidence"])
        self.assertEqual("jellyfin-imdb",
                         stream["_library_identity_evidence"])
        self.assertTrue(library.identity_trusted(stream))

        url = stream["url"]
        self.assertTrue(url.startswith("https://picker.example/library/"))
        for forbidden in ("jellyfin.invalid", PASSWORD, ACCESS_TOKEN,
                          "api_key", "x-emby-token"):
            self.assertNotIn(forbidden, url.lower())
        capability = library._decode_capability(url.rsplit("/", 1)[-1])
        self.assertEqual({
            "item": MOVIE_ID, "source": SOURCE_ID,
            "container": "mkv", "session": SESSION_ID, "mode": "d",
        }, capability)

    async def test_provider_id_mismatch_and_wrong_media_type_are_not_played(self):
        playback_calls = 0

        def handler(req):
            nonlocal playback_calls
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            if req.url.path == f"/Users/{USER_ID}/Items":
                return response(req, payload={
                    "Items": [movie_item("tt1219827"),
                              series_item("tt0113568")],
                    "TotalRecordCount": 2,
                })
            playback_calls += 1
            return response(req, payload={"MediaSources": [media_source()]})

        self.install(handler)
        self.assertEqual([], await library.streams("movie", "tt0113568"))
        self.assertEqual(0, playback_calls)

    async def test_index_paginates_and_never_uses_broken_provider_filter(self):
        starts = []
        fillers = [
            {"Id": f"filler{i:04d}", "Type": "Movie", "ProviderIds": {}}
            for i in range(1000)
        ]

        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            if req.url.path == f"/Users/{USER_ID}/Items":
                self.assertNotIn("AnyProviderIdEquals", req.url.params)
                start = int(req.url.params["StartIndex"])
                starts.append(start)
                items = fillers if start == 0 else [movie_item()]
                return response(req, payload={
                    "Items": items, "TotalRecordCount": 1001})
            return response(req, payload={
                "PlaySessionId": SESSION_ID,
                "MediaSources": [media_source()],
            })

        self.install(handler)
        self.assertEqual(1, len(await library.streams("movie", "tt0113568")))
        self.assertEqual([0, 1000], starts)

    async def test_strict_episode_match_uses_parent_series_imdb_and_coordinates(self):
        played = []

        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            self.assert_token_header(req)
            if req.url.path == f"/Users/{USER_ID}/Items":
                return response(req, payload={
                    "Items": [series_item(), series_item(
                        "tt0290978", Id="series999")],
                    "TotalRecordCount": 2,
                })
            if req.url.path == f"/Shows/{SERIES_ID}/Episodes":
                self.assertEqual("1", req.url.params["Season"])
                self.assertEqual(USER_ID, req.url.params["UserId"])
                return response(req, payload={"Items": [
                    {"Id": EPISODE_ID, "Type": "Episode",
                     "ParentIndexNumber": 1, "IndexNumber": 2},
                    {"Id": "episode002", "Type": "Episode",
                     "ParentIndexNumber": 1, "IndexNumber": 3},
                    {"Id": "episode003", "Type": "Movie",
                     "ParentIndexNumber": 1, "IndexNumber": 2},
                    {"Id": "episode004", "Type": "Episode",
                     "ParentIndexNumber": "bad", "IndexNumber": 2},
                ]})
            if req.url.path.startswith("/Items/"):
                played.append(req.url.path)
                return response(req, payload={
                    "PlaySessionId": SESSION_ID,
                    "MediaSources": [media_source()],
                })
            self.fail(f"unexpected request {req.url}")

        self.install(handler)
        found = await library.streams("series", "tt0386676:1:2")
        self.assertEqual(1, len(found))
        self.assertEqual([f"/Items/{EPISODE_ID}/PlaybackInfo"], played)
        self.assertTrue(library.identity_trusted(found[0]))

        # A coordinate mismatch is independently negative-cached and must not
        # reuse the positive episode stream.
        self.assertEqual([], await library.streams(
            "series", "tt0386676:1:99"))

    async def test_cached_result_avoids_reauthentication_index_and_playback(self):
        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            if req.url.path == f"/Users/{USER_ID}/Items":
                return response(req, payload={
                    "Items": [movie_item()], "TotalRecordCount": 1})
            return response(req, payload={"MediaSources": [media_source()]})

        self.install(handler)
        first = await library.streams("movie", "tt0113568")
        calls_after_first = len(self.calls)
        second = await library.streams("movie", "tt0113568")
        self.assertIs(first, second)
        self.assertEqual(calls_after_first, len(self.calls))

    async def test_malformed_ids_and_unsupported_media_return_empty(self):
        async def handler(req):
            self.fail(f"invalid request should not call Jellyfin: {req.url}")

        self.install(handler)
        for media, media_id in (
            ("audio", "tt0113568"),
            ("movie", "not-an-imdb-id"),
            ("series", "not-an-imdb-id:1:2"),
        ):
            with self.subTest(media=media, media_id=media_id):
                self.assertEqual([], await library.streams(media, media_id))


class CapabilityTests(NativeJellyfinCase):
    async def test_capability_rejects_tamper_expiry_and_cross_instance_secret(self):
        token = library._mint(MOVIE_ID, SOURCE_ID, "mkv", SESSION_ID).rsplit(
            "/", 1)[-1]
        self.assertEqual(MOVIE_ID, library._decode_capability(token)["item"])

        body, signature = token.split(".", 1)
        changed = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            library._decode_capability(body + "." + changed)

        with patch.object(library.time, "time",
                          return_value=time.time() + 3700):
            with self.assertRaisesRegex(ValueError, "invalid or expired"):
                library._decode_capability(token)

        with patch.object(library, "_ADDON_SECRET", b"another-instance"):
            with self.assertRaisesRegex(ValueError, "invalid or expired"):
                library._decode_capability(token)

    async def test_capability_rejects_unsafe_upstream_identifiers(self):
        for item, source, container in (
            ("../movie", SOURCE_ID, "mkv"),
            (MOVIE_ID, "source/../../bad", "mkv"),
            (MOVIE_ID, SOURCE_ID, "mkv?api_key=leak"),
        ):
            with self.subTest(item=item, source=source, container=container):
                token = library._mint(item, source, container).rsplit("/", 1)[-1]
                with self.assertRaises(ValueError):
                    library._decode_capability(token)


class PlaybackProxyTests(NativeJellyfinCase):
    async def test_range_proxy_injects_token_and_preserves_seek_headers(self):
        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            self.assertEqual("GET", req.method)
            self.assertEqual(f"/Videos/{MOVIE_ID}/stream.mkv", req.url.path)
            self.assertEqual("true", req.url.params["static"])
            self.assertEqual(SOURCE_ID, req.url.params["MediaSourceId"])
            self.assertEqual(SESSION_ID, req.url.params["PlaySessionId"])
            self.assertEqual("bytes=100-199", req.headers.get("range"))
            self.assertEqual('"etag-one"', req.headers.get("if-range"))
            self.assert_token_header(req)
            return response(req, 206, content=b"synthetic-video-bytes",
                            headers={
                                "content-type": "video/x-matroska",
                                "content-range": "bytes 100-120/1000",
                                "content-length": "21",
                                "accept-ranges": "bytes",
                                "etag": '"etag-one"',
                                "set-cookie": "jellyfin-secret-cookie=yes",
                                "x-emby-token": ACCESS_TOKEN,
                            })

        self.install(handler)
        token = library._mint(MOVIE_ID, SOURCE_ID, "mkv", SESSION_ID).rsplit(
            "/", 1)[-1]
        result = await library.serve(token, request(headers={
            "Range": "bytes=100-199", "If-Range": '"etag-one"',
            "Cookie": "must-not-forward=yes",
        }))
        self.assertIsInstance(result, StreamingResponse)
        self.assertEqual(206, result.status_code)
        self.assertEqual("bytes 100-120/1000", result.headers["content-range"])
        self.assertEqual("bytes", result.headers["accept-ranges"])
        self.assertNotIn("set-cookie", result.headers)
        self.assertNotIn("x-emby-token", result.headers)
        body = b"".join([part async for part in result.body_iterator])
        self.assertEqual(b"synthetic-video-bytes", body)

    async def test_invalid_capability_is_404_without_contacting_jellyfin(self):
        async def handler(req):
            self.fail(f"invalid capability contacted Jellyfin: {req.url}")

        self.install(handler)
        result = await library.serve("not.a.valid.capability", request())
        self.assertEqual(404, result.status_code)
        self.assertEqual([], self.calls)

    async def test_playback_401_reauthenticates_once(self):
        logins = 0
        playback_tokens = []

        def handler(req):
            nonlocal logins
            if req.url.path == "/Users/AuthenticateByName":
                logins += 1
                return response(req, payload=auth_payload(f"token-{logins}"))
            playback_tokens.append(req.headers.get("x-emby-token"))
            if len(playback_tokens) == 1:
                return response(req, 401, {"error": "expired"})
            return response(req, 206, content=b"ok", headers={
                "content-type": "video/x-matroska",
                "content-range": "bytes 0-1/2",
                "content-length": "2",
            })

        self.install(handler)
        token = library._mint(MOVIE_ID, SOURCE_ID, "mkv").rsplit("/", 1)[-1]
        result = await library.serve(token, request(headers={"Range": "bytes=0-1"}))
        self.assertEqual(206, result.status_code)
        self.assertEqual(b"ok", b"".join(
            [part async for part in result.body_iterator]))
        self.assertEqual(2, logins)
        self.assertEqual(["token-1", "token-2"], playback_tokens)

    async def test_head_preserves_metadata_without_streaming_body(self):
        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            self.assertEqual("HEAD", req.method)
            return response(req, 200, content=b"", headers={
                "content-type": "video/x-matroska",
                "content-length": "1000",
                "accept-ranges": "bytes",
            })

        self.install(handler)
        token = library._mint(MOVIE_ID, SOURCE_ID, "mkv").rsplit("/", 1)[-1]
        result = await library.serve(token, request("HEAD"))
        self.assertEqual(200, result.status_code)
        self.assertEqual("1000", result.headers["content-length"])
        self.assertEqual("bytes", result.headers["accept-ranges"])


class TranscodeTests(NativeJellyfinCase):
    """A codec the player can't decode is served via a Jellyfin HLS transcode,
    with the access token kept server-side across master, variant and segment."""

    def _u_of(self, url: str) -> str:
        return library._unb64(dict(parse_qsl(urlsplit(url).query))["u"]).decode()

    def _handler(self):
        def handler(req):
            path = req.url.path
            if path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            self.assert_token_header(req)
            if path == f"/Users/{USER_ID}/Items":
                return response(req, payload={
                    "Items": [movie_item()], "TotalRecordCount": 1})
            if path == f"/Items/{MOVIE_ID}/PlaybackInfo":
                if "DeviceProfile" in json.loads(req.content):
                    return response(req, payload={"MediaSources": [{
                        "Id": SOURCE_ID,
                        "TranscodingUrl": (
                            f"/videos/{MOVIE_ID}/master.m3u8"
                            f"?api_key={ACCESS_TOKEN}&DeviceId=dev"
                            f"&MediaSourceId={SOURCE_ID}"),
                    }]})
                return response(req, payload={
                    "PlaySessionId": SESSION_ID,
                    "MediaSources": [hls_source()]})
            if path == f"/videos/{MOVIE_ID}/master.m3u8":
                master = (
                    "#EXTM3U\n"
                    '#EXT-X-STREAM-INF:BANDWIDTH=4000000,'
                    'CODECS="avc1.640028,mp4a.40.2"\n'
                    f"main.m3u8?api_key={ACCESS_TOKEN}&MediaSourceId={SOURCE_ID}\n")
                return response(req, content=master.encode(), headers={
                    "content-type": "application/vnd.apple.mpegurl"})
            if path == f"/videos/{MOVIE_ID}/main.m3u8":
                variant = (
                    "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                    f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin?api_key={ACCESS_TOKEN}"\n'
                    "#EXTINF:6.0,\n"
                    f"hls1/main/0.ts?api_key={ACCESS_TOKEN}&runtimeTicks=0\n"
                    "#EXT-X-ENDLIST\n")
                return response(req, content=variant.encode(), headers={
                    "content-type": "application/vnd.apple.mpegurl"})
            if path == f"/videos/{MOVIE_ID}/hls1/main/0.ts":
                self.assertEqual("bytes=0-", req.headers.get("range"))
                return response(req, 206, content=b"\x47\x40\x11\x10segment",
                                headers={"content-type": "video/mp2t",
                                         "content-range": "bytes 0-10/11",
                                         "content-length": "11"})
            self.fail(f"unexpected Jellyfin request {req.method} {req.url}")
        return handler

    async def test_undecodable_codec_mints_hls_capability(self):
        self.install(self._handler())
        streams = await library.streams("movie", "tt0113568")
        self.assertEqual(1, len(streams))
        capability = library._decode_capability(
            streams[0]["url"].rsplit("/", 1)[-1])
        self.assertEqual("h", capability["mode"])
        self.assertTrue(library.identity_trusted(streams[0]))

    async def test_transcode_chain_keeps_token_out_of_player_urls(self):
        self.install(self._handler())
        streams = await library.streams("movie", "tt0113568")
        token = streams[0]["url"].rsplit("/", 1)[-1]

        master = await library.serve(token, request())
        self.assertEqual(200, master.status_code)
        self.assertEqual("application/vnd.apple.mpegurl", master.media_type)
        master_text = master.body.decode()
        for forbidden in (ACCESS_TOKEN, "api_key", "jellyfin.invalid"):
            self.assertNotIn(forbidden, master_text)
        variants = proxy_urls(master_text)
        self.assertEqual(1, len(variants))

        variant = await library.serve(
            token, request(query=urlsplit(variants[0]).query))
        self.assertEqual(200, variant.status_code)
        variant_text = variant.body.decode()
        for forbidden in (ACCESS_TOKEN, "api_key", "jellyfin.invalid"):
            self.assertNotIn(forbidden, variant_text)
        resources = proxy_urls(variant_text)
        self.assertEqual({".ts", ".bin"},
                         {"." + self._u_of(u).split("?")[0].rsplit(".", 1)[-1]
                          for u in resources})

        segment_url = next(u for u in resources if ".ts" in self._u_of(u))
        served = await library.serve(
            token, request(query=urlsplit(segment_url).query,
                           headers={"Range": "bytes=0-"}))
        self.assertEqual(206, served.status_code)
        body = b"".join([part async for part in served.body_iterator])
        self.assertEqual(b"\x47\x40\x11\x10segment", body)

    async def test_transcode_disabled_drops_the_source(self):
        with patch.object(library, "TRANSCODE", False):
            def handler(req):
                if req.url.path == "/Users/AuthenticateByName":
                    return response(req, payload=auth_payload())
                if req.url.path == f"/Users/{USER_ID}/Items":
                    return response(req, payload={
                        "Items": [movie_item()], "TotalRecordCount": 1})
                if req.url.path == f"/Items/{MOVIE_ID}/PlaybackInfo":
                    self.assertNotIn("DeviceProfile", json.loads(req.content))
                    return response(req, payload={
                        "PlaySessionId": SESSION_ID,
                        "MediaSources": [hls_source()]})
                self.fail(f"unexpected request {req.url}")

            self.install(handler)
            self.assertEqual([], await library.streams("movie", "tt0113568"))

    async def test_transcode_permission_missing_returns_502(self):
        def handler(req):
            if req.url.path == "/Users/AuthenticateByName":
                return response(req, payload=auth_payload())
            if req.url.path == f"/Users/{USER_ID}/Items":
                return response(req, payload={
                    "Items": [movie_item()], "TotalRecordCount": 1})
            if req.url.path == f"/Items/{MOVIE_ID}/PlaybackInfo":
                if "DeviceProfile" in json.loads(req.content):
                    return response(req, payload={  # no TranscodingUrl → blocked
                        "MediaSources": [{"Id": SOURCE_ID}]})
                return response(req, payload={
                    "PlaySessionId": SESSION_ID,
                    "MediaSources": [hls_source()]})
            self.fail(f"unexpected request {req.url}")

        self.install(handler)
        streams = await library.streams("movie", "tt0113568")
        token = streams[0]["url"].rsplit("/", 1)[-1]
        with self.assertLogs("stream-picker", level="WARNING"):
            served = await library.serve(token, request())
        self.assertEqual(502, served.status_code)

    async def test_tampered_resource_signature_is_rejected(self):
        self.install(self._handler())
        streams = await library.streams("movie", "tt0113568")
        token = streams[0]["url"].rsplit("/", 1)[-1]
        master = await library.serve(token, request())
        params = dict(parse_qsl(urlsplit(proxy_urls(master.body.decode())[0]).query))
        forged = ("A" if params["s"][0] != "A" else "B") + params["s"][1:]
        served = await library.serve(
            token, request(query=f"u={params['u']}&s={forged}"))
        self.assertEqual(404, served.status_code)


if __name__ == "__main__":
    unittest.main()
