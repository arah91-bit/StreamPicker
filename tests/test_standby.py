"""What a player should switch to when the stream it is on fails or stalls.

Implementation-plan Phase 4. A byte-identical copy is spliced into the buffer
by the server itself; a *different* encode can only be a player-side re-open
and seek to the same time — and that seek is exactly what direct usenet is
worst at, so standbys are ranked by seek cost before rank.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import display_caps, main, proxy


def _c(name, *, sig=None, cid=None, auto=True, dbr="TB+"):
    return {"u": f"https://{name}.example/f.mkv", "sig": sig or f"file:{name}",
            "cid": cid or f"url:{name}", "lbl": f"1080p {name}",
            "fn": f"{name}.mkv", "res": 1080, "dbr": dbr, "auto": auto,
            "key": f"k-{name}"}


class _Case(unittest.TestCase):
    def setUp(self):
        rep = MagicMock()
        rep.blocked.return_value = False
        rep.cooled.return_value = False
        self.rep = rep
        for p in (patch.object(proxy, "_sessions", {}),
                  patch.object(proxy, "_entries", {}),
                  patch.object(proxy, "_persist"),
                  patch("app.proxy.reputation", rep)):
            p.start()
            self.addCleanup(p.stop)

    def session(self, pool, *, auto=True, caps=""):
        playing = pool[0]
        entry = {"cands": pool[:2], "pool": pool, "id": "tt1:1:1",
                 "picker": "fast", "pin": 0, "sel": playing["key"],
                 "auto": auto, "viewer": "Viewer", "viewer_key": "vk"}
        if caps:
            entry["caps"] = caps
        proxy._sessions["tok"] = (time.time(), entry)
        return entry


class StandbyRankingTests(_Case):
    def test_low_seek_cost_first_then_rank_never_the_playing_file(self):
        playing = _c("playing")
        twin = _c("twin", cid=playing["cid"])           # spliced server-side
        nzb = _c("nzb", sig="nzb:" + "a" * 64, dbr="")
        easy = _c("easy", dbr="")
        rd = _c("rd", dbr="RD+")
        self.session([playing, twin, nzb, easy, rd])

        rows = proxy.standby("tok")["standby"]

        self.assertEqual(["1080p easy", "1080p rd", "1080p nzb"],
                         [r["name"] for r in rows])
        self.assertEqual(["low", "low", "high"], [r["seek"] for r in rows])
        self.assertEqual("easy.mkv", rows[0]["filename"])

    def test_each_standby_is_a_playable_token_for_the_same_viewer_and_screen(self):
        caps = display_caps.parse("dv=0; dev=tv1").header()
        self.session([_c("playing"), _c("other")], caps=caps)
        row = proxy.standby("tok")["standby"][0]
        token = row["url"].rsplit("/proxy/", 1)[1]
        minted = proxy._sessions[token][1]
        self.assertEqual("https://other.example/f.mkv", minted["cands"][0]["u"])
        self.assertEqual(("Viewer", "vk"), (minted["viewer"], minted["viewer_key"]))
        self.assertEqual(caps, minted["caps"])

    def test_cooled_blocked_and_unverified_copies_are_not_offered(self):
        cooled, blocked, loose = _c("cooled"), _c("blocked"), _c("loose", auto=False)
        self.rep.cooled.side_effect = lambda sig: sig == cooled["sig"]
        self.rep.blocked.side_effect = lambda sig: sig == blocked["sig"]
        self.session([_c("playing"), cooled, blocked, loose, _c("ok")])
        self.assertEqual(["1080p ok"],
                         [r["name"] for r in proxy.standby("tok")["standby"]])

    def test_an_unverified_session_offers_no_silent_substitute(self):
        self.session([_c("playing"), _c("other")], auto=False)
        self.assertEqual([], proxy.standby("tok")["standby"])

    def test_at_most_three(self):
        self.session([_c("playing")] + [_c(f"alt{i}") for i in range(6)])
        self.assertEqual(3, len(proxy.standby("tok")["standby"]))

    def test_an_unknown_token_is_none(self):
        self.assertIsNone(proxy.standby("missing"))


class FillStateTests(_Case):
    def entry(self, **kw):
        e = proxy._Entry("url:playing", "/tmp/x.bin", [], _c("playing"),
                         "video/x-matroska", 10_000, "fast", "tt1:1:1")
        for k, v in kw.items():
            setattr(e, k, v)
        proxy._entries[e.sig] = e
        self.session([_c("playing"), _c("other")])["bufcid"] = e.sig
        return e

    def state(self):
        return proxy.standby("tok")["fill"]["state"]

    def test_unbuffered(self):
        self.session([_c("playing")])
        self.assertEqual("unbuffered", self.state())

    def test_filling_complete_failed(self):
        e = self.entry(avail=100, consumers=1)
        self.assertEqual("filling", self.state())
        e.failed = True
        self.assertEqual("failed", self.state())
        e.failed, e.complete = False, True
        self.assertEqual("complete", self.state())

    def test_stalled_only_with_a_reader_waiting_near_the_edge(self):
        e = self.entry(avail=100, head=90, consumers=1,
                       grew_at=time.monotonic() - 30)
        self.assertEqual("stalled", self.state())
        e.head = 0
        e.avail = 500 * 1024 * 1024          # paused by backpressure, not stuck
        self.assertEqual("filling", self.state())


class RouteTests(_Case):
    def test_the_route_answers_and_expires(self):
        self.session([_c("playing"), _c("other")])
        client = TestClient(main.app)
        ok = client.get("/proxy/tok/standby.json")
        self.assertEqual(200, ok.status_code)
        self.assertEqual("no-store", ok.headers["cache-control"])
        self.assertEqual(1, len(ok.json()["standby"]))
        self.assertEqual(410, client.get("/proxy/nope/standby.json").status_code)


if __name__ == "__main__":
    unittest.main()
