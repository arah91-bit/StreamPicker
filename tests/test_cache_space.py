"""The read-ahead cache must bound itself by the disk it is on.

Field-review case (2026-10-15): nothing checked free space. BUFFER_CACHE_GB
defaults to 100 GB whatever the volume holds, and a full disk made every
producer's write raise — "producer crashed", the entry failed, every buffered
stream truncated mid-scene. An unbuffered write can also return *short* as the
disk fills, and the producer used to advance `avail` by the whole chunk anyway.
"""

import asyncio
import errno
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import proxy

TOTAL = 64 * 1024
DATA = b"\x1aE\xdf\xa3" + bytes(TOTAL - 4)
GB = 1024 ** 3


class _Resp:
    def __init__(self, chunk=4096):
        self.status_code = 206
        self.headers = {"content-range": f"bytes 0-{TOTAL - 1}/{TOTAL}"}
        self._chunk = chunk

    async def aiter_raw(self):
        for i in range(0, TOTAL, self._chunk):
            yield DATA[i:i + self._chunk]

    async def aclose(self):
        pass


class _Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rep = MagicMock()
        for p in (patch("app.proxy.reputation", self.rep),
                  patch("app.proxy.telemetry.record_buffer"),
                  patch("app.proxy.usenet_health"),
                  patch.object(proxy, "_entries", {}),
                  patch.object(proxy, "BUFFER_MIN_FREE_BYTES", 5 * GB),
                  patch.object(proxy, "_SPACE_CHECK_BYTES", 1)):
            p.start()
            self.addCleanup(p.stop)

    def entry(self, sig, *, consumers=0, last=0.0):
        cand = {"u": f"https://cdn/{sig}", "sig": "file:" + "a" * 64,
                "lbl": sig, "dbr": "", "cid": sig}
        e = proxy._Entry(sig, os.path.join(self.dir.name, sig + ".bin"),
                         [cand], cand, "video/x-matroska", TOTAL, "fast", "tt1")
        e.consumers, e.last = consumers, last
        open(e.path, "wb").close()
        proxy._entries[sig] = e
        return e


class RoomToWriteTests(_Case):
    def test_idle_entries_are_evicted_oldest_first_until_there_is_room(self):
        old = self.entry("old", last=1)
        newer = self.entry("newer", last=2)
        playing = self.entry("playing", consumers=1, last=0)
        frees = iter([1 * GB, 6 * GB])
        with patch.object(proxy, "_free_bytes", side_effect=lambda: next(frees)):
            self.assertTrue(proxy._room_to_write())
        self.assertNotIn("old", proxy._entries)
        self.assertIn("newer", proxy._entries)
        self.assertIn("playing", proxy._entries)
        self.assertFalse(os.path.exists(old.path))
        del newer, playing

    def test_a_reader_and_the_caller_are_never_evicted(self):
        me = self.entry("me")
        self.entry("watched", consumers=1)
        with patch.object(proxy, "_free_bytes", return_value=1 * GB):
            self.assertFalse(proxy._room_to_write(keep=me))
        self.assertEqual({"me", "watched"}, set(proxy._entries))

    def test_an_unknown_volume_never_blocks(self):
        with patch.object(proxy, "_free_bytes", return_value=None):
            self.assertTrue(proxy._room_to_write())


class WriteAllTests(unittest.TestCase):
    def test_a_short_write_is_finished_not_assumed(self):
        written = bytearray()

        class Short:
            def write(self, view):
                n = min(len(view), 3)
                written.extend(view[:n])
                return n

        proxy._write_all(Short(), b"0123456789")
        self.assertEqual(b"0123456789", bytes(written))

    def test_a_write_that_makes_no_progress_is_disk_full(self):
        class Stuck:
            def write(self, view):
                return 0

        with self.assertRaises(OSError) as cm:
            proxy._write_all(Stuck(), b"x")
        self.assertEqual(errno.ENOSPC, cm.exception.errno)


class ProducerSpaceTests(_Case):
    async def test_a_full_volume_stops_the_fill_without_blaming_the_source(self):
        e = self.entry("e", consumers=1)
        resp = _Resp()
        with patch.object(proxy, "_free_bytes", return_value=1 * GB):
            await proxy._produce(e, "tok", resp, resp.aiter_raw(), [])
        self.assertTrue(e.failed)
        self.assertFalse(e.complete)
        self.rep.observe.assert_not_called()
        self.rep.cooldown.assert_not_called()

    async def test_enospc_mid_write_makes_room_and_rewrites_the_chunk(self):
        e = self.entry("e", consumers=1)
        self.entry("idle", last=0)
        real = proxy._write_all
        failed = []

        def flaky(f, data):
            if not failed:
                failed.append(True)
                f.write(data[:100])            # a partial write, then ENOSPC
                raise OSError(errno.ENOSPC, "No space left on device")
            real(f, data)

        def free():
            # Plenty until the disk fills under us; then low until the idle
            # entry is evicted.
            if failed and "idle" in proxy._entries:
                return 1 * GB
            return 10 * GB

        resp = _Resp()
        with patch.object(proxy, "_free_bytes", side_effect=free), \
             patch.object(proxy, "_write_all", side_effect=flaky):
            await proxy._produce(e, "tok", resp, resp.aiter_raw(), [])
        self.assertTrue(e.complete)
        self.assertNotIn("idle", proxy._entries, "room was made by evicting")
        with open(e.path, "rb") as f:
            self.assertEqual(DATA, f.read())


    async def test_a_second_enospc_is_a_clean_stop_not_a_crash(self):
        e = self.entry("e", consumers=1)

        def full(f, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        resp = _Resp()
        with patch.object(proxy, "_free_bytes", return_value=None), \
             patch.object(proxy, "_write_all", side_effect=full), \
             patch.object(proxy.logger, "exception") as crashed:
            await proxy._produce(e, "tok", resp, resp.aiter_raw(), [])
        self.assertTrue(e.failed)
        crashed.assert_not_called()
        self.rep.observe.assert_not_called()


class UnbufferedFallbackTests(_Case):
    def test_a_new_stream_skips_the_buffer_when_there_is_no_room(self):
        with patch.object(proxy, "_free_bytes", return_value=1 * GB):
            self.assertFalse(proxy._buffer_usable({}))

    def test_a_stream_already_buffered_keeps_its_entry(self):
        e = self.entry("live", consumers=1)
        with patch.object(proxy, "_free_bytes", return_value=1 * GB):
            self.assertTrue(proxy._buffer_usable({"bufcid": e.sig}))

    async def test_revive_refuses_on_a_full_volume(self):
        e = self.entry("e")
        e.failed, e.avail = True, 1024
        with patch.object(proxy, "_free_bytes", return_value=1 * GB), \
             patch.object(proxy, "_connect_resume") as resume:
            self.assertFalse(await proxy._revive(e, "tok"))
        resume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
