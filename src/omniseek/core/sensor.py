"""Standing-query sensors: register a query, run it periodically, detect new results.

Sensors are background cache warmers with novelty detection. Each sensor:
1. Runs search_ranked for its registered query
2. Fingerprints each result as (source, source_id)
3. Diffs against its baseline to detect new information
4. Updates the baseline and records stats

A sensor is DECLARATIVE STATE OmniSeek executes mechanically; a run is an act of
PERCEPTION, and perception must land on the wall, so execution belongs in the ONE
process that can write memory (single-writer). The sensor tick runs IN-PROCESS on the
eye-http service; in P9 the daemon loop that drives it lives in omniseek.core.jobs (the
ONE fleet scheduler, WRITES_ENABLED-guarded, so no other context can ever start it), and
the sensor tick is registered there as job row #1. The MCP tool omniseek_sensor action=run
triggers one sensor immediately for testing. The razor: the agent registers what to
monitor (judgment); the diff is mechanical. (The old launchd cron runner was a second,
memory-less perception path with writes disabled; it is deleted, not fixed.)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path.home() / ".omniseek" / "state" / "sensors.json"

from omniseek.core.recall import graph  # noqa: E402 (the graph write verb + mint registry)

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
    notify_if: Optional[list[str]] = None  # keyword filter: Bark ONLY when a new result matches (None = any-new)
    notify_if_match: str = "any"           # "any" (default) | "all" of the notify_if substrings must appear
    detect_absence: bool = False           # opt-in: also alert when a tracked STABLE-source item DISAPPEARS
    gone_since: dict = field(default_factory=dict)  # identity -> iso first-seen-gone (the absence latch)
    baseline: list[list[str]] = field(default_factory=list)  # [[source, source_id], ...]
    created_at: str = ""
    last_run_at: Optional[str] = None
    last_new_count: int = 0
    total_runs: int = 0


_SENSOR_FIELDS = frozenset(f.name for f in fields(Sensor))

# Per-sensor push source. The push outlet routes by source; a sensor pushes under
# DEFAULT_NOTIFY_SOURCE unless the side file beside sensors.json maps its id to another source.
# The side file keeps sensors.json's format exactly as older builds read it (an older build loads
# sensors.json with Sensor(**row), so one extra key there would read the whole store as empty and
# the next save would wipe every sensor). Rolling back leaves the side file unread and harmless.
DEFAULT_NOTIFY_SOURCE = "eye.sensor"
NOTIFY_SOURCES_FILE = "sensor_notify_sources.json"
# A custom source stays inside the sensor namespace (eye.sensor.<name>), lowercase dotted words.
_NOTIFY_SOURCE_RE = re.compile(r"^eye\.sensor(\.[a-z0-9_]+)+$")
_WARNED_MISSING: set[str] = set()


def normalize_notify_source(source: Optional[str]) -> str:
    """The stored form of a requested push source: "" for the default (empty, None or eye.sensor
    itself), else the name if it is eye.sensor.<name>; anything else raises ValueError."""
    name = (source or "").strip()
    if not name or name == DEFAULT_NOTIFY_SOURCE:
        return ""
    if not _NOTIFY_SOURCE_RE.match(name):
        raise ValueError(f"notify_source {source!r} must be empty (default {DEFAULT_NOTIFY_SOURCE}) "
                         f"or {DEFAULT_NOTIFY_SOURCE}.<name> in lowercase letters, digits, _ and dots")
    return name


# One lock for every mutating load-modify-save cycle on sensors.json (the _RULINGS_LOCK idiom).
# The atomic tmp+replace in _save only prevents a TORN file; without this lock two concurrent
# writers (the in-process scheduler thread vs a manual omniseek_sensor action=run on a tool worker
# thread) would each rewrite the WHOLE file from their own stale _load snapshot and silently lose
# the other's update (a lost baseline = already-seen results re-reported as new). Module-level so
# every SensorStore instance over the same default path shares it.
_STORE_LOCK = threading.Lock()


class SensorStore:
    """Thread-safe CRUD on the sensors JSON file (atomic write via rename; mutations serialize
    under _STORE_LOCK so concurrent scheduler + manual runs never lose each other's updates)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _DEFAULT_STATE_PATH
        self.sources_path = self.path.with_name(NOTIFY_SOURCES_FILE)
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Sensor]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # Unknown keys (a field a newer build wrote) are dropped, not fatal: a whole-file
            # failure here reads as an EMPTY store and the next save would wipe every sensor.
            return {s["id"]: Sensor(**{k: v for k, v in s.items() if k in _SENSOR_FIELDS})
                    for s in raw}
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

    # ── the push-source side file: {sensor id: source name}, only non-default entries ──
    def _read_sources(self) -> tuple[dict[str, str], Optional[str]]:
        """(mapping, problem). problem is None when the file read clean, "missing" when there is no
        file, else why it is unreadable (the mapping then holds only the entries that read clean)."""
        if not self.sources_path.exists():
            return {}, "missing"
        try:
            raw = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {}, f"unreadable ({exc})"
        if not isinstance(raw, dict):
            return {}, f"not a JSON object ({type(raw).__name__})"
        good = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)
                and v and _NOTIFY_SOURCE_RE.match(v)}
        bad = sorted(set(raw) - set(good))
        return good, (f"bad entries ignored: {bad}" if bad else None)

    def _save_sources(self, mapping: dict[str, str]) -> None:
        self._ensure_dir()
        tmp = self.sources_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(mapping.items())), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.sources_path)

    def notify_sources(self) -> dict[str, str]:
        """The readable {sensor id: source} entries (custom sources only); a damaged file logs a
        warning and yields what read clean (an unreadable file: nothing, so every sensor defaults)."""
        mapping, problem = self._read_sources()
        if problem and problem != "missing":
            log.warning("%s %s -> sensors without a readable entry push under %s",
                        self.sources_path, problem, DEFAULT_NOTIFY_SOURCE)
        return mapping

    def notify_source(self, sensor_id: str) -> str:
        """The source this sensor pushes under: its side-file entry, else DEFAULT_NOTIFY_SOURCE.
        Fail-open: a missing file warns once per process, a damaged one warns on every read."""
        mapping, problem = self._read_sources()
        if problem == "missing":
            key = str(self.sources_path)
            if key not in _WARNED_MISSING:
                _WARNED_MISSING.add(key)
                log.warning("%s missing -> every sensor pushes under %s",
                            self.sources_path, DEFAULT_NOTIFY_SOURCE)
        elif problem:
            log.warning("%s %s -> sensor %s pushes under %s unless its entry read clean",
                        self.sources_path, problem, sensor_id, DEFAULT_NOTIFY_SOURCE)
        return mapping.get(sensor_id) or DEFAULT_NOTIFY_SOURCE

    def _put_source_locked(self, sensor_id: str, source: str) -> None:
        """Set (or, for "", drop) one entry; caller holds _STORE_LOCK. Refuses to rewrite a file it
        could not read, so a damaged file's other entries are never silently lost."""
        mapping, problem = self._read_sources()
        if problem and problem != "missing":
            raise ValueError(f"{self.sources_path} {problem}; fix or move it aside first")
        if mapping.get(sensor_id, "") == source:
            return
        if source:
            mapping[sensor_id] = source
        else:
            del mapping[sensor_id]
        self._save_sources(mapping)

    def set_notify_source(self, sensor_id: str, source: Optional[str]) -> Optional[str]:
        """Point one sensor at a push source ("" or None = back to the default), leaving sensors.json
        untouched. Returns the sensor's source now, None if the sensor does not exist; raises
        ValueError on a bad name or a damaged side file."""
        name = normalize_notify_source(source)
        with _STORE_LOCK:
            if sensor_id not in self._load():
                return None
            self._put_source_locked(sensor_id, name)
        return name or DEFAULT_NOTIFY_SOURCE

    def list_all(self) -> list[dict]:
        return [asdict(s) for s in self._load().values()]

    def get(self, sensor_id: str) -> Optional[Sensor]:
        return self._load().get(sensor_id)

    def create(self, query: str, sources: Optional[list[str]] = None,
               schedule: str = "daily", notify: bool = False,
               notify_if: Optional[list[str]] = None, notify_if_match: str = "any",
               detect_absence: bool = False, notify_source: Optional[str] = None) -> Sensor:
        import hashlib
        name = normalize_notify_source(notify_source)
        with _STORE_LOCK:
            if name:
                _, problem = self._read_sources()
                if problem and problem != "missing":
                    raise ValueError(f"{self.sources_path} {problem}; fix or move it aside first")
            sensors = self._load()
            sid = "sensor_" + hashlib.sha256(
                f"{query}:{time.time()}".encode()).hexdigest()[:12]
            from datetime import datetime, timezone
            s = Sensor(id=sid, query=query, sources=sources, schedule=schedule, notify=notify,
                       notify_if=notify_if, notify_if_match=notify_if_match,
                       detect_absence=detect_absence,
                       created_at=datetime.now(timezone.utc).isoformat())
            sensors[sid] = s
            self._save(sensors)
            if name:
                self._put_source_locked(sid, name)
        return s

    def delete(self, sensor_id: str) -> bool:
        with _STORE_LOCK:
            sensors = self._load()
            if sensor_id not in sensors:
                return False
            del sensors[sensor_id]
            self._save(sensors)
            try:
                self._put_source_locked(sensor_id, "")
            except ValueError as exc:
                log.warning("sensor %s deleted; its push-source entry was not removed: %s",
                            sensor_id, exc)
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
# gets). Every run now executes IN OmniSeek-http process (a manual omniseek_sensor action=run, or the
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
        from omniseek.core.recall import writer
        writer.enqueue_graph(nodes, edges)
    except Exception as exc:  # noqa: BLE001, a tap failure must NEVER break a sensor run
        log.debug("sensor graph tap swallowed: %s", exc)


def _sensor_window(sources: Optional[list[str]]) -> int:
    """The largest ``sensor_window`` the sensor's named sources declare (0 when none does, or when the
    sensor is a broad one with no sources)."""
    if not sources:
        return 0
    try:
        from omniseek.core import fetcher
        return max((int(getattr(fetcher.get_adapter(n), "sensor_window", 0) or 0) for n in sources),
                   default=0)
    except Exception:  # noqa: BLE001
        return 0


def run_sensor(sensor: Sensor, store: SensorStore, limit: int = 15) -> dict:
    """Execute one sensor: search -> diff baseline -> update. Returns a summary dict."""
    from omniseek.core import fetcher
    from datetime import datetime, timezone

    # A source that lists a personal feed (cubox, github_starred) declares ``sensor_window``: how many
    # items one run must see so a busy day is not cut at the default limit of 15 (a cut item would be
    # missed for good once it scrolls out of the source's lookback). Sources without it keep the old
    # call exactly.
    window = _sensor_window(sensor.sources)
    if window:
        limit = max(limit, window)
        ranked, _meta = fetcher.search_ranked(
            sensor.query, sources=sensor.sources, limit=limit, per_source=window)
    else:
        ranked, _meta = fetcher.search_ranked(
            sensor.query, sources=sensor.sources, limit=limit)
    # The seeding run is the first one that sees anything: an empty run (no credential yet, a quota
    # refusal) leaves the baseline empty, so counting runs would mislabel the later backlog as new.
    first_run = not sensor.baseline

    current_keys = set()
    for doc in ranked:
        current_keys.add((doc.source, doc.source_id))

    baseline_set = {tuple(b) for b in sensor.baseline}
    new_keys = current_keys - baseline_set
    new_docs = [d for d in ranked if (d.source, d.source_id) in new_keys]

    # notify_if content predicate (MONITOR precision): when set, only the NEW docs whose title/content
    # match the keyword condition count toward a Bark. notify_if=None reproduces the exact any-new path.
    # Pure in-process substring match (no regex -> no dependency, no ReDoS surface).
    if sensor.notify_if:
        _needles = [n.lower() for n in sensor.notify_if if n]
        _all = (sensor.notify_if_match or "any").lower() == "all"
        def _matches(d) -> bool:
            hay = f"{d.title or ''} {getattr(d, 'content', '') or ''}".lower()
            return (all(n in hay for n in _needles) if _all else any(n in hay for n in _needles)) if _needles else True
        notify_docs = [d for d in new_docs if _matches(d)]
    else:
        notify_docs = new_docs

    # Absence/disappearance detection (opt-in, stable-source-scoped): a tracked page_watch item whose
    # identity vanished from the results = went dark/removed. Computed from the OLD baseline HERE, before
    # the cap rebuild below replaces it. Latched via gone_since so a gone episode Barks ONCE and clears
    # when the item returns; detect_absence=False -> fresh_gone stays empty (byte-identical to today).
    _now_iso = datetime.now(timezone.utc).isoformat()
    if sensor.detect_absence:
        gone_now = absence_diff(sensor.baseline, current_keys)
        for _id in set(sensor.gone_since) - gone_now:      # returned -> clear the latch
            sensor.gone_since.pop(_id, None)
        fresh_gone = sorted(gone_now - set(sensor.gone_since))
        for _id in fresh_gone:
            sensor.gone_since[_id] = _now_iso
    else:
        fresh_gone = []

    # Bound the baseline (a fully-rewritten-every-tick list on an always-on service): current keys
    # ALWAYS survive at the front, so a still-visible item is never dropped (no false re-report); only
    # keys that have scrolled out of the result window tail off past the cap. Replaces the old monotonic
    # (baseline_set | current_keys) union that grew forever.
    cap = max(limit * 10, 200)  # ~10 result windows of headroom before a still-visible key could fall out
    _ordered = list(current_keys) + [tuple(b) for b in sensor.baseline if tuple(b) not in current_keys]
    sensor.baseline = [list(k) for k in _ordered[:cap]]
    sensor.last_run_at = _now_iso
    sensor.last_new_count = len(new_keys)
    sensor.total_runs += 1
    store.update(sensor)

    # FAIL-OPEN graph tap (design section 6 + P4): mint the RUN DIFF (observed edges sensor -> new
    # doc) here, where the new-result diff is final, BEFORE the summary returns. The baseline mints
    # nothing; an empty diff mints nothing (a no-news run is not an accretion event).
    _tap(sensor, list(new_keys), sensor.last_run_at)

    # FAIL-OPEN shadow output: the new items of opted-in sources go to a fixed JSONL file an external
    # comparison job reads (format in shadow_feed's docstring). Other sources write nothing.
    try:
        from omniseek.core import shadow_feed
        shadow_feed.record_new(sensor, new_docs, sensor.last_run_at, first_run)
    except Exception as exc:  # noqa: BLE001, a shadow write must NEVER break a sensor run
        log.debug("shadow feed swallowed: %s", exc)

    return {
        "sensor_id": sensor.id,
        "query": sensor.query,
        "total_results": len(ranked),
        "new_count": len(new_keys),
        "new_titles": [d.title for d in new_docs[:5]],
        "notify_new_count": len(notify_docs),
        "notify_titles": [d.title for d in notify_docs[:5]],
        "gone_count": len(fresh_gone),
        "gone_titles": fresh_gone[:5],
        "baseline_size": len(sensor.baseline),
        "run_at": sensor.last_run_at,
    }


def compute_diff(results: list, baseline: list[list[str]]) -> list:
    """Pure diff function for testing: returns (source, source_id) tuples not in baseline."""
    baseline_set = {tuple(b) for b in baseline}
    return [(r.source, r.source_id) for r in results
            if (r.source, r.source_id) not in baseline_set]


# ── absence / disappearance detection (borrowed idea: Huginn GapDetectorAgent, scoped noise-safe) ──
# The sensor's default diff is APPEARANCE-only (new = current - baseline); a tracked item that goes
# DARK (a watched policy page 404s -> page_watch emits no doc -> its key silently vanishes) never
# surfaces, a silent failure on OmniSeek's honest-empty-over-silent-wrong contract. Absence is sound
# ONLY for STABLE-identity sources (page_watch's source_id is "{name}:{fp}", a fixed membership set);
# on a churny ranked query the reverse diff is dominated by rank-window churn, so this is gated to a
# stable-source allowlist + an opt-in flag (detect_absence) and NEVER runs for a general query sensor.
_ABSENCE_STABLE_SOURCES = frozenset({"page_watch"})


def _absence_identity(source: str, source_id: str) -> str:
    """The change-INVARIANT identity of a stable-source key: source + the source_id prefix before the
    first ':' (page_watch's '{name}:{fp}' -> 'page_watch:{name}'), so a CONTENT change (new fp, same
    prefix) is NOT absence -- only a truly gone prefix is."""
    return f"{source}:{(source_id or '').split(':', 1)[0]}"


def absence_diff(baseline: list[list[str]], current_keys: set,
                 stable_sources: frozenset = _ABSENCE_STABLE_SOURCES) -> set:
    """Stable-source identities present in the OLD baseline but whose prefix has NO key in the current
    result set = a tracked item that went dark/removed. Scoped to stable_sources (a churny query has no
    stable identity). Pure."""
    base_ids = {_absence_identity(b[0], b[1]) for b in baseline
                if len(b) >= 2 and b[0] in stable_sources}
    cur_ids = {_absence_identity(s, sid) for (s, sid) in current_keys if s in stable_sources}
    return base_ids - cur_ids


# ── The in-process sensor tick (P6 perception; the P9 loop lives in jobs.py) ──────────────────────
# A run is an act of PERCEPTION and perception must land on the wall, so execution belongs in the
# ONE process that can write memory. due_sensors is PURE (unit-testable); scheduler_tick runs every
# due sensor SERIALLY and isolates a failing sensor so one bad query never stops the rest. In P9 the
# daemon LOOP that drives this tick moved into omniseek.core.jobs (the ONE scheduler for the whole
# fleet): the sensor tick is now registered there as job row #1 (scheduler_tick_for_sensors), and the
# WRITES_ENABLED + double-start guards that used to gate the sensor loop now gate jobs.start_scheduler.
# The old launchd cron runner is deleted. sensor.py keeps only the sensor SEMANTICS (this tick).

_SCHEDULE_SECONDS = {"hourly": 3600, "daily": 86400, "weekly": 604800}
_UNKNOWN_SCHEDULE_LOGGED: set[str] = set()   # log an unknown schedule once per sensor id (debug)


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
    except Exception as exc:  # noqa: BLE001 (a bad stamp reads as never-run, never a crash)
        log.debug("sensor last_run_at unparseable (%r): %s", ts, exc)
        return None


def due_sensors(store: SensorStore, now: float, schedules: Optional[dict] = None) -> list["Sensor"]:
    """PURE (unit-testable): the sensors that are due at ``now`` (epoch seconds). A sensor with no
    (or unparseable) ``last_run_at`` counts as never run.

    ``schedules`` None: every stored sensor, on its own ``schedule`` (``now - last_run_at >=
    interval``). ``schedules`` a {sensor id: jobs.Schedule} map read from the resident-task table
    (``table_sensor_schedules``): ONLY the sensors named there, each on the table's 何时跑; a stored
    sensor the map does not name is never due."""
    due: list["Sensor"] = []
    for raw in store.list_all():
        s = store.get(raw["id"])
        if s is None:
            continue
        last = _parse_iso(s.last_run_at)
        if schedules is not None:
            sched = schedules.get(s.id)
            if sched is not None and table_is_due(sched, now, last):
                due.append(s)
        elif last is None or (now - last) >= _interval_seconds(s):
            due.append(s)
    return due


# ── Optional: a resident-task table (SERVICES.tsv) decides WHICH sensors run and WHEN ────────────
# A deployment that keeps a shared table of its resident tasks can make it the one place that says
# whether and when each sensor runs, since a sensor is a resident task too. A table row for a
# sensor: 归谁 全知之眼, 种类 内部调度, 标签或单元 omniseek.core.sensor:<id>, 何时跑 "眼内部调度 <spec>".
# The table is read with the deployment's own reader (scripts/services.py), never a second parser.
# A stored definition the table does not list is kept but never runs. The table being unreadable
# stops this round loudly (see scheduler_tick_for_sensors).
# Without a table (sensor_table_path() is None) every stored sensor runs on its own ``schedule``,
# the reader is never imported and nothing about a table is logged or pushed.

SENSOR_TABLE_ENV = "OMNISEEK_SERVICES_TSV"   # must match the table reader's own variable and default
_DEFAULT_SENSOR_TABLE: Optional[Path] = None  # the public build ships no resident-task table
TABLE_WHEN_PREFIX = "眼内部调度"
TABLE_ALERT_COOLDOWN_S = 6 * 3600
_table_alert_last: dict[str, float] = {}


class SensorTableError(RuntimeError):
    """The resident-task table could not be used, so no sensor ran this round."""


def sensor_table_path(table_path=None) -> Optional[Path]:
    """The table sensors are scheduled from, or None when this deployment has none: ``table_path``
    > $OMNISEEK_SERVICES_TSV > _DEFAULT_SENSOR_TABLE (None in a build without a table). Resolved
    without importing the reader, so a deployment without one never needs it."""
    if table_path:
        return Path(table_path).expanduser()
    env = os.environ.get(SENSOR_TABLE_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return _DEFAULT_SENSOR_TABLE


def _services():
    """The table reader, scripts/services.py (importable without omniseek by design, the same
    import infra_jobs._declared_resident_labels uses). Imported only in table mode, on first use.
    Raises when it cannot be loaded."""
    scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import services  # noqa: PLC0415 -- deliberately late: the server must import without it
    return services


def parse_table_when(cell: str):
    """A table 何时跑 cell -> jobs.Schedule, or ValueError. The trailing full-width note is dropped
    (services.table_value), then the value must be ``眼内部调度 <spec>`` where <spec> is one of:
      hourly | daily | weekly        interval since the last run (OmniSeek's original words)
      每天 HH:MM[、HH:MM...]          fixed local times every day (the table's own launchd wording)
      every:Ns | daily@HH:MM[,..] | weekly@ddd-HH:MM | monthly@D-HH:MM   (omniseek.core.jobs wording)
    A fixed daily time missed (machine asleep, service down) is made up once later the same day,
    never on a later day (table_is_due)."""
    from omniseek.core import jobs  # noqa: PLC0415 -- jobs reaches back into sensor when it registers rows
    value = _services().table_value(cell or "")
    if not value.startswith(TABLE_WHEN_PREFIX):
        raise ValueError(f"何时跑 must start with {TABLE_WHEN_PREFIX!r}, got {value!r}")
    spec = value[len(TABLE_WHEN_PREFIX):].strip()
    if not spec:
        raise ValueError(f"何时跑 {value!r} names no schedule after {TABLE_WHEN_PREFIX!r}")
    if spec.lower() in _SCHEDULE_SECONDS:
        return jobs.Schedule(kind="interval", seconds=_SCHEDULE_SECONDS[spec.lower()], raw=spec)
    if spec.startswith("每天"):
        times = [t.strip() for t in spec[len("每天"):].split("、")]
        if not all(times):
            raise ValueError(f"每天 needs HH:MM times joined by 、: {spec!r}")
        return jobs.parse_schedule("daily@" + ",".join(times))
    return jobs.parse_schedule(spec)


def table_is_due(sched, now: float, last_run: Optional[float]) -> bool:
    """Whether a table schedule is due at ``now`` given the sensor's ``last_run`` (None = never).
    PURE but for the local timezone. A daily fixed-time schedule (每天 / daily@) is due when today's
    most recent slot has passed and the sensor has not run since it: a slot missed while the machine
    was asleep or the service down is made up ONCE later the same day, and a day that passed
    without it is not made up on the next day (the next run is that day's own slot). Every other
    kind follows omniseek.core.jobs.is_due."""
    from omniseek.core import jobs  # noqa: PLC0415
    if sched.kind != "daily":
        return jobs.is_due(sched, now, last_run)
    from datetime import datetime  # noqa: PLC0415
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    passed = [today.replace(hour=h, minute=m).timestamp() for (h, m) in sched.times]
    passed = [t for t in passed if t <= now]
    if not passed:
        return False
    return last_run is None or last_run < max(passed)


def table_sensor_schedules(path=None) -> tuple[dict, list[str]]:
    """Read the resident-task table: ({sensor id: jobs.Schedule}, [row problems]). Only rows with
    归谁 全知之眼, 种类 内部调度 and 标签或单元 omniseek.core.sensor:<id> count. A row whose 何时跑
    cannot be read, or a second row for the same sensor, becomes a problem string and that row is
    left out (its sensor does not run). Raises services.TableError (or any load error) when the
    table as a whole is unusable; never answers an unusable table with an empty map."""
    svc = _services()
    schedules: dict = {}
    problems: list[str] = []
    for row in svc.omniseek_rows(path):
        unit = svc.table_value(row["标签或单元"])
        if svc.table_value(row["种类"]) != svc.KIND_INTERNAL or not unit.startswith(svc.SENSOR_PREFIX):
            continue
        sid = unit[len(svc.SENSOR_PREFIX):]
        where = f"第 {row.get('_line', '?')} 行 {row['名字']}"
        if sid in schedules:
            problems.append(f"{where}: {sid} 在表上出现第二次，这一行不算")
            continue
        try:
            schedules[sid] = parse_table_when(row["何时跑"])
        except ValueError as exc:
            problems.append(f"{where}: 何时跑 {row['何时跑']!r} 读不懂（{exc}），{sid} 这一轮不跑")
    return schedules, problems


def table_row_hint(sensor: "Sensor") -> Optional[str]:
    """What omniseek_sensor create tells the caller in table mode: the definition exists, but only a
    table row (and a release of the table) makes it run. Names the cells that row needs. None when
    there is no table: the sensor then runs on its own schedule and there is nothing to add."""
    if sensor_table_path() is None:
        return None
    num = sensor.id[len("sensor_"):] if sensor.id.startswith("sensor_") else sensor.id
    return (f"已建定义，但还不会自动跑：要在常驻任务表 SERVICES.tsv 加一行（名字 服务/eye-sensor-{num}，"
            f"种类 内部调度，标签或单元 omniseek.core.sensor:{sensor.id}，归谁 全知之眼，"
            f"何时跑 {TABLE_WHEN_PREFIX} {sensor.schedule}），表发版后才会按表上的何时跑运行；"
            f"在那之前可以用 action=\"run\" 手动跑。")


def _table_alert(key: str, title: str, body: str, now: Optional[float] = None) -> None:
    """One push per ``key`` per TABLE_ALERT_COOLDOWN_S (in-process memory: the scheduler lives in
    one long-running process). Best-effort: a failed push never breaks the tick."""
    now = time.time() if now is None else now
    last = _table_alert_last.get(key)
    if last is not None and now - last < TABLE_ALERT_COOLDOWN_S:
        return
    _table_alert_last[key] = now
    try:
        _alert(title, body)
    except Exception as exc:  # noqa: BLE001
        log.debug("sensor table alert swallowed (%s)", exc)


def scheduler_tick(store: SensorStore, schedules: Optional[dict] = None) -> dict:
    """Run every DUE sensor serially via ``run_sensor`` (each wrapped in try/except so one failing
    sensor never stops the rest), Bark on ``sensor.notify and summary["new_count"] > 0``, and return
    a mechanical summary ``{"checked", "ran", "failed"}``. ``schedules`` as in ``due_sensors`` (the
    job entry passes the table's map; None keeps each sensor's own schedule). Logs ONE info line per
    tick that ran anything; a zero-due tick logs NOTHING (silence there means idle; the launchd
    service log must not fill with heartbeats). The tick needs no extra lock: SensorStore is
    thread-safe and run_sensor is reentrant-safe (a concurrent manual run at worst double-searches;
    the cache absorbs it)."""
    now = time.time()
    due = due_sensors(store, now, schedules)
    ran: list[str] = []
    failed: list[str] = []
    for s in due:
        try:
            summary = run_sensor(s, store)
            ran.append(s.id)
            if s.notify and (summary.get("notify_new_count", summary.get("new_count", 0)) > 0
                             or summary.get("gone_count", 0) > 0):
                _bark_new_results(s, summary, store)
        except Exception:  # noqa: BLE001 (one bad sensor must never stop the tick)
            log.exception("sensor %s (%s) failed in scheduler tick", s.id, s.query)
            failed.append(s.id)
    if ran or failed:
        log.info("sensor scheduler tick: checked %d, ran %d, failed %d",
                 len(due), len(ran), len(failed))
    return {"checked": len(due), "ran": ran, "failed": failed}


def scheduler_tick_for_sensors(table_path=None) -> dict:
    """JOB-ROW ENTRY POINT (P9): the sensor tick as registered job #1 in omniseek.core.jobs, called
    with no argument every time the "sensors" row is due (``table_path`` is for tests; None means
    sensor_table_path's order: $OMNISEEK_SERVICES_TSV, then _DEFAULT_SENSOR_TABLE).

    No table (sensor_table_path() is None): every stored sensor runs on its own ``schedule``
    (scheduler_tick with no map), the reader is not imported, nothing is pushed about a table.

    With a table, which sensors run and when comes from it (table_sensor_schedules): a
    stored sensor the table does not list never runs; a sensor row whose 何时跑 cannot be read, or
    one naming a sensor with no stored definition, is skipped and pushed once per 6h. When the
    table as a whole cannot be used (missing file, missing column, bad row shape, no 全知之眼 row,
    reader not loadable) NO sensor runs this round, the reason is logged and pushed once per 6h,
    and SensorTableError is raised so the job registry records the "sensors" row as failed (the
    in-process analog of the table checker's exit code 2). Never an unnoticed full run, never an
    unnoticed stop.

    Due sensors run serially, a failing sensor is isolated, and Bark-on-new stays inside
    run_sensor/_bark_new_results. The WRITES_ENABLED + double-start guards live on
    jobs.start_scheduler, which owns the single daemon thread this row runs under."""
    path = sensor_table_path(table_path)
    if path is None:
        return scheduler_tick(SensorStore())
    shown = str(table_path) if table_path is not None else "SERVICES.tsv"
    try:
        shown = str(_services().services_tsv_path(path))
        schedules, problems = table_sensor_schedules(path)
    except Exception as exc:  # noqa: BLE001 -- TableError, or the reader itself not loadable
        msg = (f"眼的 sensor 这一轮一个都没跑：常驻任务表 {shown} 用不了（{exc}）。"
               f"修好表，或用环境变量 OMNISEEK_SERVICES_TSV 指向一份好表；修好后下一轮自动恢复。")
        log.error("%s", msg)
        _table_alert("table_unusable", "眼 sensor 停了：常驻任务表用不了", msg)
        raise SensorTableError(msg) from exc
    store = SensorStore()
    defined = {raw["id"] for raw in store.list_all()}
    for sid in sorted(set(schedules) - defined):
        problems.append(f"表上有 {sid}，眼里没有这个 sensor 的定义，跑不了")
    off_table = sorted(defined - set(schedules))
    if off_table:
        log.debug("sensors not on the resident-task table (kept, not run): %s", off_table)
    if problems:
        for p in problems:
            log.warning("sensor table %s: %s", shown, p)
        _table_alert("rows:" + "|".join(sorted(problems)), "眼 sensor：常驻任务表有几行用不了",
                     f"常驻任务表 {shown}：\n" + "\n".join(problems) + "\n其余 sensor 照表跑。")
    out = scheduler_tick(store, schedules)
    out.update({"table": shown, "off_table": off_table, "problems": problems})
    return out


# ── Bark push (P6, ported from the deleted runner's _notify; the impl moved to notify.py in P9) ────
# The runner pushed via scripts/_sentinel_common (urllib); in-process we use OmniSeek's EXISTING httpx
# dependency and read the credential file the auth.py way (fail-open to no-op when absent). P9 lifted
# that impl into omniseek.core.notify so the generalized job registry shares ONE push primitive; this
# thin alias keeps the P6 call sites (_bark_new_results) unchanged and the same fail-open contract +
# GROUP "OmniSeek" (spelled so the omniseek sync's OmniSeek->OmniSeek rename lands on both sides).

def _alert(title: str, body: str, *, source: str | None = None) -> None:
    """Fail-open alarm: delegates to notify.alert (WeCom; Bark was deleted 2026-08-12). Kept as a module-local
    name so the existing _bark_new_results call site is untouched and any monkeypatch of this symbol
    in a test still works. ``source`` is passed only for a non-default source, so a
    default sensor's call is unchanged and still goes out tagged eye.sensor."""
    from omniseek.core import notify
    if source:
        notify.alert(title, body, source=source)
    else:
        notify.alert(title, body)


def _bark_new_results(sensor: "Sensor", summary: dict, store: Optional[SensorStore] = None) -> None:
    """Shape the runner's message for a notify=True sensor that turned up new results and push it:
    title = the sensor query, body = the new count + the first new titles, under the sensor's push
    source from ``store``'s side file (default store when None). Fail-open via _alert."""
    titles = summary.get("notify_titles") or summary.get("new_titles") or []
    count = summary.get("notify_new_count", summary.get("new_count", 0))
    parts = []
    if count > 0:
        parts.append(f"{count} 条新结果" + ("\n" + "\n".join(titles) if titles else ""))
    if summary.get("gone_count", 0) > 0:
        parts.append(f"⚠️ {summary['gone_count']} 个被盯项已消失: " + ", ".join(summary.get("gone_titles") or []))
    body = "\n".join(parts) if parts else f"{count} 条新结果"
    try:
        source = (store or SensorStore()).notify_source(sensor.id)
    except Exception as exc:  # noqa: BLE001 (a lookup failure must never cost the push)
        log.warning("sensor %s push source lookup failed (%s) -> %s",
                    sensor.id, exc, DEFAULT_NOTIFY_SOURCE)
        source = DEFAULT_NOTIFY_SOURCE
    if source != DEFAULT_NOTIFY_SOURCE:
        _alert(sensor.query, body, source=source)
    else:
        _alert(sensor.query, body)
