"""The ONE bridge from a thread into OmniSeek's event loop.

Async migration step two. The fetch core is async; the callers that still run on a THREAD (the
in-service background loops: cache warmer, recall ingest, the job scheduler's sensor tick; a sync
tool body on an AnyIO worker thread; a source's one sync entry) reach it through
``run()`` here, and nowhere else. It is anyio's own ``from_thread.run`` with the event-loop token the
service captured ONCE at lifespan startup (``bind()``, called from serve_http's lifespan), so:

  - the coroutine runs on the service's one loop (no second loop per call, no ``asyncio.run`` in a
    background thread);
  - the calling thread's WHOLE contextvars.Context is carried into the coroutine (anyio runs the
    hand-off inside ``copy_context()``), so every fetch contextvar (fresh / cache_only / refresh
    margin / diag trace / deadline / initiator / flight scope / filter upstream / probe ledger ...)
    arrives as the caller set it, with no hand-kept list that a new variable could be missing from;
  - an AnyIO worker thread (a tool body under ``anyio.to_thread.run_sync``) needs no token at all
    (anyio knows its loop), so the stdio server, which has no lifespan of ours, works unchanged.

Failure is loud, never silent: a plain thread with no bound token raises ``anyio.NoEventLoopError``;
a loop that has finished raises ``anyio.RunFinishedError``; calling from a thread that is itself
running an event loop raises RuntimeError (blocking there would deadlock that loop).

A standalone process (a CLI such as ``python -m omniseek.core.prewarm``) has no service loop: it uses
``run_standalone()``, which starts that process's one loop, binds the bridge to it for the duration,
and runs the sync body on a worker thread.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys
import threading
import time
from typing import Any, Callable, Optional

import anyio
import anyio.from_thread
import anyio.lowlevel
import anyio.to_process
import anyio.to_thread

# The service loop's token, captured once by bind() at lifespan startup. None until then (and in a
# process that never binds: the stdio server, a test): an AnyIO worker thread then uses its own loop.
_token: Optional[anyio.lowlevel.EventLoopToken] = None

# --- The offload lanes (the reverse bridge: loop -> elsewhere) -----------
# PROCESS lane = run_cpu() below: CPU work whose input is already in memory (parse, score, convert)
# runs in a worker PROCESS, never on a thread of this one. A thread doing pure Python here shares the
# one GIL with the loop thread, and the loop, to get the GIL back, may wait a whole switch interval
# (5 ms) at every hand-off (the convoy, bpo-7946): a handful of parse threads kept the loop answering
# hundreds of ms late, measured live 11-03 and in a replay. A worker
# process has its own GIL. What crosses is pickled (the function by its module path, the arguments,
# the result), so the function must be module-level and its arguments plain data; pickling runs on the
# loop, so send what the work reads, not whole objects. A worker that dies fails that one call loudly
# (anyio.BrokenWorkerProcess, recorded like any source error); nothing falls back to a thread.
# IO lane = IO_LANE: everything that WAITS (network, disk, DNS, subprocess, CDP, a lock, the loop via
# run() above) and every tool body; it passes limiter=IO_LANE explicitly. A waiting thread holds the
# GIL only between waits, so the ceiling stays 256.
# Mixed (a legacy sync source's whole .search: waits on the network AND parses) stays on
# fetcher._LEGACY_LIMITER.
# PROC_LANE_TOKENS is PROVISIONAL (a replay picked it from 2 and 4); the
# live probe on the host machine settles it.
PROC_LANE_TOKENS = 2
PROC_LANE = anyio.CapacityLimiter(PROC_LANE_TOKENS)
# anyio's to_process pickles a call's arguments BEFORE it waits for its limiter, so every call queued
# behind the lane's two slots held a second, pickled copy of its input (a parse call: the whole answer
# body) for as long as it waited. run_cpu takes a PROC_LANE slot first and only
# then hands the call to to_process, under this inner limiter of the same size, which therefore never
# makes anyone wait: what waits is the call, not its pickle.
_PROC_SLOTS = anyio.CapacityLimiter(PROC_LANE_TOKENS)
# The process lane's worker pids, each reported by the worker itself with every result (_tagged), so the
# memory guard counts the workers' footprint with OmniSeek's own (eye.memguard.lane_pids, which drops a
# pid that ended). Added to on the loop, read on the guard's thread.
LANE_PIDS: set = set()
IO_LANE_TOKENS = 256
IO_LANE = anyio.CapacityLimiter(IO_LANE_TOKENS)
# PROVISIONAL (2026-11-17): the most one small hop of a tool on the IO lane (a cache read or
# write, a DNS lookup, a key or state file) may wait, its wait for a free slot included; see run_io.
# Derivation (the replays, like fetcher._STEP_BUDGET_S): an IO-lane hop waited at most
# 3.8 s for a slot during the bursts, and a cache read ran at most 0.6 s itself; 3.8 + 10 x 0.6, rounded
# up to 10 s. Calibration: a hop past it is a miss (a read) or detached (a write); fetcher's step lines
# name the steps, these hops fail open silently, so read their counts in a replay before changing it.
IO_HOP_BUDGET_S = 10.0


async def run_io(fn: Callable[..., Any], *args: Any, budget: float = IO_HOP_BUDGET_S) -> tuple:
    """From the LOOP: ``fn(*args)`` on a worker thread of the IO lane, waited at most ``budget`` seconds
    (OmniSeek's one bounded wait, fetcher._arun_bounded). ``(True, result)``, or ``(False, None)`` past the
    budget: the thread is detached and finishes on its own (a write still lands), and the caller takes
    its fail-open path (a cache read past it is a miss). Re-raises ``fn``'s exception. For a hop whose
    work is small and whose wait is mostly for a slot: when every slot is held, an unbounded hop waits
    as long as the holders do (11-17: 89 minutes)."""
    from omniseek.core import fetcher  # fetcher imports this module at its top: import at call time
    return await fetcher._arun_bounded(anyio.to_thread.run_sync(fn, *args, limiter=IO_LANE), budget)


async def run_cpu(fn: Callable[..., Any], *args: Any, cancellable: bool = False) -> Any:
    """From the LOOP: ``fn(*args)`` in a worker process of the process lane; returns its result or
    re-raises its exception. ``fn`` module-level, ``args`` picklable (functools.partial of such a
    function for keywords). Workers are anyio's: started on demand, reused, ended after 5 idle minutes
    and when the loop ends; a worker imports only the modules of the functions it is sent.

    ``cancellable`` (anyio 4.13 to_process semantics): False, the default, shields the wait for the
    answer, so cancelling the caller lets the call finish and the worker goes back to the pool; True
    lets a cancellation through: anyio then kills the worker (SIGKILL) and drops it from the pool, and
    the next call starts a fresh one. A caller that bounds the call with a cancel scope passes True, so
    a worker stuck past the bound is replaced instead of holding a slot of the lane.

    Side effect, once per process: the main module's ``__file__`` becomes None. An anyio worker re-runs
    the parent's main module as ``__mp_main__`` when that module has a file (so a pickled function of
    ``__main__`` resolves, as with multiprocessing). OmniSeek never sends one, and the re-run is all
    cost or harm: the HTTP service's main (serve_http) loads the whole eye and reads the bearer token,
    about 0.5 GB per worker against about 0.07 GB for what the parsers import; a script without a
    ``__name__ == "__main__"`` guard (a test script, say) would run again in every worker, and start
    workers of its own from there. Without a file the worker skips that step."""
    main = sys.modules.get("__main__")
    if main is not None and getattr(main, "__file__", None):
        main.__file__ = None
    # The caller's deadline (the gates' one, _guard.deadline_until: a fan-out source, a URL read, a
    # health probe) holds here too: a call still waiting for a slot when it comes is not started
    # (GateBusy, the gates' own sentence). Without it a burst of broad searches left hundreds of parse
    # calls queued behind the lane's two workers for minutes after their callers had answered, each
    # holding its input (the replay of 11-17's burst: 400 to 550 queued 70 s after the last answer,
    # the footprint held at 4.6 to 5 GB by them). Work outside a deadline (the ranking, a graph build)
    # waits; the ranking bounds itself (rank.amerge_rank's timeout).
    from omniseek.core import _guard  # imported here: _guard's importers sit above this module
    until = _guard.current_deadline()
    admitted = False
    t0 = time.monotonic()
    with anyio.move_on_after(None if until is None else max(0.0, until - t0)):
        await PROC_LANE.acquire()
        admitted = True
    if not admitted:
        raise _guard.GateBusy(_guard.deadline_refusal("process lane", time.monotonic() - t0))
    try:
        pid, out = await anyio.to_process.run_sync(_tagged, fn, *args, cancellable=cancellable,
                                                   limiter=_PROC_SLOTS)
    finally:
        PROC_LANE.release()
    LANE_PIDS.add(pid)
    return out


def _tagged(fn: Callable[..., Any], *args: Any) -> tuple:
    """Runs IN the worker: ``fn(*args)`` and this worker's pid (run_cpu records it, see LANE_PIDS)."""
    return os.getpid(), fn(*args)


def bind() -> None:
    """Capture the RUNNING loop as the bridge's target. MUST be called from the loop (the lifespan
    startup does it); raises anyio.NoEventLoopError anywhere else."""
    global _token
    _token = anyio.lowlevel.current_token()


def run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """From a THREAD: run ``await fn(*args, **kwargs)`` on the bound loop and BLOCK for its result
    (re-raises its exception). See the module docstring for the guarantees and the failures."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("portal.run called from a thread running an event loop would deadlock it; "
                           "await the coroutine there instead")
    return anyio.from_thread.run(functools.partial(fn, *args, **kwargs), token=_token)


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """From SYNC code that may run anywhere (a script, a thread of the service, a test): ``await
    fn(*args, **kwargs)`` and block for its result. The service's loop when this process has one (the
    bound token, or the loop of an AnyIO worker thread), exactly as ``run``; in a process that never
    bound a loop, this process's OWN loop (started once, on a daemon thread, kept for the life of the
    process), so the pooled async clients stay on one loop however many calls come. This is what the
    sources' one sync entry (``sources._base.SourceBase.search``) goes through."""
    try:
        return run(fn, *args, **kwargs)
    except anyio.NoEventLoopError:
        pass
    return anyio.from_thread.run(functools.partial(fn, *args, **kwargs), token=_own_loop())


_own_token: Optional[anyio.lowlevel.EventLoopToken] = None
_own_lock = threading.Lock()


def _own_loop() -> anyio.lowlevel.EventLoopToken:
    """The token of this process's own loop (``call`` in a process with no bound loop), started once."""
    with _own_lock:
        if _own_token is None:
            ready = threading.Event()

            async def _serve() -> None:
                global _own_token
                _own_token = anyio.lowlevel.current_token()
                ready.set()
                await anyio.sleep_forever()

            threading.Thread(target=anyio.run, args=(_serve,), name="eye-own-loop", daemon=True).start()
            ready.wait()
        return _own_token


def run_standalone(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """For a process with NO service loop (a CLI, a script, an offline test): start this process's one
    loop, bind the bridge to it while ``fn(*args, **kwargs)`` runs on a worker thread, then put the
    previous binding back. Returns fn's result."""
    async def _main():
        global _token
        prev = _token
        bind()
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs), limiter=IO_LANE)
        finally:
            _token = prev
    return anyio.run(_main)
