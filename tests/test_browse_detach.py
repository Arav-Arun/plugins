import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ardia_cli import command_metrics
from ardia_cli.browse import changes, commands, readpages
from ardia_cli.browse.errors import BrowseError
from ardia_cli.browse.state import State

START = "https://mine.example/start"
DEST = "https://mine.example/dest"


class FakeBrowser:
    def __init__(self, state):
        self.state = state
        self.link = mock.Mock()
        self.sent = []
        self.attached = 0
        self.url = START
        self.loader = "L1"
        self.disable_hangs = False
        self.after_load = None

    async def send(self, method, params=None, session=None, timeout=30, mutation=False):
        self.sent.append((method, session))
        if method == "Target.attachToTarget":
            self.attached += 1
            return {"sessionId": f"S{self.attached}"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F", "url": self.url, "loaderId": self.loader, "securityOrigin": "https://mine.example"}}}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.callFunctionOn":
            source = params["functionDeclaration"]
            value = "Destination" if "document.title" in source else "complete" if "readyState" in source else False
            return {"result": {"value": value}}
        if method == "Page.navigate":
            self.url, self.loader = params["url"], "L2"
            asyncio.get_running_loop().call_soon(self.loaded, session)
            return {"frameId": "F", "loaderId": "L2"}
        if method == "Network.disable" and self.disable_hangs:
            await asyncio.sleep(30)
        return {}

    def loaded(self, session):
        self.state.on_event("Page.frameNavigated", {"frame": {"id": "F", "url": self.url, "loaderId": "L2"}}, session)
        self.state.on_event("Page.lifecycleEvent", {"frameId": "F", "loaderId": "L2", "name": "load"}, session)
        if self.after_load:
            self.after_load(session)


class OpenAfterDetach(unittest.TestCase):
    def setUp(self):
        self.state = State({"session": "t", "data_root": "/nowhere"}, lambda: None)
        self.state.instance = "I"
        self.state.target_infos = {"T": {"targetId": "T", "url": START, "type": "page"}}
        self.state.current = "T"
        self.browser = FakeBrowser(self.state)
        self.state.browser = self.browser
        self.metrics = command_metrics.Metrics("open", False, True)

        async def connect():
            return None

        async def before_navigating(page, url):
            return None

        async def arrival(ctx, page, came_from):
            return None

        self.state.connect = connect
        for patch in (mock.patch.object(readpages, "before_navigating", before_navigating), mock.patch.object(changes, "arrival", arrival),
                      mock.patch.object(changes, "STOP_TIMEOUT", 0.05)):
            patch.start()
            self.addCleanup(patch.stop)

    def open(self):
        async def run():
            command_metrics.current.set(self.metrics)
            await self.state.page("T")
            ctx = commands.Ctx(self.state, {}, "/", {}, None, "open")
            return await commands.cmd_open(ctx, [DEST])

        return asyncio.run(run())

    def test_an_open_whose_network_disable_goes_unanswered_keeps_its_tab_and_finishes(self):
        self.browser.disable_hangs = True
        result = self.open()
        self.assertEqual(result.data["url"], DEST)
        self.assertEqual(result.data["title"], "Destination")
        self.assertEqual(self.browser.attached, 1)
        self.assertNotIn("Target.detachFromTarget", [method for method, _ in self.browser.sent])
        self.assertEqual(self.state.pages["T"].session_id, "S1")
        self.assertTrue(self.metrics.values.get("network_disable_unconfirmed"))
        self.assertNotIn("reattached_after_detach", self.metrics.values)

    def test_an_open_whose_tab_ardia_detached_but_kept_open_attaches_again_and_finishes(self):
        self.browser.after_load = lambda session: self.state.on_event("Inspector.detached", {"reason": "Render process gone."}, session)
        result = self.open()
        self.assertEqual(result.data["url"], DEST)
        self.assertEqual(result.data["title"], "Destination")
        self.assertEqual(self.browser.attached, 2)
        self.assertEqual(self.state.pages["T"].session_id, "S2")
        self.assertTrue(self.metrics.values.get("reattached_after_detach"))

    def test_an_open_whose_tab_closed_still_fails_as_detached(self):
        self.browser.after_load = lambda session: self.state.on_event("Target.targetDestroyed", {"targetId": "T"}, None)
        with self.assertRaises(BrowseError) as failed:
            self.open()
        self.assertEqual(failed.exception.code, "tab_detached")
        self.assertEqual(self.browser.attached, 1)
        self.assertNotIn("reattached_after_detach", self.metrics.values)


if __name__ == "__main__":
    unittest.main()
