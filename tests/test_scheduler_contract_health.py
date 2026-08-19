import unittest
from types import SimpleNamespace
from unittest import mock

from omniseek.core import jobs


class SchedulerContractHealthTests(unittest.TestCase):
    def test_status_exposes_only_stable_contract_diagnostics(self):
        heartbeat = SimpleNamespace(
            build_id="a" * 40,
            generation="12345678-1234-4234-8234-123456789abc",
            tick_seq=3,
            last_emitted_at_utc="2026-08-04T20:00:03+00:00",
            artifacts=SimpleNamespace(
                schema_digest="b" * 64,
                policy_digest="c" * 64,
                mode="calibration-probe",
            ),
        )
        with mock.patch.object(jobs, "_scheduler_heartbeat", heartbeat):
            with mock.patch.object(jobs, "_scheduler_contract_error", None):
                status = jobs.scheduler_contract_status()

        self.assertEqual(
            status,
            {
                "state": "running",
                "error_class": None,
                "last_emitted_at_utc": "2026-08-04T20:00:03+00:00",
                "build_id": "a" * 40,
                "schema_digest": "b" * 64,
                "policy_digest": "c" * 64,
                "mode": "calibration-probe",
                "phase": "running",
                "scheduler_generation": "12345678-1234-4234-8234-123456789abc",
            },
        )

    def test_status_is_not_started_without_a_producer(self):
        with mock.patch.object(jobs, "_scheduler_heartbeat", None):
            status = jobs.scheduler_contract_status()

        self.assertEqual(status["state"], "not_started")
        self.assertIsNone(status["build_id"])
        self.assertNotIn("path", status)

    def test_successful_publication_clears_a_transient_contract_error(self):
        cases = (
            ("publish_running", jobs._publish_running_heartbeat),
            ("begin_tick", jobs._begin_heartbeat_tick),
        )
        for method_name, publish in cases:
            with self.subTest(method_name=method_name):
                heartbeat = mock.Mock()
                getattr(heartbeat, method_name).side_effect = [OSError("first write failed"), None]
                with mock.patch.object(jobs, "_scheduler_heartbeat", heartbeat):
                    with mock.patch.object(jobs, "_scheduler_contract_error", None):
                        publish()
                        self.assertEqual(jobs._scheduler_contract_error, "OSError")

                        publish()
                        self.assertIsNone(jobs._scheduler_contract_error)


if __name__ == "__main__":
    unittest.main()
