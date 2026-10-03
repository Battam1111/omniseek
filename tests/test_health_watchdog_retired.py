"""Contract: the source-health watchdog PROBES retired sources, and only watches them for coming back.

The defect this pins (found 2026-10-04): run_source_health dropped every retired source from the
probe list before probing, so the retired handling further down (no fail streak, and the
``已退役源复活`` alert plus the ``retired_alive`` state when a retired source answers again) never saw
one. higheredjobs_cs sat retired by a stale overlay from 2026-06-18 to 2026-10-04 while healthy, and
nothing flagged it. The contract:

  1. a retired source is probed (non-CDP ones on both lanes; CDP ones only on the full lane);
  2. it never gets a fail streak, a down alert, a degraded/refused entry or a last_status row;
  3. when its probe answers, it lands in ``retired_alive`` and the alive-again alert fires ONCE;
     a second run does not alert again;
  4. when its probe fails, it stays silent;
  5. a normal source is unaffected.

Harness as in test_health_watchdog_prune.py: a TEMP state file with a write tripwire, a stubbed
probe, captured alerts, a stubbed registry and ``retired_reason``. No network, no CDP, no launchctl.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import omniseek.server as server
from omniseek.core import fetcher, infra_jobs

ALIVE_TITLE = "已退役源复活"
RETIRED = {"retired_alive": "retired: stale overlay 2026-06-18",
           "retired_dead": "retired: upstream gone 2026-07-10"}
ANSWERS = {"normal": (True, "OK"),
           "retired_alive": (True, "OK (feed answers again)"),
           "retired_dead": (False, "HTTP 503 Service Unavailable")}


class RetiredSourcesThroughRunSourceHealth(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="omniseek-wd-retired-")
        self.tmpdir = Path(self._tmp.name).resolve()
        self.statefile = self.tmpdir / "health-watchdog-state.json"
        self.addCleanup(self._tmp.cleanup)
        self.alerts: list[tuple[str, str]] = []
        self.probed: list[str] = []

    def _guarded_save(self, real_save):
        def guarded(path, data):
            p = Path(path).resolve()
            if self.tmpdir not in p.parents:
                raise AssertionError(f"STATE ISOLATION BREACH: watchdog write escaped to {p}")
            return real_save(path, data)
        return guarded

    def _probe(self, adapter):
        self.probed.append(adapter.name)
        return ANSWERS[adapter.name]

    def _run(self, scope: str = "noncdp"):
        adapters = {n: SimpleNamespace(name=n, explicit_only=False) for n in ANSWERS}
        with mock.patch.object(infra_jobs, "_HEALTH_STATE", self.statefile), \
             mock.patch.object(infra_jobs, "_save_state",
                               self._guarded_save(infra_jobs._save_state)), \
             mock.patch.object(infra_jobs, "_health_probe", self._probe), \
             mock.patch.object(infra_jobs, "_heal_cdp_chrome", lambda: []), \
             mock.patch.object(infra_jobs, "_alert",
                               lambda title, body="", **kw: self.alerts.append((title, body))), \
             mock.patch.object(server, "load_sources", lambda: None), \
             mock.patch.object(fetcher, "all_adapter_names", lambda: sorted(adapters)), \
             mock.patch.object(fetcher, "get_adapter", adapters.get), \
             mock.patch.object(fetcher, "retired_reason",
                               lambda a: RETIRED.get(getattr(a, "name", ""), "")):
            summary = infra_jobs.run_source_health(scope=scope)
        saved = json.loads(self.statefile.read_text(encoding="utf-8"))
        return summary, saved

    def _alive_alerts(self):
        return [a for a in self.alerts if a[0].startswith(ALIVE_TITLE)]

    def test_retired_sources_are_probed(self):
        self._run()
        self.assertEqual(sorted(self.probed), sorted(ANSWERS),
                         "a retired source was skipped before probing, so its alive-again signal can never fire")

    def test_a_retired_source_that_answers_alerts_once_and_lands_in_retired_alive(self):
        _summary, saved = self._run()
        self.assertEqual(saved.get("retired_alive"), ["retired_alive"])
        alive = self._alive_alerts()
        self.assertEqual(len(alive), 1, self.alerts)
        self.assertIn("retired_alive", alive[0][1])
        self.assertNotIn("retired_dead", alive[0][1])
        self.alerts.clear()
        _summary, saved = self._run()
        self.assertEqual(self._alive_alerts(), [], "the alive-again alert fired again on the second run")
        self.assertEqual(saved.get("retired_alive"), ["retired_alive"])

    def test_retired_sources_get_no_fail_streak_no_status_and_no_alert(self):
        for _ in range(infra_jobs.N_CONSECUTIVE + 1):  # past the point a normal source would alert
            _summary, saved = self._run()
        for name in RETIRED:
            self.assertNotIn(name, saved.get("fails", {}), f"{name} got a fail streak")
            self.assertNotIn(name, saved.get("last_status", {}), f"{name} got a last_status row")
            self.assertNotIn(name, saved.get("unmeasured", {}))
            self.assertNotIn(f"down:{name}", saved.get("_alerts", {}))
            self.assertNotIn(name, saved.get("degraded", []))
            self.assertNotIn(name, saved.get("refused", []))
        self.assertIn("retired_dead", self.probed)
        dead_alerts = [a for a in self.alerts if "retired_dead" in a[0] + a[1]]
        self.assertEqual(dead_alerts, [], "a retired source whose probe fails must stay silent")
        self.assertNotIn("retired_dead", saved.get("retired_alive", []))

    def test_a_normal_source_is_unaffected(self):
        _summary, saved = self._run()
        self.assertIs(saved["last_status"].get("normal"), True)
        self.assertEqual(saved["fails"].get("normal"), 0)
        self.assertEqual([a for a in self.alerts if "normal" in a[1]], [])

    def test_the_full_lane_also_probes_retired_sources(self):
        with mock.patch.object(infra_jobs, "_CDP_INSTANCES", {}):
            _summary, saved = self._run(scope="all")
        self.assertEqual(sorted(self.probed), sorted(ANSWERS))
        self.assertEqual(saved.get("retired_alive"), ["retired_alive"])
        self.assertEqual(len(self._alive_alerts()), 1, self.alerts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
