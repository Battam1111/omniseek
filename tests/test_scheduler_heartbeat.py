import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from omniseek.core.contracts.heartbeat import (
    SchedulerHeartbeat,
    load_contract_artifacts,
)


GENERATION = "12345678-1234-4234-8234-123456789abc"


class Clock:
    def __init__(self):
        self.utc_values = iter(
            [
                "2026-08-04T20:00:00+00:00",
                "2026-08-04T20:00:01+00:00",
                "2026-08-04T20:00:02+00:00",
                "2026-08-04T20:00:03+00:00",
                "2026-08-04T20:00:04+00:00",
            ]
        )
        self.monotonic_values = iter([100, 200, 300, 400, 500])

    def utc_now(self):
        return next(self.utc_values)

    def monotonic_ns(self):
        return next(self.monotonic_values)


class SchedulerHeartbeatTests(unittest.TestCase):
    def make_heartbeat(self, path: Path, clock: Clock) -> SchedulerHeartbeat:
        return SchedulerHeartbeat(
            path=path,
            artifacts=load_contract_artifacts(),
            build_id="a" * 40,
            host_boot_id="boot-session-1",
            omniseek_pid=123,
            generation=GENERATION,
            utc_now=clock.utc_now,
            monotonic_ns=clock.monotonic_ns,
        )

    def test_starting_record_is_complete_probe_mode_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler-heartbeat"
            heartbeat = self.make_heartbeat(path, Clock())

            record = heartbeat.publish_starting()

            self.assertEqual(record["schema"], "omniseek.core-scheduler-heartbeat/v1")
            self.assertEqual(record["component_id"], "eye-scheduler")
            self.assertEqual(record["producer"], {"repo": "omniseek", "build_id": "a" * 40})
            self.assertEqual(record["host_boot_id"], "boot-session-1")
            self.assertEqual(record["omniseek_pid"], 123)
            self.assertEqual(record["scheduler_generation"], GENERATION)
            self.assertEqual(record["phase"], "starting")
            self.assertEqual(record["tick_seq"], 0)
            self.assertIsNone(record["last_tick_at_utc"])
            self.assertIsNone(record["last_tick_monotonic_ns"])
            self.assertEqual(record["scheduler_started_at_utc"], "2026-08-04T20:00:00+00:00")
            self.assertEqual(record["scheduler_started_monotonic_ns"], 100)
            self.assertEqual(record["emitted_at_utc"], "2026-08-04T20:00:01+00:00")
            self.assertEqual(record["emitted_monotonic_ns"], 200)
            self.assertEqual(record["contract"]["mode"], "calibration-probe")
            self.assertIsNone(record["startup_grace_s"])
            self.assertIsNone(record["stale_after_s"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)

    def test_begin_tick_increments_once_and_later_emission_retains_tick_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler-heartbeat"
            heartbeat = self.make_heartbeat(path, Clock())

            first = heartbeat.begin_tick()
            later = heartbeat.publish_running()

            self.assertEqual(first["phase"], "running")
            self.assertEqual(first["tick_seq"], 1)
            self.assertEqual(first["last_tick_at_utc"], "2026-08-04T20:00:01+00:00")
            self.assertEqual(first["last_tick_monotonic_ns"], 200)
            self.assertEqual(later["tick_seq"], 1)
            self.assertEqual(later["last_tick_at_utc"], first["last_tick_at_utc"])
            self.assertEqual(later["last_tick_monotonic_ns"], first["last_tick_monotonic_ns"])

    def test_publish_running_requires_a_started_tick(self):
        with tempfile.TemporaryDirectory() as td:
            heartbeat = self.make_heartbeat(Path(td) / "scheduler-heartbeat", Clock())

            with self.assertRaisesRegex(RuntimeError, "begin_tick"):
                heartbeat.publish_running()

    @unittest.skipIf(os.name == "nt", "concurrent replace is a macOS target-platform gate")
    def test_atomic_replacement_never_exposes_partial_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scheduler-heartbeat"
            clock = Clock()
            heartbeat = self.make_heartbeat(path, clock)
            heartbeat.publish_starting()
            errors = []
            stop = threading.Event()

            def read_loop():
                while not stop.is_set():
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if payload["scheduler_generation"] != GENERATION:
                            errors.append("wrong generation")
                    except Exception as exc:
                        errors.append(type(exc).__name__)

            reader = threading.Thread(target=read_loop)
            reader.start()
            try:
                heartbeat.begin_tick()
                heartbeat.publish_running()
            finally:
                stop.set()
                reader.join()

            self.assertEqual(errors, [])
            self.assertEqual(list(path.parent.glob(path.name + ".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
