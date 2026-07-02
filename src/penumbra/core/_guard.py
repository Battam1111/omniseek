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

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


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
    def pace(self, on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None) -> None:
        """Reserve the next request-start slot (>= ``min_interval_s`` after the previous), then wait
        for it. The slot reservation is under the lock; the wait is NOT, so callers do not serialize
        on the lock itself, only on the wire-rate. Bounds requests/second across all callers + threads.

        ``on_backlog`` (optional) is consulted UNDER the reservation lock with the seconds this caller
        would wait: if it returns an exception, that exception is raised WITHOUT reserving a slot (so a
        pathological backlog sheds load + fails fast instead of stacking an unbounded gate wait, and the
        backlog drains rather than growing). Backends that never fast-fail on the gate pass nothing.
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
        if wait > 0:
            time.sleep(wait)

    def pace_backlog_s(self) -> float:
        """How long the NEXT request would wait on the rate gate, read-only (no reservation). Lets a
        caller fast-fail at its own site (beside its breaker check) when the backlog is pathological,
        without tangling with the in-loop retry bookkeeping."""
        return max(0.0, self.pace_state["next_at"] - time.monotonic())

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
