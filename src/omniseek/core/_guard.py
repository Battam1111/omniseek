"""Shared backend load-guard: one concurrency cap + one rate pacer + one circuit breaker.

Extracts the byte-identical guard machinery that ``_openalex``, ``_s2`` and ``_github`` each
carried verbatim (the 2026-07-01 parsimony audit, P1): an in-flight cap (now ``FairPermits``), a
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

import asyncio
import collections
import contextlib
import contextvars
import logging
import threading
import time
from typing import Callable, Iterable, Optional

import anyio

from omniseek.core import _probe

logger = logging.getLogger(__name__)

def _holder():
    """Who holds or waits: the running asyncio task, else the current thread. A child task or thread
    is a holder of its own: a new request that takes a gate like any other (review, pending 2)."""
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running event loop in this thread
        task = None
    return task if task is not None else threading.get_ident()


def _holder_label(holder) -> str:
    return getattr(holder, "get_name", lambda: None)() or f"thread {holder}"


# REQUESTS THAT WERE SENT (review N2, driver ruling of 2026-09-29). A hold's reservation is handed back
# only when nothing was sent after it was made. Every hold registers a handle in the current context
# while it runs; recording a response (upstreams.observe calls mark_sent) marks every handle
# registered in that context as sent. So a failure after a response (a same-host hop that cannot
# connect, a later hop turned away by another host's gate) no longer hands back a start slot and a
# window place for a request that did reach the upstream.
_active_holds: contextvars.ContextVar = contextvars.ContextVar("omniseek_eye_active_holds", default=())


class HoldHandle:
    """What ``hold``/``ahold`` yield: ``sent`` turns True once a response arrived while it was held.
    It also carries the permit's lease, so the request's progress can renew it (``renew``)."""

    __slots__ = ("sent", "active", "pool", "lease", "request_s")

    def __init__(self, pool=None, lease=None, request_s: float = 0.0) -> None:
        self.sent = False
        self.active = True
        self.pool, self.lease, self.request_s = pool, lease, request_s

    def renew(self) -> None:
        """Data arrived for this request: its lease now ends ``request_s`` from now (never earlier
        than it already did). A holder that makes no progress is not renewed and is reclaimed."""
        if self.active and self.pool is not None and self.lease is not None:
            self.pool.renew(self.lease, time.monotonic() + self.request_s)


def mark_sent() -> None:
    """A response arrived: every hold running in this context has had its request sent."""
    for handle in _active_holds.get():
        handle.sent = True


# RENEWED BY PROGRESS (driver ruling of 2026-09-29 on section 17.5, item 2). Where OmniSeek sees a
# request's body arrive (http's progress streams, the curl tier's download loop), every block of data
# renews the leases of the holds that request runs under; they are taken once, when the response
# arrives (``active_handles``), so the renewal reaches them wherever the body is read.
def active_handles() -> tuple:
    """The holds running in this context now."""
    return tuple(h for h in _active_holds.get() if h.active)


def renew_handles(handles: tuple) -> None:
    """A block of the body arrived: renew these holds' leases (see ``HoldHandle.renew``)."""
    for handle in handles:
        handle.renew()


def _register_hold(handle: HoldHandle):
    live = tuple(h for h in _active_holds.get() if h.active)
    return _active_holds.set(live + (handle,))


def _unregister_hold(handle: HoldHandle, token) -> None:
    handle.active = False
    try:
        _active_holds.reset(token)
    except (ValueError, RuntimeError):  # left from another context: it is inactive, pruned later
        pass


# ONE LINE AND LEASES FOR EVERY GATE (driver ruling of 2026-09-29 on review N1 and P1).
class _Lease:
    """One permit handed out by a ``FairPermits``: who holds it and when its lease ends (None: never,
    for a permit taken with ``acquire``). ``active`` turns False when it is given back or reclaimed."""

    __slots__ = ("holder", "expires_at", "active", "since")

    def __init__(self, holder, expires_at: Optional[float]) -> None:
        self.holder, self.expires_at, self.active, self.since = holder, expires_at, True, time.monotonic()


class _Waiter:
    """One place in a ``FairPermits`` line: a thread (``event``) or a coroutine (``fut`` on ``loop``)."""

    __slots__ = ("holder", "expires_at", "event", "loop", "fut", "lease")

    def __init__(self, holder, expires_at: Optional[float], loop=None, fut=None) -> None:
        self.holder, self.expires_at = holder, expires_at
        self.event = threading.Event() if loop is None else None
        self.loop, self.fut = loop, fut
        self.lease: Optional[_Lease] = None   # set, under the pool's lock, when a permit is handed over


def _resolve(fut) -> None:
    if not fut.done():
        fut.set_result(True)


class FairPermits:
    """The permits of one gate: a counting semaphore with two things a threading semaphore lacks.

    FIRST COME, FIRST SERVED (review N1). Sync and async callers wait in ONE line, and a permit that
    comes free goes straight to the first of them: a thread is woken by its event, a coroutine by
    ``call_soon_threadsafe`` on its own loop. A newcomer never takes a permit while anyone is in line,
    so a caller that sends one request after another can no longer keep a single-permit gate to
    itself. A coroutine cancelled after a permit was handed to it passes the permit on.

    LEASES (review P1). The gate does not trust a holder to give a permit back. A permit taken with
    ``take``/``atake`` carries a lease ending at ``expires_at`` (the caller's waiting budget plus the
    request's own timeout); when it runs out, the pool takes the permit back, logs it and counts it
    (``reclaimed``), and the late holder's ``give_back`` is then a no-op, so a permit is never
    returned twice. Leases are checked whenever a permit is asked for and while anyone waits (a
    waiter wakes at the earliest expiry), so a stuck holder blocks a line no longer than its lease.

    The gate also knows who holds it (review P2): ``held_by(holder)`` reads the live leases, so a
    record never outlives its permit, whichever context or thread gives the permit back.

    ``acquire``/``release`` (and ``with``) keep the threading-semaphore interface for tests and code
    that manages a permit by hand: FIFO too, without a lease. ``_value`` is the number of free permits
    and ``_initial_value`` the cap, as on a threading semaphore."""

    def __init__(self, n: int, name: str = "gate") -> None:
        self._initial_value = n
        self._name = name
        self._free = n
        self._lock = threading.Lock()
        self._waiters: collections.deque = collections.deque()
        self._leases: list = []   # live leased permits
        self._anon = 0            # permits taken with acquire(), given back with release()
        self.reclaimed = 0

    @property
    def _value(self) -> int:
        return self._free

    # ── under self._lock ──────────────────────────────────────────────────────────────────────────
    def _lease_locked(self, holder, expires_at: Optional[float]) -> _Lease:
        lease = _Lease(holder, expires_at)
        if holder is None:
            self._anon += 1
        else:
            self._leases.append(lease)
        return lease

    def _hand_over_locked(self) -> None:
        """A permit came free: it goes to the first waiter still in line, else back to the pool."""
        while self._waiters:
            w = self._waiters.popleft()
            lease = self._lease_locked(w.holder, w.expires_at)
            if w.loop is None:
                w.lease = lease
                w.event.set()
                return
            try:
                w.loop.call_soon_threadsafe(_resolve, w.fut)
            except RuntimeError:  # its loop is closed: nobody is there to take it
                self._drop_locked(lease)
                continue
            w.lease = lease
            return
        self._free += 1

    def _drop_locked(self, lease: _Lease) -> None:
        lease.active = False
        if lease.holder is None:
            self._anon -= 1
        elif lease in self._leases:
            self._leases.remove(lease)

    def _give_back_locked(self, lease: _Lease) -> None:
        if lease.active:
            self._drop_locked(lease)
            self._hand_over_locked()

    def _reclaim_locked(self, now: float) -> None:
        for lease in [x for x in self._leases if x.expires_at is not None and x.expires_at <= now]:
            self.reclaimed += 1
            logger.warning("%s: a permit held %.1fs by %s outlived its lease; the gate took it back",
                           self._name, now - lease.since, _holder_label(lease.holder))
            self._give_back_locked(lease)

    def _next_expiry_locked(self) -> Optional[float]:
        ends = [x.expires_at for x in self._leases if x.expires_at is not None]
        return min(ends) if ends else None

    def _take_now_locked(self, holder, expires_at: Optional[float]) -> Optional[_Lease]:
        self._reclaim_locked(time.monotonic())
        if self._free > 0 and not self._waiters:
            self._free -= 1
            return self._lease_locked(holder, expires_at)
        return None

    def _wait_step_locked(self, w: _Waiter, until: Optional[float]) -> "tuple[bool, float]":
        """(done, seconds to wait before the next look). Done: ``w.lease`` holds the permit, or the
        wait is over without one (``w`` has left the line)."""
        if w.lease is not None:
            return True, 0.0
        now = time.monotonic()
        self._reclaim_locked(now)   # may hand a reclaimed permit to w
        if w.lease is not None:
            return True, 0.0
        if until is not None and now >= until:
            self._waiters.remove(w)
            return True, 0.0
        ends = [t for t in (until, self._next_expiry_locked()) if t is not None]
        return False, (min(ends) - now) if ends else -1.0

    # ── leased permits ────────────────────────────────────────────────────────────────────────
    def take(self, holder, until: Optional[float], expires_at: Optional[float]) -> Optional[_Lease]:
        """A permit whose lease ends at ``expires_at``, waiting in line until ``until`` (None: take it
        only if one is free now and nobody is in line). None when none came in time."""
        with self._lock:
            lease = self._take_now_locked(holder, expires_at)
            if lease is not None or until is None or until <= time.monotonic():
                return lease
            w = _Waiter(holder, expires_at)
            self._waiters.append(w)
        return self._wait_thread(w, until)

    def _wait_thread(self, w: _Waiter, until: Optional[float]) -> Optional[_Lease]:
        try:
            while True:
                with self._lock:
                    done, pause = self._wait_step_locked(w, until)
                    if done:
                        return w.lease
                w.event.wait(None if pause < 0 else max(pause, 0.0))
        except BaseException:
            self._leave_line(w)
            raise

    async def atake(self, holder, until: Optional[float],
                    expires_at: Optional[float]) -> Optional[_Lease]:
        """Async twin of ``take``: waits in the same line, on a future of the running loop (no thread
        and no polling); cancelled, it leaves the line and passes on a permit already handed to it."""
        loop = asyncio.get_running_loop()
        with self._lock:
            lease = self._take_now_locked(holder, expires_at)
            if lease is not None or until is None or until <= time.monotonic():
                return lease
            w = _Waiter(holder, expires_at, loop, loop.create_future())
            self._waiters.append(w)
        try:
            while True:
                with self._lock:
                    done, pause = self._wait_step_locked(w, until)
                    if done:
                        return w.lease
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(w.fut), None if pause < 0 else max(pause, 0.0))
        except BaseException:
            self._leave_line(w)
            raise

    def _leave_line(self, w: _Waiter) -> None:
        """A waiter stops waiting (cancelled, interrupted): out of the line, and a permit already
        handed to it goes on to the next in line."""
        with self._lock:
            if w.lease is not None:
                self._give_back_locked(w.lease)
            elif w in self._waiters:
                self._waiters.remove(w)

    def renew(self, lease: Optional[_Lease], expires_at: float) -> None:
        """Move a live lease's end to ``expires_at`` when that is later (the request is making
        progress). A no-op for a lease already given back or reclaimed."""
        if lease is None:
            return
        with self._lock:
            if lease.active and lease.expires_at is not None and expires_at > lease.expires_at:
                lease.expires_at = expires_at

    def give_back(self, lease: Optional[_Lease]) -> None:
        """Return a permit. A no-op when the pool has already taken it back (its lease ran out)."""
        if lease is None:
            return
        with self._lock:
            self._give_back_locked(lease)

    def held_by(self, holder) -> bool:
        """Whether ``holder`` holds a live leased permit of this gate now."""
        with self._lock:
            self._reclaim_locked(time.monotonic())
            return any(x.holder == holder for x in self._leases)

    def reclaim_expired(self) -> None:
        """Take back the permits whose lease has run out (the health view calls this before it reads)."""
        with self._lock:
            self._reclaim_locked(time.monotonic())

    # ── the threading-semaphore interface (no lease) ──────────────────────────────────────────────────
    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        with self._lock:
            if self._take_now_locked(None, None) is not None:
                return True
            if not blocking or (timeout is not None and timeout <= 0):
                return False
            w = _Waiter(None, None)
            self._waiters.append(w)
        return self._wait_thread(w, None if timeout is None else time.monotonic() + timeout) is not None

    def release(self) -> None:
        with self._lock:
            if self._anon <= 0:
                raise ValueError("FairPermits released too many times")
            self._anon -= 1
            self._hand_over_locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# THE CALLER'S DEADLINE (driver ruling 2, 2026-09-29). A gate never keeps a caller waiting past the
# caller's own deadline: the permit wait and the start wait together stay within the declared
# max_wait_s AND the time the caller has left, across every gate one request passes. The deadline
# rides this context variable from where it is decided (broad and named search, omniseek_read, the health
# probe) down to the gates; code with no deadline set gets the declared max_wait_s alone. Monotonic
# seconds (time.monotonic), absolute.
_deadline_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_eye_deadline", default=None)


@contextlib.contextmanager
def deadline_until(at: Optional[float]):
    """Inside this block no gate waits past monotonic time ``at``; an earlier deadline already in
    force wins. ``None`` changes nothing."""
    if at is None:
        yield
        return
    cur = _deadline_var.get()
    token = _deadline_var.set(at if cur is None else min(cur, at))
    try:
        yield
    finally:
        _reset_deadline(token)


def _reset_deadline(token) -> None:
    try:
        _deadline_var.reset(token)
    except (ValueError, RuntimeError):  # reset from another context: the value dies with it
        pass


def deadline_after(seconds: Optional[float]):
    """``deadline_until`` ``seconds`` from now (``None`` changes nothing)."""
    return deadline_until(None if seconds is None else time.monotonic() + max(0.0, float(seconds)))


def current_deadline() -> Optional[float]:
    """The caller's deadline in force here (monotonic seconds), or None."""
    return _deadline_var.get()


def wait_until(max_wait: float) -> float:
    """The latest moment a gate may keep the current caller waiting: ``max_wait`` seconds from now,
    or the caller's deadline when that comes first."""
    until = time.monotonic() + max(0.0, float(max_wait))
    d = _deadline_var.get()
    return until if d is None else min(until, d)


class GateBusy(TimeoutError):
    """A shared concurrency gate did not free a permit within its caller budget."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        _probe.note_held(str(self))   # a running health check records that nothing was sent


class RequestNotSent(Exception):
    """Thrown into a hold to say its request never left (see HopGates.close(unsent=True))."""


# REQUESTS THAT NEVER LEFT (review F8). A hold reserves its start slot, and a place in every window,
# before its body runs. When the body ends in one of these exceptions the request was not sent (a later
# gate refused it, the connection was never made, it was cancelled while still waiting), so the
# reservation is handed back instead of starving the requests that do go out. Other layers register
# their own "not sent" exceptions (upstreams: UpstreamBusy; http: httpx.ConnectError / ConnectTimeout).
_UNSENT: tuple = (GateBusy, RequestNotSent)


def register_unsent(*types) -> None:
    """Add exception types that mean "the request inside the hold was never sent"."""
    global _UNSENT
    _UNSENT = tuple(dict.fromkeys(_UNSENT + tuple(types)))


# ASYNC ADMISSION WITHOUT A WORKER THREAD (review F1 and F4, 2026-09-29). The async twins used to wait
# for a permit in anyio.to_thread.run_sync(sema.acquire, True, max_wait) under a shield. asyncio's own
# task.cancel() (asyncio.run cancelling a detached straggler when an omniseek_gather child returns) is not
# stopped by AnyIO's shield: the coroutine ended, the worker thread went on to take the permit, and
# nothing ever gave it back, so a one-permit gate stayed full until a restart. The same wait also held
# one of the loop's worker-thread tokens, which every to_thread call on that loop shares. A gate's
# permits (FairPermits) are now waited for in its one line on a future (atake); a plain semaphore
# handed to bounded_async_slot is polled with a non-blocking acquire, the way fetcher._aegress admits
# egress. Either way the permit is taken inside the coroutine or not at all, so no cancellation can
# strand it, and no thread waits.
_ADMISSION_POLL_S = 0.02


async def _apoll_acquire(gate, until: float) -> bool:
    """Take a permit of the threading semaphore ``gate`` by polling, until monotonic time ``until``."""
    if gate.acquire(blocking=False):
        return True
    while True:
        left = until - time.monotonic()
        if left <= 0:
            return False
        await anyio.sleep(min(_ADMISSION_POLL_S, left))
        if gate.acquire(blocking=False):
            return True


def _request_s(request_s: Optional[float], max_wait: float) -> float:
    """The request's own timeout; a path that cannot say it gets its declared ``max_wait`` instead."""
    return float(request_s) if request_s is not None else float(max_wait)


def _lease_end(end: float, request_s: Optional[float], max_wait: float) -> float:
    """When a permit taken now must be back: the end of the caller's waiting budget plus the request's
    own timeout (renewed by the request's progress, see ``HoldHandle.renew``)."""
    return end + _request_s(request_s, max_wait)


@contextlib.contextmanager
def bounded_slot(
    gate,
    max_wait: float,
    on_busy: Callable[[float], BaseException],
    *,
    request_s: Optional[float] = None,
):
    """Acquire a threading gate with a finite wait (``max_wait``, cut to the caller's deadline) and
    guaranteed release. A ``FairPermits`` gate is taken in its line and with a lease."""
    end = wait_until(max_wait)
    left = end - time.monotonic()
    if left <= 0:
        # Nothing tried: the caller's time is already up (not a saturated gate, review N4)
        logger.info("gate not tried: past the caller's budget (free=%s)", getattr(gate, "_value", "?"))
        raise on_busy(0.0)
    if isinstance(gate, FairPermits):
        lease = gate.take(_holder(), end, _lease_end(end, request_s, max_wait))
        acquired = lease is not None
    else:
        lease, acquired = None, gate.acquire(timeout=left)
    if not acquired:
        # Record WHY the wait failed. A saturated gate reads the same to the caller whether the
        # permits are in flight (real load, self-shedding working) or stuck (nothing running, yet
        # nothing free), and the second case is a bug that only shows up as sources going dark:
        # six Stack Exchange sources sat unusable behind a gate whose permits never came back
        # (2026-09-09), and there was nothing in the logs to tell the two apart. _value is CPython's
        # free-permit counter, read defensively because it is not API.
        logger.warning("gate saturated: no permit after %.1fs (free=%s)",
                       left, getattr(gate, "_value", "?"))
        raise on_busy(max_wait)
    handle = HoldHandle(gate, lease, _request_s(request_s, max_wait)) if lease is not None else None
    token = _register_hold(handle) if handle is not None else None
    try:
        yield
    finally:
        if handle is not None:
            _unregister_hold(handle, token)
            gate.give_back(lease)
        else:
            gate.release()


@contextlib.asynccontextmanager
async def bounded_async_slot(
    gate,
    max_wait: float,
    on_busy: Callable[[float], BaseException],
    *,
    request_s: Optional[float] = None,
):
    """Async, cancellation-safe twin of bounded_slot: the permit is taken inside this coroutine (a
    ``FairPermits`` gate in its line with a lease, any other gate by polling, ``_apoll_acquire``),
    so no cancellation can leave it taken, and no worker thread waits. A saturated gate fails fast at
    ``max_wait`` (or the caller's deadline); a deadline already past refuses at once, as the sync
    twin does (review N4)."""
    end = wait_until(max_wait)
    if end - time.monotonic() <= 0:
        raise on_busy(0.0)
    if isinstance(gate, FairPermits):
        lease = await gate.atake(_holder(), end, _lease_end(end, request_s, max_wait))
        acquired = lease is not None
    else:
        lease, acquired = None, await _apoll_acquire(gate, end)
    if not acquired:
        raise on_busy(max_wait)
    handle = HoldHandle(gate, lease, _request_s(request_s, max_wait)) if lease is not None else None
    token = _register_hold(handle) if handle is not None else None
    try:
        yield
    finally:
        if handle is not None:
            _unregister_hold(handle, token)
            gate.give_back(lease)
        else:
            gate.release()


class Reservation:
    """One reserved request start: when it is (``start``, monotonic), how long the caller waits for it,
    and the pacer state before and after, so an unsent request can hand it back (``BackendGuard.refund``)."""

    __slots__ = ("start", "wait", "prev_next_at", "next_at")

    def __init__(self, start: float, wait: float, prev_next_at: float, next_at: float) -> None:
        self.start, self.wait, self.prev_next_at, self.next_at = start, wait, prev_next_at, next_at


class BackendGuard:
    """One backend's concurrency cap + rate pacer + circuit breaker.

    ``name`` names the backend for the breaker-open log line. ``max_inflight`` sizes the
    ``FairPermits`` (one first-come-first-served line, leased permits). ``break_after`` /
    ``break_for_s`` are the consecutive-failure count that
    opens the circuit and the seconds it stays open. ``min_interval_s`` is the minimum spacing
    between request STARTS enforced by ``pace()`` (0.0 = no pacing, for a semaphore-only user).
    ``extra_state`` is merged into ``.state`` for backends that carry extra breaker-adjacent fields
    (OpenAlex's per-bucket ``dry_until``). ``log`` overrides the logger the breaker-open warning is
    emitted through, so a backend can keep its own module logger name.

    ``windows`` is an optional list of ``(limit, seconds)`` budgets: at most ``limit`` request STARTS
    in any ``seconds``-long window (an upstream's "25 per minute" / "600 per 5 minutes" / "10,000 per
    hour"). Enforced by the same slot reservation as ``min_interval_s``, so it holds across every
    caller, thread and event loop, and it costs nothing until a window is actually full (unlike a
    min-interval translation of the same budget, which would slow every burst).
    """

    def __init__(self, name: str, max_inflight: int, break_after: int = 5,
                 break_for_s: float = 120.0, min_interval_s: float = 0.0,
                 extra_state: Optional[dict] = None,
                 log: Optional[logging.Logger] = None,
                 windows: Optional[Iterable[tuple]] = None) -> None:
        self.name = name
        self.max_inflight = max_inflight
        self.break_after = break_after
        self.break_for_s = break_for_s
        self.min_interval_s = min_interval_s
        self._log = log or logger
        # (limit, seconds) budgets and, per budget, the start times of the last ``limit`` reserved
        # requests (a deque capped at ``limit``: an older start can never constrain a future one).
        self.windows: list[tuple[int, float]] = [(int(n), float(s)) for n, s in (windows or ())]
        # Pruned by age (_window_start), not by count, so a reservation that is handed back
        # (refund) can be removed without resurrecting an entry a count limit had dropped.
        self._win_logs = [collections.deque() for _ in self.windows]

        # Concurrency cap: at most ``max_inflight`` in-flight calls across all callers + threads, one
        # first-come-first-served line for sync and async callers, every permit on a lease.
        self.sema = FairPermits(max_inflight, name)

        # Breaker state + its lock. The dict the health probes read directly (fails / open_until /
        # last_429) plus any backend-specific extras merged in.
        self.state: dict = {"fails": 0, "open_until": 0.0, "last_429": 0.0}
        if extra_state:
            self.state.update(extra_state)
        self.lock = threading.Lock()

        # Rate pacer: reserve the next request-start slot under this lock, then sleep (outside it).
        # ``floor``: the latest start a Retry-After (defer) asked for; a refund never rolls the next
        # start back below it (review N3).
        self.pace_state: dict = {"next_at": 0.0, "floor": 0.0}
        self.pace_lock = threading.Lock()

    # ── rate pacer ────────────────────────────────────────────────────────────
    def reserve_pace_slot(
            self, on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None, *,
            until: Optional[float] = None) -> float:
        """Reserve the next request-start slot (>= ``min_interval_s`` after the previous) UNDER the pace
        lock and return the seconds to WAIT for it. The caller does the wait itself: a sync caller via
        ``time.sleep`` (see ``pace``), a native-async caller via ``await anyio.sleep`` -- so the async
        egress can honor the SAME shared rate gate WITHOUT holding a thread during the wait. The
        reservation (pure sync arithmetic under a brief lock, no IO) is safe on the event loop.

        ``until`` (monotonic, optional): a start later than this is not reserved; ``on_backlog(wait)``
        (the exception to raise, or None for a GateBusy) is raised instead (shed load + fail fast).
        """
        return self.reserve(on_backlog, until=until).wait

    def reserve(self, on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None, *,
                until: Optional[float] = None) -> "Reservation":
        """Reserve the next request start (>= ``min_interval_s`` after the previous, room in every
        window) UNDER the pace lock and return it; the caller waits ``.wait`` itself. When the start
        would come after ``until`` (the caller's budget, monotonic), nothing is reserved (the backlog
        drains instead of growing) and ``on_backlog(wait)`` (or a GateBusy) is raised."""
        with self.pace_lock:
            now = time.monotonic()
            start = self._window_start(max(now, self.pace_state["next_at"]), now)
            wait = start - now
            if until is not None and start > until:
                raise ((on_backlog(wait) if on_backlog is not None else None)
                       or GateBusy(f"{self.name}: next allowed start {wait:.1f}s away, past the "
                                   "caller's budget; not sent"))
            res = Reservation(start, wait, self.pace_state["next_at"], start + self.min_interval_s)
            self.pace_state["next_at"] = res.next_at
            for log in self._win_logs:
                log.append(start)
        return res

    def refund(self, res: "Reservation") -> None:
        """Hand back a reservation whose request never left (review F8): its place in every window,
        and the start slot itself when nothing has been reserved after it, never earlier than the
        latest Retry-After asked for (review N3)."""
        with self.pace_lock:
            for log in self._win_logs:
                try:
                    log.remove(res.start)
                except ValueError:
                    pass
            if self.pace_state["next_at"] == res.next_at:
                self.pace_state["next_at"] = max(res.prev_next_at, self.pace_state.get("floor", 0.0))

    def _window_start(self, start: float, now: float) -> float:
        """The earliest start >= ``start`` that leaves room in every (limit, seconds) window. Called
        under the pace lock. Entries at or before ``now - seconds`` can never constrain a start at or
        after ``now`` and are dropped. Reserved starts never decrease (each is >= the previous
        next_at, and a refund rolls next_at back only when nothing was reserved after it), so each
        log is sorted and the entry ``limit`` places from its end is the one a new start must clear
        by ``seconds``. Pushing ``start`` for one window can only help the others, so a few passes
        always settle."""
        for (_limit, secs), log in zip(self.windows, self._win_logs):
            while log and log[0] <= now - secs:
                log.popleft()
        for _ in range(len(self._win_logs) + 1):
            moved = False
            for (limit, secs), log in zip(self.windows, self._win_logs):
                if len(log) >= limit and start < log[-limit] + secs:
                    start = log[-limit] + secs
                    moved = True
            if not moved:
                break
        return start

    def defer(self, seconds: float) -> None:
        """Push the next allowed request start at least ``seconds`` into the future, for EVERY caller
        (an upstream's Retry-After or Stack Exchange's ``backoff``). A caller that would then wait
        past its own budget sheds through its ``on_backlog`` instead of hanging."""
        if not seconds or seconds <= 0:
            return
        with self.pace_lock:
            at = time.monotonic() + float(seconds)
            self.pace_state["next_at"] = max(self.pace_state["next_at"], at)
            self.pace_state["floor"] = max(self.pace_state.get("floor", 0.0), at)

    # ── who holds it + permit-first hold (the one entry point for a declared upstream) ─────────────
    def held(self) -> bool:
        """True iff the current holder (task, else thread) holds a live permit of this guard. The
        record lives in the gate (review P2): it ends with the permit, whichever context or thread
        gives the permit back (or when the lease runs out). A child task or thread is not the holder."""
        try:
            return self.sema.held_by(_holder())
        except Exception:  # noqa: BLE001 (a read of the record must never break an egress)
            return False

    @staticmethod
    def _late(on_busy, on_backlog):
        return lambda w: (on_backlog(w) if on_backlog else None) or on_busy(w)

    def _bounds(self, max_wait: float, until: Optional[float], request_s: Optional[float],
                wait: bool) -> "tuple[float, float, float]":
        """(end of the waiting budget, seconds of it left, when the lease ends)."""
        end = (wait_until(max_wait) if until is None else until) if wait else time.monotonic()
        return end, end - time.monotonic(), _lease_end(end, request_s, max_wait)

    @contextlib.contextmanager
    def hold(self, max_wait: float, on_busy: Callable[[float], BaseException],
             on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None, *,
             until: Optional[float] = None, request_s: Optional[float] = None, wait: bool = True):
        """Take a concurrency permit FIRST, then reserve and wait for the request-start slot WHILE
        holding it, then run the body; release on exit.

        The order is the point. Pacing before the permit (``pace()`` then ``slot()``) lets callers
        whose slots were reserved while every permit was busy start back to back the moment a slow
        request frees one, so starts can land closer than ``min_interval_s``. Holding the permit
        while waiting for the slot means every start is at least ``min_interval_s`` after the
        previous one, and with ``max_inflight=1`` the upstream sees exactly one request at a time.

        ONE budget for both waits (driver ruling 2): the permit wait (in the gate's one line, review
        N1) and the start wait together end at ``until`` (monotonic), by default ``max_wait`` from now
        or the caller's deadline if sooner. No permit by then raises ``on_busy``; a start slot past it
        raises ``on_backlog(wait)`` (or ``on_busy``) at once, reserving nothing; a budget already used
        up before the gate is tried raises ``on_backlog(0.0)`` (past this call's budget, not a
        saturated gate). ``wait=False`` tries once: a permit free now with nobody in line and a start
        slot open now, else the same errors at once (review P6: a caller holding a place in another
        line must not wait here).

        LEASE (review P1): the permit's lease ends at the end of the waiting budget plus ``request_s``
        (the request's own timeout; the declared ``max_wait`` when the caller cannot say it); then the
        gate takes the permit back even if this holder never returns.

        Yields a ``HoldHandle`` (None when re-entered). A request that never left (an exception
        registered with ``register_unsent``, or a cancel before the body) hands its reservation back,
        unless a response already arrived in this context while it was held (review N2).
        Re-entrant per holder: inside an outer ``hold`` of the same guard it is a no-op."""
        if self.held():
            yield None
            return
        late = self._late(on_busy, on_backlog)
        end, left, lease_end = self._bounds(max_wait, until, request_s, wait)
        if wait and left <= 0:   # the budget was used up before the gate was tried: say so
            raise late(0.0)
        lease = self.sema.take(_holder(), end if wait else None, lease_end)
        if lease is None:
            raise on_busy(max(0.0, left))
        try:
            res = self.reserve(late, until=end if wait else None)
            if not wait and res.wait > 0:
                self.refund(res)
                raise late(res.wait)
            handle = HoldHandle(self.sema, lease, _request_s(request_s, max_wait))
            token = _register_hold(handle)
            in_body = False
            try:
                if res.wait > 0:
                    time.sleep(res.wait)
                in_body = True
                yield handle
            except BaseException as exc:
                if (not in_body or isinstance(exc, _UNSENT)) and not handle.sent:
                    self.refund(res)
                raise
            finally:
                _unregister_hold(handle, token)
        finally:
            self.sema.give_back(lease)

    @contextlib.asynccontextmanager
    async def ahold(self, max_wait: float, on_busy: Callable[[float], BaseException],
                    on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None, *,
                    until: Optional[float] = None, request_s: Optional[float] = None,
                    wait: bool = True):
        """Async twin of ``hold``: the permit is waited for in the same one line, on a future of the
        running loop (no worker thread, no polling, and no cancellation can leave it taken), the slot
        wait is ``await anyio.sleep``; same order, budget, lease, try-once mode, refund and
        re-entrancy."""
        if self.held():
            yield None
            return
        late = self._late(on_busy, on_backlog)
        end, left, lease_end = self._bounds(max_wait, until, request_s, wait)
        if wait and left <= 0:   # the budget was used up before the gate was tried: say so
            raise late(0.0)
        lease = await self.sema.atake(_holder(), end if wait else None, lease_end)
        if lease is None:
            raise on_busy(max(0.0, left))
        try:
            res = self.reserve(late, until=end if wait else None)
            if not wait and res.wait > 0:
                self.refund(res)
                raise late(res.wait)
            handle = HoldHandle(self.sema, lease, _request_s(request_s, max_wait))
            token = _register_hold(handle)
            in_body = False
            try:
                if res.wait > 0:
                    await anyio.sleep(res.wait)
                in_body = True
                yield handle
            except BaseException as exc:
                if (not in_body or isinstance(exc, _UNSENT)) and not handle.sent:
                    self.refund(res)
                raise
            finally:
                _unregister_hold(handle, token)
        finally:
            self.sema.give_back(lease)

    def snapshot(self) -> dict:
        """A read-only view for the health block: the enforced numbers and how loaded they are now."""
        reclaim = getattr(self.sema, "reclaim_expired", None)
        if reclaim is not None:
            reclaim()   # a permit whose lease ran out is not in flight any more
        now = time.monotonic()
        with self.pace_lock:
            backlog = max(0.0, self.pace_state["next_at"] - now)
            used = [sum(1 for t in log if t > now - secs)
                    for (_, secs), log in zip(self.windows, self._win_logs)]
        return {
            "max_inflight": self.max_inflight,
            "in_flight": self.max_inflight - int(getattr(self.sema, "_value", self.max_inflight)),
            "min_interval_s": self.min_interval_s,
            "windows": [{"limit": n, "seconds": s, "used": u}
                        for (n, s), u in zip(self.windows, used)],
            "backlog_s": round(backlog, 2),
            "breaker_open": self.is_open(),
            # permits the gate took back from holders whose lease ran out (review P1)
            "leases_reclaimed": int(getattr(self.sema, "reclaimed", 0)),
        }

    def pace(self, on_backlog: Optional[Callable[[float], Optional[BaseException]]] = None, *,
             max_wait: Optional[float] = None, until: Optional[float] = None) -> "Reservation":
        """Reserve the next request-start slot (>= ``min_interval_s`` after the previous), then wait
        for it. The slot reservation is under the lock; the wait is NOT, so callers do not serialize
        on the lock itself, only on the wire-rate. Bounds requests/second across all callers + threads.

        The wait is bounded (driver ruling 2): by ``until`` if given, else ``max_wait`` from now cut to
        the caller's deadline, else the caller's deadline alone. A start past that bound is not
        reserved; ``on_backlog(wait)`` (or a GateBusy) is raised instead, so a pathological backlog
        sheds load and drains. Returns the reservation (hand it to ``refund`` if the request is then
        not sent)."""
        if until is None:
            until = wait_until(max_wait) if max_wait is not None else _deadline_var.get()
        res = self.reserve(on_backlog, until=until)
        if res.wait > 0:
            time.sleep(res.wait)
        return res

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
    # and the async acquire waits in the gate's one line on a future of its own loop (FairPermits.atake;
    # it used to be a shielded worker-thread acquire, which asyncio's own cancel could still separate
    # from the release: review F1, 2026-09-29), so no cancel can separate the acquire from the release.
    @contextlib.contextmanager
    def slot(self, max_wait: float, on_busy: Callable[[float], BaseException], *,
             until: Optional[float] = None, request_s: Optional[float] = None):
        """Bounded concurrency-cap acquire (sync): wait in the gate's one line until ``until``
        (default ``max_wait`` from now, cut to the caller's deadline) for a permit; if none comes in
        time, raise ``on_busy`` (fail fast -> the caller degrades to []/None) instead of blocking
        unboundedly. The permit is on a lease (see ``hold``). Guarantees release."""
        end, left, lease_end = self._bounds(max_wait, until, request_s, True)
        if left <= 0:
            raise on_busy(0.0)
        lease = self.sema.take(_holder(), end, lease_end)
        if lease is None:
            raise on_busy(left)
        handle = HoldHandle(self.sema, lease, _request_s(request_s, max_wait))
        token = _register_hold(handle)   # the request's progress renews the lease
        try:
            yield
        finally:
            _unregister_hold(handle, token)
            self.sema.give_back(lease)

    @contextlib.asynccontextmanager
    async def aslot(self, max_wait: float, on_busy: Callable[[float], BaseException], *,
                    until: Optional[float] = None, request_s: Optional[float] = None):
        """Bounded + cancellation-safe concurrency-cap acquire (async): waits in the same one line on
        a future of the running loop, so no cancellation (AnyIO's or asyncio's own) can leave the
        permit taken and no worker thread waits. Bounded and leased like ``slot``; a deadline already
        past refuses at once, as ``slot`` does (review N4)."""
        end, left, lease_end = self._bounds(max_wait, until, request_s, True)
        if left <= 0:
            raise on_busy(0.0)
        lease = await self.sema.atake(_holder(), end, lease_end)
        if lease is None:
            raise on_busy(left)
        handle = HoldHandle(self.sema, lease, _request_s(request_s, max_wait))
        token = _register_hold(handle)
        try:
            yield
        finally:
            _unregister_hold(handle, token)
            self.sema.give_back(lease)

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
