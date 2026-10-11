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
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
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

_wd_lock = threading.Lock()
_wd_state: dict = {"armed": False, "t0": None, "phase": "running", "cancel": None,
                   "thread": None, "backstop": False}


def shutdown_cap_s() -> float:
    """The cap in seconds from OMNISEEK_SHUTDOWN_CAP_S, else SHUTDOWN_CAP_S_DEFAULT; 0 disables."""
    raw = os.environ.get("OMNISEEK_SHUTDOWN_CAP_S", "").strip()
    if not raw:
        return SHUTDOWN_CAP_S_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("lifecycle: OMNISEEK_SHUTDOWN_CAP_S=%r is not a number; using %.0f",
                    raw, SHUTDOWN_CAP_S_DEFAULT)
        return SHUTDOWN_CAP_S_DEFAULT


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


def arm_shutdown_watchdog(cap_s: Optional[float] = None,
                          exit_fn: Callable[[int], None] = os._exit,
                          backstop: bool = True) -> "Optional[threading.Thread]":
    """Start the shutdown cap: a daemon thread that, if the process is still alive ``cap_s`` seconds
    later, logs one WARNING naming the phase reached, the registered loops still running and every
    other live thread, dumps every thread's stack (faulthandler), and calls ``exit_fn(1)``. With
    ``backstop`` it also arms faulthandler.dump_traceback_later(cap_s + BACKSTOP_EXTRA_S, exit=True),
    which runs in a C thread and needs no GIL. Idempotent: a second call while armed returns None. It is
    NOT disarmed when the lifespan finishes: the teardown after it (asyncio.run closing the default
    executor, the interpreter joining non-daemon threads) can stall too, and a clean exit ends the
    process and the thread with it. FAIL-OPEN: an error here is logged, never raised."""
    cap = shutdown_cap_s() if cap_s is None else max(0.0, float(cap_s))
    if cap <= 0:
        return None
    with _wd_lock:
        if _wd_state["armed"]:
            return None
        _wd_state["armed"] = True
        _wd_state["t0"] = time.monotonic()
        cancel = threading.Event()
        _wd_state["cancel"] = cancel

    def _wait() -> None:
        if cancel.wait(cap):
            return
        try:
            loops, others = _still_running()
            log.warning("lifecycle: shutdown cap hit: still alive %.0fs after the lifespan shutdown "
                        "began, stuck in phase %r; registered loops still running %s; other live "
                        "threads %s; dumping every thread's stack, then forcing exit(1)",
                        cap, _wd_state.get("phase"), loops, others)
        except Exception as exc:  # noqa: BLE001 -- the exit below must happen regardless
            log.warning("lifecycle: shutdown cap hit after %.0fs (report failed: %s)", cap, exc)
        _flush_all()
        fd = _stderr_fd()
        if fd is not None:
            try:
                faulthandler.dump_traceback(file=fd, all_threads=True)
            except Exception:  # noqa: BLE001
                pass
            try:   # a raw fd write needs no logging state, so the exit always leaves one line
                os.write(fd, f"lifecycle: forced exit(1) {cap:.0f}s after the lifespan shutdown "
                             f"began\n".encode())
            except Exception:  # noqa: BLE001
                pass
        exit_fn(1)

    try:
        t = threading.Thread(target=_wait, name="shutdown-watchdog", daemon=True)
        t.start()
        _wd_state["thread"] = t
        if backstop:
            fd = _stderr_fd()
            if fd is not None:
                faulthandler.dump_traceback_later(cap + BACKSTOP_EXTRA_S, exit=True, file=fd)
                _wd_state["backstop"] = True
        log.info("lifecycle: shutdown cap armed (%.0fs, backstop %s)", cap,
                 f"+{BACKSTOP_EXTRA_S:.0f}s" if _wd_state["backstop"] else "off")
        return t
    except Exception as exc:  # noqa: BLE001 -- the cap must never break the shutdown it guards
        log.warning("lifecycle: could not arm the shutdown cap: %s", exc)
        return None


def disarm_shutdown_watchdog() -> None:
    """Cancel the watchdog and the faulthandler backstop and reset the state (tests; a process that
    shuts down for real keeps them armed until it exits)."""
    with _wd_lock:
        cancel = _wd_state.get("cancel")
        if cancel is not None:
            cancel.set()
        if _wd_state.get("backstop"):
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:  # noqa: BLE001
                pass
        _wd_state.update(armed=False, t0=None, phase="running", cancel=None, thread=None,
                         backstop=False)


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
