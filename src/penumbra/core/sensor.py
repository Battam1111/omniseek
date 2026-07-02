"""Standing-query sensors: register a query, run it periodically, detect new results.

Sensors are background cache warmers with novelty detection. Each sensor:
1. Runs search_ranked for its registered query
2. Fingerprints each result as (source, source_id)
3. Diffs against its baseline to detect new information
4. Updates the baseline and records stats

A sensor is DECLARATIVE STATE the eye executes mechanically; a run is an act of
PERCEPTION, and perception must land on the wall, so execution belongs in the ONE
process that can write memory (single-writer). start_scheduler ticks the due sensors
IN-PROCESS on the eye-http service (WRITES_ENABLED-guarded, so no other context can
ever start it). The MCP tool penumbra_sensor action=run triggers one sensor immediately for
testing. The razor: the agent registers what to monitor (judgment); the diff is
mechanical. (The old launchd cron runner was a second, memory-less perception path with
writes disabled; it is deleted, not fixed.)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
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
    notify: bool = False  # when a SCHEDULED run finds NEW results, push one Bark (in-process scheduler)
    baseline: list[list[str]] = field(default_factory=list)  # [[source, source_id], ...]
    created_at: str = ""
    last_run_at: Optional[str] = None
    last_new_count: int = 0
    total_runs: int = 0


# One lock for every mutating load-modify-save cycle on sensors.json (the _RULINGS_LOCK idiom).
# The atomic tmp+replace in _save only prevents a TORN file; without this lock two concurrent
# writers (the in-process scheduler thread vs a manual penumbra_sensor action=run on a tool worker
# thread) would each rewrite the WHOLE file from their own stale _load snapshot and silently lose
# the other's update (a lost baseline = already-seen results re-reported as new). Module-level so
# every SensorStore instance over the same default path shares it.
_STORE_LOCK = threading.Lock()


class SensorStore:
    """Thread-safe CRUD on the sensors JSON file (atomic write via rename; mutations serialize
    under _STORE_LOCK so concurrent scheduler + manual runs never lose each other's updates)."""

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
        with _STORE_LOCK:
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
        with _STORE_LOCK:
            sensors = self._load()
            if sensor_id not in sensors:
                return False
            del sensors[sensor_id]
            self._save(sensors)
        return True

    def update(self, sensor: Sensor) -> None:
        with _STORE_LOCK:
            sensors = self._load()
            sensors[sensor.id] = sensor
            self._save(sensors)


# ── graph write tap (design section 6 + P4 taps row): mint the RUN DIFF, never the baseline ──
# THE MINT RULE (design "Mint the product, not the intermediate"): a sensor's PRODUCT is the set of
# results THIS RUN newly detected (the diff); the baseline is state and mints nothing. The builder
# below is PURE (takes the Sensor + the new (source, source_id) pairs + the run timestamp, returns
# (nodes, edges) in the writer's dict shapes) so the smoke can golden-test it with zero network;
# ``_tap`` wraps enqueue_graph fail-open (a tap failure must NEVER break the run summary the agent
# gets). Every run now executes IN the eye-http process (a manual penumbra_sensor action=run, or the
# in-process scheduler below), where ``WRITES_ENABLED`` is on, so observed edges accrue from every
# run. (The launchd cron runner that ran OUTSIDE the writer process, minting nothing, is deleted:
# a memory-less perception path was the wrong structure, not a thing to bridge.)

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


# ── The in-process scheduler (P6): the ONE perception path ───────────────────────────────────────
# A run is an act of PERCEPTION and perception must land on the wall, so execution belongs in the
# ONE process that can write memory. The scheduler ticks every due sensor SERIALLY in the eye-http
# process (start_scheduler is WRITES_ENABLED-guarded, so no cron / smoke / CLI context can start a
# second, memory-less path). due_sensors is PURE (unit-testable); scheduler_tick isolates a failing
# sensor so one bad query never stops the rest. The old launchd cron runner is deleted.

_SCHEDULE_SECONDS = {"hourly": 3600, "daily": 86400, "weekly": 604800}
_UNKNOWN_SCHEDULE_LOGGED: set[str] = set()   # log an unknown schedule once per sensor id (debug)
_scheduler_started = False                   # idempotence guard: a double call cannot start two threads
_scheduler_lock = threading.Lock()


def _interval_seconds(sensor: "Sensor") -> int:
    """The schedule interval in seconds; an UNKNOWN schedule degrades to daily (logged once per
    sensor id at debug level, so a typo is visible without spamming). Pure but for the one-shot log."""
    sched = (sensor.schedule or "").strip().lower()
    if sched in _SCHEDULE_SECONDS:
        return _SCHEDULE_SECONDS[sched]
    if sensor.id not in _UNKNOWN_SCHEDULE_LOGGED:
        _UNKNOWN_SCHEDULE_LOGGED.add(sensor.id)
        log.debug("sensor %s: unknown schedule %r -> daily", sensor.id, sensor.schedule)
    return _SCHEDULE_SECONDS["daily"]


def _parse_iso(ts: Optional[str]) -> Optional[float]:
    """An ISO timestamp -> epoch seconds; None/unparseable -> None (a sensor with no valid last_run_at
    is treated as never-run, i.e. due immediately). Mechanical, no guessing."""
    if not ts:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception as exc:  # noqa: BLE001 — a bad stamp reads as never-run, never a crash
        log.debug("sensor last_run_at unparseable (%r): %s", ts, exc)
        return None


def due_sensors(store: SensorStore, now: float) -> list["Sensor"]:
    """PURE (unit-testable): the sensors whose ``now - last_run_at >= interval(schedule)``. A sensor
    with no (or unparseable) ``last_run_at`` is due immediately (never run). ``now`` is epoch seconds
    (the caller passes time.time()); reading the store is the only side effect."""
    due: list["Sensor"] = []
    for raw in store.list_all():
        s = store.get(raw["id"])
        if s is None:
            continue
        last = _parse_iso(s.last_run_at)
        if last is None or (now - last) >= _interval_seconds(s):
            due.append(s)
    return due


def scheduler_tick(store: SensorStore) -> dict:
    """Run every DUE sensor serially via ``run_sensor`` (each wrapped in try/except so one failing
    sensor never stops the rest), Bark on ``sensor.notify and summary["new_count"] > 0``, and return
    a mechanical summary ``{"checked", "ran", "failed"}``. Logs ONE info line per tick that ran
    anything; a zero-due tick logs NOTHING (silence there means idle — the launchd service log must
    not fill with heartbeats). The tick needs no extra lock: SensorStore is thread-safe and run_sensor
    is reentrant-safe (a concurrent manual run at worst double-searches; the cache absorbs it)."""
    now = time.time()
    due = due_sensors(store, now)
    ran: list[str] = []
    failed: list[str] = []
    for s in due:
        try:
            summary = run_sensor(s, store)
            ran.append(s.id)
            if s.notify and summary.get("new_count", 0) > 0:
                _bark_new_results(s, summary)
        except Exception:  # noqa: BLE001 — one bad sensor must never stop the tick
            log.exception("sensor %s (%s) failed in scheduler tick", s.id, s.query)
            failed.append(s.id)
    if ran or failed:
        log.info("sensor scheduler tick: checked %d, ran %d, failed %d",
                 len(due), len(ran), len(failed))
    return {"checked": len(due), "ran": ran, "failed": failed}


def start_scheduler(interval_s: int = 900, initial_delay_s: int = 120) -> Optional[threading.Thread]:
    """Start the daemon scheduler thread (loops ``sleep(initial_delay_s); while True: tick;
    sleep(interval_s)``). The initial delay keeps deploy restarts from firing runs mid-restart-storm.

    Two guards make this the ONE perception path: (1) it REFUSES to start unless
    ``writer.WRITES_ENABLED`` is truthy (the scheduler only ever belongs in the writer process — a
    cron / smoke / CLI import leaves it False, so no memory-less path can ever tick); (2) a module
    idempotence flag so a double call cannot start two threads. Returns the Thread it started, or
    None when a guard refused. Import the writer INSIDE so a smoke import that never enables writes
    still sees the refusal, not an import-order surprise."""
    global _scheduler_started
    try:
        from penumbra.core.recall import writer
        writes_on = bool(writer.WRITES_ENABLED)
    except Exception as exc:  # noqa: BLE001 — cannot read the gate -> treat as OFF, refuse to start
        log.warning("sensor scheduler: cannot read WRITES_ENABLED (%s); not starting", exc)
        return None
    if not writes_on:
        log.warning("sensor scheduler: WRITES_ENABLED is off; not starting "
                    "(the scheduler only ever belongs in the writer process)")
        return None
    with _scheduler_lock:
        if _scheduler_started:
            return None
        _scheduler_started = True

    def _loop() -> None:
        time.sleep(max(0, initial_delay_s))
        while True:
            try:
                scheduler_tick(SensorStore())
            except Exception:  # noqa: BLE001 — a tick must never kill the loop
                log.exception("sensor scheduler tick crashed; continuing")
            time.sleep(max(1, interval_s))

    t = threading.Thread(target=_loop, name="sensor-scheduler", daemon=True)
    t.start()
    return t


# ── Bark push (P6, ported from the deleted runner's _notify) ──────────────────────────────────────
# The runner pushed via scripts/_sentinel_common (urllib); in-process we use the eye's EXISTING httpx
# dependency and read the credential file the auth.py way (fail-open to no-op when absent). The GROUP
# string is spelled "Penumbra" so the penumbra sync's Penumbra->Penumbra rename lands correctly on both
# sides. Message shape is the runner's: title = the sensor query, body = new_count + first new titles.

_BARK_CREDS_PATH = Path.home() / ".penumbra" / "credentials" / "bark.json"


def _bark_push(title: str, body: str) -> None:
    """POST one Bark notification (group "Penumbra", ~3s timeout). FAIL-OPEN: an absent credentials
    file, a missing device_key, or any HTTP/parse failure is a debug log and a silent return, NEVER
    an exception (a push failure must never break a sensor run). Reads ~/.penumbra/credentials/bark.json
    ({device_key, api_base}) the way auth.load() reads a source credential file."""
    try:
        if not _BARK_CREDS_PATH.exists():
            log.debug("bark push skipped: no credentials at %s", _BARK_CREDS_PATH)
            return
        creds = json.loads(_BARK_CREDS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — unreadable creds -> no-op, never raise
        log.debug("bark push skipped: credentials unreadable (%s)", exc)
        return
    key = (creds or {}).get("device_key")
    api = (creds or {}).get("api_base") or "https://api.day.app"
    if not key:
        log.debug("bark push skipped: no device_key in credentials")
        return
    try:
        import httpx
        httpx.post(f"{api.rstrip('/')}/{key}",
                   json={"title": title, "body": body, "group": "Penumbra"},
                   timeout=3.0)
    except Exception as exc:  # noqa: BLE001 — the push is best-effort; a failure never breaks a run
        log.debug("bark push failed (%s)", exc)


def _bark_new_results(sensor: "Sensor", summary: dict) -> None:
    """Shape the runner's message for a notify=True sensor that turned up new results and push it:
    title = the sensor query, body = the new count + the first new titles. Fail-open via _bark_push."""
    titles = summary.get("new_titles") or []
    body = f"{summary.get('new_count', 0)} 条新结果" + ("\n" + "\n".join(titles) if titles else "")
    _bark_push(sensor.query, body)
