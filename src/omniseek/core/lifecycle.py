"""S2 graceful-shutdown contract: a central stop/drain registry for OmniSeek's forever-loops, plus
the ASGI-lifespan composition that drains them on shutdown.

LEAF module: it imports ONLY the stdlib (threading / logging / time / contextlib) and nothing from
omniseek, so every loop module can import it without a cycle. The registry lets serve_http's shutdown
drain every background loop uniformly, without any loop having to know about the others.

Why this exists (S2 of the async-core migration): today serve_http is SIGKILLed on a deploy restart,
so the recall single-writer queue can lose queued writes mid-flush. Each forever-loop now carries a
module-level stop Event and registers here when it starts; on shutdown the composed ASGI lifespan
calls drain_all(), which sets every stop Event (so a loop parked in _STOP.wait wakes immediately)
and joins the threads within a bounded budget, giving the recall writer a chance to FINAL-FLUSH its
queue first. The composition is FAIL-SAFE: it delegates startup + shutdown to FastMCP's own lifespan
UNCHANGED, and any drain error is logged, never raised (a drain fault must not corrupt the shutdown
handshake). The stop machinery is purely ADDITIVE: a loop behaves identically until its stop is set,
which only ever happens on shutdown.

Shutdown cap (2026-10-11): uvicorn awaits the lifespan shutdown with NO timeout (its
timeout_graceful_shutdown only bounds the connection wait), and twice the process stalled right after
"Waiting for application shutdown." without ever reaching the drain log line (10-10 15:55 for over
3 minutes, 10-11 12:25 long enough to roll back a deploy). arm_shutdown_watchdog bounds that segment
from the moment it starts until the process is gone: past the cap it logs ONE WARNING naming the
phase reached and every thread still alive, dumps every thread's stack, and forces the exit. A
faulthandler backstop BACKSTOP_EXTRA_S later does the same from a C thread, for the case where the
GIL itself is held and no Python thread (the watchdog included) can run.

Drain cap (2026-10-11, eye-drain-cap): the segment BEFORE that, from SIGTERM until uvicorn has waited
for the open connections and running requests, is capped too. uvicorn's timeout_graceful_shutdown
(DRAIN_CAP_S_DEFAULT) bounds it cooperatively (cancels the requests still running and moves on to the
lifespan shutdown); arm_drain_watchdog, called from the server's signal handler, bounds it hard, for
the case where the event loop is blocked and that timeout can never fire. The two stages share one
watchdog thread and one clock from SIGTERM, so SIGTERM to exit is at most drain cap + shutdown cap +
BACKSTOP_EXTRA_S (30 + 30 + 5 = 65 s by default).

Every stop signal uvicorn handles (its HANDLED_SIGNALS: SIGINT, SIGTERM, plus SIGBREAK on Windows)
arms the drain cap, and the logs name the signal that arrived. On Windows a Python signal handler
cannot arm it while the event loop is blocked: SIGBREAK does not interrupt a blocking call on the
main thread (time.sleep there wakes for SIGINT only), so the handler runs only after the call
returns. There the drain cap is ALSO armed from a console control handler, which Windows runs on a
thread of its own the moment Ctrl+Break / Ctrl+C / console close arrives (_win_console_ctrl_arm).
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# (name, stop_event, thread-or-None, drain_last). Appended (or replaced by name) under _lock as each
# loop starts; read on shutdown by drain_all. A None thread means "no handle to join" (drain still
# sets its stop, it just cannot wait on it). drain_last=True marks a loop that must be joined AFTER
# every producer (the recall writer, which owns unflushed writes) so nothing enqueues past its flush.
_stops: "list[tuple[str, threading.Event, Optional[threading.Thread], bool]]" = []
_lock = threading.Lock()


def register_loop(name: str, stop_event: "threading.Event",
                  thread: "Optional[threading.Thread]" = None, drain_last: bool = False) -> None:
    """Register one forever-loop so drain_all can stop + join it on shutdown. Each loop calls this at
    start with its name + its module-level stop Event + its own thread handle. A repeat registration
    of the same name REPLACES the prior entry (a loop that ever restarts does not leave a stale
    dead-thread row behind).

    drain_last=True marks the loop that must be joined LAST (the recall writer, which owns the
    unflushed write queue): drain_all joins every producer first, then the drain_last loop(s), so no
    producer can enqueue after the writer's FINAL FLUSH. Producers leave it False (the default)."""
    with _lock:
        for i, (existing, _ev, _t, _dl) in enumerate(_stops):
            if existing == name:
                _stops[i] = (name, stop_event, thread, drain_last)
                return
        _stops.append((name, stop_event, thread, drain_last))


def request_stop_all() -> None:
    """Set EVERY registered stop Event. A loop parked in _STOP.wait(interval), or polling its queue
    with a bounded get, wakes immediately, so shutdown never has to wait a full interval. This does
    not join anything (that is drain_all's job); it only requests the stop."""
    with _lock:
        entries = list(_stops)
    for name, ev, _t, _dl in entries:
        try:
            ev.set()
        except Exception as exc:  # noqa: BLE001 -- one bad Event never blocks stopping the rest
            log.warning("lifecycle: could not set stop for %s: %s", name, exc)


def drain_all(timeout_total: float = 5.0) -> dict:
    """Graceful drain on shutdown: set every stop Event, then join each registered thread within a
    SHARED time budget. Returns {"drained": [names that exited], "running": [names still alive]} and
    logs the split. FAIL-OPEN: it never raises (the caller runs it in a shutdown ``finally`` and a
    drain fault must not corrupt shutdown). The threads are daemons, so a still-running one dies with
    the process anyway; the join only gives each loop the chance to finish its FINAL FLUSH first."""
    request_stop_all()
    with _lock:
        entries = list(_stops)
    deadline = time.monotonic() + max(0.0, timeout_total)
    drained: list[str] = []
    running: list[str] = []
    # Join every producer loop FIRST, then the drain_last loop(s) (the recall writer) LAST, so no
    # producer can enqueue past the writer's final flush. All stops were already set up front by
    # request_stop_all, so this only sequences the JOINS; it never delays a stop. Registration order
    # is preserved within each group. The SHARED budget is unchanged: the drain_last loop simply gets
    # whatever remains after the producers are joined.
    ordered = [e for e in entries if not e[3]] + [e for e in entries if e[3]]
    for name, _ev, thread, _dl in ordered:
        if thread is None or not thread.is_alive():
            drained.append(name)
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            thread.join(timeout=remaining)
        except Exception as exc:  # noqa: BLE001 -- a join fault never aborts draining the rest
            log.warning("lifecycle: join(%s) errored: %s", name, exc)
        (drained if not thread.is_alive() else running).append(name)
    if running:
        log.warning("lifecycle drain: %d drained %s, %d STILL RUNNING %s (within %.1fs budget)",
                    len(drained), drained, len(running), running, timeout_total)
    else:
        log.info("lifecycle drain: all %d loop(s) drained %s", len(drained), drained)
    return {"drained": drained, "running": running}


# PROVISIONAL. Seconds the lifespan shutdown (our drain, FastMCP's session-manager shutdown, then
# uvicorn's and the interpreter's teardown) may take before the watchdog forces the exit.
# Derivation (2026-10-11, eye-shutdown-cap report section 3): a healthy shutdown is bounded by the
# drain budget (drain_all, 5 s) plus the longest wait the session manager can legitimately have, a
# tool body still running in an AnyIO worker thread that cancellation cannot interrupt (memguard's
# restart drill measured 17 s for one transcription), plus the rest of the teardown (measured 0 to
# 2 s after the drain line): 5 + 17 + 2 = 24 s, rounded up to 30 s, the same value as
# memguard.EXIT_GRACE_S. Measured: in the 9 normal shutdowns logged since 10-10, "Waiting for
# application shutdown." to "Finished server process" took at most 0 to 4 s (lower bound) and 2 to 8
# s (upper bound, the log stamps are coarse), so 30 s is about 4x the slowest. The deploy health wait
# is 90 s and a new process is ready 2 to 5 s after the old one exits, so a capped stall can no longer
# fail a deploy. Override with OMNISEEK_SHUTDOWN_CAP_S (0 disables the cap and the backstop).
# Calibration plan: every shutdown now logs "lifecycle: lifespan shutdown done in N s" (uvicorn's own
# "Finished server process" line follows it; no atexit hook can add a later stamp, since uvicorn
# re-raises the SIGTERM it caught and the default action ends the process); after 20 restarts compare
# the largest N with the cap, and treat each "shutdown cap hit" WARNING as a stall to explain from its stack dump (lower the
# cap only if no legitimate shutdown comes near it, raise it if a legitimate one is cut).
SHUTDOWN_CAP_S_DEFAULT = 30.0
# PROVISIONAL. Extra seconds before the faulthandler backstop fires. It only matters when the Python
# watchdog cannot run or cannot finish (GIL held by native code, a logging lock never released), and
# the watchdog's own report (one log line, a stack dump, a flush) takes well under a second, so 5 s
# leaves it room. Calibration: a "Timeout (0:00:35)!" faulthandler dump in the log without the
# watchdog's WARNING before it means the backstop was needed; keep it then, else it never fired.
BACKSTOP_EXTRA_S = 5.0
# PROVISIONAL. Seconds uvicorn may wait, after SIGTERM, for the open connections and the running
# requests to finish (uvicorn's timeout_graceful_shutdown); past it uvicorn cancels the requests still
# running ("Cancel N running task(s), timeout graceful shutdown exceeded") and starts the lifespan
# shutdown. What it waits on is in-flight MCP tool calls.
# Derivation (2026-10-11, eye-drain-cap report section 2): over the 10 deploy restarts logged
# 10-10..10-11 (SIGTERM = the mtime of the deploy receipt written just before `launchctl kickstart
# -k`; end of the wait = the log stamp next to "Waiting for application shutdown.") the wait was at
# most 2 to 6 s, once at most 10 s (coarse stamps). The "about 26 s" in OmniSeek-shutdown-cap report
# was measured from the last log line BEFORE the SIGTERM; that restart waited at most 4 s. A broad
# omniseek_search ends at its 11 s source deadline (16 s with fresh=True). 30 s is 3x the coarse worst
# case, 5x the usual maximum, longer than a full broad search, and the same value as
# SHUTDOWN_CAP_S_DEFAULT and memguard.EXIT_GRACE_S. Upper limit: the deploy health wait (90 s) must
# cover drain cap + shutdown cap + backstop + a new process getting ready (2 to 5 s), so at most 50.
# Cost: a call still running 30 s into a restart (a named-source drill up to 45 s, omniseek_gather up to
# 60 s, a transcription) is cancelled, as a memguard hard restart already does.
# Override with OMNISEEK_DRAIN_CAP_S (0 turns off both uvicorn's timeout and the hard drain watchdog).
# Calibration plan: every signalled shutdown now logs "lifecycle: SIGTERM to lifespan shutdown N s"
# (the connection wait plus uvicorn's 0.1 s pause), and every cut call shows up as uvicorn's "Cancel N
# running task(s)" line; after 20 restarts compare the largest N with the cap, and look up what each
# cancelled call was (raise the cap if routine calls are cut, lower it toward 3x the largest N if
# none comes near).
DRAIN_CAP_S_DEFAULT = 30.0
# PROVISIONAL. Seconds past the drain cap before the HARD drain watchdog fires. uvicorn's own timeout
# moves on to the lifespan shutdown within milliseconds of the cap when its event loop runs (log one
# line, cancel the tasks); this grace only gives that cooperative path priority, so the hard watchdog
# fires only when the loop is blocked. It does not change the overall ceiling: the lifespan stage that
# follows ends at most drain cap + shutdown cap after SIGTERM whatever this is. Calibration: a "shutdown
# cap hit" WARNING in the connections stage right after a "Cancel N running task(s)" line would mean
# the grace is too short for a healthy loop; raise it then.
DRAIN_GRACE_S = 5.0

_wd_lock = threading.Lock()


def _wd_reset() -> dict:
    return {"stage": None, "t_signal": None, "signal": None, "drain_s": None, "t0": None,
            "deadline": None, "phase": "running", "cancel": None, "wake": None, "thread": None,
            "exit_fn": os._exit, "backstop": False, "announce": False}


# stage: None (not shutting down), "connections" (SIGTERM seen, uvicorn waiting for connections and
# requests) or "lifespan" (the lifespan shutdown and the teardown after it). deadline is monotonic.
_wd_state: dict = _wd_reset()


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("lifecycle: %s=%r is not a number; using %.0f", name, raw, default)
        return default


def shutdown_cap_s() -> float:
    """The cap in seconds from OMNISEEK_SHUTDOWN_CAP_S, else SHUTDOWN_CAP_S_DEFAULT; 0 disables."""
    return _env_seconds("OMNISEEK_SHUTDOWN_CAP_S", SHUTDOWN_CAP_S_DEFAULT)


def drain_cap_s() -> float:
    """The drain cap in seconds from OMNISEEK_DRAIN_CAP_S, else DRAIN_CAP_S_DEFAULT; 0 disables."""
    return _env_seconds("OMNISEEK_DRAIN_CAP_S", DRAIN_CAP_S_DEFAULT)


def set_shutdown_phase(phase: str) -> None:
    """Record which shutdown step is running, so a cap hit can say where the shutdown stopped."""
    _wd_state["phase"] = phase


def _still_running() -> "tuple[list[str], list[str]]":
    """(registered loops whose thread is alive, every other live thread as 'name[daemon]')."""
    got = _lock.acquire(timeout=1.0)   # never let the report itself hang on the registry lock
    try:
        entries = list(_stops)
    finally:
        if got:
            _lock.release()
    loops = [n for n, _ev, t, _dl in entries if t is not None and t.is_alive()]
    loop_threads = {t for _n, _ev, t, _dl in entries if t is not None}
    me = threading.current_thread()
    others = []
    for t in threading.enumerate():
        if t is me or t in loop_threads:
            continue
        others.append(f"{t.name}{'[daemon]' if t.daemon else ''}")
    return loops, others


def _flush_all() -> None:
    for h in logging.getLogger().handlers + log.handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):   # os._exit skips the stdio flush; the lines must land
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass


def _stderr_fd() -> Optional[int]:
    try:
        return sys.stderr.fileno()
    except Exception:  # noqa: BLE001 -- a captured / replaced stderr has no fd
        return None


def _fire() -> None:
    """The cap was hit: report, dump every thread's stack, force the exit."""
    st = _wd_state
    stage = st.get("stage")
    if stage == "connections":
        since, start = st.get("signal") or "SIGTERM", st.get("t_signal")
        where = ("uvicorn has not finished waiting for open connections and running requests "
                 f"(drain cap {st.get('drain_s') or 0:.0f}s + {DRAIN_GRACE_S:.0f}s grace)")
    else:
        since, start = "the lifespan shutdown began", st.get("t0")
        where = "the lifespan shutdown and the teardown after it have not finished"
    elapsed = time.monotonic() - start if start is not None else 0.0
    try:
        loops, others = _still_running()
        log.warning("lifecycle: shutdown cap hit: still alive %.0fs after %s, %s, stuck in phase %r; "
                    "registered loops still running %s; other live threads %s; dumping every "
                    "thread's stack, then forcing exit(1)",
                    elapsed, since, where, st.get("phase"), loops, others)
    except Exception as exc:  # noqa: BLE001 -- the exit below must happen regardless
        log.warning("lifecycle: shutdown cap hit %.0fs after %s (report failed: %s)",
                    elapsed, since, exc)
    _flush_all()
    fd = _stderr_fd()
    if fd is not None:
        try:
            faulthandler.dump_traceback(file=fd, all_threads=True)
        except Exception:  # noqa: BLE001
            pass
        try:   # a raw fd write needs no logging state, so the exit always leaves one line
            os.write(fd, f"lifecycle: forced exit(1) {elapsed:.0f}s after {since}\n".encode())
        except Exception:  # noqa: BLE001
            pass
    st.get("exit_fn", os._exit)(1)


def _watch(cancel: "threading.Event", wake: "threading.Event") -> None:
    """The one watchdog thread: sleep until the current deadline (re-read on every wake, since the
    lifespan stage replaces the connections stage's deadline), then fire."""
    while True:
        if cancel.is_set():
            return
        if _wd_state.get("announce"):   # the signal handler armed the drain stage; log for it here
            _wd_state["announce"] = False
            drain = _wd_state.get("drain_s") or 0.0
            log.info("lifecycle: drain cap armed (uvicorn waits up to %.0fs for connections, hard "
                     "cap %.0fs, backstop %s)", drain, drain + DRAIN_GRACE_S,
                     f"+{BACKSTOP_EXTRA_S:.0f}s" if _wd_state.get("backstop") else "off")
        deadline = _wd_state.get("deadline")
        if deadline is None:
            wake.wait()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wake.wait(remaining)
        wake.clear()   # cleared BEFORE the deadline is re-read, so a new deadline is never missed
    if not cancel.is_set():
        _fire()


def _ensure_thread() -> "threading.Thread":
    """Start the watchdog thread unless it is running. Call under _wd_lock, never from a signal
    handler (Thread.start takes threading's internal locks)."""
    t = _wd_state.get("thread")
    if t is not None and t.is_alive():
        return t
    cancel, wake = threading.Event(), threading.Event()
    t = threading.Thread(target=_watch, args=(cancel, wake), name="shutdown-watchdog", daemon=True)
    _wd_state.update(cancel=cancel, wake=wake, thread=t)
    t.start()
    return t


def _arm_backstop(seconds: float) -> bool:
    fd = _stderr_fd()
    if fd is None:
        return False
    faulthandler.dump_traceback_later(seconds, exit=True, file=fd)   # replaces an earlier one
    return True


def prepare_drain_watchdog(exit_fn: Callable[[int], None] = os._exit) -> bool:
    """Start the (idle) watchdog thread ahead of any signal, so arm_drain_watchdog, which runs inside
    the signal handler, only has to set a deadline and wake it. Returns True when ready. FAIL-OPEN."""
    try:
        with _wd_lock:
            _wd_state["exit_fn"] = exit_fn
            _ensure_thread()
        return True
    except Exception as exc:  # noqa: BLE001 -- the cap must never break the server it guards
        log.warning("lifecycle: could not prepare the drain watchdog: %s", exc)
        return False


def arm_drain_watchdog(drain_s: float, grace_s: float = DRAIN_GRACE_S, backstop: bool = True,
                       signame: str = "SIGTERM") -> bool:
    """Arm the connections stage: called from the server's signal handler (and on Windows from its
    console control handler), it gives the process ``drain_s + grace_s`` seconds to get from the
    signal ``signame`` to the lifespan shutdown (where
    arm_shutdown_watchdog takes over, on the same clock). Past that the watchdog thread reports and
    forces exit(1); with ``backstop`` faulthandler does the same BACKSTOP_EXTRA_S later from a C
    thread. uvicorn calls signal handlers through signal.signal, so this runs on the main thread
    between two bytecodes even while the event loop is blocked, which is exactly when uvicorn's own
    timeout_graceful_shutdown cannot fire (on POSIX a blocking call is interrupted for the handler;
    on Windows it is not, hence the console control handler there).

    SIGNAL-HANDLER SAFE by construction: no lock of this module, no logging, no thread start (the
    thread was started by prepare_drain_watchdog; it logs the "armed" line). Returns True when armed;
    False when the drain cap is off, a stage is already armed, or the thread was never prepared."""
    try:
        if drain_s <= 0 or _wd_state.get("stage") is not None:
            return False
        wake, t = _wd_state.get("wake"), _wd_state.get("thread")
        if wake is None or t is None or not t.is_alive():
            return False
        now = time.monotonic()
        limit = drain_s + max(0.0, grace_s)
        _wd_state.update(t_signal=now, signal=signame, drain_s=float(drain_s), stage="connections",
                         phase=f"{signame} received: uvicorn's main loop has not yet started the "
                               "shutdown (it checks every 0.1 s, so the event loop is busy)",
                         deadline=now + limit)
        if backstop:
            _wd_state["backstop"] = _arm_backstop(limit + BACKSTOP_EXTRA_S)
        _wd_state["announce"] = True
        wake.set()
        return True
    except Exception:  # noqa: BLE001 -- never raise out of a signal handler
        return False


def _drop_drain_stage() -> None:
    """With the shutdown cap off (cap 0), reaching the lifespan shutdown ends the connections stage
    with no deadline after it: clear its deadline and its backstop. FAIL-OPEN."""
    try:
        with _wd_lock:
            if _wd_state.get("stage") != "connections":
                return
            _wd_state.update(stage="lifespan", deadline=None)
            if _wd_state.get("backstop"):
                faulthandler.cancel_dump_traceback_later()
                _wd_state["backstop"] = False
            wake = _wd_state.get("wake")
            if wake is not None:
                wake.set()
    except Exception as exc:  # noqa: BLE001
        log.warning("lifecycle: could not drop the drain stage: %s", exc)


def arm_shutdown_watchdog(cap_s: Optional[float] = None,
                          exit_fn: Callable[[int], None] = os._exit,
                          backstop: bool = True) -> "Optional[threading.Thread]":
    """Arm the lifespan stage of the shutdown cap: if the process is still alive ``cap_s`` seconds
    later, the watchdog thread logs one WARNING naming the phase reached, the registered loops still
    running and every other live thread, dumps every thread's stack (faulthandler), and calls
    ``exit_fn(1)``. With ``backstop`` it also arms faulthandler.dump_traceback_later(<that deadline> +
    BACKSTOP_EXTRA_S, exit=True), which runs in a C thread and needs no GIL. When the connections
    stage was armed (arm_drain_watchdog), the deadline is also kept within drain cap + ``cap_s`` of
    the SIGTERM, so the whole shutdown stays within drain cap + cap + BACKSTOP_EXTRA_S, and the time
    since the SIGTERM is logged. With ``cap_s`` 0 (the cap off) it arms nothing and drops the
    connections stage's deadline, so the drain cap never cuts the lifespan short. Idempotent: a second call while the lifespan stage is armed returns
    None. It is NOT disarmed when the lifespan finishes: the teardown after it (asyncio.run closing the
    default executor, the interpreter joining non-daemon threads) can stall too, and a clean exit
    ends the process and the thread with it. FAIL-OPEN: an error here is logged, never raised."""
    cap = shutdown_cap_s() if cap_s is None else max(0.0, float(cap_s))
    if cap <= 0:
        _drop_drain_stage()   # the lifespan was reached; the drain deadline must not cut it short
        return None
    try:
        with _wd_lock:
            if _wd_state.get("stage") == "lifespan":
                return None
            now = time.monotonic()
            deadline = now + cap
            waited = None
            since = _wd_state.get("signal") or "SIGTERM"
            t_signal = _wd_state.get("t_signal")
            if _wd_state.get("stage") == "connections" and t_signal is not None:
                waited = now - t_signal
                deadline = min(deadline, t_signal + (_wd_state.get("drain_s") or 0.0) + cap)
            _wd_state.update(stage="lifespan", t0=now, deadline=deadline, exit_fn=exit_fn)
            t = _ensure_thread()
            if backstop:
                _wd_state["backstop"] = _arm_backstop(deadline - now + BACKSTOP_EXTRA_S)
            elif _wd_state.get("backstop"):   # the drain stage's backstop would fire too early
                faulthandler.cancel_dump_traceback_later()
                _wd_state["backstop"] = False
            _wd_state["wake"].set()
            has_backstop = _wd_state["backstop"]
        if waited is not None:
            log.info("lifecycle: %s to lifespan shutdown %.1fs (uvicorn's wait for connections)",
                     since, waited)
        log.info("lifecycle: shutdown cap armed (%.0fs, backstop %s)", deadline - now,
                 f"+{BACKSTOP_EXTRA_S:.0f}s" if has_backstop else "off")
        return t
    except Exception as exc:  # noqa: BLE001 -- the cap must never break the shutdown it guards
        log.warning("lifecycle: could not arm the shutdown cap: %s", exc)
        return None


def disarm_shutdown_watchdog() -> None:
    """Cancel the watchdog and the faulthandler backstop and reset the state (tests; a process that
    shuts down for real keeps them armed until it exits)."""
    with _wd_lock:
        for key in ("cancel", "wake"):
            ev = _wd_state.get(key)
            if ev is not None:
                ev.set()
        if _wd_state.get("backstop"):
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:  # noqa: BLE001
                pass
        _wd_state.clear()
        _wd_state.update(_wd_reset())


def cap_lifespan_shutdown(uvicorn_lifespan, cap_s: Optional[float] = None,
                          exit_fn: Callable[[int], None] = os._exit, backstop: bool = True) -> bool:
    """Arm the shutdown cap in the SAME synchronous step in which uvicorn logs "Waiting for application
    shutdown.", by wrapping the ``shutdown`` coroutine of uvicorn's lifespan object (``server.lifespan``,
    a LifespanOn). compose_lifespan arms too, but only once the event loop gets back to our lifespan
    code; a synchronous call blocking the loop thread first (the two stalls stopped right after that
    log line, before drain_all's) would keep that arming from ever running. Duck-typed, so this module
    stays free of a uvicorn import. Returns True when wrapped. FAIL-OPEN."""
    try:
        orig = uvicorn_lifespan.shutdown
    except Exception:  # noqa: BLE001
        return False

    async def _capped_shutdown(*a, **k):
        arm_shutdown_watchdog(cap_s, exit_fn=exit_fn, backstop=backstop)
        set_shutdown_phase("uvicorn: lifespan.shutdown sent, event loop not yet back in our lifespan "
                           "code (something else holds the loop thread)")
        return await orig(*a, **k)

    try:
        uvicorn_lifespan.shutdown = _capped_shutdown
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("lifecycle: could not wrap uvicorn's lifespan shutdown: %s", exc)
        return False


def capped_server_class(server_base, cap_s: Optional[float] = None,
                        exit_fn: Callable[[int], None] = os._exit, backstop: bool = True):
    """Return a subclass of ``server_base`` (uvicorn.Server, passed in so this module stays free of a
    uvicorn import) with both stages of the shutdown cap wired in:

    - connections stage: the first exit signal arms arm_drain_watchdog with the config's
      ``timeout_graceful_shutdown`` (uvicorn's own cooperative cap on the same wait; the caller sets
      it, drain_cap_s() in serve_http; None or 0 leaves this stage off); on Windows a console
      control handler registered at startup arms it too (see _win_console_ctrl_arm);
    - lifespan stage: cap_lifespan_shutdown(``cap_s``) at startup, as before.

    serve_http and the subprocess tests use this same class, so the tests run the production
    wiring. Only uvicorn's documented Server methods are overridden (handle_exit, startup, shutdown),
    each calling super()."""

    class CappedServer(server_base):
        def __init__(self, config, *args, **kwargs):
            super().__init__(config, *args, **kwargs)
            self._drain_s = float(getattr(config, "timeout_graceful_shutdown", None) or 0.0)
            self._drain_armed = False
            self._console_ctrl = None
            if self._drain_s > 0:
                prepare_drain_watchdog(exit_fn=exit_fn)

        def handle_exit(self, sig, frame):
            if not self._drain_armed:   # only the first signal; a second one must not touch any lock
                self._drain_armed = True
                arm_drain_watchdog(self._drain_s, backstop=backstop, signame=_signal_name(sig))
            super().handle_exit(sig, frame)

        async def startup(self, sockets=None):
            cap_lifespan_shutdown(self.lifespan, cap_s=cap_s, exit_fn=exit_fn, backstop=backstop)
            if self._drain_s > 0:   # uvicorn's own handlers are in place by now (capture_signals)
                self._console_ctrl = _win_console_ctrl_arm(_handled_signals(server_base),
                                                           self._drain_s, backstop)
            await super().startup(sockets=sockets)

        async def shutdown(self, sockets=None):
            if _wd_state.get("stage") == "connections":
                set_shutdown_phase("uvicorn: waiting for open connections and running requests "
                                   "(timeout_graceful_shutdown)")
            await super().shutdown(sockets=sockets)
            _win_console_ctrl_remove(getattr(self, "_console_ctrl", None))

    return CappedServer


def _signal_name(sig) -> str:
    try:
        return signal.Signals(sig).name
    except Exception:  # noqa: BLE001 -- runs inside a signal handler, must not raise
        return f"signal {sig}"


def _handled_signals(server_base) -> tuple:
    """The signals the server installs its handle_exit for: HANDLED_SIGNALS of the module defining
    ``server_base`` or a base of it (uvicorn.server). () when none is found, so nothing is armed for
    a signal the server may ignore."""
    for klass in getattr(server_base, "__mro__", ()):
        handled = getattr(sys.modules.get(klass.__module__), "HANDLED_SIGNALS", None)
        if handled is not None:
            return tuple(handled)
    return ()


# Windows console control events as the C runtime (UCRT) turns them into signals: CTRL_C_EVENT (0)
# into SIGINT, every other event (Ctrl+Break, console close, logoff, system shutdown) into SIGBREAK.
_CTRL_C_EVENT = 0
_console_ctrl_keep: list = []   # registered callbacks stay referenced: Windows may call one any time


def _win_console_ctrl_arm(handled: tuple, drain_s: float, backstop: bool):
    """Windows only: register a console control handler that arms the drain cap the moment a console
    event arrives whose signal is in ``handled``. Windows runs it on a thread of its own, so it arms
    even while the main thread is blocked in a call that SIGBREAK does not interrupt (there the
    Python signal handler, and so handle_exit, runs only after the call returns). It returns FALSE,
    so the C runtime's handler runs next and uvicorn's handle_exit still gets the signal; the second
    arming is a no-op. Returns the registered callback, None elsewhere or on failure. FAIL-OPEN."""
    if sys.platform != "win32" or drain_s <= 0:
        return None
    sigint, sigbreak = signal.SIGINT, getattr(signal, "SIGBREAK", None)
    names = {s: _signal_name(s) for s in (sigint, sigbreak) if s is not None and s in handled}
    if not names:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        def _on_console_ctrl(ctrl_type):
            sig = sigint if ctrl_type == _CTRL_C_EVENT else sigbreak
            if sig in names:
                arm_drain_watchdog(drain_s, backstop=backstop, signame=names[sig])
            return False

        cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(_on_console_ctrl)
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True):
            log.warning("lifecycle: SetConsoleCtrlHandler failed (error %d); the drain cap arms "
                        "only from the signal handler", ctypes.GetLastError())
            return None
        _console_ctrl_keep.append(cb)
        return cb
    except Exception as exc:  # noqa: BLE001 -- the cap must never break the server it guards
        log.warning("lifecycle: could not register the console control handler: %s", exc)
        return None


def _win_console_ctrl_remove(cb) -> None:
    """Unregister a handler from _win_console_ctrl_arm once uvicorn's shutdown is over. FAIL-OPEN."""
    if cb is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, False)
    except Exception:  # noqa: BLE001
        pass


def compose_lifespan(fastmcp_lifespan, cap_s: Optional[float] = None,
                     drain_timeout: float = 5.0,
                     exit_fn: Callable[[int], None] = os._exit, backstop: bool = True):
    """Wrap FastMCP's ASGI lifespan so OUR loops drain on shutdown, delegating startup + shutdown to
    FastMCP UNCHANGED.

    ``fastmcp_lifespan`` is Starlette's ``app.router.lifespan_context``: a callable ``app -> async
    context manager`` (FastMCP sets it to its own streamable-http session lifespan). The wrapper
    enters that lifespan (its STARTUP runs unchanged), re-yields FastMCP's own lifespan state so
    Starlette copies it into the lifespan scope (it is None today, but re-yielding keeps us correct
    if a future FastMCP yields real state), and on the way out drains our loops in a ``finally``
    BEFORE FastMCP's shutdown runs. FAIL-SAFE: a drain error is logged, never raised, so a drain
    fault cannot corrupt the shutdown handshake. Returned value is a new ``lifespan_context`` of the
    same ``app -> async context manager`` shape, so it drops straight back onto the router.

    ``cap_s`` > 0 arms the shutdown cap (arm_shutdown_watchdog) the moment the shutdown side
    starts, before anything that could stall; None (the default, used by the offline tests) arms
    nothing. serve_http passes shutdown_cap_s(). Each step records its phase for the cap's report.
    ``drain_timeout`` is drain_all's shared join budget; ``exit_fn`` / ``backstop`` go to
    arm_shutdown_watchdog (tests inject a recorder and turn the backstop off)."""
    @contextlib.asynccontextmanager
    async def _lifespan(app_):
        t0 = None
        try:
            async with fastmcp_lifespan(app_) as state:   # FastMCP startup + shutdown run UNCHANGED
                try:
                    yield state                            # preserve FastMCP's lifespan state (None today)
                finally:
                    t0 = time.monotonic()
                    if cap_s:
                        arm_shutdown_watchdog(cap_s, exit_fn=exit_fn, backstop=backstop)
                    set_shutdown_phase("lifespan: draining background loops (lifecycle.drain_all)")
                    try:
                        drain_all(timeout_total=drain_timeout)  # graceful drain of our loops on shutdown
                    except Exception as exc:  # noqa: BLE001 -- a drain error must not corrupt shutdown
                        log.warning("lifespan drain_all failed: %s", exc)
                    set_shutdown_phase("lifespan: FastMCP session manager shutdown "
                                       "(waits for its task group, incl. tool bodies in worker threads)")
        finally:
            if t0 is not None:
                set_shutdown_phase("after lifespan: uvicorn / asyncio / interpreter teardown")
                log.info("lifecycle: lifespan shutdown done in %.1fs", time.monotonic() - t0)
    return _lifespan
