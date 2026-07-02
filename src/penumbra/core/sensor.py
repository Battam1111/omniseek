"""Standing-query sensors: register a query, run it periodically, detect new results.

Sensors are background cache warmers with novelty detection. Each sensor:
1. Runs search_ranked for its registered query
2. Fingerprints each result as (source, source_id)
3. Diffs against its baseline to detect new information
4. Updates the baseline and records stats

The cron job (scripts/sensor_runner.py, initially disabled) runs sensors on schedule.
The MCP tool penumbra_sensor_run triggers one sensor immediately for testing.
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

_DEFAULT_STATE_PATH = Path.home() / ".penumbra" / "state" / "sensors.json"

from penumbra.core.recall import graph  # noqa: E402 (the graph write verb + mint registry)

# Vocabulary this tap MINTS (vocabulary-by-minting, design section 3): declared on the tap itself,
# registered at import, folded into ``graph.declared_vocabulary`` as the computed union; the smoke
# tripwire bounds ACTUAL graph data to that union. The sensor tap is the P4 event layer: it mints
# ONE sensor node + observed edges (sensor -> doc) for the results THIS RUN detected as new. THE
# MINT RULE (design "Mint the product"): a sensor mints the RUN DIFF, not the baseline: the
# baseline is state (it mints nothing), the diff is the product (the mint-the-product rule applied
# to novelty detection). A no-news run mints nothing at all (not even the sensor node): a run that
# surfaced nothing new is not an accretion event. The observed method is sensor:diff.
GRAPH_MINTS = {
    "kinds": ["sensor"],
    "edge_types": ["observed"],
    "methods": ["sensor:diff"],
}
graph.register_mints("sensor", kinds=GRAPH_MINTS["kinds"],
                     edge_types=GRAPH_MINTS["edge_types"], methods=GRAPH_MINTS["methods"])


@dataclass
class Sensor:
    id: str
    query: str
    sources: Optional[list[str]] = None
    schedule: str = "daily"
    notify: bool = False  # when the cron runs a sensor and it has NEW results, push one Bark
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
               schedule: str = "daily", notify: bool = False) -> Sensor:
        import hashlib
        sensors = self._load()
        sid = "sensor_" + hashlib.sha256(
            f"{query}:{time.time()}".encode()).hexdigest()[:12]
        from datetime import datetime, timezone
        s = Sensor(id=sid, query=query, sources=sources, schedule=schedule, notify=notify,
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


# ── graph write tap (design section 6 + P4 taps row): mint the RUN DIFF, never the baseline ──
# THE MINT RULE (design "Mint the product, not the intermediate"): a sensor's PRODUCT is the set of
# results THIS RUN newly detected (the diff); the baseline is state and mints nothing. The builder
# below is PURE (takes the Sensor + the new (source, source_id) pairs + the run timestamp, returns
# (nodes, edges) in the writer's dict shapes) so the smoke can golden-test it with zero network;
# ``_tap`` wraps enqueue_graph fail-open (a tap failure must NEVER break the run summary the agent
# gets). NOTE the launchd cron runner (scripts/sensor_runner.py) runs OUTSIDE the eye-http process,
# so ``WRITES_ENABLED`` stays off there and cron runs mint nothing BY DESIGN; observed edges accrue
# only from in-process runs (penumbra_sensor action=run). Routing the cron through the HTTP service is a
# canon open item, deliberately NOT built here.

def _observed_mints(sensor: "Sensor", new_pairs: list, run_at: str) -> tuple[list[dict], list[dict]]:
    """From ONE sensor + the ``(source, source_id)`` pairs THIS RUN detected as new: ONE sensor node
    (``sensor:{id}``, label=query) plus one ``observed`` M-edge sensor -> doc per new pair (method
    ``sensor:diff``, attrs {run_at}). An EMPTY diff mints NOTHING (not even the sensor node), since
    a no-news run is not an accretion event (the mint-the-product rule: the diff is the product, the
    baseline is state). Doc endpoints use ``graph.doc_node_id``; they may be virtual/thin rows (a
    stored edge does not require a node row for its endpoints). Pure."""
    pairs = [(s, sid) for (s, sid) in (new_pairs or []) if s and sid]
    if not pairs:
        return [], []
    sensor_nid = f"sensor:{sensor.id}"
    nodes: list[dict] = [{"id": sensor_nid, "kind": "sensor", "label": sensor.query, "attrs": None}]
    edges: list[dict] = []
    for source, source_id in pairs:
        edges.append({"src": sensor_nid, "dst": graph.doc_node_id(source, source_id),
                      "type": "observed", "tier": "M", "method": "sensor:diff",
                      "attrs": {"run_at": run_at}})
    return nodes, edges


def _tap(sensor: "Sensor", new_pairs: list, run_at: str) -> None:
    """FAIL-OPEN wrapper (the relations.py idiom): build the (nodes, edges) from the run diff and
    enqueue them through the single-writer queue. Never raises (a tap failure must NEVER break the
    run summary); NO-OP when writes are disabled (cron) or the diff is empty. Import the writer
    INSIDE the try so an import hiccup degrades to a swallow, never a broken sensor run."""
    try:
        nodes, edges = _observed_mints(sensor, new_pairs, run_at)
        if not nodes and not edges:
            return
        from penumbra.core.recall import writer
        writer.enqueue_graph(nodes, edges)
    except Exception as exc:  # noqa: BLE001, a tap failure must NEVER break a sensor run
        log.debug("sensor graph tap swallowed: %s", exc)


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

    # FAIL-OPEN graph tap (design section 6 + P4): mint the RUN DIFF (observed edges sensor -> new
    # doc) here, where the new-result diff is final, BEFORE the summary returns. The baseline mints
    # nothing; an empty diff mints nothing (a no-news run is not an accretion event).
    _tap(sensor, list(new_keys), sensor.last_run_at)

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
