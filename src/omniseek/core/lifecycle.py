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
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Optional

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


def compose_lifespan(fastmcp_lifespan):
    """Wrap FastMCP's ASGI lifespan so OUR loops drain on shutdown, delegating startup + shutdown to
    FastMCP UNCHANGED.

    ``fastmcp_lifespan`` is Starlette's ``app.router.lifespan_context``: a callable ``app -> async
    context manager`` (FastMCP sets it to its own streamable-http session lifespan). The wrapper
    enters that lifespan (its STARTUP runs unchanged), re-yields FastMCP's own lifespan state so
    Starlette copies it into the lifespan scope (it is None today, but re-yielding keeps us correct
    if a future FastMCP yields real state), and on the way out drains our loops in a ``finally``
    BEFORE FastMCP's shutdown runs. FAIL-SAFE: a drain error is logged, never raised, so a drain
    fault cannot corrupt the shutdown handshake. Returned value is a new ``lifespan_context`` of the
    same ``app -> async context manager`` shape, so it drops straight back onto the router."""
    @contextlib.asynccontextmanager
    async def _lifespan(app_):
        async with fastmcp_lifespan(app_) as state:   # FastMCP startup + shutdown run UNCHANGED
            try:
                yield state                            # preserve FastMCP's lifespan state (None today)
            finally:
                try:
                    drain_all()                        # graceful drain of our loops on shutdown
                except Exception as exc:  # noqa: BLE001 -- a drain error must not corrupt shutdown
                    log.warning("lifespan drain_all failed: %s", exc)
    return _lifespan
