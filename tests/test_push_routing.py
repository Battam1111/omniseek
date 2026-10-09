"""Push routing at the send points (2026-10-11).

The push outlet routes by source, and a push carrying questions goes to the operator whatever its source
routes to. Held here, at the send points (no push leaves the machine; every push is faked):
  - a degraded session the warmer looked at alerts WITH the question asking for a re-login;
  - the weekly report goes out under its own source, eye.weekly;
  - a sensor's push source: eye.sensor by default (unchanged call), else the name the
    side file sensor_notify_sources.json maps its id to; sensors.json keeps the exact format the
    4b4ce20 build reads (held against a frozen copy of that build's model), a missing or damaged
    side file falls back to the default with a warning, and omniseek_sensor create / update / list /
    delete carry notify_source.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import asdict, dataclass, field, fields
from typing import Optional
from pathlib import Path
from unittest import mock

from omniseek.core import infra_jobs, notify
from omniseek.core import sensor as sensor_mod


class WarmerQuestionTests(unittest.TestCase):
    def test_a_degraded_session_asks_for_a_relogin(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        sent: list = []

        class _Session:
            def __enter__(self):
                return object()

            def __exit__(self, *exc):
                return False

        driver = types.ModuleType("patchright.sync_api")
        driver.sync_playwright = lambda: _Session()
        package = types.ModuleType("patchright")
        package.sync_api = driver
        server = types.ModuleType("omniseek.server")
        server.load_sources = lambda: None
        bad = {"label": "论坛甲", "ok": False, "notes": 0, "acw_tc": None, "reason": "logged out",
               "self_heals": False}
        with mock.patch.dict(sys.modules, {"patchright": package, "patchright.sync_api": driver,
                                           "omniseek.server": server}), \
                mock.patch.dict(os.environ, {"WARMER_FORCE": "1", "WARMER_ONLY": ""}), \
                mock.patch.object(infra_jobs, "_WARMER_STATE", tmp / "warmer.json"), \
                mock.patch.object(infra_jobs, "_MAINT_FLAG", tmp / "cdp-maintenance"), \
                mock.patch.object(infra_jobs, "_WARMER_INSTANCES", {}), \
                mock.patch.object(infra_jobs, "_FORUM_WARMERS", [("论坛甲", "forum_a", "q")]), \
                mock.patch.object(infra_jobs, "_warm_forum_one", lambda *a: dict(bad)), \
                mock.patch.object(infra_jobs, "_jsleep", lambda lo, hi: None), \
                mock.patch.object(infra_jobs, "_alert",
                                  lambda title, body, **kw: sent.append((title, kw))):
            infra_jobs.run_session_warmer()
        self.assertEqual(len(sent), 1)
        title, kw = sent[0]
        self.assertEqual(title, "论坛甲 session 退化")
        self.assertEqual(kw["questions"], ["请 VNC 进主机，在 论坛甲 的 Chrome 窗口重新扫码登录该账号"])


class WeeklySourceTests(unittest.TestCase):
    def test_the_weekly_report_has_its_own_source(self):
        from omniseek.core import briefing, fetcher
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        pushed: list = []
        with mock.patch.object(infra_jobs, "_DIGEST_DIR", tmp), \
                mock.patch.object(infra_jobs, "_load_digest_themes",
                                  lambda: [{"label": "T", "query": "q", "sources": None}]), \
                mock.patch.object(briefing, "build_briefing", lambda themes: None), \
                mock.patch.object(fetcher, "search_ranked", lambda *a, **k: ([], {})), \
                mock.patch.object(notify, "wecom_push",
                                  lambda title, body, **kw: pushed.append((title, kw)) or True):
            infra_jobs.run_digest()
        self.assertEqual(len(pushed), 1)
        self.assertTrue(pushed[0][0].startswith("OmniSeek 周报"))
        self.assertEqual(pushed[0][1], {"source": "eye.weekly"})


# ── the 4b4ce20 build's sensor model, frozen verbatim (Sensor + SensorStore._load / _save) ─────────
# The rollback target: if OmniSeek is rolled back to that build, this is the code that reads the
# sensors.json the new build wrote. It loads with Sensor(**row): one unknown key and the whole file
# reads as empty (the next save then wipes every sensor). Do not edit to follow the live model.
@dataclass
class _Sensor4b4ce20:
    id: str
    query: str
    sources: Optional[list[str]] = None
    schedule: str = "daily"
    notify: bool = False
    notify_if: Optional[list[str]] = None
    notify_if_match: str = "any"
    detect_absence: bool = False
    gone_since: dict = field(default_factory=dict)
    baseline: list[list[str]] = field(default_factory=list)
    created_at: str = ""
    last_run_at: Optional[str] = None
    last_new_count: int = 0
    total_runs: int = 0


def _load_4b4ce20(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {s["id"]: _Sensor4b4ce20(**s) for s in raw}
    except Exception:
        return {}


def _dump_4b4ce20(sensors: dict) -> str:
    return json.dumps([asdict(s) for s in sensors.values()], ensure_ascii=False, indent=1)


class SensorNotifySourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.path = self.tmp / "sensors.json"
        self.store = sensor_mod.SensorStore(self.path)
        self.side = self.tmp / "sensor_notify_sources.json"
        sensor_mod._WARNED_MISSING.clear()
        self.addCleanup(sensor_mod._WARNED_MISSING.clear)

    def _two_sensors(self):
        a = self.store.create("q-a", notify=True, notify_if=["新加坡"])
        b = self.store.create("q-b", sources=["arxiv"], notify=True, detect_absence=True)
        b.baseline = [["arxiv", "1"], ["arxiv", "2"]]
        b.gone_since = {"arxiv:3": "2026-10-01T00:00:00+00:00"}
        b.last_run_at, b.total_runs, b.last_new_count = "2026-10-08T00:00:00+00:00", 7, 2
        self.store.update(b)
        return a, b

    # ── rollback safety: sensors.json stays what the 4b4ce20 build reads ──
    def test_the_model_fields_are_the_4b4ce20_fields(self):
        self.assertEqual([f.name for f in fields(sensor_mod.Sensor)],
                         [f.name for f in fields(_Sensor4b4ce20)])

    def test_setting_sources_leaves_sensors_json_byte_identical_for_the_old_loader(self):
        a, b = self._two_sensors()
        text_before = self.path.read_text(encoding="utf-8")
        old_before = _load_4b4ce20(self.path)
        self.assertEqual(len(old_before), 2)
        self.assertEqual(self.store.set_notify_source(b.id, "eye.sensor.opening"), "eye.sensor.opening")
        self.assertEqual(self.store.set_notify_source(a.id, "eye.sensor.other"), "eye.sensor.other")
        self.assertEqual(self.store.set_notify_source(a.id, ""), "eye.sensor")
        self.assertEqual(self.path.read_text(encoding="utf-8"), text_before)
        self.assertEqual(_load_4b4ce20(self.path), old_before)
        self.assertEqual(json.loads(self.side.read_text(encoding="utf-8")),
                         {b.id: "eye.sensor.opening"})

    def test_every_file_the_new_build_writes_is_read_whole_by_the_old_loader(self):
        a, b = self._two_sensors()
        self.store.set_notify_source(b.id, "eye.sensor.opening")
        c = self.store.create("q-c", notify=True, notify_source="eye.sensor.opening")
        c.total_runs = 3
        self.store.update(c)                     # a scheduler-style save after the source is set
        self.store.delete(a.id)
        old = _load_4b4ce20(self.path)
        self.assertEqual(sorted(old), sorted([b.id, c.id]))
        for sid, s in old.items():                # same values the new build holds, field for field
            self.assertEqual(asdict(s), asdict(self.store.get(sid)))
        # and the new build writes exactly what the old build would write for the same sensors
        self.assertEqual(self.path.read_text(encoding="utf-8"), _dump_4b4ce20(old))
        self.assertNotIn("source", self.path.read_text(encoding="utf-8").replace('"sources"', ""))

    def test_a_file_the_old_build_wrote_reads_the_same_in_the_new_build(self):
        old = {"sensor_x": _Sensor4b4ce20(id="sensor_x", query="q", notify=True,
                                          baseline=[["arxiv", "9"]], total_runs=4)}
        self.path.write_text(_dump_4b4ce20(old), encoding="utf-8")
        self.assertEqual(asdict(self.store.get("sensor_x")), asdict(old["sensor_x"]))
        self.assertEqual(self.store.notify_source("sensor_x"), "eye.sensor")

    # ── the side file: fallback and warnings ──
    def test_a_missing_side_file_falls_back_to_the_default_with_a_warning(self):
        a, _ = self._two_sensors()
        self.assertFalse(self.side.exists())
        with self.assertLogs(sensor_mod.log, "WARNING") as logs:
            self.assertEqual(self.store.notify_source(a.id), "eye.sensor")
        self.assertIn("missing", logs.output[0])

    def test_a_damaged_side_file_falls_back_to_the_default_with_a_warning(self):
        a, b = self._two_sensors()
        for damaged in ("{not json", "[1, 2]", json.dumps({a.id: 5, b.id: "eye.infra_jobs"})):
            self.side.write_text(damaged, encoding="utf-8")
            with self.assertLogs(sensor_mod.log, "WARNING") as logs:
                self.assertEqual(self.store.notify_source(b.id), "eye.sensor")
            self.assertTrue(logs.output)
            with self.assertLogs(sensor_mod.log, "WARNING"):
                self.assertEqual(self.store.notify_sources(), {})

    def test_good_entries_survive_a_bad_neighbour(self):
        a, b = self._two_sensors()
        self.side.write_text(json.dumps({a.id: 5, b.id: "eye.sensor.opening"}), encoding="utf-8")
        with self.assertLogs(sensor_mod.log, "WARNING"):
            self.assertEqual(self.store.notify_source(b.id), "eye.sensor.opening")

    def test_a_damaged_side_file_is_never_overwritten(self):
        a, b = self._two_sensors()
        self.side.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.set_notify_source(a.id, "eye.sensor.opening")
        with self.assertRaises(ValueError):
            self.store.create("q-c", notify_source="eye.sensor.opening")
        self.assertEqual(len(self.store.list_all()), 2)          # the refused create made nothing
        with self.assertLogs(sensor_mod.log, "WARNING"):
            self.assertTrue(self.store.delete(a.id))              # delete still works, just warns
        self.assertEqual(self.side.read_text(encoding="utf-8"), "{not json")

    def test_delete_drops_the_entry(self):
        a, b = self._two_sensors()
        self.store.set_notify_source(a.id, "eye.sensor.opening")
        self.store.set_notify_source(b.id, "eye.sensor.opening")
        self.store.delete(a.id)
        self.assertEqual(json.loads(self.side.read_text(encoding="utf-8")), {b.id: "eye.sensor.opening"})

    def test_names_are_checked(self):
        a, _ = self._two_sensors()
        for bad in ("eye.infra_jobs", "eye.sensor.", "eye.sensor.Opening", "eye.sensor opening", "x"):
            with self.assertRaises(ValueError, msg=bad):
                self.store.set_notify_source(a.id, bad)
        self.assertEqual(self.store.set_notify_source(a.id, "eye.sensor"), "eye.sensor")
        self.assertFalse(self.side.exists())                      # the default writes nothing
        self.assertIsNone(self.store.set_notify_source("sensor_missing", "eye.sensor.opening"))

    # ── the push ──
    def test_the_push_source_follows_the_side_file(self):
        calls: list = []
        with mock.patch.object(notify, "alert",
                               lambda title, body, **kw: calls.append((title, kw)) or ["wecom"]):
            s = self.store.create("plain", notify=True)
            sensor_mod._bark_new_results(s, {"new_count": 1, "new_titles": ["A"]}, self.store)
            self.store.set_notify_source(s.id, "eye.sensor.opening")
            sensor_mod._bark_new_results(s, {"new_count": 1, "new_titles": ["A"]}, self.store)
            self.side.write_text("{not json", encoding="utf-8")
            with self.assertLogs(sensor_mod.log, "WARNING"):
                sensor_mod._bark_new_results(s, {"new_count": 1, "new_titles": ["A"]}, self.store)
        self.assertEqual(calls, [("plain", {}), ("plain", {"source": "eye.sensor.opening"}),
                                 ("plain", {})])

    def test_the_scheduler_pushes_under_the_stores_side_file(self):
        s = self.store.create("tick", notify=True, notify_source="eye.sensor.opening")
        calls: list = []
        with mock.patch.object(sensor_mod, "run_sensor",
                               lambda sen, store, **k: {"new_count": 1, "new_titles": ["A"]}), \
                mock.patch.object(notify, "alert",
                                  lambda title, body, **kw: calls.append((title, kw)) or ["wecom"]):
            out = sensor_mod.scheduler_tick(self.store)
        self.assertEqual(out["ran"], [s.id])
        self.assertEqual(calls, [("tick", {"source": "eye.sensor.opening"})])

    def test_an_unknown_stored_key_does_not_empty_the_store(self):
        self.store.create("q")
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        rows[0]["a_field_from_a_newer_build"] = 1
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        self.assertEqual(len(self.store.list_all()), 1)

    # ── the tool ──
    def test_eye_sensor_create_update_list_delete(self):
        from omniseek.server import omniseek_sensor
        tool = getattr(omniseek_sensor, "__wrapped__", omniseek_sensor)
        with mock.patch.object(sensor_mod, "_DEFAULT_STATE_PATH", self.path):
            made = tool(action="create", query="q", notify=True)
            sid = made["sensor"]["id"]
            self.assertEqual(made["sensor"]["notify_source"], "eye.sensor")
            self.assertEqual(tool(action="list")["sensors"][0]["notify_source"], "eye.sensor")
            out = tool(action="update", sensor_id=sid, notify_source="eye.sensor.opening")
            self.assertEqual((out["updated"], out["sensor"]["notify_source"]),
                             (True, "eye.sensor.opening"))
            self.assertEqual(tool(action="list")["sensors"][0]["notify_source"], "eye.sensor.opening")
            self.assertEqual(tool(action="update", sensor_id=sid, notify_source="")["sensor"]["notify_source"],
                             "eye.sensor")
            self.assertIn("error", tool(action="update", sensor_id=sid))
            self.assertIn("error", tool(action="update", notify_source="eye.sensor.opening"))
            self.assertIn("error", tool(action="update", sensor_id="sensor_missing",
                                        notify_source="eye.sensor.opening"))
            self.assertIn("error", tool(action="update", sensor_id=sid, notify_source="eye.weekly"))
            self.assertIn("error", tool(action="create", query="q2", notify_source="Eye.Sensor.X"))
            made2 = tool(action="create", query="q2", notify=True, notify_source="eye.sensor.opening")
            sid2 = made2["sensor"]["id"]
            self.assertEqual(made2["sensor"]["notify_source"], "eye.sensor.opening")
            rows = {r["id"]: r["notify_source"] for r in tool(action="list")["sensors"]}
            self.assertEqual(rows, {sid: "eye.sensor", sid2: "eye.sensor.opening"})
            self.assertTrue(tool(action="delete", sensor_id=sid2)["deleted"])
            self.assertEqual(json.loads(self.side.read_text(encoding="utf-8")), {})
        self.assertEqual(len(_load_4b4ce20(self.path)), 1)


if __name__ == "__main__":
    unittest.main()
