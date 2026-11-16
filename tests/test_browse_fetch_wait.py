import time
import unittest
from types import SimpleNamespace

from ardia_cli.browse.page import FETCHES_TRACKED, Page
from ardia_cli.browse.readpages import FETCH_FRESH, fetching


class FetchWait(unittest.TestCase):
    def tracker(self):
        return SimpleNamespace(fetches_started={}, fetches_busy_until=0.0, network_on=True)

    def test_an_unfinished_fetch_counts_until_it_ages_out(self):
        page = self.tracker()
        Page.track_fetch(page, "Network.requestWillBeSent", "r1", {"type": "Fetch"})
        self.assertTrue(fetching(page, frozenset()))
        page.fetches_started["r1"] -= FETCH_FRESH + 0.1
        self.assertFalse(fetching(page, frozenset()))

    def test_age_comes_from_when_the_request_started(self):
        page = self.tracker()
        page.fetches_started["old"] = time.monotonic() - 5
        self.assertFalse(fetching(page, frozenset()))

    def test_finished_other_types_and_earlier_requests_dont_count(self):
        page = self.tracker()
        Page.track_fetch(page, "Network.requestWillBeSent", "img", {"type": "Image"})
        Page.track_fetch(page, "Network.requestWillBeSent", "done", {"type": "XHR"})
        Page.track_fetch(page, "Network.loadingFinished", "done", {})
        Page.track_fetch(page, "Network.requestWillBeSent", "before", {"type": "XHR"})
        self.assertFalse(fetching(page, frozenset({"before"})))
        Page.track_fetch(page, "Network.requestWillBeSent", "failed", {"type": "XHR"})
        Page.track_fetch(page, "Network.loadingFailed", "failed", {})
        self.assertFalse(fetching(page, frozenset({"before"})))

    def test_a_fresh_fetch_pushed_out_by_a_burst_still_counts(self):
        page = self.tracker()
        for index in range(FETCHES_TRACKED + 1):
            Page.track_fetch(page, "Network.requestWillBeSent", f"r{index}", {"type": "Fetch"})
        self.assertNotIn("r0", page.fetches_started)
        for index in range(1, FETCHES_TRACKED + 1):
            Page.track_fetch(page, "Network.loadingFinished", f"r{index}", {})
        self.assertEqual(page.fetches_started, {})
        self.assertTrue(fetching(page, frozenset()))
        page.fetches_busy_until -= FETCH_FRESH + 0.1
        self.assertFalse(fetching(page, frozenset()))

    def test_expired_fetches_make_room_without_holding_the_wait(self):
        page = self.tracker()
        stale = time.monotonic() - FETCH_FRESH - 1
        page.fetches_started = {f"old{index}": stale for index in range(FETCHES_TRACKED)}
        Page.track_fetch(page, "Network.requestWillBeSent", "new", {"type": "XHR"})
        self.assertEqual(list(page.fetches_started), ["new"])
        self.assertEqual(page.fetches_busy_until, 0.0)

    def test_no_network_means_no_wait(self):
        page = self.tracker()
        Page.track_fetch(page, "Network.requestWillBeSent", "r1", {"type": "Fetch"})
        page.network_on = False
        self.assertFalse(fetching(page, frozenset()))


if __name__ == "__main__":
    unittest.main()
