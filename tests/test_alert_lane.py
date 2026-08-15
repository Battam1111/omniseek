"""Contract for the alarm lane (2026-08-12, after Bark was deleted from the fleet).

The failure it exists to prevent: every infra alarm pushed to Bark alone, Bark was unreachable from
the mini (three probes, the connection never establishing, 20s timeouts), and the push helper was
fail-open and SILENT. So alarms were written, counted, logged as pushed, and delivered nowhere. A
siren nobody hears is worse than no siren, because the quiet reads as calm. Bark is now gone and
WeCom is the one channel; what these tests hold is that a silent lane can never happen again.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniseek.core import infra_jobs as J
from omniseek.core import notify


class AlertLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "alert-delivery.json"
        self.p = [patch.object(notify, "_ALERT_DELIVERY_PATH", self.ledger),
                  patch.object(J, "_ALERT_DELIVERY_PATH", self.ledger)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def _alert(self, *, delivered: bool):
        with patch.object(notify, "wecom_push", lambda *a, **k: delivered):
            return notify.alert("t", "b")

    def _ledger(self):
        return json.loads(self.ledger.read_text(encoding="utf-8"))

    def test_the_lane_reports_which_channel_took_it(self):
        self.assertEqual(self._alert(delivered=True), ["wecom"])

    def test_total_delivery_failure_is_warned_and_recorded_not_swallowed(self):
        with self.assertLogs("omniseek.core.notify", level="WARNING") as cm:
            self.assertEqual(self._alert(delivered=False), [])
        self.assertTrue(any("NOT DELIVERED" in m for m in cm.output), cm.output)
        self.assertEqual(self._ledger()["undelivered_streak"], 1)

    def test_the_undelivered_streak_accumulates_then_clears_on_a_success(self):
        self._alert(delivered=False)
        self._alert(delivered=False)
        self.assertEqual(self._ledger()["undelivered_streak"], 2)
        self._alert(delivered=True)
        self.assertEqual(self._ledger()["undelivered_streak"], 0)

    def test_the_daily_audit_surfaces_a_disconnected_siren(self):
        self._alert(delivered=False)
        faults = J._audit_alert_delivery()
        self.assertTrue(any("一条都没送达" in f for f in faults), faults)

    def test_a_healthy_lane_produces_no_audit_fault(self):
        self._alert(delivered=True)
        self.assertEqual(J._audit_alert_delivery(), [])

    def test_missing_credentials_return_False_rather_than_raising(self):
        with patch.object(notify, "_WECOM_CREDS_PATH", Path(self.tmp.name) / "nope.json"):
            self.assertFalse(notify.wecom_push("t", "b"))

    def test_the_job_side_wrapper_never_propagates_a_broken_alarm(self):
        def _boom(*a, **k):
            raise RuntimeError("channel on fire")
        with patch.object(notify, "alert", _boom):
            J._alert("t", "b")          # must not raise: a broken siren cannot break the job

    def test_retired_bark_hints_are_absorbed_so_old_callers_do_not_crash(self):
        with patch.object(notify, "wecom_push", lambda *a, **k: True):
            self.assertEqual(notify.alert("t", "b", group="OmniSeek-Health", level="active"), ["wecom"])
        J._alert("t", "b", group="OmniSeek")

    def test_bark_is_gone_from_the_module(self):
        # 全线删除: no code path may reach the retired channel.
        self.assertFalse(hasattr(notify, "bark_push"))
        self.assertFalse(hasattr(J, "_bark"))


if __name__ == "__main__":
    unittest.main()
