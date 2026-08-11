"""Sync -> async PORTAL: reach the bound event loop from a SYNC (worker) thread.

S3a of the async migration (charter ASYNC-CORE-DESIGN.md D9). This is a PURE-ADDITION leaf:
it converts NO operation. Every eye tool still runs its body SYNC on a worker thread via
``server._threaded`` -> ``anyio.to_thread.run_sync``. The portal only proves + provides the
BRIDGE a sync caller will use to run a coroutine on the FastMCP/uvicorn loop once operations
actually become async (S3b onward). Nothing here changes any tool's behavior today.

Stdlib only (asyncio / threading; the contextvar OBJECTS are read from the sibling cache/diag
leaves at call time). One authoritative loop is captured at ``bind()`` (called from the loop),
and ``submit()`` runs a coroutine on it from a NON-loop thread via
``asyncio.run_coroutine_threadsafe``, blocking for the result. Two hazards it handles:

  - DEADLOCK GUARD: ``submit()`` from the loop thread itself would block the loop waiting on the
    loop -> deadlock. bind() captures the loop thread's ident; submit() compares
    ``threading.get_ident()`` to it and raises RuntimeError instead of hanging.
  - CONTEXTVAR PROPAGATION (D9): ``run_coroutine_threadsafe`` runs the coro in the LOOP's own
    context, NOT the caller's, so the caller's fetch contextvars (cache fresh / cache_only /
    refresh_margin + diag trace) would be lost. submit() captures them on the caller's thread and
    re-applies them INSIDE a wrapper coroutine, so an async fetch honors the caller's
    cache_only / fresh exactly like the sync path does; the wrapper resets them after, so nothing
    leaks into the loop's context for the next coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

# The authoritative loop, set once from the loop thread at bind(). None until bound.
_loop: Optional[asyncio.AbstractEventLoop] = None
# The ident of the thread bind() ran on. Because bind() is called FROM the loop (an async
# context), this IS the loop thread's ident, which the deadlock guard compares against.
_loop_thread_ident: Optional[int] = None

# A trivial value the self-test round-trips to confirm the bridge is live.
_SELFTEST_SENTINEL = "penumbra-portal-selftest-ok"


def bind(loop: asyncio.AbstractEventLoop) -> None:
    """Register the authoritative event loop. MUST be called FROM the loop (an async context),
    so ``threading.get_ident()`` here is the loop thread's ident -> captured for the deadlock
    guard. Idempotent: production binds the one FastMCP loop exactly once; re-binding just
    overwrites (a test spins up throwaway loops and rebinds)."""
    global _loop, _loop_thread_ident
    _loop = loop
    _loop_thread_ident = threading.get_ident()


def is_bound() -> bool:
    """True once a loop has been bound."""
    return _loop is not None


def submit(coro, *, timeout: Optional[float] = None):
    """From a SYNC (non-loop) thread: run ``coro`` on the bound loop and BLOCK for its result.

    Raises RuntimeError if no loop is bound, or if called from the loop thread itself (the
    deadlock guard: doing so would block the loop waiting on the loop). Propagates the caller's
    fetch contextvars into the coroutine (D9). Returns the coro's result, re-raises the coro's
    exception, or raises TimeoutError if ``timeout`` elapses first."""
    loop = _loop
    if loop is None:
        coro.close()  # refuse cleanly; never leave an un-awaited coroutine dangling
        raise RuntimeError("portal is not bound to an event loop")
    if threading.get_ident() == _loop_thread_ident:
        coro.close()
        raise RuntimeError(
            "portal.submit called from the loop thread would deadlock; "
            "call it from a worker (sync) thread")

    # Capture the CALLER's fetch contextvars HERE, on the caller's thread. run_coroutine_threadsafe
    # runs coro in the LOOP's context, not ours, so without this an async fetch would silently
    # ignore the caller's cache_only / fresh. Read the exact ContextVar objects from cache/diag.
    from penumbra.core import cache as _cache
    from penumbra.core import diag as _diag
    _fresh = _cache._fresh_var.get()
    _cache_only = _cache._cache_only_var.get()
    _margin = _cache._refresh_margin_var.get()
    _trace = _diag._trace_var.get()

    async def _wrapped():
        # Re-apply the captured values in the loop's context, run the real coro, then RESET so the
        # loop's context is clean for the next coroutine (no cross-request leak).
        t_fresh = _cache._fresh_var.set(_fresh)
        t_only = _cache._cache_only_var.set(_cache_only)
        t_margin = _cache._refresh_margin_var.set(_margin)
        t_trace = _diag._trace_var.set(_trace)
        try:
            return await coro
        finally:
            _cache._fresh_var.reset(t_fresh)
            _cache._cache_only_var.reset(t_only)
            _cache._refresh_margin_var.reset(t_margin)
            _diag._trace_var.reset(t_trace)

    fut = asyncio.run_coroutine_threadsafe(_wrapped(), loop)
    return fut.result(timeout)  # blocks; re-raises the coro's exception; TimeoutError past `timeout`


def self_test(timeout: float = 5.0) -> bool:
    """Round-trip a trivial coroutine through the bound loop from THIS (sync) thread and confirm
    it returns the sentinel. Returns True on success, False on ANY failure; NEVER raises.

    Must be called from a NON-loop (worker) thread: server._ensure_portal runs it via
    ``anyio.to_thread.run_sync`` so it proves the REAL sync-thread -> loop round-trip through the
    actual FastMCP loop, not a same-thread shortcut."""
    try:
        async def _ping():
            return _SELFTEST_SENTINEL
        return submit(_ping(), timeout=timeout) == _SELFTEST_SENTINEL
    except Exception as exc:  # noqa: BLE001 -- self_test must never raise into its caller
        log.warning("portal self-test error: %s", exc)
        return False
