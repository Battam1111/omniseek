"""Process memory ceiling for the long-lived eye HTTP service (the backstop behind the root fixes).

The root fixes live in eye.recall.embed: a fixed token-length ladder bounds the MPS graph cache, and
an empty_cache after every forward stops the MPS allocator from hoarding freed blocks (its default
low watermark on this 16 GB machine is ~17 GB, so it never gives memory back on its own). Those
bound the growth paths that were MEASURED. This module bounds the ones that were not: ASR on MPS,
a future model, a leak nobody has found yet. It watches this process's physical footprint (the
same number Activity Monitor and `footprint` report, read via proc_pid_rusage) and, past a ceiling,
restarts the process the way a deploy does: SIGTERM to itself, uvicorn's graceful shutdown, the
lifespan drain (eye.lifecycle.drain_all flushes the recall writer), then launchd (KeepAlive=true,
ThrottleInterval=30) starts a fresh process.

Two thresholds, both derived from the memory budget (see SOFT_MB_DEFAULT for the derivation and
the calibration plan):
  soft: first release device caches; if still over, restart at the next idle moment (no request in
        flight), so a restart never lands in the middle of an agent's call.
  hard: restart now, idle or not. The in-flight request is NOT answered (uvicorn closes its
        connection), but its tool body keeps running in an AnyIO worker thread and the server's
        shutdown waits for that thread: a SIGTERM alone does not end a busy process (eye-mem-2
        drill: 17 s for one transcription). So every restart also arms an exit watchdog that forces
        the exit EXIT_GRACE_S after the SIGTERM. The forced exit drops the in-flight call's result
        (the caller retries); documents already handed to the recall writer are safe, because they
        reach the observation journal first and the next process materializes them.
A threshold has to hold for several consecutive samples before it acts, so a short spike (a matrix
rebuild, one large forward) does not cost a restart. Both are disabled by setting them to 0.

Fail-open throughout: a platform without proc_pid_rusage (not macOS) or any reading error disables
the guard and logs once; it never raises into the service.
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("omniseek.core.memguard")

# Ceilings, MB of phys_footprint, SET FROM THE MEMORY BUDGET (omniseek_orders/eye-mem-3/report.md,
# part 3). The budget for OmniSeek HTTP service on this 16 GB machine: stable <= 3.5 GB, peak
# <= 4.5 GB (4608 MB). A healthy process never crosses the peak budget, so:
#   soft = peak budget + 0.5 GB margin = 4608 + 512 = 5120 MB: crossing it means the process is
#          already outside the budget (a leak or an unbounded path), so release caches and restart
#          at the next idle moment; the 0.5 GB margin absorbs one sample's overshoot of a transient
#          (a matrix rebuild) without paying a restart for it.
#   hard = soft + 1.5 GB = 5120 + 1536 = 6656 MB: room for an in-flight call to finish before a
#          forced restart, still far below the point where the machine was already swapping.
# These are not tuned to observed footprints: the budget is the requirement and the ceilings follow
# from it. Calibration plan: after deploy, log the footprint every CHECK_INTERVAL_S (the guard does
# this at INFO every LOG_EVERY checks); over one full ingest cycle plus 1440 more samples, check
# whether stable and peak stay within the budget. Observation only checks the budget; if it is
# missed, the fix is in the memory path that broke it, not a higher ceiling.
# OMNISEEK_MEM_SOFT_MB / OMNISEEK_MEM_HARD_MB override these for a drill or a bench only; the
# service launcher (scripts/services.py) does not set them, so these defaults are what runs.
SOFT_MB_DEFAULT = 5120
HARD_MB_DEFAULT = 6656
CHECK_INTERVAL_S = 60.0
# Consecutive over-threshold samples before acting. 3 x 60 s rides out a matrix rebuild (its
# transient is the one known multi-sample spike); hard acts after 2 because by then swap is the cost.
SOFT_CONSECUTIVE = 3
HARD_CONSECUTIVE = 2
# No soft or hard restart in the first MIN_UPTIME_S: if a fresh process is already over the line,
# restarting every ThrottleInterval would be a loop, not a fix; it logs and waits instead.
MIN_UPTIME_S = 600.0
IDLE_POLL_S = 1.0
LOG_EVERY = 10
# PROVISIONAL. Seconds between the restart's SIGTERM and a forced os._exit when the process is still
# alive. Derivation: the lifespan drain budget is 5 s (lifecycle.drain_all); the clean exits measured in
# OmniSeek-mem-2 drill took 1 to 6 s from SIGTERM; 30 s is 5x the slowest. Calibration plan: count the
# "memguard: forced exit(1)" lines after deploy; any hit on a soft (idle) restart means a clean exit took longer
# than 30 s and the value or the drain needs a second look.
EXIT_GRACE_S = 30.0

_RUSAGE_INFO_V2 = 2


class _RusageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


_libproc = None


def footprint_mb(pid: Optional[int] = None) -> Optional[float]:
    """phys_footprint of `pid` (default: this process) in MB, or None where unavailable."""
    global _libproc
    if sys.platform != "darwin":
        return None
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = _RusageInfoV2()
        rc = _libproc.proc_pid_rusage(ctypes.c_int(pid or os.getpid()), ctypes.c_int(_RUSAGE_INFO_V2),
                                      ctypes.byref(info))
        if rc != 0:
            return None
        return info.ri_phys_footprint / (1024 * 1024)
    except Exception:  # noqa: BLE001 -- a reading fault disables the guard, never the service
        return None


# ── in-flight request count (pure ASGI, so streaming bodies are counted until their last chunk) ──
_inflight = 0
_inflight_lock = threading.Lock()


def inflight() -> int:
    return _inflight


def _inc() -> None:
    global _inflight
    with _inflight_lock:
        _inflight += 1


def _dec() -> None:
    global _inflight
    with _inflight_lock:
        _inflight = max(0, _inflight - 1)


class InflightMiddleware:
    """Counts HTTP requests from arrival until the final response body chunk (or an exception)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        _inc()
        done = False

        async def _send(message):
            nonlocal done
            await send(message)
            if (not done and message.get("type") == "http.response.body"
                    and not message.get("more_body", False)):
                done = True
                _dec()

        try:
            await self.app(scope, receive, _send)
        finally:
            if not done:
                done = True
                _dec()


# ── the guard ──

def _env_mb(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("memguard: %s=%r is not an integer; using %d", name, raw, default)
        return default


def _release_device_caches() -> None:
    """Give cached MPS blocks back to the system. Only touches torch if it is already imported (the
    guard must not be the thing that loads it), and takes the embedder's forward lock so it never
    runs inside an embedding forward."""
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        if not torch.backends.mps.is_available():
            return
        from omniseek.core.recall import embed
        with embed._fwd_lock:
            torch.mps.empty_cache()
    except Exception as exc:  # noqa: BLE001
        log.debug("memguard: device cache release skipped (%s)", exc)


def _arm_exit_watchdog(grace_s: float, exit_fn: Callable[[int], None] = os._exit) -> threading.Thread:
    """A daemon thread that forces the exit if the process outlives the grace period after a restart's
    SIGTERM. A clean exit ends the process first and takes the thread with it."""
    def _wait() -> None:
        time.sleep(grace_s)
        log.warning("memguard: still alive %.0fs after SIGTERM (a busy worker thread blocks the exit); "
                    "forcing exit, launchd restarts the service", grace_s)
        for h in logging.getLogger().handlers + log.handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001
                pass
        for stream in (sys.stdout, sys.stderr):   # os._exit skips the stdio flush; the line must land
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        try:   # a raw fd write needs no logging or stdio state, so the exit always leaves one line
            os.write(2, f"memguard: forced exit(1) {grace_s:.0f}s after SIGTERM\n".encode())
        except Exception:  # noqa: BLE001
            pass
        exit_fn(1)

    t = threading.Thread(target=_wait, name="memguard-exit-watchdog", daemon=True)
    t.start()
    return t


def _restart_self() -> None:
    _arm_exit_watchdog(EXIT_GRACE_S)
    os.kill(os.getpid(), signal.SIGTERM)


class MemGuard:
    def __init__(self, soft_mb: int, hard_mb: int, *,
                 read: Callable[[], Optional[float]] = footprint_mb,
                 busy: Callable[[], int] = inflight,
                 release: Callable[[], None] = _release_device_caches,
                 restart: Callable[[], None] = _restart_self,
                 clock: Callable[[], float] = time.monotonic,
                 min_uptime_s: float = MIN_UPTIME_S):
        self.soft_mb, self.hard_mb = soft_mb, hard_mb
        self._read, self._busy, self._release, self._restart = read, busy, release, restart
        self._clock = clock
        self._born = clock()
        self.min_uptime_s = min_uptime_s
        self.soft_hits = 0
        self.hard_hits = 0
        self.released = False
        self.pending = False      # soft restart decided, waiting for an idle moment
        self.fired: Optional[str] = None
        self.checks = 0
        self.last_mb: Optional[float] = None

    def check(self) -> Optional[str]:
        """One sample. Returns "hard" / "soft" when it restarted, else None."""
        if self.fired:
            return None
        mb = self._read()
        self.checks += 1
        self.last_mb = mb
        if mb is None:
            return None
        if self.checks % LOG_EVERY == 1:
            log.info("memguard: footprint %.0f MB (soft %d, hard %d, in-flight %d)",
                     mb, self.soft_mb, self.hard_mb, self._busy())
        self.hard_hits = self.hard_hits + 1 if self.hard_mb and mb >= self.hard_mb else 0
        self.soft_hits = self.soft_hits + 1 if self.soft_mb and mb >= self.soft_mb else 0
        if not self.soft_hits:
            self.released = False
            self.pending = False
        old_enough = self._clock() - self._born >= self.min_uptime_s
        if self.hard_hits >= HARD_CONSECUTIVE:
            if not old_enough:
                log.warning("memguard: footprint %.0f MB over hard %d MB within %.0fs of start; "
                            "not restarting (would loop)", mb, self.hard_mb, self.min_uptime_s)
                return None
            return self._fire("hard", mb)
        if self.soft_hits >= SOFT_CONSECUTIVE and not self.pending:
            if not self.released:
                log.warning("memguard: footprint %.0f MB over soft %d MB; releasing device caches",
                            mb, self.soft_mb)
                self._release()
                self.released = True
                self.soft_hits = 0
                return None
            if not old_enough:
                log.warning("memguard: footprint %.0f MB over soft %d MB within %.0fs of start; "
                            "not restarting (would loop)", mb, self.soft_mb, self.min_uptime_s)
                return None
            log.warning("memguard: footprint %.0f MB still over soft %d MB after a cache release; "
                        "restart scheduled for the next idle moment", mb, self.soft_mb)
            self.pending = True
        if self.pending and self._busy() == 0:
            return self._fire("soft", mb)
        return None

    def _fire(self, kind: str, mb: float) -> str:
        self.fired = kind
        log.warning("memguard: %s restart at footprint %.0f MB (in-flight %d): SIGTERM to self, "
                    "launchd restarts the service", kind, mb, self._busy())
        self._restart()
        return kind

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set() and not self.fired:
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 -- the guard never takes the service down
                log.warning("memguard: check failed (%s)", exc)
            stop.wait(IDLE_POLL_S if self.pending else CHECK_INTERVAL_S)


_STOP = threading.Event()


def start() -> Optional[threading.Thread]:
    """Start the guard thread for the HTTP service. None when disabled or unsupported."""
    soft = _env_mb("OMNISEEK_MEM_SOFT_MB", SOFT_MB_DEFAULT)
    hard = _env_mb("OMNISEEK_MEM_HARD_MB", HARD_MB_DEFAULT)
    if not soft and not hard:
        log.info("memguard: disabled (both thresholds 0)")
        return None
    if footprint_mb() is None:
        log.info("memguard: disabled (no phys_footprint reading on this platform)")
        return None
    guard = MemGuard(soft, hard)
    t = threading.Thread(target=guard.run, args=(_STOP,), name="memguard", daemon=True)
    t.start()
    try:
        from omniseek.core import lifecycle
        lifecycle.register_loop("memguard", _STOP, t)
    except Exception:  # noqa: BLE001
        pass
    log.info("memguard: started (soft %d MB, hard %d MB, every %.0fs)", soft, hard, CHECK_INTERVAL_S)
    return t
