"""The ONE bridge from a thread into OmniSeek's event loop (charter ASYNC-CORE-DESIGN.md D9).

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
from typing import Any, Callable, Optional

import anyio
import anyio.from_thread
import anyio.lowlevel
import anyio.to_process
import anyio.to_thread

# The service loop's token, captured once by bind() at lifespan startup. None until then (and in a
# process that never binds: the stdio server, a test): an AnyIO worker thread then uses its own loop.
_token: Optional[anyio.lowlevel.EventLoopToken] = None

# --- The offload lanes (the reverse bridge: loop -> elsewhere; ASYNC-CORE-DESIGN.md D14) -----------
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
# GIL only between waits, so the old ceiling of 256 stays.
# Mixed (a legacy sync source's whole .search: waits on the network AND parses) stays on
# fetcher._LEGACY_LIMITER.
# PROC_LANE_TOKENS is PROVISIONAL (a replay picked it from 2 and 4); the
# live probe on the host machine settles it. ASYNC-CORE-DESIGN.md D14 has the readings.
PROC_LANE_TOKENS = 2
PROC_LANE = anyio.CapacityLimiter(PROC_LANE_TOKENS)
# The process lane's worker pids, each reported by the worker itself with every result (_tagged), so the
# memory guard counts the workers' footprint with OmniSeek's own (eye.memguard.lane_pids, which drops a
# pid that ended). Added to on the loop, read on the guard's thread.
LANE_PIDS: set = set()
IO_LANE_TOKENS = 256
IO_LANE = anyio.CapacityLimiter(IO_LANE_TOKENS)


async def run_cpu(fn: Callable[..., Any], *args: Any) -> Any:
    """From the LOOP: ``fn(*args)`` in a worker process of the process lane; returns its result or
    re-raises its exception. ``fn`` module-level, ``args`` picklable (functools.partial of such a
    function for keywords). Workers are anyio's: started on demand, reused, ended after 5 idle minutes
    and when the loop ends; a worker imports only the modules of the functions it is sent.

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
    pid, out = await anyio.to_process.run_sync(_tagged, fn, *args, limiter=PROC_LANE)
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
