"""Contract: the source-health watchdog prunes rows for sources that are GONE, and only those.

The leak this pins: a source's row enters health-watchdog-state.json the first run it is probed
(``fails`` / ``last_status`` / a ``down:<name>`` cooldown in ``_alerts``) and nothing ever removed
it when the adapter was deleted from the tree. The retire path only self-cleans sources that are
still REGISTERED, so a genuinely-gone source kept its counter forever (twitter_x was cleared by
hand). Three properties matter more than the pruning itself, and each has a test below:

  1. the infra pseudo-rows survive. ``_health_track`` writes a ``_cdp:<label>`` row per CDP-Chrome
     instance into the same flat dict as the sources, so the state file mixes two namespaces;
  2. a source that is merely DISABLED by the deployer's profile survives. It is still a registered
     adapter and run_source_health still probes it (all_adapter_names() has no profile filter), so
     pruning by enablement would delete a counter the same run re-creates. Likewise a CDP source
     during a 6h fast-lane run: registered, deliberately not probed, must keep its row;
  3. the prune is VISIBLE -- logged AND pushed. The scheduler discards a job's return value
     (jobs._run_with_budget calls row.fn() and throws the result away), so a summary key alone
     would reach nobody, and a silent prune is how state disappears without anyone learning.

STATE ISOLATION IS THE POINT OF THE HARNESS, not a detail: tests in this repo have written into
~/.omniseek/state and poisoned the evidence they measure. The integration case repoints
``_HEALTH_STATE`` into a TemporaryDirectory AND wraps ``_save_state`` in a tripwire that raises if
any write lands outside it. Pure/offline throughout: no network, no CDP, no launchctl, no clock.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import omniseek.server as server
from omniseek.core import fetcher, infra_jobs

PROD_STATE = Path.home() / ".omniseek" / "state"

# The non-source rows the live state carried when this guard was written (2026-08-12).
# _cdp:9222-shared and _cdp:9223-xhs are written every full run by _health_track; _cdp_infra is a
# FOSSIL no current code path writes or reads. All three must SURVIVE a prune, because the rule is
# the reserved "_" namespace and not a literal list -- an unknown infra row fails safe (kept).
INFRA_ROWS = ("_cdp:9222-shared", "_cdp:9223-xhs", "_cdp_infra")


def seed_state() -> dict:
    """A watchdog state shaped like the real one: sources + infra rows in one flat dict."""
    return {
        "fails": {
            "arxiv": 0,
            "zhihu": 1,                 # a _CDP_SOURCES member: registered, not probed by the fast lane
            "walled_but_registered": 3,  # registered, disabled by the deployer's profile
            "gone_source": 7,            # the adapter no longer exists in the tree
            "_cdp:9222-shared": 0,
            "_cdp:9223-xhs": 0,
            "_cdp_infra": 0,
        },
        "last_status": {
            "arxiv": True,
            "zhihu": True,
            "walled_but_registered": False,
            "gone_source": False,
            "_cdp:9222-shared": True,
            "_cdp:9223-xhs": True,
        },
        "_alerts": {
            "down:gone_source": 1.0,
            "down:zhihu": 2.0,
            "down:walled_but_registered": 3.0,
            "heal_fail:com.omniseek.cdp.xhs": 4.0,   # a SERVICE label, not a source -- must survive
        },
        "degraded": ["zhihu"],
        "last_run": "2026-08-12T05:00:00",
    }


REGISTERED = {"arxiv", "zhihu", "walled_but_registered"}


class PruneHelperContract(unittest.TestCase):
    """_prune_stale_health_rows is PURE: a dict in, a dict mutated, a report out. No IO at all."""

    def test_the_infra_allowlist_is_the_reserved_underscore_namespace(self):
        self.assertEqual(infra_jobs._INFRA_ROW_PREFIX, "_")
        for row in INFRA_ROWS:
            self.assertTrue(row.startswith(infra_jobs._INFRA_ROW_PREFIX), row)
        # The rows a run actually writes are DERIVED from _CDP_INSTANCES, never hand-listed, so
        # adding a CDP instance cannot leave its row outside the allowlist.
        self.assertEqual({f"_cdp:{label}" for label in infra_jobs._CDP_INSTANCES},
                         {"_cdp:9222-shared", "_cdp:9223-xhs"})

    def test_a_source_that_no_longer_exists_is_pruned_from_every_container(self):
        state = seed_state()
        report = infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        self.assertEqual(report["pruned"], ["gone_source"])
        self.assertNotIn("gone_source", state["fails"])
        self.assertNotIn("gone_source", state["last_status"])
        self.assertNotIn("down:gone_source", state["_alerts"])

    def test_infra_rows_survive_and_an_unwritten_one_is_reported_not_dropped(self):
        state = seed_state()
        report = infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        for row in INFRA_ROWS:
            self.assertNotIn(row, report["pruned"])
            self.assertIn(row, state["fails"])
        # _cdp_infra is in the reserved namespace but no current job writes it: KEPT, and named, so
        # a fossil row cannot sit in the state forever without anyone learning it is there.
        self.assertEqual(report["orphan_infra"], ["_cdp_infra"])

    def test_a_service_scoped_alert_key_is_not_mistaken_for_a_source(self):
        state = seed_state()
        infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        self.assertIn("heal_fail:com.omniseek.cdp.xhs", state["_alerts"])

    def test_registered_sources_survive_including_the_ones_no_run_probes(self):
        state = seed_state()
        infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        for name in REGISTERED:
            self.assertIn(name, state["fails"], f"{name} is registered and must keep its counter")

    def test_an_empty_registry_prunes_nothing(self):
        """An empty live set means the registry did not load, not that every source vanished."""
        state = seed_state()
        report = infra_jobs._prune_stale_health_rows(state, set())
        self.assertEqual(report["pruned"], [])
        self.assertEqual(report["skipped"], "empty-registry")
        self.assertEqual(set(state["fails"]), set(seed_state()["fails"]))

    def test_the_prune_is_idempotent(self):
        state = seed_state()
        first = infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        second = infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        self.assertEqual(first["pruned"], ["gone_source"])
        self.assertEqual(second["pruned"], [])

    def test_a_corrupt_container_degrades_instead_of_raising(self):
        state = {"fails": None, "last_status": ["not", "a", "dict"], "_alerts": {}}
        report = infra_jobs._prune_stale_health_rows(state, set(REGISTERED))
        self.assertEqual(report["pruned"], [])


class PruneThroughRunSourceHealth(unittest.TestCase):
    """End-to-end through run_source_health, against a TEMP state file that is never production."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="omniseek-wd-prune-")
        self.tmpdir = Path(self._tmp.name).resolve()
        self.statefile = self.tmpdir / "health-watchdog-state.json"
        self.statefile.write_text(json.dumps(seed_state()), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self.alerts: list[tuple[str, str]] = []

    def _guarded_save(self, real_save):
        def guarded(path, data):
            p = Path(path).resolve()
            if self.tmpdir not in p.parents:
                raise AssertionError(f"STATE ISOLATION BREACH: watchdog write escaped to {p}")
            return real_save(path, data)
        return guarded

    def _run(self, scope: str = "noncdp"):
        """A fully offline run: no source imports, no probes, no browser, no launchctl, no push.

        scope="noncdp" is the FAST lane and the harsh case for this contract: it neither probes the
        CDP sources nor refreshes the _cdp:* infra rows, and it MERGES the previous last_status
        instead of rebuilding it -- so every row that survives does so because the prune spared it,
        not because this run happened to rewrite it.
        """
        adapters = {n: SimpleNamespace(name=n, explicit_only=False) for n in REGISTERED}
        with mock.patch.object(infra_jobs, "_HEALTH_STATE", self.statefile), \
             mock.patch.object(infra_jobs, "_save_state",
                               self._guarded_save(infra_jobs._save_state)), \
             mock.patch.object(infra_jobs, "_health_probe", lambda a: (True, "OK")), \
             mock.patch.object(infra_jobs, "_heal_cdp_chrome", lambda: []), \
             mock.patch.object(infra_jobs, "_alert",
                               lambda title, body="", **kw: self.alerts.append((title, body))), \
             mock.patch.object(server, "load_sources", lambda: None), \
             mock.patch.object(fetcher, "all_adapter_names", lambda: sorted(adapters)), \
             mock.patch.object(fetcher, "get_adapter", adapters.get), \
             mock.patch.object(fetcher, "retired_reason", lambda a: ""), \
             mock.patch.object(fetcher, "is_enabled_by_profile",
                               lambda n: n != "walled_but_registered"):
            # Recorded UNDER the patch: this run really did see that source as profile-disabled,
            # so "it survived" below is a statement about the prune, not about the fixture.
            self.disabled_during_run = not fetcher.is_enabled_by_profile("walled_but_registered")
            summary = infra_jobs.run_source_health(scope=scope)
        saved = json.loads(self.statefile.read_text(encoding="utf-8"))
        return summary, saved

    def test_the_temp_state_file_is_not_production(self):
        self.assertNotEqual(self.statefile.parent, PROD_STATE)
        self.assertFalse(str(self.statefile).startswith(str(PROD_STATE)))

    def test_a_gone_source_is_pruned_from_the_persisted_state(self):
        _summary, saved = self._run()
        self.assertNotIn("gone_source", saved["fails"],
                         "a source that no longer exists kept its consecutive-fail counter")
        self.assertNotIn("gone_source", saved["last_status"])
        self.assertNotIn("down:gone_source", saved["_alerts"])

    def test_infra_rows_and_unprobed_registered_sources_survive_the_fast_lane(self):
        _summary, saved = self._run()
        for row in INFRA_ROWS:
            self.assertIn(row, saved["fails"], f"infra row {row} must never be pruned")
        self.assertTrue(saved["last_status"]["_cdp:9222-shared"])
        # zhihu is registered but is a _CDP_SOURCES member, so the fast lane never probes it.
        self.assertIn("zhihu", saved["fails"])
        self.assertIn("zhihu", saved["last_status"])

    def test_a_profile_disabled_source_survives(self):
        """walled_but_registered is registered and profile-DISABLED; the prune keys off the former."""
        _summary, saved = self._run()
        self.assertTrue(self.disabled_during_run,
                        "fixture broken: the source was not profile-disabled during the run")
        self.assertIn("walled_but_registered", saved["fails"])
        self.assertEqual(saved["fails"]["walled_but_registered"], 0)  # probed + refreshed, not dropped

    def test_the_prune_is_visible_not_silent(self):
        with self.assertLogs(infra_jobs.log, level="WARNING") as captured:
            summary, _saved = self._run()
        self.assertIn("gone_source", "\n".join(captured.output))
        self.assertEqual(summary["pruned"], ["gone_source"])
        pushed = [a for a in self.alerts if "gone_source" in a[1]]
        self.assertTrue(pushed, f"the prune pushed no alert naming the dropped row: {self.alerts}")

    def test_a_second_run_prunes_nothing_and_pushes_nothing(self):
        self._run()
        self.alerts.clear()
        summary, saved = self._run()
        self.assertEqual(summary["pruned"], [])
        self.assertEqual(self.alerts, [])
        for row in INFRA_ROWS:
            self.assertIn(row, saved["fails"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
