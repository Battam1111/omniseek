"""Shared backend load-guard: one concurrency cap + one rate pacer + one circuit breaker.

Extracts the byte-identical guard machinery that ``_openalex``, ``_s2`` and ``_github`` each
carried verbatim (the 2026-07-01 parsimony audit, P1): a ``BoundedSemaphore`` in-flight cap, a
min-interval request-start pacer under its own lock, and a consecutive-failure circuit breaker
over a ``{fails, open_until, last_429, ...}`` state dict guarded by its own lock. Those three
modules differed ONLY in constants (concurrency cap, pace interval) and in how they wired these
primitives at their call sites; the primitives themselves were the same code three times. This is
that code once. It is judgment-free plumbing: it owns the storage (``.state`` dict, ``.lock``,
``.sema``, the pace lock/state) and the primitive operations, and each backend keeps its own
API-specific wrappers, pacing call sites, health probes and error/log wording by reaching the
primitives here (including reading ``.state`` fields the health probes surface, and stamping
``last_429`` exactly as the backends do today).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Callable, Optional

import anyio

logger = logging.getLogger(__name__)


class GateBusy(TimeoutError):
    """A shared concurrency gate did not free a permit within its caller budget."""


@contextlib.contextmanager
def bounded_slot(
    gate,
    max_wait: float,
    on_busy: Callable[[float], BaseException],
):
    """Acquire a threading gate with a finite wait and guaranteed release."""
    if not gate.acquire(timeout=max_wait):
        raise on_busy(max_wait)
    try:
        yield
    finally:
        gate.release()


@contextlib.asynccontextmanager
async def bounded_async_slot(
    gate,
    max_wait: float,
    on_busy: Callable[[float], BaseException],
):
    """Async, cancellation-safe twin of bounded_slot.

    The blocking acquire runs off-loop and is shielded so cancellation cannot take a permit
    between the acquire and the release finally. A saturated gate fails fast instead of
    creating an unbounded queue of worker threads.
    """
    acquired = False
    with anyio.CancelScope(shield=True):
        acquired = await anyio.to_thread.run_sync(gate.acquire, True, max_wait)
    if not acquired:
        raise on_busy(max_wait)
    try:
        yield
    finally:
        gate.release()


class BackendGuard:
    """One backend's concurrency cap + rate pacer + circuit breaker.

    ``name`` names the backend for the breaker-open log line. ``max_inflight`` sizes the
    ``BoundedSemaphore``. ``break_after`` / ``break_for_s`` are the consecutive-failure count that
    opens the circuit and the seconds it stays open. ``min_interval_s`` is the minimum spacing
    between request STARTS enforced by ``pace()`` (0.0 = no pacing, for a semaphore-only user).
    ``extra_state`` is merged into ``.state`` for backends that carry extra breaker-adjacent fields
    (OpenAlex's per-bucket ``dry_until``). ``log`` overrides the logger the breaker-open warning is
    emitted through, so a backend can keep its own module logger name.
    """

    def __init__(self, name: str, max_inflight: int, break_after: int = 5,
                 break_for_s: float = 120.0, min_interval_s: float = 0.0,
                 extra_state: Optional[dict] = None,
                 log: Optional[logging.Logger] = None) -> None:
        self.name = name
        self.break_after = break_after
        self.break_for_s = break_for_s
        self.min_interval_s = min_interval_s
        self._log = log or logger

        # Concurrency cap: at most ``max_inflight`` in-flight calls across all callers + threads.
        self.sema = threading.BoundedSemaphore(max_inflight)

        # Breaker state + its lock. The dict the health probes read directly (fails / open_until /
        # last_429) plus any backend-specific extras merged in.
        self.state: dict = {"fails": 0, "open_until": 0.0, "last_429": 0.0}
        if extra_state:
            self.state.update(extra_state)
        self.lock = threading.Lock()

        # Rate pacer: reserve the next request-start slot under this lock, then sleep (outside it).
        self.pace_state: dict = {"next_at": 0.0}
        self.pace_lock = threading.Lock()

    # ── rate pacer ────────────────────────────────────────────────────────────
    def reserve_pace_slot(
            self, on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None) -> float:
        """Reserve the next request-start slot (>= ``min_interval_s`` after the previous) UNDER the pace
        lock and return the seconds to WAIT for it. The caller does the wait itself: a sync caller via
        ``time.sleep`` (see ``pace``), a native-async caller via ``await anyio.sleep`` -- so the async
        egress can honor the SAME shared rate gate WITHOUT holding a thread during the wait. The
        reservation (pure sync arithmetic under a brief lock, no IO) is safe on the event loop.

        ``on_backlog`` (optional) is consulted UNDER the lock with the seconds this caller would wait: if
        it returns an exception, that exception is raised WITHOUT reserving a slot (shed load + fail fast).
        """
        with self.pace_lock:
            now = time.monotonic()
            start = max(now, self.pace_state["next_at"])
            wait = start - now
            if on_backlog is not None:
                exc = on_backlog(wait)
                if exc is not None:
                    # Do NOT reserve (leave next_at) so the backlog drains; signal the caller.
                    raise exc
            self.pace_state["next_at"] = start + self.min_interval_s
        return wait

    def pace(self, on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None) -> None:
        """Reserve the next request-start slot (>= ``min_interval_s`` after the previous), then wait
        for it. The slot reservation is under the lock; the wait is NOT, so callers do not serialize
        on the lock itself, only on the wire-rate. Bounds requests/second across all callers + threads.

        ``on_backlog`` (optional) is consulted UNDER the reservation lock with the seconds this caller
        would wait: if it returns an exception, that exception is raised WITHOUT reserving a slot (so a
        pathological backlog sheds load + fails fast instead of stacking an unbounded gate wait, and the
        backlog drains rather than growing). Backends that never fast-fail on the gate pass nothing.
        """
        wait = self.reserve_pace_slot(on_backlog)
        if wait > 0:
            time.sleep(wait)

    def pace_backlog_s(self) -> float:
        """How long the NEXT request would wait on the rate gate, read-only (no reservation). Lets a
        caller fast-fail at its own site (beside its breaker check) when the backlog is pathological,
        without tangling with the in-loop retry bookkeeping."""
        return max(0.0, self.pace_state["next_at"] - time.monotonic())

    # ── concurrency gate (bounded + cancellation-safe acquire) ─────────────────
    # A raw ``with self.sema:`` acquire is UNBOUNDED: if every permit is held (a saturating fan-out,
    # or a leaked permit) the caller blocks forever, so an interactive call hangs the whole MCP idle
    # window (the 300s resolve_identity hang, 2026-07-18). And the async ``to_thread.run_sync(
    # self.sema.acquire)`` sitting OUTSIDE a try/finally LEAKS a permit under cancellation: anyio's
    # to_thread is abandon_on_cancel=False, so a deadline/client cancel landing on that await lets the
    # worker thread FINISH the acquire (permit taken) and only THEN raise CancelledError, before the
    # try -> the release never runs. Enough leaks drain the shared pool and every OA/S2 call then hangs
    # on acquire (the observed outage). These two helpers close both holes: acquire is BOUNDED (raise +
    # degrade past ``max_wait``, the same shed-load discipline the rate gate already applies on backlog),
    # and the async acquire is SHIELDED so a cancel cannot separate the acquire from the release.
    @contextlib.contextmanager
    def slot(self, max_wait: float, on_busy: Callable[[float], BaseException]):
        """Bounded concurrency-cap acquire (sync): block up to ``max_wait`` for a permit; if none frees
        in time, raise ``on_busy(max_wait)`` (fail fast -> the caller degrades to []/None) instead of
        blocking unboundedly. Guarantees release. Drop-in for ``with self.sema:``."""
        if not self.sema.acquire(timeout=max_wait):
            raise on_busy(max_wait)
        try:
            yield
        finally:
            self.sema.release()

    @contextlib.asynccontextmanager
    async def aslot(self, max_wait: float, on_busy: Callable[[float], BaseException]):
        """Bounded + cancellation-safe concurrency-cap acquire (async). The threading BoundedSemaphore
        is acquired OFF the loop (a ``with self.sema:`` on the loop would freeze it) and the acquire is
        SHIELDED so a deadline/client cancel cannot take the permit and then skip the release (the leak
        that drained the shared pool). Bounded like ``slot``: no permit within ``max_wait`` -> raise
        ``on_busy`` -> degrade, never an unbounded acquire hang. Drop-in for the acquire + try/finally
        around one ``await``."""
        acquired = False
        # Shield ONLY the blocking acquire: a cancel arriving here is deferred until the acquire returns
        # (True/False), then fires at the first checkpoint inside the body -> the finally still releases.
        with anyio.CancelScope(shield=True):
            acquired = await anyio.to_thread.run_sync(self.sema.acquire, True, max_wait)
        if not acquired:
            raise on_busy(max_wait)
        try:
            yield
        finally:
            self.sema.release()  # BoundedSemaphore: cross-thread release is legal

    # ── circuit breaker ───────────────────────────────────────────────────────
    def is_open(self) -> bool:
        """True iff the circuit is currently open (recent consecutive failures). A non-probing read."""
        with self.lock:
            return time.time() < self.state["open_until"]

    def record_ok(self) -> None:
        """Reset the consecutive-failure streak after a success."""
        with self.lock:
            self.state["fails"] = 0

    def record_fail(self) -> None:
        """Record one failure; open the circuit once ``break_after`` consecutive failures pile up."""
        with self.lock:
            self.state["fails"] += 1
            if self.state["fails"] >= self.break_after:
                self.state["open_until"] = time.time() + self.break_for_s
                self.state["fails"] = 0
                self._log.warning("%s circuit OPEN for %.0fs (consecutive failures)",
                                  self.name, self.break_for_s)
