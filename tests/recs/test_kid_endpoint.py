"""The admin kid-mode endpoint.

Kid mode with no age filters nothing, so "this viewer is a child" and "this
is the ceiling on what they are shown" cannot be allowed to come apart — the
endpoint is the only place every client passes through.
"""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.recs import kids, main


class KidEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver")
        self.secret = main.config.require("SETUP_SECRET")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _post(self, user, body):
        update = AsyncMock()
        with (
            patch.object(main.db, "get_user", AsyncMock(return_value=user)),
            patch.object(main.db, "update_kid", update),
            patch.object(main, "generate_for_user", AsyncMock()),
            patch.object(main, "_queue_profile_streaming_build", lambda _u: True),
        ):
            response = await self.client.post(
                f"/setup/{self.secret}/api/kid/{user['token']}", json=body)
        return response, update

    async def test_switching_kid_mode_on_without_an_age_still_sets_one(self):
        never_set = {"token": "t", "name": "A", "is_kid": 0,
                     "kid_age": None, "kid_birthdate": None}

        response, update = await self._post(never_set, {"is_kid": True})

        self.assertEqual(200, response.status_code)
        _token, is_kid, age, birthdate = update.await_args.args
        self.assertTrue(is_kid)
        self.assertEqual(kids.DEFAULT_AGE, age)
        self.assertEqual(kids.birthdate_from_age(kids.DEFAULT_AGE), birthdate)

    async def test_switching_kid_mode_on_keeps_an_age_already_anchored(self):
        returning = {"token": "t", "name": "A", "is_kid": 0, "kid_age": 11,
                     "kid_birthdate": kids.birthdate_from_age(11)}

        _response, update = await self._post(returning, {"is_kid": True})

        # None here means "leave the anchor alone", so the year that passed
        # while kid mode was off is not silently rolled back.
        self.assertEqual(("t", True, None, None), update.await_args.args)

    async def test_switching_kid_mode_off_leaves_the_anchor_alone(self):
        kid = {"token": "t", "name": "A", "is_kid": 1, "kid_age": 8,
               "kid_birthdate": kids.birthdate_from_age(8)}

        _response, update = await self._post(kid, {"is_kid": False})

        self.assertEqual(("t", False, None, None), update.await_args.args)

    async def test_a_chosen_age_re_anchors_from_today(self):
        kid = {"token": "t", "name": "A", "is_kid": 1, "kid_age": 4,
               "kid_birthdate": kids.birthdate_from_age(4)}

        _response, update = await self._post(kid, {"is_kid": True,
                                                   "kid_age": 12})

        self.assertEqual(
            ("t", True, 12, kids.birthdate_from_age(12)),
            update.await_args.args)

    async def test_an_age_off_the_end_of_the_slider_is_clamped(self):
        kid = {"token": "t", "name": "A", "is_kid": 1, "kid_age": 8,
               "kid_birthdate": kids.birthdate_from_age(8)}

        for sent, stored in ((99, kids.MAX_AGE), (-3, kids.MIN_AGE)):
            _response, update = await self._post(kid, {"is_kid": True,
                                                       "kid_age": sent})
            self.assertEqual(stored, update.await_args.args[2], sent)


if __name__ == "__main__":
    unittest.main()
