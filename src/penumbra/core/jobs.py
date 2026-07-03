"""The in-process JOB REGISTRY + scheduler (P9): the ONE daemon loop that runs every scheduled
piece of the eye's self-maintenance inside the writer process.

THE DERIVED ARCHITECTURE (P6 moved the fleet's center of gravity, P9 finishes the move): a run is
an act of PERCEPTION or self-maintenance and must land inside the ONE process that can write memory,
so anything that CAN die with the organ lives HERE as a declarative JOB ROW instead of an external
launchd cron. The test for what stays OUTSIDE the organ is "must this still run when the organ is
dead?" -- only the hands that restart it (one sentinel), the vault that outlives it (state-backup),
and the browsers that must not die with it (the CDP launchers) stay external. Everything else is a
row registered here.

This generalizes the P6 sensors-only scheduler (sensor.py) into a registry with a mechanical
schedule vocabulary. The sensor tick becomes job row #1; the transplanted watchdogs / curator /
audit / digest become the other rows (see infra_jobs.py). The graph_view / register_mints DNA
(a registry dict populated by a register_* call at import) is the shape here too.

THE RAZOR stays: the registry runs declared jobs mechanically on a schedule; WHICH jobs exist and
WHAT they do is judgment expressed in code + the profile override, never inferred here.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# One persisted last-run map for ALL jobs (the SensorStore atomic tmp+replace idiom, one file keyed
# by job name). Distinct from sensors.json: sensors carry their own baselines; this only remembers
# each job's last_run epoch + its per-job Bark-on-exception cooldown stamp.
STATE_PATH = Path.home() / ".penumbra" / "state" / "scheduler-state.json"
# The dead-man file the EXTERNAL sentinel watches: the scheduler loop touches it every tick, so a
# stale mtime while /healthz is still OK means "organ alive but scheduler dead" (the sentinel's
# distinct alarm). Kept beside the state file under ~/.penumbra/state/.
HEARTBEAT_PATH = Path.home() / ".penumbra" / "state" / "scheduler-heartbeat"

TICK_SECONDS = 900        # the loop wakes every 15 min; a job runs when its own schedule is due
INITIAL_DELAY_S = 120     # keep deploy restart-storms from firing jobs mid-restart
JOB_BARK_COOLDOWN_S = 24 * 3600   # Bark at most once per 24h per job on repeated exceptions

# ── the schedule vocabulary (mechanical parser, PURE + unit-testable) ─────────────────────────────
# every:<N>s | daily@HH:MM[,HH:MM...] | weekly@ddd-HH:MM (ddd in mon..sun) | monthly@<D>-HH:MM
# An UNKNOWN / malformed spec raises ValueError AT REGISTRATION (loud, never a silent default). This
# DIFFERS DELIBERATELY from the sensors' lenient daily fallback (sensor.py keeps that for sensors,
# which come from agent input at runtime); a JOB schedule is code the author wrote, so a typo must
# fail the build, not degrade to a surprise cadence.
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Schedule:
    """A parsed schedule. kind is one of: interval | daily | weekly | monthly.
      interval: seconds set (every:Ns).
      daily:    times = [(H, M), ...] sorted.
      weekly:   weekday in 0..6 (Mon=0), times = [(H, M)].
      monthly:  dom (day-of-month 1..31), times = [(H, M)].
    Calendar kinds carry a slot list so due computation is a pure function of (schedule, now,
    last_run)."""
    kind: str
    seconds: int = 0
    times: tuple = ()          # tuple of (hour, minute)
    weekday: int = -1          # 0=Mon .. 6=Sun (weekly only)
    dom: int = 0               # day-of-month (monthly only)
    raw: str = ""


def _parse_hhmm(s: str) -> tuple[int, int]:
    """'HH:MM' -> (hour, minute), validated. Raises ValueError on anything not a real 24h time."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"bad HH:MM {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"HH:MM out of range {s!r}")
    return h, m


def parse_schedule(spec: str) -> Schedule:
    """Parse a schedule spec into a Schedule, or raise ValueError. PURE. The four forms:
      every:<N>s              e.g. every:900s   (N a positive int of seconds)
      daily@HH:MM[,HH:MM...]  e.g. daily@05:00 or daily@09:17,14:17,19:17
      weekly@ddd-HH:MM        e.g. weekly@sun-06:00 (ddd in mon..sun)
      monthly@<D>-HH:MM       e.g. monthly@1-06:00  (D the day-of-month 1..31)"""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"empty schedule spec {spec!r}")
    s = spec.strip()

    if s.startswith("every:"):
        rest = s[len("every:"):]
        if not rest.endswith("s"):
            raise ValueError(f"interval must end in 's': {spec!r}")
        n = int(rest[:-1])   # int() raises ValueError on non-numeric -> loud, as required
        if n <= 0:
            raise ValueError(f"interval seconds must be positive: {spec!r}")
        return Schedule(kind="interval", seconds=n, raw=s)

    if s.startswith("daily@"):
        body = s[len("daily@"):]
        times = tuple(sorted(_parse_hhmm(t) for t in body.split(",") if t != ""))
        if not times:
            raise ValueError(f"daily needs at least one HH:MM: {spec!r}")
        return Schedule(kind="daily", times=times, raw=s)

    if s.startswith("weekly@"):
        body = s[len("weekly@"):]
        if "-" not in body:
            raise ValueError(f"weekly must be ddd-HH:MM: {spec!r}")
        ddd, hhmm = body.split("-", 1)
        ddd = ddd.strip().lower()
        if ddd not in _WEEKDAYS:
            raise ValueError(f"weekly weekday must be one of {_WEEKDAYS}: {spec!r}")
        return Schedule(kind="weekly", weekday=_WEEKDAYS.index(ddd),
                        times=(_parse_hhmm(hhmm),), raw=s)

    if s.startswith("monthly@"):
        body = s[len("monthly@"):]
        if "-" not in body:
            raise ValueError(f"monthly must be <D>-HH:MM: {spec!r}")
        dom_s, hhmm = body.split("-", 1)
        dom = int(dom_s)   # int() raises ValueError on non-numeric -> loud
        if not (1 <= dom <= 31):
            raise ValueError(f"monthly day-of-month must be 1..31: {spec!r}")
        return Schedule(kind="monthly", dom=dom, times=(_parse_hhmm(hhmm),), raw=s)

    raise ValueError(f"unknown schedule spec {spec!r} (want every:Ns | daily@HH:MM | "
                     f"weekly@ddd-HH:MM | monthly@<D>-HH:MM)")


# ── most-recent-slot computation for calendar schedules (PURE) ────────────────────────────────────
# A calendar job is due when now >= the most recent scheduled slot AND last_run < that slot. This
# means AT MOST ONE run per slot (no catch-up storms): a slot older than the previous slot never
# replays, because we only ever compare against the SINGLE most-recent past slot. We compute that
# slot as an epoch so due-ness is last_run < most_recent_slot <= now -- a clean total order.

def _most_recent_slot(sched: Schedule, now: float) -> Optional[float]:
    """The epoch of the most recent scheduled firing at or before ``now`` (local time, the launchd
    StartCalendarInterval convention these jobs inherit), or None if there is no past slot yet.
    PURE except for reading the local timezone via datetime. Interval schedules return None (they
    are handled by elapsed-time, not slots)."""
    if sched.kind == "interval":
        return None
    now_dt = datetime.fromtimestamp(now)
    best: Optional[float] = None

    def _consider(dt: datetime) -> None:
        nonlocal best
        if dt.timestamp() <= now and (best is None or dt.timestamp() > best):
            best = dt.timestamp()

    if sched.kind == "daily":
        # today's slots and yesterday's slots cover the most-recent-past for any time-of-day.
        for day_off in (0, 1):
            base = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            base = base.fromtimestamp(base.timestamp() - day_off * 86400)
            for (h, m) in sched.times:
                _consider(base.replace(hour=h, minute=m))
        return best

    if sched.kind == "weekly":
        h, m = sched.times[0]
        # walk back up to 8 days to find the most recent matching weekday+time.
        for day_off in range(0, 8):
            d = datetime.fromtimestamp(now_dt.timestamp() - day_off * 86400)
            if d.weekday() == sched.weekday:
                _consider(d.replace(hour=h, minute=m, second=0, microsecond=0))
        return best

    if sched.kind == "monthly":
        h, m = sched.times[0]
        # this month's slot and the previous month's slot bracket the most-recent-past. Skip a month
        # that has no such day (e.g. day 31 in a 30-day month): that month simply contributes no slot.
        y, mo = now_dt.year, now_dt.month
        for (yy, mm) in ((y, mo), (y - 1, 12) if mo == 1 else (y, mo - 1)):
            try:
                dt = datetime(yy, mm, sched.dom, h, m)
            except ValueError:
                continue  # e.g. Feb 30 -> no slot that month
            _consider(dt)
        return best

    return None


def is_due(sched: Schedule, now: float, last_run: Optional[float]) -> bool:
    """Whether a job on ``sched`` is due at ``now`` given its ``last_run`` epoch (None = never run).
    PURE. Interval: now - last_run >= seconds (never-run is due). Calendar: due when now has reached
    the most recent slot AND last_run is before that slot (so exactly one run per slot; a missed
    older slot never replays)."""
    if sched.kind == "interval":
        if last_run is None:
            return True
        return (now - last_run) >= sched.seconds
    slot = _most_recent_slot(sched, now)
    if slot is None:
        return False   # no past slot yet (e.g. a fresh deploy before the first daily time)
    if last_run is None:
        return True    # never run and a slot has passed -> due
    return last_run < slot


# ── the registry (the register_mints DNA: a dict populated by register_job at import) ──────────────
# Per-job wall-clock budget ceiling. The external sentinel declares the scheduler WEDGED when the
# heartbeat goes stale past its threshold (2x the 900s tick = 1800s); the tick touches the heartbeat
# before EVERY job, so worst-case staleness is ONE job's budget, and every budget must therefore
# stay under that threshold with margin. A budget is a resource cap (the max_nodes class), never a
# judgment.
_MAX_JOB_BUDGET_S = 1500


@dataclass
class JobRow:
    name: str
    schedule: Schedule
    fn: Callable[[], object]
    enabled: bool = True     # the shipped default; the profile override can flip it
    budget_s: int = 600      # wall-clock cap per run; a job past it is skipped, never waited on
    needs: None = None       # reserved (spec: needs: none) -- no dependency edges in P9


_REGISTRY: "dict[str, JobRow]" = {}


def register_job(name: str, schedule: str, fn: Callable[[], object], enabled: bool = True,
                 budget_s: int = 600) -> JobRow:
    """Register one job row. Parses ``schedule`` NOW so an unknown/malformed spec raises ValueError
    AT REGISTRATION (import time), never a silent default. A duplicate ``name`` also raises (two
    rows fighting over one last-run key is a bug), as does a budget outside (0, _MAX_JOB_BUDGET_S]
    (a budget at or past the sentinel's wedge threshold would let one slow job read as a dead
    scheduler). Returns the stored JobRow. The profile override is NOT applied here (it is read
    live at tick time via ``job_enabled``), so tests can register a row and inspect it independent
    of any ~/.penumbra/profile.json."""
    if name in _REGISTRY:
        raise ValueError(f"duplicate job name {name!r}")
    if not (0 < int(budget_s) <= _MAX_JOB_BUDGET_S):
        raise ValueError(f"job {name!r} budget_s={budget_s} outside (0, {_MAX_JOB_BUDGET_S}]")
    sched = parse_schedule(schedule)   # raises ValueError on garbage -> loud at registration
    row = JobRow(name=name, schedule=sched, fn=fn, enabled=enabled, budget_s=int(budget_s))
    _REGISTRY[name] = row
    return row


def registry() -> "dict[str, JobRow]":
    """The live registry dict (name -> JobRow). Read-only use; the smoke's registry tripwire walks
    it to assert every shipped row's fn is callable + its schedule parsed."""
    return _REGISTRY


def _profile_jobs() -> dict:
    """The profile's ``jobs`` override section: {"<name>": false|true}. Read via the profile module
    (fail-open to {} on a missing/corrupt profile, exactly like every other profile facet), so a
    deployer can disable/enable a job by DATA without a code change. Absent -> {} -> shipped
    defaults win."""
    try:
        from penumbra.core import profile
        v = profile._load().get("jobs")
        return v if isinstance(v, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- a bad profile never changes the shipped defaults
        log.debug("profile jobs override unreadable (%s) -> shipped defaults", exc)
        return {}


def job_enabled(row: JobRow, overrides: Optional[dict] = None) -> bool:
    """Whether ``row`` runs, honoring the profile override: an explicit jobs[name] value WINS over
    the shipped default; absent -> the shipped default. ``overrides`` lets the tick read the profile
    once per pass (and lets tests inject a dict)."""
    ov = _profile_jobs() if overrides is None else overrides
    if row.name in ov:
        return bool(ov[row.name])
    return bool(row.enabled)


# ── persisted last-run + bark-cooldown state (atomic, the SensorStore idiom) ──────────────────────
_STATE_LOCK = threading.Lock()


def _load_state() -> dict:
    """{name: {"last_run": epoch, "last_bark": epoch}}. Missing/corrupt -> {} (a lost last-run map at
    worst re-runs due jobs once; never a crash)."""
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler-state.json unreadable (%s) -> empty", exc)
        return {}


def _save_state(state: dict) -> None:
    """Atomic tmp+replace (a kill mid-write can never truncate the live file)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _touch_heartbeat() -> None:
    """Bump the dead-man file's mtime (create it if absent). The external sentinel compares its age
    to 2x the tick interval to detect a wedged scheduler while /healthz still answers."""
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 -- a heartbeat write failure must never kill the tick
        log.debug("heartbeat touch failed (%s)", exc)


def due_jobs(now: float, state: dict, overrides: Optional[dict] = None) -> list[JobRow]:
    """PURE-ish (reads the registry + the passed state/overrides): the ENABLED jobs whose schedule
    is due at ``now``. Deterministic order = registration order (dict insertion order), so the
    serial run order is stable + testable."""
    out: list[JobRow] = []
    for row in _REGISTRY.values():
        if not job_enabled(row, overrides):
            continue
        last = (state.get(row.name) or {}).get("last_run")
        last_f = float(last) if isinstance(last, (int, float)) else None
        if is_due(row.schedule, now, last_f):
            out.append(row)
    return out


def run_due_jobs(now: Optional[float] = None) -> dict:
    """The tick BODY (call it directly in tests; the loop calls it every TICK_SECONDS): touch the
    heartbeat, then run every due enabled job SERIALLY. Each job runs in its own try/except with a
    per-job elapsed log line; a failing job NEVER stops the rest and Barks on exception (24h cooldown
    per job, cooldown state in scheduler-state.json). Records each run's last_run only on the jobs
    that RAN (attempted). Returns {"checked", "ran", "failed"}."""
    now = time.time() if now is None else now
    _touch_heartbeat()
    with _STATE_LOCK:
        state = _load_state()
    overrides = _profile_jobs()
    due = due_jobs(now, state, overrides)
    ran: list[str] = []
    failed: list[str] = []
    for row in due:
        # Heartbeat BEFORE every job: the dead-man staleness is bounded by ONE job's budget, never
        # the serial sum of a whole tick (else a legitimately long tick reads as a wedged scheduler
        # and the sentinel kickstarts the organ mid-run).
        _touch_heartbeat()
        t0 = time.monotonic()
        outcome, exc = _run_with_budget(row)
        elapsed = time.monotonic() - t0
        # last_run advances on EVERY outcome: a job that raises or overruns each run must not
        # re-fire on every 900s tick (spam + starvation); its own schedule governs the retry, and
        # the Bark (cooldown-gated) surfaces the breakage.
        state.setdefault(row.name, {})["last_run"] = now
        if outcome == "ok":
            ran.append(row.name)
            log.info("job %s ok in %.1fs", row.name, elapsed)
        elif outcome == "timeout":
            failed.append(row.name)
            log.error("job %s exceeded its %ds budget (tick moved on; the run continues as a "
                      "harmless daemon zombie)", row.name, row.budget_s)
            _bark_job_failure(row, state, now, kind="overran its budget")
        else:
            failed.append(row.name)
            log.error("job %s FAILED in %.1fs: %r", row.name, elapsed, exc)
            _bark_job_failure(row, state, now)
    if ran or failed:
        with _STATE_LOCK:
            # merge our last_run updates into whatever is on disk now (a manual run could have
            # touched a different key), then persist.
            disk = _load_state()
            for name in ran + failed:
                disk.setdefault(name, {}).update(state.get(name, {}))
            _save_state(disk)
        log.info("scheduler tick: checked %d, ran %d, failed %d", len(due), len(ran), len(failed))
    return {"checked": len(due), "ran": ran, "failed": failed}


def _run_with_budget(row: JobRow) -> "tuple[str, object]":
    """Run one job on a disposable worker thread, bounded by ``row.budget_s`` wall-clock. Returns
    ``("ok"|"failed"|"timeout", exc_or_None)``. Python threads cannot be killed, so a timed-out fn
    keeps running as a daemon ZOMBIE (harmless: every shipped core is fail-open and internally
    bounded), but the TICK moves on: one slow job degrades to one skipped job, never a frozen
    fleet, and the heartbeat keeps beating between jobs so the sentinel's dead-man stays quiet."""
    holder: list = []
    def _call() -> None:
        try:
            row.fn()
        except BaseException as exc:  # noqa: BLE001 -- carried to the caller across the thread
            holder.append(exc)
    t = threading.Thread(target=_call, name=f"job-{row.name}", daemon=True)
    t.start()
    t.join(row.budget_s)
    if t.is_alive():
        return "timeout", None
    if holder:
        return "failed", holder[0]
    return "ok", None


def _bark_job_failure(row: JobRow, state: dict, now: float, kind: str = "raised") -> None:
    """Bark ONE alert that a job raised/overran, at most once per JOB_BARK_COOLDOWN_S per job (the
    cooldown stamp lives in scheduler-state.json beside last_run). Fail-open via notify.bark_push."""
    last_bark = (state.get(row.name) or {}).get("last_bark")
    if isinstance(last_bark, (int, float)) and (now - last_bark) < JOB_BARK_COOLDOWN_S:
        return
    state.setdefault(row.name, {})["last_bark"] = now
    try:
        from penumbra.core import notify
        notify.bark_push(f"eye job failed: {row.name}",
                         f"job '{row.name}' ({row.schedule.raw}) {kind} in the in-process "
                         f"scheduler; check the eye-http log.", group="Penumbra")
    except Exception as exc:  # noqa: BLE001 -- the alert is best-effort; never break the tick
        log.debug("job-failure bark swallowed (%s)", exc)


# ── the ONE daemon loop (absorbs the P6 sensors-only loop; same two guards) ───────────────────────
_scheduler_started = False
_scheduler_lock = threading.Lock()


def start_scheduler(interval_s: int = TICK_SECONDS,
                    initial_delay_s: int = INITIAL_DELAY_S) -> Optional[threading.Thread]:
    """Start the daemon scheduler thread (``sleep(initial_delay_s); while True: run_due_jobs;
    sleep(interval_s)``). Registers the shipped rows first (idempotent).

    Two guards make this the ONE self-maintenance path (unchanged from P6): (1) it REFUSES to start
    unless ``writer.WRITES_ENABLED`` is truthy (the scheduler only ever belongs in the writer
    process -- a cron / smoke / CLI import leaves it False, so no memory-less path ticks); (2) a
    module idempotence flag so a double call cannot start two threads. Returns the Thread it started,
    or None when a guard refused. Import the writer INSIDE so a smoke import that never enables writes
    still sees the refusal, not an import-order surprise."""
    global _scheduler_started
    try:
        from penumbra.core.recall import writer
        writes_on = bool(writer.WRITES_ENABLED)
    except Exception as exc:  # noqa: BLE001 -- cannot read the gate -> treat as OFF, refuse to start
        log.warning("job scheduler: cannot read WRITES_ENABLED (%s); not starting", exc)
        return None
    if not writes_on:
        log.warning("job scheduler: WRITES_ENABLED is off; not starting "
                    "(the scheduler only ever belongs in the writer process)")
        return None
    with _scheduler_lock:
        if _scheduler_started:
            return None
        _scheduler_started = True

    register_shipped_jobs()

    def _loop() -> None:
        time.sleep(max(0, initial_delay_s))
        while True:
            try:
                run_due_jobs()
            except Exception:  # noqa: BLE001 -- a tick must never kill the loop
                log.exception("job scheduler tick crashed; continuing")
            time.sleep(max(1, interval_s))

    t = threading.Thread(target=_loop, name="job-scheduler", daemon=True)
    t.start()
    return t


_shipped_registered = False


def register_shipped_jobs() -> None:
    """Register every SHIPPED job row exactly once (idempotent). Row #1 is the P6 sensor tick,
    absorbed here unchanged (its own Bark-on-new logic stays inside sensor.run_sensor /
    _bark_new_results). The rest are the transplanted infra jobs (infra_jobs.py). Import the fns
    INSIDE so a bare ``import jobs`` (e.g. a unit test of the parser) does not drag in the whole eye.
    """
    global _shipped_registered
    if _shipped_registered:
        return
    _shipped_registered = True
    from penumbra.core import sensor
    from penumbra.core import infra_jobs

    # Row #1: the P6 sensor scheduler tick, now a job row (semantics unchanged).
    register_job("sensors", "every:900s", sensor.scheduler_tick_for_sensors)

    # Transplanted infra rows (see infra_jobs.py for each core's transplanted rationale + state).
    register_job("source-health", "daily@05:00", infra_jobs.run_source_health, budget_s=900)
    register_job("wewerss-probe", "every:1800s", infra_jobs.run_wewerss_probe)
    register_job("session-warmer", "daily@09:17,14:17,19:17", infra_jobs.run_session_warmer,
                 budget_s=900)
    register_job("log-rotation", "daily@04:50", infra_jobs.run_log_rotation)
    register_job("curator", "monthly@1-06:00", infra_jobs.run_curator, enabled=True,
                 budget_s=1500)
    register_job("source-audit", "weekly@sun-06:00", infra_jobs.run_source_audit, enabled=True,
                 budget_s=900)
    register_job("digest", "weekly@mon-09:00", infra_jobs.run_digest, enabled=False,
                 budget_s=900)
