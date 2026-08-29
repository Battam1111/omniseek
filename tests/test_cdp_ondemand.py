"""On-demand CDP browser lifecycle (2026-08-29).

Covers the pure logic: port parsing, service mapping, last-use stamps, and the reaper's decision
table. The launchctl calls themselves are mocked -- the real ones were verified by hand on the
mini (stop stays stopped, cold start 1s, login session survives) and cannot run in CI.
"""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from omniseek.core import infra_jobs
from omniseek.core.sources.walled import _cdp


class CdpPortMappingTests(unittest.TestCase):
    def test_port_is_parsed_from_the_url(self):
        self.assertEqual(_cdp.cdp_port("http://127.0.0.1:9224"), "9224")
        self.assertEqual(_cdp.cdp_port("http://localhost:9222/json"), "9222")

    def test_port_is_none_when_absent(self):
        self.assertIsNone(_cdp.cdp_port("http://127.0.0.1"))
        self.assertIsNone(_cdp.cdp_port(""))

    def test_known_ports_map_to_launchd_labels(self):
        self.assertEqual(_cdp.cdp_service_for("http://127.0.0.1:9224"),
                         "com.omniseek.cdp.xhs-cn")
        self.assertEqual(_cdp.cdp_service_for("http://127.0.0.1:9222"),
                         "com.omniseek.cdp.cn-forums")

    def test_unknown_port_maps_to_nothing(self):
        """The jailed render Chromium (9444) is a container, not a launchd service: it must be
        left completely alone so its behaviour stays byte-identical to before this shipped."""
        self.assertIsNone(_cdp.cdp_service_for("http://127.0.0.1:9444"))


class LastUseStampTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        patcher = mock.patch.object(_cdp, "_CDP_STATE_DIR", Path(self._tmp.name) / "cdp-lastuse")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_stamp_round_trips(self):
        before = time.time()
        _cdp.touch_last_use("http://127.0.0.1:9224")
        stamped = _cdp.read_last_use("9224")
        self.assertIsNotNone(stamped)
        self.assertGreaterEqual(stamped, before)

    def test_unstamped_port_reads_none(self):
        self.assertIsNone(_cdp.read_last_use("9225"))

    def test_stamping_an_urlless_port_is_a_noop(self):
        _cdp.touch_last_use("no-port-here")  # must not raise

    def test_unreadable_stamp_reads_none_rather_than_raising(self):
        _cdp._CDP_STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_cdp._CDP_STATE_DIR / "9222").write_text("not a number", encoding="utf-8")
        self.assertIsNone(_cdp.read_last_use("9222"))


class EnsureBrowserTests(unittest.TestCase):
    def test_noop_off_darwin(self):
        with mock.patch.object(_cdp.sys, "platform", "win32"), \
             mock.patch.object(_cdp, "cdp_health") as health, \
             mock.patch.object(_cdp.subprocess, "run") as run:
            _cdp.ensure_browser("http://127.0.0.1:9224")
        health.assert_not_called()
        run.assert_not_called()

    def test_noop_for_unmanaged_port(self):
        with mock.patch.object(_cdp.sys, "platform", "darwin"), \
             mock.patch.object(_cdp, "cdp_health") as health, \
             mock.patch.object(_cdp.subprocess, "run") as run:
            _cdp.ensure_browser("http://127.0.0.1:9444")
        health.assert_not_called()
        run.assert_not_called()

    def test_healthy_browser_is_not_restarted(self):
        with mock.patch.object(_cdp.sys, "platform", "darwin"), \
             mock.patch.object(_cdp, "cdp_health", return_value=(True, "OK")), \
             mock.patch.object(_cdp, "touch_last_use") as touch, \
             mock.patch.object(_cdp.subprocess, "run") as run:
            _cdp.ensure_browser("http://127.0.0.1:9224")
        run.assert_not_called()
        touch.assert_called_once()

    def test_dead_browser_is_started_then_stamped(self):
        health = mock.Mock(side_effect=[(False, "down"), (False, "down"), (True, "OK")])
        with mock.patch.object(_cdp.sys, "platform", "darwin"), \
             mock.patch.object(_cdp, "cdp_health", health), \
             mock.patch.object(_cdp, "touch_last_use") as touch, \
             mock.patch.object(_cdp.subprocess, "run") as run, \
             mock.patch.object(_cdp.os, "getuid", return_value=501, create=True), \
             mock.patch.object(_cdp.time, "sleep"):
            _cdp.ensure_browser("http://127.0.0.1:9224")
        run.assert_called_once()
        self.assertIn("kickstart", run.call_args[0][0])
        self.assertIn("com.omniseek.cdp.xhs-cn", run.call_args[0][0][-1])
        touch.assert_called_once()

    def test_browser_that_never_comes_up_raises(self):
        with mock.patch.object(_cdp.sys, "platform", "darwin"), \
             mock.patch.object(_cdp, "cdp_health", return_value=(False, "down")), \
             mock.patch.object(_cdp.subprocess, "run"), \
             mock.patch.object(_cdp.os, "getuid", return_value=501, create=True), \
             mock.patch.object(_cdp.time, "sleep"), \
             mock.patch.object(_cdp, "_START_TIMEOUT_S", 0.01):
            with self.assertRaises(RuntimeError):
                _cdp.ensure_browser("http://127.0.0.1:9224")


def _maint_flag(present: bool):
    """A stand-in for the cdp-maintenance flag path.

    The whole object is replaced rather than its .exists patched: Path instances expose read-only
    method attributes, so patching the method on the instance raises AttributeError."""
    flag = mock.Mock()
    flag.exists.return_value = present
    return flag


class ReaperDecisionTests(unittest.TestCase):
    """The reaper's decision table. Each case pins ONE branch so a future edit that collapses
    them shows up as a named failure rather than as browsers dying mid-call."""

    def _run(self, *, healthy, last_use, idle_s=1800):
        with mock.patch.object(infra_jobs.sys, "platform", "darwin"), \
             mock.patch.object(infra_jobs, "_MAINT_FLAG", _maint_flag(False)), \
             mock.patch.object(infra_jobs, "CDP_IDLE_TIMEOUT_S", idle_s), \
             mock.patch.object(_cdp, "cdp_health", side_effect=lambda url: (healthy(url), "")), \
             mock.patch.object(_cdp, "read_last_use", side_effect=last_use), \
             mock.patch.object(_cdp, "touch_last_use") as touch, \
             mock.patch.object(infra_jobs.subprocess, "run") as run, \
             mock.patch.object(infra_jobs.os, "getuid", return_value=501, create=True):
            result = infra_jobs.run_cdp_reaper()
        return result, run, touch

    def test_idle_browser_is_stopped(self):
        old = time.time() - 7200
        result, run, _ = self._run(healthy=lambda u: "9224" in u, last_use=lambda p: old)
        self.assertEqual(len(result["stopped"]), 1)
        self.assertTrue(result["stopped"][0].startswith("9224"))
        self.assertIn("TERM", run.call_args[0][0])

    def test_recently_used_browser_is_kept(self):
        fresh = time.time() - 60
        result, run, _ = self._run(healthy=lambda u: "9224" in u, last_use=lambda p: fresh)
        self.assertEqual(result["stopped"], [])
        run.assert_not_called()

    def test_already_stopped_browser_is_skipped(self):
        result, run, _ = self._run(healthy=lambda u: False, last_use=lambda p: 0)
        self.assertEqual(result["stopped"], [])
        self.assertEqual(len(result["already_down"]), 4)
        run.assert_not_called()

    def test_unstamped_browser_is_seeded_not_reaped(self):
        """First cycle after deploy: a browser that is up but has no stamp must NOT be killed,
        or the deploy itself would take out a browser someone is using."""
        result, run, touch = self._run(healthy=lambda u: "9222" in u, last_use=lambda p: None)
        self.assertEqual(result["stopped"], [])
        self.assertIn("9222:seeded", result["kept"])
        touch.assert_called_once()
        run.assert_not_called()

    def test_maintenance_flag_stops_the_reaper(self):
        with mock.patch.object(infra_jobs.sys, "platform", "darwin"), \
             mock.patch.object(infra_jobs, "_MAINT_FLAG", _maint_flag(True)), \
             mock.patch.object(infra_jobs.subprocess, "run") as run:
            result = infra_jobs.run_cdp_reaper()
        self.assertEqual(result, {"skipped": "maintenance"})
        run.assert_not_called()


class ReaperJobRowTests(unittest.TestCase):
    def test_reaper_is_registered_process_isolated(self):
        from omniseek.core import jobs
        jobs.register_shipped_jobs()
        row = jobs.registry().get("cdp-reaper")
        self.assertIsNotNone(row, "cdp-reaper job row must be registered")
        self.assertEqual(row.process_entrypoint,
                         ("omniseek.core.infra_jobs", "run_cdp_reaper"))


if __name__ == "__main__":
    unittest.main()
