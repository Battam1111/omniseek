"""Standing-query sensors: register a query, run it periodically, detect new results.

Sensors are background cache warmers with novelty detection. Each sensor:
1. Runs search_ranked for its registered query
2. Fingerprints each result as (source, source_id)
3. Diffs against its baseline to detect new information
4. Updates the baseline and records stats

The cron job (scripts/sensor_runner.py, initially disabled) runs sensors on schedule.
The MCP tool eye_sensor_run triggers one sensor immediately for testing.
The razor: the agent registers what to monitor (judgment); the diff is mechanical.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path.home() / ".polaris" / "state" / "sensors.json"


@dataclass
class Sensor:
    id: str
    query: str
    sources: Optional[list[str]] = None
    schedule: str = "daily"
    baseline: list[list[str]] = field(default_factory=list)  # [[source, source_id], ...]
    created_at: str = ""
    last_run_at: Optional[str] = None
    last_new_count: int = 0
    total_runs: int = 0


class SensorStore:
    """Thread-safe CRUD on the sensors JSON file (atomic write via rename)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _DEFAULT_STATE_PATH
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Sensor]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {s["id"]: Sensor(**s) for s in raw}
        except Exception as exc:
            log.warning("sensors.json unreadable (%s) -> empty", exc)
            return {}

    def _save(self, sensors: dict[str, Sensor]) -> None:
        self._ensure_dir()
        data = json.dumps([asdict(s) for s in sensors.values()],
                          ensure_ascii=False, indent=1)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(self.path)

    def list_all(self) -> list[dict]:
        return [asdict(s) for s in self._load().values()]

    def get(self, sensor_id: str) -> Optional[Sensor]:
        return self._load().get(sensor_id)

    def create(self, query: str, sources: Optional[list[str]] = None,
               schedule: str = "daily") -> Sensor:
        import hashlib
        sensors = self._load()
        sid = "sensor_" + hashlib.sha256(
            f"{query}:{time.time()}".encode()).hexdigest()[:12]
        from datetime import datetime, timezone
        s = Sensor(id=sid, query=query, sources=sources, schedule=schedule,
                   created_at=datetime.now(timezone.utc).isoformat())
        sensors[sid] = s
        self._save(sensors)
        return s

    def delete(self, sensor_id: str) -> bool:
        sensors = self._load()
        if sensor_id not in sensors:
            return False
        del sensors[sensor_id]
        self._save(sensors)
        return True

    def update(self, sensor: Sensor) -> None:
        sensors = self._load()
        sensors[sensor.id] = sensor
        self._save(sensors)


def run_sensor(sensor: Sensor, store: SensorStore, limit: int = 15) -> dict:
    """Execute one sensor: search -> diff baseline -> update. Returns a summary dict."""
    from penumbra.core import fetcher
    from datetime import datetime, timezone

    ranked, _meta = fetcher.search_ranked(
        sensor.query, sources=sensor.sources, limit=limit)

    current_keys = set()
    for doc in ranked:
        current_keys.add((doc.source, doc.source_id))

    baseline_set = {tuple(b) for b in sensor.baseline}
    new_keys = current_keys - baseline_set
    new_docs = [d for d in ranked if (d.source, d.source_id) in new_keys]

    sensor.baseline = [list(k) for k in (baseline_set | current_keys)]
    sensor.last_run_at = datetime.now(timezone.utc).isoformat()
    sensor.last_new_count = len(new_keys)
    sensor.total_runs += 1
    store.update(sensor)

    return {
        "sensor_id": sensor.id,
        "query": sensor.query,
        "total_results": len(ranked),
        "new_count": len(new_keys),
        "new_titles": [d.title for d in new_docs[:5]],
        "baseline_size": len(sensor.baseline),
        "run_at": sensor.last_run_at,
    }


def compute_diff(results: list, baseline: list[list[str]]) -> list:
    """Pure diff function for testing: returns (source, source_id) tuples not in baseline."""
    baseline_set = {tuple(b) for b in baseline}
    return [(r.source, r.source_id) for r in results
            if (r.source, r.source_id) not in baseline_set]
