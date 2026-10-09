import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omniseek.core import jobs
from tests.isolated_job_fixture import CHILD_LIFETIME_S

# Both waits below end as soon as their condition holds, so their size only matters on the failure
# path. Each is 3x the largest value measured with this test's own fixture, rounded up to a whole
# second (mini, 2026-10-07, 60 runs per condition: no extra load, 8 busy processes, and 8 busy plus
# 8 loops spawning orphans at 1-minute load 21 to 34). A larger value only slows a real failure;
# a smaller one risks a false failure on a busier machine.

# How long the test waits for the fixture to report its pids before letting the budget run anyway
# (the test then fails on the missing marker). Spawn to marker written: max 0.125 s with no extra
# load, 0.205 s with 8 busy processes, 1.79 s at load 21 to 34; 3 x 1.79 s rounds up to 6 s.
_STARTUP_HANG_GUARD_S = 6.0

# How long after the timeout returns the test waits for each pid of the group to disappear. Group
# kill sent to pid gone (kill(pid, 0) raises ESRCH), job and child alike: max 0.014 s, 0.019 s and
# 0.015 s in the three conditions, so 3 x 0.019 s rounds up to 1 s. It must also end before a child
# the kill missed would exit by itself (CHILD_LIFETIME_S after spawn), or a survivor would pass for
# dead: the test asserts that below.
_VANISH_WAIT_S = 1.0


def _marker_written(marker: Path) -> bool:
    try:
        return marker.read_text(encoding="ascii").endswith("\n")
    except FileNotFoundError:
        return False


def _budget_from_readiness(marker: Path, budget_s: float, gated: list):
    """A Popen whose budgeted wait starts once the job has written its pids, not at spawn.

    The test needs the budget to run out while the job and its child are both up. Counting the budget
    from spawn assumed start-up (a Python launch, the imports, a second Python launch for the child)
    always fits in it; under load it does not, and the group was killed before the child existed.
    The budgeted wait itself, the timeout and the group kill are the production code, unchanged.

    This relies on production budgeting the job with ``Popen.wait(timeout=budget_s)``. Every time the
    readiness wait runs it appends to ``gated``, so the test can assert it did: if production changes
    how it waits, the test fails saying so instead of quietly counting from spawn again."""
    class ReadyPopen(subprocess.Popen):
        def wait(self, timeout=None):
            if timeout == budget_s and "omniseek.core.job_runner" in self.args:
                gated.append(timeout)
                guard = time.monotonic() + _STARTUP_HANG_GUARD_S
                while (not _marker_written(marker) and self.poll() is None
                       and time.monotonic() < guard):
                    time.sleep(0.01)
            return super().wait(timeout=timeout)
    return mock.patch.object(jobs.subprocess, "Popen", ReadyPopen)


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class JobProcessIsolationTests(unittest.TestCase):
    def setUp(self):
        self.original_registry = dict(jobs._REGISTRY)
        jobs._REGISTRY.clear()

    def tearDown(self):
        jobs._REGISTRY.clear()
        jobs._REGISTRY.update(self.original_registry)

    def test_process_entrypoint_is_retained_on_the_job_row(self):
        row = jobs.register_job(
            "isolated-config",
            "every:1s",
            lambda: None,
            process_entrypoint=("tests.isolated_job_fixture", "run_forever_with_child"),
        )

        self.assertEqual(
            row.process_entrypoint,
            ("tests.isolated_job_fixture", "run_forever_with_child"),
        )

    @unittest.skipIf(os.name == "nt", "the production target is POSIX process-group isolation")
    def test_isolated_job_timeout_kills_the_entire_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "pids"
            previous = os.environ.get("OMNISEEK_TEST_MARKER")
            os.environ["OMNISEEK_TEST_MARKER"] = str(marker)
            try:
                row = jobs.register_job(
                    "isolated-timeout",
                    "every:1s",
                    lambda: None,
                    budget_s=1,
                    process_entrypoint=(
                        "tests.isolated_job_fixture",
                        "run_forever_with_child",
                    ),
                )
                gated = []
                with _budget_from_readiness(marker, row.budget_s, gated):
                    outcome, _ = jobs._run_with_budget(row)
            finally:
                if previous is None:
                    os.environ.pop("OMNISEEK_TEST_MARKER", None)
                else:
                    os.environ["OMNISEEK_TEST_MARKER"] = previous

            self.assertTrue(gated, "production no longer budgets the job with Popen.wait(timeout=budget);"
                            " the readiness wait never ran, so the budget counted from spawn")
            self.assertEqual(outcome, "timeout")
            self.assertTrue(_marker_written(marker), "the job never reported its pids")
            pids = [int(value) for value in marker.read_text(encoding="ascii").split()]

            # The group kill only SENDS the signal; _run_with_budget then reaps the job process alone.
            # The child acts on the signal when it next gets a CPU and, orphaned, is reaped by launchd
            # (init) when that gets one: both can be later than the return. So wait for each pid to
            # disappear, for at most _VANISH_WAIT_S. That must end before a child the kill missed
            # would exit by itself: from its spawn, at most the start-up guard passes before the
            # budget starts, then the budget, the kill and reap (measured under 0.02 s), this wait.
            self.assertLess(_STARTUP_HANG_GUARD_S + row.budget_s + _VANISH_WAIT_S, CHILD_LIFETIME_S)
            deadline = time.monotonic() + _VANISH_WAIT_S
            for pid in pids:
                while _running(pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual([pid for pid in pids if _running(pid)], [],
                             "the timeout left part of the job's process group running")


if __name__ == "__main__":
    unittest.main()
