"""Without a resident-task table configured, the sensor tick keeps each sensor's own schedule.

A deployment may schedule sensors from a table of its resident tasks (sensor.sensor_table_path).
When none is configured (no default, no environment variable), the job entry must be the plain
scheduler tick: every stored sensor runs on its own ``schedule``, the table reader is never
imported, nothing about a table is pushed, and omniseek_sensor create adds no table note.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from omniseek.core import sensor as sensor_mod
from omniseek.core.sensor import Sensor, SensorStore


class NoTableConfigured(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / "sensors.json"
        self.pushes: list = []
        self.runs: list = []
        env = {k: v for k, v in os.environ.items() if k != sensor_mod.SENSOR_TABLE_ENV}
        patches = [
            mock.patch.object(sensor_mod, "_DEFAULT_SENSOR_TABLE", None),
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(sensor_mod, "_DEFAULT_STATE_PATH", self.state),
            mock.patch.object(sensor_mod, "_alert", lambda *a, **k: self.pushes.append(a)),
            mock.patch.object(sensor_mod, "run_sensor", self.fake_run),
            mock.patch.object(sensor_mod, "_services",
                              side_effect=AssertionError("the table reader must not be imported")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, s, store, limit=15):
        self.runs.append(s.id)
        s.last_run_at = datetime.now(timezone.utc).isoformat()
        store.update(s)
        return {"sensor_id": s.id, "new_count": 0, "new_titles": []}

    def add(self, sid, schedule, ran_hours_ago=None):
        last = None
        if ran_hours_ago is not None:
            last = (datetime.now(timezone.utc) - timedelta(hours=ran_hours_ago)).isoformat()
        SensorStore(self.state).update(Sensor(id=sid, query=f"q {sid}", schedule=schedule,
                                              created_at="2026-01-01T00:00:00+00:00",
                                              last_run_at=last))

    def test_path_is_none(self):
        self.assertIsNone(sensor_mod.sensor_table_path())

    def test_tick_runs_each_sensor_on_its_own_schedule(self):
        self.add("sensor_new", "daily")                       # never ran: due
        self.add("sensor_hourly", "hourly", ran_hours_ago=2)  # due
        self.add("sensor_daily", "daily", ran_hours_ago=2)    # not due yet
        self.add("sensor_weekly", "weekly", ran_hours_ago=200)  # due
        out = sensor_mod.scheduler_tick_for_sensors()
        self.assertEqual(set(out), {"checked", "ran", "failed"})
        self.assertEqual(sorted(out["ran"]), ["sensor_hourly", "sensor_new", "sensor_weekly"])
        self.assertEqual(out["failed"], [])
        self.assertEqual(self.pushes, [])

    def test_no_sensors_no_push(self):
        self.assertEqual(sensor_mod.scheduler_tick_for_sensors(),
                         {"checked": 0, "ran": [], "failed": []})
        self.assertEqual(self.pushes, [])

    def test_create_adds_no_table_note(self):
        s = Sensor(id="sensor_x", query="q", schedule="weekly", created_at="2026-01-01T00:00:00+00:00")
        self.assertIsNone(sensor_mod.table_row_hint(s))
        from omniseek.server import omniseek_sensor
        tool = getattr(omniseek_sensor, "__wrapped__", omniseek_sensor)
        made = tool(action="create", query="q", schedule="weekly")
        self.assertTrue(made["created"])
        self.assertNotIn("schedule_note", made)


if __name__ == "__main__":
    unittest.main()
