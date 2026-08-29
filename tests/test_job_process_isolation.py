import os
import tempfile
import time
import unittest
from pathlib import Path

from omniseek.core import jobs


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
                outcome, _ = jobs._run_with_budget(row)
            finally:
                if previous is None:
                    os.environ.pop("OMNISEEK_TEST_MARKER", None)
                else:
                    os.environ["OMNISEEK_TEST_MARKER"] = previous

            self.assertEqual(outcome, "timeout")
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            pids = [int(value) for value in marker.read_text(encoding="ascii").split()]

            for pid in pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
