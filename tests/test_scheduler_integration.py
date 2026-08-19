import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest import mock

from omniseek.core import jobs
from omniseek.core.contracts.heartbeat import ContractArtifacts
from omniseek.core.recall import writer


class FakeHeartbeat:
    def __init__(self, events):
        self.events = events
        self.generation = "12345678-1234-4234-8234-123456789abc"

    def publish_starting(self):
        self.events.append("starting")
        return {}

    def begin_tick(self):
        self.events.append("begin_tick")
        return {}

    def publish_running(self):
        self.events.append("running")
        return {}


class FakeThread:
    events = None

    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        self.events.append("thread_start")


class SchedulerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_started = jobs._scheduler_started
        self.original_heartbeat = getattr(jobs, "_scheduler_heartbeat", None)
        self.original_registry = dict(jobs._REGISTRY)
        self.original_shipped = jobs._shipped_registered
        self.original_writes_enabled = writer.WRITES_ENABLED
        jobs._scheduler_started = False
        jobs._scheduler_heartbeat = None
        jobs._REGISTRY.clear()
        jobs._shipped_registered = True
        jobs._STOP.clear()

    def tearDown(self):
        jobs._scheduler_started = self.original_started
        jobs._scheduler_heartbeat = self.original_heartbeat
        jobs._REGISTRY.clear()
        jobs._REGISTRY.update(self.original_registry)
        jobs._shipped_registered = self.original_shipped
        writer.WRITES_ENABLED = self.original_writes_enabled
        jobs._STOP.clear()

    def test_starting_is_published_before_thread_start_and_double_start_is_inert(self):
        events = []
        heartbeat = FakeHeartbeat(events)
        FakeThread.events = events
        writer.WRITES_ENABLED = True

        with mock.patch.object(jobs, "_make_scheduler_heartbeat", return_value=heartbeat) as factory:
            with mock.patch.object(jobs.threading, "Thread", FakeThread):
                first = jobs.start_scheduler(interval_s=900, initial_delay_s=120)
                second = jobs.start_scheduler(interval_s=900, initial_delay_s=120)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(events, ["starting", "thread_start"])
        self.assertEqual(factory.call_count, 1)
        self.assertIs(jobs._scheduler_heartbeat, heartbeat)

    def test_boot_identity_comes_from_the_macos_boot_session(self):
        completed = subprocess.CompletedProcess(
            args=["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            returncode=0,
            stdout="BOOT-SESSION-1\n",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(jobs._read_host_boot_id(), "BOOT-SESSION-1")

        run.assert_called_once()

    def test_runtime_scheduler_values_must_equal_the_packaged_policy(self):
        artifacts = ContractArtifacts(
            schema_digest="b" * 64,
            policy_digest="c" * 64,
            mode="calibration-probe",
            initial_delay_s=120.0,
            tick_interval_s=900.0,
            max_job_budget_s=1500.0,
            startup_grace_s=None,
            stale_after_s=None,
        )
        with mock.patch.object(jobs, "load_contract_artifacts", return_value=artifacts):
            with self.assertRaisesRegex(ValueError, "tick interval"):
                jobs._make_scheduler_heartbeat(interval_s=901, initial_delay_s=120)

    def test_starting_publication_failure_does_not_mark_started_or_create_thread(self):
        writer.WRITES_ENABLED = True
        heartbeat = FakeHeartbeat([])
        heartbeat.publish_starting = mock.Mock(side_effect=OSError("write failed"))

        with mock.patch.object(jobs, "_make_scheduler_heartbeat", return_value=heartbeat):
            with mock.patch.object(jobs.threading, "Thread", FakeThread):
                with self.assertRaisesRegex(OSError, "write failed"):
                    jobs.start_scheduler(interval_s=900, initial_delay_s=120)

        self.assertFalse(jobs._scheduler_started)
        self.assertIsNone(jobs._scheduler_heartbeat)
        self.assertEqual(heartbeat.events, [])

    def test_tick_emits_at_entry_before_and_after_job_and_at_completion(self):
        with tempfile.TemporaryDirectory() as td:
            events = []
            jobs._scheduler_heartbeat = FakeHeartbeat(events)
            jobs.STATE_PATH = Path(td) / "scheduler-state.json"
            jobs.register_job(
                "one",
                "every:1s",
                lambda: events.append("job"),
                budget_s=1,
            )

            result = jobs.run_due_jobs(now=1000.0)

        self.assertEqual(result, {"checked": 1, "ran": ["one"], "failed": []})
        self.assertEqual(events, ["begin_tick", "running", "job", "running", "running"])


if __name__ == "__main__":
    unittest.main()
