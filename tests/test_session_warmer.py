"""Session warmer vs the CDP idle reaper (2026-09-26).

The warmer runs three times a day; the idle reaper had usually stopped its browsers by then. It
connected without starting them, got ECONNREFUSED, and alerted "session degraded, re-login via VNC"
for all three accounts: a false instruction, since nobody had looked at the sessions. These tests
pin the fix: start the browser first (as every read does), and never turn "could not reach the
browser" into a re-login alert. The browser driver and launchctl are faked; nothing here touches an
account.
"""
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from omniseek.core import infra_jobs
from omniseek.core.sources.walled import _cdp


class _Stop(Exception):
    """Raised by the fake browser once connect succeeded: the page flow after it is not under test."""


class _FakeBrowser:
    @property
    def contexts(self):
        raise _Stop("past the connect")


class _FakePlaywright:
    def __init__(self, order, refuse=False):
        self.order, self.refuse = order, refuse
        self.chromium = self

    def connect_over_cdp(self, url):
        self.order.append(("connect", url))
        if self.refuse:
            raise RuntimeError(f"BrowserType.connect_over_cdp: connect ECONNREFUSED {url}")
        return _FakeBrowser()


def _warm(p, label="大陆号-xiaohongshu"):
    cdp, home, search_tpl, key, probe = infra_jobs._WARMER_INSTANCES[label]
    return infra_jobs._warm_one(p, label, cdp, home, search_tpl, key, probe)


class WarmOneTests(unittest.TestCase):
    def test_a_reaped_browser_is_started_before_connecting(self):
        order = []
        with mock.patch.object(_cdp, "ensure_browser",
                               side_effect=lambda url: order.append(("ensure", url))):
            res = _warm(_FakePlaywright(order))
        self.assertEqual(order, [("ensure", "http://127.0.0.1:9224"),
                                 ("connect", "http://127.0.0.1:9224")])
        self.assertFalse(res["unprobed"])  # it got past the connect, into the page flow
        self.assertIn("warm flow raised", res["reason"])

    def test_a_browser_that_will_not_start_is_unprobed_and_never_connected(self):
        order = []
        with mock.patch.object(_cdp, "ensure_browser", side_effect=RuntimeError(
                "CDP browser com.omniseek.cdp.xhs-cn did not become ready within 20s")):
            res = _warm(_FakePlaywright(order))
        self.assertTrue(res["unprobed"])
        self.assertTrue(res["reason"].startswith("browser start failed"))
        self.assertEqual(order, [])
        self.assertFalse(infra_jobs._needs_relogin_alert(res))

    def test_a_refused_connect_is_unprobed_not_degraded(self):
        with mock.patch.object(_cdp, "ensure_browser"):
            res = _warm(_FakePlaywright([], refuse=True))
        self.assertTrue(res["unprobed"])
        self.assertTrue(res["reason"].startswith("cdp connect failed"))
        self.assertFalse(infra_jobs._needs_relogin_alert(res))


class AlertDecisionTests(unittest.TestCase):
    def test_only_a_looked_at_bad_session_earns_the_relogin_alert(self):
        bad = {"ok": False, "unprobed": False,
               "reason": "degraded: search notes=0 dom_items=0 login_overlay=True"}
        self.assertTrue(infra_jobs._needs_relogin_alert(bad))
        self.assertFalse(infra_jobs._needs_relogin_alert({**bad, "ok": True}))
        self.assertFalse(infra_jobs._needs_relogin_alert({**bad, "unprobed": True}))
        self.assertFalse(infra_jobs._needs_relogin_alert({**bad, "self_heals": True}))
        # forum results carry no ``unprobed`` key at all; a looked-at failure there still counts
        self.assertTrue(infra_jobs._needs_relogin_alert({"ok": False, "reason": "degraded"}))


class IncidentReplayTests(unittest.TestCase):
    """2026-09-26 19:24: all three CDP browsers reaped, connect refused, three false alerts."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.state_path = tmp / "session-warmer.json"
        self._patches = [
            mock.patch.object(infra_jobs, "_WARMER_STATE", self.state_path),
            mock.patch.object(infra_jobs, "_MAINT_FLAG", tmp / "cdp-maintenance"),
            mock.patch.object(infra_jobs, "_jsleep", lambda lo, hi: None),
            mock.patch.dict(os.environ, {"WARMER_FORCE": "1", "WARMER_ONLY": ""}),
            # The incident had three live accounts; replay it with none sealed (the 2026-10-07
            # mainland seal skips 9224, covered by tests/test_xhs_cn_seal.py).
            mock.patch.object(infra_jobs, "_warmer_sealed", lambda cdp: ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _run(self, ensure):
        order, sent = [], []
        fake_p = _FakePlaywright(order, refuse=True)

        class _Session:
            def __enter__(self):
                return fake_p

            def __exit__(self, *exc):
                return False

        driver = types.ModuleType("patchright.sync_api")
        driver.sync_playwright = lambda: _Session()
        package = types.ModuleType("patchright")
        package.sync_api = driver
        server = types.ModuleType("omniseek.server")

        def _no_forums():
            raise RuntimeError("the forum half is not under test")

        server.load_sources = _no_forums
        with mock.patch.dict(sys.modules, {"patchright": package, "patchright.sync_api": driver,
                                           "omniseek.server": server}), \
                mock.patch.object(_cdp, "ensure_browser", side_effect=ensure), \
                mock.patch.object(infra_jobs, "_alert",
                                  lambda title, body, **_kw: sent.append(title)):
            infra_jobs.run_session_warmer()
        return order, sent, json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_unreachable_browsers_raise_no_relogin_alert(self):
        order, sent, state = self._run(ensure=lambda url: None)
        self.assertEqual(sent, [])
        self.assertEqual([r["unprobed"] for r in state["last_results"]], [True, True, True])
        self.assertEqual(len([o for o in order if o[0] == "connect"]), 3)

    def test_every_account_gets_its_browser_started(self):
        started = []
        self._run(ensure=started.append)
        self.assertEqual(sorted(started), ["http://127.0.0.1:9223", "http://127.0.0.1:9224",
                                           "http://127.0.0.1:9225"])


class NoBypassRatchetTests(unittest.TestCase):
    def test_every_direct_cdp_connect_outside_the_cdp_module_starts_the_browser_first(self):
        """Two incidents of one class: a caller drove a CDP browser without the on-demand start
        and reported a merely reaped browser as a failure (douyin's 53-run health streak, then this
        warmer). _cdp.cdp_call already starts the browser; any other direct connect_over_cdp in
        src/ must call ensure_browser earlier in the same function. scripts/omniseek_doctor.py is out of
        scope on purpose: a diagnosis must see a stopped browser as stopped, not start it."""
        src = Path(__file__).resolve().parents[1] / "src" / "omniseek"
        offenders = []
        for path in sorted(src.rglob("*.py")):
            if path.name == "_cdp.py":
                continue
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"^[^#\n]*\.connect_over_cdp\(", text, flags=re.M):
                defs = [d.start() for d in re.finditer(r"^[ \t]*def ", text[:m.start()], flags=re.M)]
                if "ensure_browser(" not in text[(defs[-1] if defs else 0):m.start()]:
                    offenders.append(f"{path.relative_to(src)}:{text.count(chr(10), 0, m.start()) + 1}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
