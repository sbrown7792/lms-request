"""Unit tests for the parts that are awkward to exercise over HTTP.

stdlib unittest, no extra dependencies:

    .venv/bin/python -m unittest discover -s tests -v

The live end-to-end path is covered by scripts/probe.py plus the checks in
README.md; these cover the arithmetic and the state machine.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from lmsrequest.lms import LMS, resolve_art_ref
from lmsrequest.party import Party, RequestRefused
from lmsrequest.ratelimit import RateLimiter
from lmsrequest.store import Store


# Keep the engine's own log lines out of the test report.
logging.getLogger("lmsrequest").setLevel(logging.CRITICAL)


def temp_store() -> Store:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


class FakeLMS:
    """Just enough LMS to test queue arithmetic: a list and an index."""

    def __init__(self, tracks: list[str] | None = None, cur: int = 0) -> None:
        self.tracks = list(tracks or [])
        self.cur = cur
        self.commands: list[list] = []
        self.playing = False

    async def queue(self, player):
        return self.cur, [
            {"index": i, "url": u, "title": u, "artist": "", "art": None}
            for i, u in enumerate(self.tracks)
        ]

    async def playlist_add(self, player, url, title=None):
        self.commands.append(["add", url])
        self.tracks.append(url)

    async def playlist_move(self, player, frm, to):
        self.commands.append(["move", frm, to])
        self.tracks.insert(to, self.tracks.pop(frm))

    async def playlist_delete_index(self, player, index):
        self.commands.append(["delete", index])
        self.tracks.pop(index)

    async def playlist_jump(self, player, index):
        self.commands.append(["index", index])

    async def play(self, player):
        self.playing = True

    async def set_shuffle(self, player, mode):
        self.commands.append(["shuffle", mode])

    async def set_repeat(self, player, mode):
        self.commands.append(["repeat", mode])

    async def status(self, player, start=0, count=0):
        return {
            "playlist_cur_index": self.cur,
            "playlist_tracks": len(self.tracks),
            "playlist_loop": [
                {"playlist index": i, "url": u, "title": u}
                for i, u in enumerate(self.tracks)
            ],
        }

    @staticmethod
    def rows(result):
        for key, value in result.items():
            if key.endswith("_loop") and isinstance(value, list):
                return value
        return []

    def image_url(self, path):
        return path


class ArtworkProxyTests(unittest.TestCase):
    """Clients cannot reach LMS, so no LMS URL may ever escape to one."""

    BASE = "http://lms:9000"
    HOST = "lms"

    def resolve(self, ref):
        return resolve_art_ref(ref, self.BASE, self.HOST)

    def test_relative_path_resolves_against_lms(self) -> None:
        self.assertEqual(
            self.resolve("/imageproxy/abc/image.jpg"),
            "http://lms:9000/imageproxy/abc/image.jpg",
        )

    def test_tidal_cdn_is_allowed(self) -> None:
        url = "https://resources.tidal.com/images/a/b/320x320.jpg"
        self.assertEqual(self.resolve(url), url)

    def test_absolute_lms_url_is_allowed(self) -> None:
        url = "http://lms:9000/music/42/cover.jpg"
        self.assertEqual(self.resolve(url), url)

    def test_arbitrary_host_is_refused(self) -> None:
        for ref in (
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.1/admin",
            "https://evil.example.com/x.jpg",
            "http://tidal.com.evil.example.com/x.jpg",
        ):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.resolve(ref)

    def test_traversal_and_odd_schemes_are_refused(self) -> None:
        for ref in (
            "../../etc/passwd",
            "/imageproxy/../../etc/passwd",
            "//evil.example.com/x.jpg",
            "file:///etc/passwd",
            "",
            "   ",
        ):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.resolve(ref)

    def test_image_url_never_leaks_the_lms_address(self) -> None:
        lms = LMS("lms", 9000)
        for value in (
            "/imageproxy/http%3A%2F%2Fresources.tidal.com%2Fa.jpg/image.jpg",
            "http://resources.tidal.com/images/a/1280x1280.jpg",
            "/music/42/cover.jpg",
        ):
            proxied = lms.image_url(value)
            self.assertTrue(proxied.startswith("/art?p="), proxied)
            self.assertNotIn("lms:9000", proxied)

    def test_image_url_is_idempotent(self) -> None:
        """describe() can hand back an already-proxied value from the store."""
        lms = LMS("lms", 9000)
        once = lms.image_url("/imageproxy/a.jpg")
        self.assertEqual(lms.image_url(once), once)

    def test_image_url_passes_none_through(self) -> None:
        self.assertIsNone(LMS("lms", 9000).image_url(None))
        self.assertIsNone(LMS("lms", 9000).image_url(""))

    def test_proxied_reference_survives_the_round_trip(self) -> None:
        """The percent-encoding in an imageproxy path must not be mangled."""
        from urllib.parse import parse_qs, urlsplit as split
        original = "/imageproxy/http%3A%2F%2Fresources.tidal.com%2Fa.jpg/image.jpg"
        proxied = LMS("lms", 9000).image_url(original)
        got = parse_qs(split(proxied).query)["p"][0]
        self.assertEqual(got, original)
        self.assertEqual(self.resolve(got), self.BASE + original)


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = temp_store()
        self.limiter = RateLimiter(self.store, capacity=3, refill_seconds=60)

    def test_spends_capacity_then_refuses(self) -> None:
        for _ in range(3):
            self.assertTrue(self.limiter.take("guest").allowed)
        verdict = self.limiter.take("guest")
        self.assertFalse(verdict.allowed)
        self.assertIn("try again", verdict.message)

    def test_refills_over_time(self) -> None:
        for _ in range(3):
            self.limiter.take("guest")
        self.assertFalse(self.limiter.take("guest").allowed)
        # Backdate the bucket by one refill interval.
        tokens, updated = self.store.bucket("guest")
        self.store.save_bucket("guest", tokens, updated - 60)
        self.assertTrue(self.limiter.take("guest").allowed)

    def test_never_exceeds_capacity(self) -> None:
        self.limiter.take("guest")
        tokens, updated = self.store.bucket("guest")
        self.store.save_bucket("guest", tokens, updated - 10_000)
        self.assertEqual(self.limiter.peek("guest"), 3.0)

    def test_guests_are_independent(self) -> None:
        for _ in range(3):
            self.limiter.take("alice")
        self.assertFalse(self.limiter.take("alice").allowed)
        self.assertTrue(self.limiter.take("bob").allowed)

    def test_refund_returns_a_token(self) -> None:
        self.limiter.take("guest")
        self.assertAlmostEqual(self.limiter.peek("guest"), 2.0, places=2)
        self.limiter.refund("guest")
        self.assertAlmostEqual(self.limiter.peek("guest"), 3.0, places=2)


class StoreStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = temp_store()

    def test_veto_blocks_a_repeat_request(self) -> None:
        self.store.add_request("tidal://1.flc", "Song", "Band", None, "g1")
        self.store.mark("tidal://1.flc", "vetoed")
        self.assertTrue(self.store.requested_ever("tidal://1.flc"))

    def test_retired_request_may_be_asked_for_again(self) -> None:
        self.store.add_request("tidal://1.flc", "Song", "Band", None, "g1")
        self.store.mark("tidal://1.flc", "retired")
        self.assertFalse(self.store.requested_ever("tidal://1.flc"))

    def test_played_still_counts_as_requested(self) -> None:
        self.store.add_request("tidal://1.flc", "Song", "Band", None, "g1")
        self.store.mark("tidal://1.flc", "played")
        self.assertTrue(self.store.requested_ever("tidal://1.flc"))

    def test_pending_is_per_guest(self) -> None:
        self.store.add_request("tidal://1.flc", "A", "", None, "g1")
        self.store.add_request("tidal://2.flc", "B", "", None, "g1")
        self.store.add_request("tidal://3.flc", "C", "", None, "g2")
        self.assertEqual(self.store.pending_count_for("g1"), 2)
        self.assertEqual(self.store.pending_count_for("g2"), 1)


class BanTests(unittest.IsolatedAsyncioTestCase):
    def build(self):
        store = temp_store()
        store.set("player_id", "aa:bb")
        lms = FakeLMS(["p0", "p1", "p2"])
        limiter = RateLimiter(store, capacity=10, refill_seconds=60)
        return Party(lms, None, store, limiter), lms, store

    async def test_banned_address_cannot_request(self) -> None:
        party, lms, _ = self.build()
        party.ban("10.0.0.5")
        with self.assertRaises(RequestRefused) as caught:
            await party.request({"url": "x", "title": "X"}, "g1", ip="10.0.0.5")
        self.assertEqual(caught.exception.code, "banned")
        self.assertNotIn("x", lms.tracks)

    async def test_other_addresses_are_unaffected(self) -> None:
        party, lms, _ = self.build()
        party.ban("10.0.0.5")
        await party.request({"url": "x", "title": "X"}, "g2", ip="10.0.0.6")
        self.assertIn("x", lms.tracks)

    async def test_unban_restores_access(self) -> None:
        party, _, _ = self.build()
        party.ban("10.0.0.5")
        party.unban("10.0.0.5")
        await party.request({"url": "x", "title": "X"}, "g1", ip="10.0.0.5")

    async def test_search_is_blocked_for_banned_addresses(self) -> None:
        party, _, _ = self.build()
        party.ban("10.0.0.5")
        with self.assertRaises(RequestRefused):
            party.ensure_not_banned("10.0.0.5")
        party.ensure_not_banned("10.0.0.9")  # must not raise

    async def test_activity_records_the_address(self) -> None:
        party, _, _ = self.build()
        await party.request({"url": "x", "title": "X", "artist": "A"}, "g1", ip="10.0.0.7")
        feed = party.activity()
        self.assertEqual(feed["requests"][0]["ip"], "10.0.0.7")
        self.assertEqual(feed["requests"][0]["title"], "X")
        self.assertFalse(feed["requests"][0]["banned"])

    async def test_activity_flags_requests_from_banned_addresses(self) -> None:
        party, _, _ = self.build()
        await party.request({"url": "x", "title": "X"}, "g1", ip="10.0.0.7")
        party.ban("10.0.0.7")
        self.assertTrue(party.activity()["requests"][0]["banned"])

    def test_empty_address_is_never_banned(self) -> None:
        """A missing address must not match a ban and lock out everyone."""
        _, _, store = self.build()
        store.add_ban("")
        self.assertFalse(store.is_banned(""))

    def test_reset_keeps_bans(self) -> None:
        party, _, store = self.build()
        party.ban("10.0.0.5")
        store.reset_party()
        self.assertTrue(store.is_banned("10.0.0.5"))


class InjectionTests(unittest.IsolatedAsyncioTestCase):
    def build(self, tracks, cur=0, capacity=10, max_pending=10):
        store = temp_store()
        store.set("player_id", "aa:bb")
        store.set("player_name", "Test")
        lms = FakeLMS(tracks, cur)
        limiter = RateLimiter(store, capacity=capacity, refill_seconds=60)
        party = Party(lms, None, store, limiter, max_pending_per_guest=max_pending)
        return party, lms, store

    def track(self, n):
        return {"url": f"req://{n}", "title": f"Req {n}", "artist": "X"}

    async def test_first_request_goes_straight_after_the_current_track(self) -> None:
        party, lms, _ = self.build(["p0", "p1", "p2", "p3"])
        result = await party.request(self.track(1), "g1")
        self.assertEqual(result["position"], 1)
        self.assertEqual(lms.tracks, ["p0", "req://1", "p1", "p2", "p3"])

    async def test_requests_stay_in_the_order_they_were_asked_for(self) -> None:
        party, lms, _ = self.build(["p0", "p1", "p2", "p3"])
        for n in (1, 2, 3):
            await party.request(self.track(n), f"g{n}")
        self.assertEqual(
            lms.tracks,
            ["p0", "req://1", "req://2", "req://3", "p1", "p2", "p3"],
        )

    async def test_an_interloper_does_not_break_the_order(self) -> None:
        """The regression this design exists for.

        The host promotes a curated track into the middle of the request block
        from Material Skin. A new request must still land after every earlier
        request, not inside them.
        """
        party, lms, _ = self.build(["p0", "p1", "p2"])
        await party.request(self.track(1), "g1")
        await party.request(self.track(2), "g2")
        # Host drags p2 to sit between the current track and request 1.
        lms.tracks.insert(1, lms.tracks.pop(lms.tracks.index("p2")))
        self.assertEqual(lms.tracks, ["p0", "p2", "req://1", "req://2", "p1"])
        await party.request(self.track(3), "g3")
        self.assertEqual(
            lms.tracks, ["p0", "p2", "req://1", "req://2", "req://3", "p1"]
        )

    async def test_request_onto_an_empty_queue_starts_playback(self) -> None:
        party, lms, _ = self.build([])
        result = await party.request(self.track(1), "g1")
        self.assertEqual(result["position"], 1)
        self.assertTrue(lms.playing)

    async def test_placement_follows_the_current_track(self) -> None:
        party, lms, _ = self.build(["p0", "p1", "p2", "p3"], cur=2)
        await party.request(self.track(1), "g1")
        self.assertEqual(lms.tracks, ["p0", "p1", "p2", "req://1", "p3"])

    async def test_duplicate_is_refused(self) -> None:
        party, _, _ = self.build(["p0", "p1"])
        await party.request(self.track(1), "g1")
        with self.assertRaises(RequestRefused) as caught:
            await party.request(self.track(1), "g2")
        self.assertEqual(caught.exception.code, "duplicate")

    async def test_per_guest_pending_cap(self) -> None:
        party, _, _ = self.build(["p0", "p1"], max_pending=2)
        await party.request(self.track(1), "g1")
        await party.request(self.track(2), "g1")
        with self.assertRaises(RequestRefused) as caught:
            await party.request(self.track(3), "g1")
        self.assertEqual(caught.exception.code, "too_many_pending")

    async def test_rate_limit_is_reported_as_such(self) -> None:
        party, _, _ = self.build(["p0", "p1"], capacity=1)
        await party.request(self.track(1), "g1")
        with self.assertRaises(RequestRefused) as caught:
            await party.request(self.track(2), "g1")
        self.assertEqual(caught.exception.code, "rate_limited")

    async def test_paused_requests_are_refused(self) -> None:
        party, _, store = self.build(["p0", "p1"])
        store.set("requests_open", False)
        with self.assertRaises(RequestRefused) as caught:
            await party.request(self.track(1), "g1")
        self.assertEqual(caught.exception.code, "paused")

    async def test_no_player_selected_is_refused_clearly(self) -> None:
        party, _, store = self.build(["p0"])
        store.set("player_id", None)
        with self.assertRaises(RequestRefused) as caught:
            await party.request(self.track(1), "g1")
        self.assertEqual(caught.exception.code, "no_player")

    # --- promoting a track already in the party playlist -----------------

    async def test_request_for_a_playlist_track_moves_it_instead_of_copying(self) -> None:
        """The song must not end up in the queue twice."""
        party, lms, _ = self.build(["p0", "p1", "p2", "p3"])
        result = await party.request({"url": "p3", "title": "P3"}, "g1")
        self.assertEqual(lms.tracks, ["p0", "p3", "p1", "p2"])
        self.assertEqual(lms.tracks.count("p3"), 1)
        self.assertEqual(result["position"], 1)
        self.assertNotIn(["add", "p3"], lms.commands)

    async def test_promoted_track_lands_after_existing_requests(self) -> None:
        party, lms, _ = self.build(["p0", "p1", "p2", "p3"])
        await party.request(self.track(1), "g1")
        # ["p0", "req://1", "p1", "p2", "p3"]
        await party.request({"url": "p3", "title": "P3"}, "g2")
        self.assertEqual(lms.tracks, ["p0", "req://1", "p3", "p1", "p2"])

    async def test_promote_upward_accounts_for_the_splice(self) -> None:
        """Moving up the queue: LMS reads the destination post-removal.

        With the requested copy sitting *between* the current track and the
        last request, a naive destination is one slot too far right.
        """
        party, lms, _ = self.build(["p0", "p1", "p2", "p3"])
        await party.request(self.track(1), "g1")
        await party.request(self.track(2), "g2")
        # ["p0", "req://1", "req://2", "p1", "p2", "p3"] -> ask for p1
        await party.request({"url": "p1", "title": "P1"}, "g3")
        self.assertEqual(
            lms.tracks, ["p0", "req://1", "req://2", "p1", "p2", "p3"]
        )
        self.assertEqual(lms.tracks.count("p1"), 1)

    async def test_promote_from_deep_in_the_queue(self) -> None:
        party, lms, _ = self.build([f"p{i}" for i in range(200)])
        await party.request(self.track(1), "g1")
        result = await party.request({"url": "p150", "title": "P150"}, "g2")
        self.assertEqual(lms.tracks[:4], ["p0", "req://1", "p150", "p1"])
        self.assertEqual(lms.tracks.count("p150"), 1)
        self.assertEqual(len(lms.tracks), 201)  # one added, none duplicated
        self.assertEqual(result["position"], 2)

    async def test_promote_collapses_duplicates_in_the_playlist(self) -> None:
        """A long playlist can hold the same song twice; a request must not
        leave the second copy behind to play again later."""
        tracks = ["p0", "p1", "dup", "p2", "p3", "dup", "p4", "dup"]
        party, lms, _ = self.build(tracks)
        await party.request({"url": "dup", "title": "Dup"}, "g1")
        self.assertEqual(lms.tracks, ["p0", "dup", "p1", "p2", "p3", "p4"])
        self.assertEqual(lms.tracks.count("dup"), 1)

    async def test_duplicates_behind_the_current_track_are_left_alone(self) -> None:
        """Already-played entries are history; only what's ahead matters."""
        party, lms, _ = self.build(["dup", "p1", "dup", "p2"], cur=1)
        await party.request({"url": "dup", "title": "Dup"}, "g1")
        self.assertEqual(lms.tracks, ["dup", "p1", "dup", "p2"])
        self.assertEqual(lms.tracks.count("dup"), 2)  # the one at 0 has played

    async def test_requesting_the_current_track_is_refused(self) -> None:
        party, _, _ = self.build(["p0", "p1"])
        with self.assertRaises(RequestRefused) as caught:
            await party.request({"url": "p0", "title": "P0"}, "g1")
        self.assertEqual(caught.exception.code, "playing_now")

    async def test_already_played_track_is_added_afresh(self) -> None:
        """A track behind the current index is gone; re-request adds a copy."""
        party, lms, _ = self.build(["p0", "p1", "p2"], cur=2)
        await party.request({"url": "p0", "title": "P0"}, "g1")
        self.assertEqual(lms.tracks, ["p0", "p1", "p2", "p0"])

    async def test_promoted_track_is_credited_as_a_request(self) -> None:
        party, lms, store = self.build(["p0", "p1", "p2"])
        await party.request({"url": "p2", "title": "P2"}, "g1")
        self.assertTrue(store.requested_ever("p2"))
        self.assertEqual(store.pending_count_for("g1"), 1)

    async def test_deep_curated_copy_does_not_drag_the_insertion_point(self) -> None:
        """The bug a 900-track playlist exposed.

        Requests are matched by URL, and a long curated playlist contains songs
        guests also ask for. A copy hundreds of tracks away must not count as
        "a request already queued" -- that pushed new requests to the end.
        """
        tracks = [f"p{i}" for i in range(500)]
        tracks[400] = "req://1"  # a curated copy of a requested song, far away
        party, lms, _ = self.build(tracks)
        await party.request(self.track(1), "g1")   # promotes index 400
        # Now a different guest asks for something new: it must land at cur+2,
        # right behind request 1 -- not at index 401.
        result = await party.request(self.track(2), "g2")
        self.assertEqual(result["position"], 2)
        self.assertEqual(lms.tracks[:3], ["p0", "req://1", "req://2"])

    async def test_display_ignores_requests_far_down_the_queue(self) -> None:
        party, lms, store = self.build([f"p{i}" for i in range(300)])
        store.add_request("p250", "Far away", "", None, "g1")
        snap = await party.snapshot(up_next=3)
        self.assertFalse(snap["now_playing"]["requested"])
        self.assertTrue(all(not t["requested"] for t in snap["up_next"]))

    async def test_refused_request_refunds_the_token(self) -> None:
        party, _, store = self.build(["p0", "p1"], capacity=2)
        with self.assertRaises(RequestRefused):
            await party.request({"url": "p0", "title": "P0"}, "g1")  # playing now
        self.assertAlmostEqual(party.limiter.peek("g1"), 2.0, places=2)

    async def test_host_veto_marks_the_track_vetoed(self) -> None:
        party, lms, store = self.build(["p0", "p1"])
        await party.request(self.track(1), "g1")
        await party.remove(1)
        self.assertNotIn("req://1", lms.tracks)
        self.assertTrue(store.requested_ever("req://1"))


if __name__ == "__main__":
    unittest.main()
