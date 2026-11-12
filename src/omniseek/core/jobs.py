"""The in-process JOB REGISTRY + scheduler (P9): the ONE daemon loop that runs every scheduled
piece of OmniSeek's self-maintenance inside the writer process.

THE DERIVED ARCHITECTURE (P6 moved the fleet's center of gravity, P9 finishes the move): a run is
an act of PERCEPTION or self-maintenance and must land inside the ONE process that can write memory,
so anything that CAN die with the organ lives HERE as a declarative JOB ROW instead of an external
launchd cron. The test for what stays OUTSIDE the organ is "must this still run when the organ is
dead?" -- only the browsers that must not die with it (the CDP launchers) stay external. Restarting a dead or
hung organ is launchd's KeepAlive (a process that exits), the MCP front door's that forwards calls to OmniSeek
(a process alive but not answering: it kickstarts the organ when a forwarded call fails), and
this module's own wedge watchdog (a live process whose scheduler thread stopped). Everything else is a row
registered here.

This generalizes the P6 sensors-only scheduler (sensor.py) into a registry with a mechanical
schedule vocabulary. The sensor tick becomes job row #1; the transplanted health probe / session
warmer / browser reaper / log rotation / AIID snapshot become the other rows (see
infra_jobs.py). The graph_view / register_mints DNA
(a registry dict populated by a register_* call at import) is the shape here too.

THE RAZOR stays: the registry runs declared jobs mechanically on a schedule; WHICH jobs exist and
WHAT they do is judgment expressed in code + the profile override, never inferred here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from omniseek.core.contracts.build_identity import resolve_build_id
from omniseek.core.contracts.heartbeat import SchedulerHeartbeat, load_contract_artifacts
from omniseek import where

log = logging.getLogger(__name__)

# One persisted last-run map for ALL jobs (the SensorStore atomic tmp+replace idiom, one file keyed
# by job name). Distinct from sensors.json: sensors carry their own baselines; this only remembers
# each job's last_run epoch + its per-job Bark-on-exception cooldown stamp.
STATE_PATH = where.state() / "scheduler-state.json"
# The dead-man file the wedge watchdog below watches: the scheduler loop touches it every tick, so a
# stale mtime while the process still runs means "organ alive but scheduler dead". Kept beside the
# state file under <state>/.
HEARTBEAT_PATH = where.state() / "scheduler-heartbeat"

TICK_SECONDS = 900        # the loop wakes when the next job is due, and at least every 15 min (the heartbeat)
INITIAL_DELAY_S = 120     # keep deploy restart-storms from firing jobs mid-restart
# After a tick the loop sleeps until the earliest next run of an enabled job, at most a tick and at least
# LOOP_MIN_SLEEP_S. Until 11-04 it slept a fixed tick: cdp-reaper (every 600 s) really ran every 900 s, and a job not
# yet due at the first tick after a restart waited a whole extra tick (97.4 h of logs read:
# 11 gaps over two periods, all across restarts). 60 s is a tenth of the shortest registered period (600 s); it also
# bounds a job whose last_run cannot be saved to one rerun a minute instead of a busy loop.
LOOP_MIN_SLEEP_S = 60
JOB_BARK_COOLDOWN_S = 24 * 3600   # Bark at most once per 24h per job on repeated exceptions
# The wedge watchdog (until 10-20 an external watchdog script did this from outside): a heartbeat
# file older than WEDGE_FACTOR ticks means the scheduler thread wedged while the process lives on. The line is the
# old one, unchanged (900 s x 2 = 1800 s; the tick publishes the heartbeat before and after every job, and
# _MAX_JOB_BUDGET_S stays under the line). The watchdog then logs one line and exits the process with WEDGED_EXIT;
# launchd restarts it (eye-http's unit is KeepAlive = true, 10_deploy/nix/modules/agents.nix), which respawns the
# thread. WATCHDOG_CHECK_S only bounds how late after the line it notices (the external one looked every 600 s).
WEDGE_FACTOR = 2
WEDGED_EXIT = 75          # EX_TEMPFAIL: any non-zero code; launchd's KeepAlive restarts on every exit
WATCHDOG_CHECK_S = 60

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


def _next_slot(sched: Schedule, now: float, last_run: Optional[float] = None) -> Optional[float]:
    """The epoch of the NEXT firing STRICTLY AFTER ``now`` (the forward twin of _most_recent_slot),
    for the fleet-status display and for timing the loop's wake (seconds_to_next_run); it never decides
    what runs, is_due does. Interval: (last_run or now) + period,
    floored at now if already overdue. Calendar: the earliest future matching slot, or None if none in
    the next-month/8-day window. PURE except reading the local timezone via datetime."""
    if sched.kind == "interval":
        base = last_run if isinstance(last_run, (int, float)) else now
        return max(base + sched.seconds, now)
    now_dt = datetime.fromtimestamp(now)
    cands: list[float] = []
    if sched.kind == "daily":
        for day_off in (0, 1):
            base = datetime.fromtimestamp(
                now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + day_off * 86400)
            cands += [base.replace(hour=h, minute=m).timestamp() for (h, m) in sched.times]
    elif sched.kind == "weekly":
        h, m = sched.times[0]
        for day_off in range(0, 8):
            d = datetime.fromtimestamp(now_dt.timestamp() + day_off * 86400)
            if d.weekday() == sched.weekday:
                cands.append(d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp())
    elif sched.kind == "monthly":
        h, m = sched.times[0]
        y, mo = now_dt.year, now_dt.month
        for (yy, mm) in ((y, mo), (y + 1, 1) if mo == 12 else (y, mo + 1)):
            try:
                cands.append(datetime(yy, mm, sched.dom, h, m).timestamp())
            except ValueError:
                continue  # e.g. day 31 in a 30-day month -> no slot
    future = [c for c in cands if c > now]
    return min(future) if future else None


# ── the registry (the register_mints DNA: a dict populated by register_job at import) ──────────────
# Per-job wall-clock budget ceiling. The wedge watchdog declares the scheduler WEDGED when the
# heartbeat goes stale past its threshold (2x the 900s tick = 1800s); the tick publishes the
# heartbeat before and after EVERY job, so worst-case staleness is ONE job's budget, and every
# budget must therefore
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
    description: str = ""     # one-line human label for the fleet-status view (observability, no logic)
    process_entrypoint: Optional[tuple[str, str]] = None
    needs: None = None       # reserved (spec: needs: none) -- no dependency edges in P9


_REGISTRY: "dict[str, JobRow]" = {}


def register_job(name: str, schedule: str, fn: Callable[[], object], enabled: bool = True,
                 budget_s: int = 600, description: str = "",
                 process_entrypoint: Optional[tuple[str, str]] = None) -> JobRow:
    """Register one job row. Parses ``schedule`` NOW so an unknown/malformed spec raises ValueError
    AT REGISTRATION (import time), never a silent default. A duplicate ``name`` also raises (two
    rows fighting over one last-run key is a bug), as does a budget outside (0, _MAX_JOB_BUDGET_S]
    (a budget at or past the watchdog's wedge threshold would let one slow job read as a dead
    scheduler). Returns the stored JobRow. The profile override is NOT applied here (it is read
    live at tick time via ``job_enabled``), so tests can register a row and inspect it independent
    of any <root>/profile.json."""
    if name in _REGISTRY:
        raise ValueError(f"duplicate job name {name!r}")
    if not (0 < int(budget_s) <= _MAX_JOB_BUDGET_S):
        raise ValueError(f"job {name!r} budget_s={budget_s} outside (0, {_MAX_JOB_BUDGET_S}]")
    if process_entrypoint is not None:
        if (not isinstance(process_entrypoint, tuple) or len(process_entrypoint) != 2
                or not all(isinstance(part, str) and part for part in process_entrypoint)):
            raise ValueError(f"job {name!r} process_entrypoint must be (module, callable)")
    sched = parse_schedule(schedule)   # raises ValueError on garbage -> loud at registration
    row = JobRow(name=name, schedule=sched, fn=fn, enabled=enabled, budget_s=int(budget_s),
                 description=description, process_entrypoint=process_entrypoint)
    _REGISTRY[name] = row
    return row


def registry() -> "dict[str, JobRow]":
    """The live registry dict (name -> JobRow). Read-only use; every shipped row's fn must be
    callable + its schedule must parse."""
    return _REGISTRY


def _profile_jobs() -> dict:
    """The profile's ``jobs`` override section: {"<name>": false|true}. Read via the profile module
    (fail-open to {} on a missing/corrupt profile, exactly like every other profile facet), so a
    deployer can disable/enable a job by DATA without a code change. Absent -> {} -> shipped
    defaults win."""
    try:
        from omniseek.core import profile
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


def _publish_running_heartbeat() -> None:
    global _scheduler_contract_error
    heartbeat = _scheduler_heartbeat
    if heartbeat is None:
        return
    try:
        heartbeat.publish_running()
        _scheduler_contract_error = None
    except Exception as exc:  # noqa: BLE001 -- later heartbeat failure must not abort a job
        _scheduler_contract_error = type(exc).__name__
        log.warning("scheduler heartbeat publication failed (%s)", exc)


def _begin_heartbeat_tick() -> None:
    global _scheduler_contract_error
    heartbeat = _scheduler_heartbeat
    if heartbeat is None:
        return
    try:
        heartbeat.begin_tick()
        _scheduler_contract_error = None
    except Exception as exc:  # noqa: BLE001 -- later heartbeat failure must not abort the tick
        _scheduler_contract_error = type(exc).__name__
        log.warning("scheduler heartbeat tick publication failed (%s)", exc)


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


def fleet_status(now: Optional[float] = None) -> list[dict]:
    """The ONE observability surface for the background-job fleet: every shipped job with its
    schedule, EFFECTIVE enabled state (profile override honored), last-run + next-run (local ISO to
    the minute), budget, and one-line description. CHEAP (reads the registry + the last-run state file
    + the profile; NO live probe), so it can ride any diagnostics call. Answers 'what jobs exist, are
    they on, when did / do they run, what do they do' at a glance -- the thing a raw scheduler-state.json
    dump does NOT. A disabled job reports next_run=None (it will not fire). Registration order."""
    now = time.time() if now is None else now
    register_shipped_jobs()  # idempotent: ensure the shipped rows are present even on a cold import
    with _STATE_LOCK:
        state = _load_state()
    overrides = _profile_jobs()

    def _iso(ep) -> Optional[str]:
        return (datetime.fromtimestamp(ep).isoformat(timespec="minutes")
                if isinstance(ep, (int, float)) else None)

    out: list[dict] = []
    for name, row in _REGISTRY.items():
        en = job_enabled(row, overrides)
        last = (state.get(name) or {}).get("last_run")
        last_f = float(last) if isinstance(last, (int, float)) else None
        nxt = _next_slot(row.schedule, now, last_f) if en else None
        out.append({
            "name": name,
            "schedule": row.schedule.raw,
            "enabled": en,
            "last_run": _iso(last_f),
            "next_run": _iso(nxt),
            "budget_s": row.budget_s,
            "desc": row.description,
        })
    return out


def seconds_to_next_run(now: Optional[float] = None, cap_s: float = TICK_SECONDS) -> float:
    """How long the loop sleeps after a tick: until the earliest next run of an enabled job (_next_slot over
    the last-run state), at least LOOP_MIN_SLEEP_S and at most ``cap_s`` (the heartbeat must stay under the
    wedge line). Only times the wake; is_due still decides what runs. A cap below LOOP_MIN_SLEEP_S wins."""
    now = time.time() if now is None else now
    with _STATE_LOCK:
        state = _load_state()
    overrides = _profile_jobs()
    nexts: list[float] = []
    for name, row in _REGISTRY.items():
        if not job_enabled(row, overrides):
            continue
        last = (state.get(name) or {}).get("last_run")
        nxt = _next_slot(row.schedule, now, float(last) if isinstance(last, (int, float)) else None)
        if nxt is not None:
            nexts.append(nxt)
    wait = (min(nexts) - now) if nexts else cap_s
    return min(cap_s, max(LOOP_MIN_SLEEP_S, wait))


def run_due_jobs(now: Optional[float] = None) -> dict:
    """The tick BODY (call it directly in tests; the loop calls it every TICK_SECONDS): publish the
    structured heartbeat, then run every due enabled job SERIALLY. Each job runs in its own
    try/except with a per-job elapsed log line; a failing job NEVER stops the rest and Barks on
    exception (24h cooldown per job, cooldown state in scheduler-state.json). Records each run's
    last_run only on the jobs that RAN (attempted). Returns {"checked", "ran", "failed"}."""
    now = time.time() if now is None else now
    _begin_heartbeat_tick()
    with _STATE_LOCK:
        state = _load_state()
    overrides = _profile_jobs()
    due = due_jobs(now, state, overrides)
    ran: list[str] = []
    failed: list[str] = []
    for row in due:
        # Heartbeat BEFORE every job: the dead-man staleness is bounded by ONE job's budget, never
        # the serial sum of a whole tick (else a legitimately long tick reads as a wedged scheduler
        # and the wedge watchdog restarts the organ mid-run).
        _publish_running_heartbeat()
        t0 = time.monotonic()
        outcome, exc = _run_with_budget(row)
        elapsed = time.monotonic() - t0
        _publish_running_heartbeat()
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
    _publish_running_heartbeat()
    return {"checked": len(due), "ran": ran, "failed": failed}


def _terminate_process_group(process: subprocess.Popen) -> None:
    """Terminate an isolated job and all descendants, escalating after a short grace period."""
    try:
        if os.name == "nt":
            if process.poll() is None:
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error("isolated job process %s did not exit after SIGKILL", process.pid)


def _run_isolated_with_budget(row: JobRow) -> "tuple[str, object]":
    """Run a process-isolated job with a killable wall-clock budget."""
    if row.process_entrypoint is None:
        raise ValueError(f"job {row.name!r} has no process entrypoint")
    module, callable_name = row.process_entrypoint
    command = [
        sys.executable,
        "-m",
        "omniseek.core.job_runner",
        module,
        callable_name,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        return "failed", exc
    try:
        returncode = process.wait(timeout=row.budget_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return "timeout", None
    if returncode == 0:
        return "ok", None
    return "failed", RuntimeError(
        f"isolated job {row.name!r} exited with status {returncode}"
    )


def _run_with_budget(row: JobRow) -> "tuple[str, object]":
    """Run one job with a bounded wall-clock budget.

    Jobs with ``process_entrypoint`` run in a new process group that can be terminated on timeout.
    Legacy in-process jobs retain the historical daemon-thread path until they are individually
    migrated, so one isolated job cannot change the semantics of the rest of the fleet.
    """
    if row.process_entrypoint is not None:
        return _run_isolated_with_budget(row)
    holder: list = []
    def _call() -> None:
        try:
            row.fn()
        except BaseException as exc:  # noqa: BLE001 -- carried to the caller across the thread
            if isinstance(exc, asyncio.CancelledError):
                raise  # D11: never eat a cancellation
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
    cooldown stamp lives in scheduler-state.json beside last_run). Fail-open via notify.alert."""
    last_bark = (state.get(row.name) or {}).get("last_bark")
    if isinstance(last_bark, (int, float)) and (now - last_bark) < JOB_BARK_COOLDOWN_S:
        return
    state.setdefault(row.name, {})["last_bark"] = now
    try:
        from omniseek.core import notify
        notify.alert(f"eye job failed: {row.name}",
                         f"job '{row.name}' ({row.schedule.raw}) {kind} in the in-process "
                         f"scheduler; check OmniSeek-http log.", group="OmniSeek")
    except Exception as exc:  # noqa: BLE001 -- the alert is best-effort; never break the tick
        log.debug("job-failure bark swallowed (%s)", exc)


# ── the ONE daemon loop (absorbs the P6 sensors-only loop; same two guards) ───────────────────────
_scheduler_started = False
_scheduler_lock = threading.Lock()
_scheduler_heartbeat: Optional[SchedulerHeartbeat] = None
_scheduler_contract_error: Optional[str] = None
# S2 graceful-shutdown stop Event: set on ASGI-lifespan shutdown so the scheduler wakes out of its
# initial-delay / tick wait immediately (see omniseek.core.lifecycle). Additive: the loop behaves
# identically until this is set, which only happens on shutdown.
_STOP = threading.Event()


def scheduler_contract_status() -> dict:
    heartbeat = _scheduler_heartbeat
    if heartbeat is None:
        return {
            "state": "not_started",
            "error_class": _scheduler_contract_error,
            "last_emitted_at_utc": None,
            "build_id": None,
            "schema_digest": None,
            "phase": None,
            "scheduler_generation": None,
        }
    phase = "running" if heartbeat.tick_seq >= 1 else "starting"
    return {
        "state": phase,
        "error_class": _scheduler_contract_error,
        "last_emitted_at_utc": heartbeat.last_emitted_at_utc,
        "build_id": heartbeat.build_id,
        "schema_digest": heartbeat.artifacts.schema_digest,
        "phase": phase,
        "scheduler_generation": heartbeat.generation,
    }


def _read_host_boot_id() -> str:
    """macOS boot-session UUID, else the Linux kernel boot_id (docker / any public checkout)."""
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True,
            check=False,
            text=True,
        )
        boot_id = result.stdout.strip()
        if result.returncode == 0 and boot_id:
            return boot_id
    except OSError:
        pass
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            boot_id = fh.read().strip()
        if boot_id:
            return boot_id
    except OSError:
        pass
    raise RuntimeError("cannot read a host boot identity (no macOS sysctl, no Linux boot_id)")


def _make_scheduler_heartbeat(*, interval_s: int, initial_delay_s: int) -> SchedulerHeartbeat:
    artifacts = load_contract_artifacts(initial_delay_s=initial_delay_s, tick_interval_s=interval_s,
                                        max_job_budget_s=_MAX_JOB_BUDGET_S)
    return SchedulerHeartbeat(
        path=HEARTBEAT_PATH,
        artifacts=artifacts,
        build_id=resolve_build_id(),
        host_boot_id=_read_host_boot_id(),
        omniseek_pid=os.getpid(),
    )


def start_scheduler(interval_s: int = TICK_SECONDS,
                    initial_delay_s: int = INITIAL_DELAY_S) -> Optional[threading.Thread]:
    """Start the daemon scheduler thread (``sleep(initial_delay_s); while True: run_due_jobs; sleep until
    the next job is due, at most interval_s``). Registers the shipped rows first (idempotent).

    Two guards make this the ONE self-maintenance path (unchanged from P6): (1) it REFUSES to start
    unless ``writer.WRITES_ENABLED`` is truthy (the scheduler only ever belongs in the writer
    process -- a cron / CLI import leaves it False, so no memory-less path ticks); (2) a
    module idempotence flag so a double call cannot start two threads. Returns the Thread it started,
    or None when a guard refused. Import the writer INSIDE so an import that never enables writes
    still sees the refusal, not an import-order surprise."""
    global _scheduler_contract_error, _scheduler_heartbeat, _scheduler_started
    try:
        from omniseek.core.recall import writer
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
        register_shipped_jobs()
        try:
            heartbeat = _make_scheduler_heartbeat(
                interval_s=interval_s,
                initial_delay_s=initial_delay_s,
            )
            heartbeat.publish_starting()
            _scheduler_contract_error = None
        except Exception as exc:
            _scheduler_contract_error = type(exc).__name__
            raise

        def _loop() -> None:
            # S2: register so a graceful shutdown can drain this loop; wait on _STOP instead of sleeping
            # so a stop wakes it out of the initial delay / tick immediately. Additive: identical until
            # _STOP is set (only on shutdown).
            from omniseek.core import lifecycle
            lifecycle.register_loop("job-scheduler", _STOP, threading.current_thread())
            _STOP.wait(max(0, initial_delay_s))
            while not _STOP.is_set():
                try:
                    run_due_jobs()
                except Exception:  # noqa: BLE001 -- a tick must never kill the loop
                    log.exception("job scheduler tick crashed; continuing")
                try:
                    wait = seconds_to_next_run(cap_s=interval_s)
                except Exception:  # noqa: BLE001 -- timing the wake must never kill the loop
                    log.exception("job scheduler: next-run time failed; sleeping a tick")
                    wait = interval_s
                _STOP.wait(max(1, wait))

        t = threading.Thread(target=_loop, name="job-scheduler", daemon=True)
        _scheduler_heartbeat = heartbeat
        _scheduler_started = True
        t.start()
        start_wedge_watchdog()
        return t


def scheduler_wedged(now: float, heartbeat_mtime: Optional[float], started: float) -> bool:
    """PURE: is the scheduler wedged? No heartbeat file (None) is never a wedge: it is ambiguous with a fresh boot.
    Age counts from the later of the file's mtime and this process's start, so a stale file the previous process
    left behind does not count before this one had its chance to write."""
    if heartbeat_mtime is None:
        return False
    return now - max(float(heartbeat_mtime), float(started)) > TICK_SECONDS * WEDGE_FACTOR


def start_wedge_watchdog(path: Path = HEARTBEAT_PATH, started: Optional[float] = None,
                         check_s: float = WATCHDOG_CHECK_S, stop: Optional[threading.Event] = None,
                         exit_fn: Callable[[int], object] = os._exit) -> threading.Thread:
    """Start the daemon thread that exits the process (WEDGED_EXIT) once the scheduler heartbeat is stale
    (scheduler_wedged). It stops looking once ``stop`` (default the scheduler's _STOP) is set, so a graceful
    shutdown is never mistaken for a wedge."""
    started = time.time() if started is None else float(started)
    stop = _STOP if stop is None else stop

    def _watch() -> None:
        while not stop.wait(max(0.01, check_s)):
            try:
                mtime: Optional[float] = path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            except Exception:  # noqa: BLE001 -- a stat failure right now is not a confirmed wedge; look again
                log.warning("job scheduler watchdog: cannot stat %s; looking again next time", path)
                continue
            now = time.time()
            if scheduler_wedged(now, mtime, started):
                log.error("job scheduler WEDGED: heartbeat %s is %ds old (> %ds) while the process runs; "
                          "exiting %d so launchd restarts OmniSeek and the scheduler thread",
                          path, int(now - max(mtime, started)), TICK_SECONDS * WEDGE_FACTOR, WEDGED_EXIT)
                print("job scheduler wedged: heartbeat %s stale; exiting %d" % (path, WEDGED_EXIT),
                      file=sys.stderr, flush=True)
                exit_fn(WEDGED_EXIT)
                return

    t = threading.Thread(target=_watch, name="job-scheduler-watchdog", daemon=True)
    t.start()
    return t


_shipped_registered = False


def register_shipped_jobs() -> None:
    """Register every SHIPPED job row exactly once (idempotent). Row #1 is the P6 sensor tick,
    absorbed here unchanged (its own Bark-on-new logic stays inside sensor.arun_sensor /
    _bark_new_results). The rest are the transplanted infra jobs (infra_jobs.py). Import the fns
    INSIDE so a bare ``import jobs`` (e.g. a unit test of the parser) does not drag in the whole eye.
    """
    global _shipped_registered
    if _shipped_registered:
        return
    _shipped_registered = True
    from omniseek.core import sensor
    from omniseek.core import infra_jobs

    # Row #1: the sensor tick. With a resident-task table (sensor.sensor_table_path) it decides which
    # sensors run and when, read on every tick; without one each sensor keeps its own schedule. This
    # row only sets how often that is checked.
    register_job("sensors", "every:900s", sensor.scheduler_tick_for_sensors,
                 description="标准查询 + novelty 监控(配置了常驻任务表 SERVICES.tsv 时按表跑，否则按各 sensor 自己的 schedule)")

    # Transplanted infra rows (see infra_jobs.py for each core's transplanted rationale + state).
    # source-health: ONE job every 6h. Every run probes the non-CDP sources, so a dead open-API/RSS
    # source surfaces in ~6-12h (N_CONSECUTIVE=2); the CDP lane (heal + serial CDP probes) rides one
    # run a day (infra_jobs._cdp_lane_due). Budget is the full run's, the one that carries the CDP lane.
    # The schedule stays a literal (services.registered_jobs reads it off this file); 21600 is
    # infra_jobs.HEALTH_EVERY_S, which the lane cadence uses (test_health_state_scope pins the two).
    register_job("source-health", "every:21600s", infra_jobs.run_source_health,
                 budget_s=infra_jobs.HEALTH_BUDGET_S,
                 description="每 6h 探非 CDP 源健康,走浏览器(CDP)的源每天带一轮;标 down / 恢复 / 降级(watchdog down 的来源)")
    register_job("session-warmer", "daily@09:17,14:17,19:17", infra_jobs.run_session_warmer,
                 budget_s=900, description="每日暖 CDP 登录态(小红书 / 9222 论坛)",
                 process_entrypoint=("omniseek.core.infra_jobs", "run_session_warmer"))
    # On-demand CDP lifecycle (2026-08-29): the four browsers used to sit resident forever, holding
    # 3.1 GB on a 16 GB machine for ~24 calls a day. This row stops the idle ones; _cdp.ensure_browser
    # brings one back in ~1s on the next call. Process-isolated so it reads last-use stamps off disk.
    register_job("cdp-reaper", "every:600s", infra_jobs.run_cdp_reaper, budget_s=120,
                 description="每 10min 停掉空闲的 CDP 浏览器(下次调用 1s 内自动拉起),并删 Chrome 留下的签名克隆",
                 process_entrypoint=("omniseek.core.infra_jobs", "run_cdp_reaper"))
    register_job("log-rotation", "daily@04:50", infra_jobs.run_log_rotation,
                 description="每日转超大日志、删过期的缓存文件")
    # The NSERC cache prime (monthly) and the weekly reading list (weekly, with its briefing agent)
    # were withdrawn on 2026-10-20 with the NSERC source.
    # The AI Incident Database snapshot (2026-10-20). The upstream publishes one a
    # week on Mondays; the last 60 came out between 10:00 and 12:xx UTC, so 21:30 local (the host machine runs
    # on Beijing time, = 13:30 UTC) is the first slot after all of them. Process-isolated: it holds a
    # ~120 MB download and a full bzip2 pass. Budget: measured ~30-100 s under load on 2026-10-20.
    register_job("aiid-snapshot", "weekly@mon-21:30", infra_jobs.run_aiid_snapshot, budget_s=900,
                 description="每周一查 AI 事故库（AIID）快照页，有新的才下、核完整、建本地索引并换上（ai_incidents 源）",
                 process_entrypoint=("omniseek.core.infra_jobs", "run_aiid_snapshot"))
