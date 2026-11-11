"""Process memory ceiling for the long-lived eye HTTP service (the backstop behind the root fixes).

The root fixes live in eye.recall.embed: a fixed token-length ladder bounds the MPS graph cache, and
an empty_cache after every forward stops the MPS allocator from hoarding freed blocks (its default
low watermark on this 16 GB machine is ~17 GB, so it never gives memory back on its own). Those
bound the growth paths that were MEASURED. This module bounds the ones that were not:
a future model, a leak nobody has found yet. It watches this process's physical footprint (the
same number Activity Monitor and `footprint` report, read through omo_proc.rusage) plus that of the
process lane's worker processes it started (eye.portal.run_cpu; lane_pids below) and, past a ceiling,
restarts the process the way a deploy does: SIGTERM to itself, uvicorn's graceful shutdown, the
lifespan drain (eye.lifecycle.drain_all flushes the recall writer), then launchd (KeepAlive=true,
ThrottleInterval=30) starts a fresh process.

Two thresholds, both derived from the machine budget (see SOFT_MB_DEFAULT for the derivation and the
calibration plan). Speech recognition is not under them: it runs in its own worker process with its
own cap (eye.asr_sched), outside OmniSeek's footprint, since 2026-11-10.
  soft: first release device caches; if still over, restart at the next idle moment (no call in
        flight; the MCP clients' long-lived GET listening streams do not count as calls), so a restart
        never lands in the middle of an agent's call.
  hard: restart now, idle or not. The restart is the graceful one a deploy gets: SIGTERM to self, so
        eye.lifecycle's caps apply (in-flight calls get drain_cap_s() to finish, the lifespan shutdown
        shutdown_cap_s()); a call still running past them is dropped (the caller retries); documents
        already handed to the recall writer are safe, because they reach the observation journal first
        and the next process materializes them. Every restart also arms an exit watchdog
        (exit_grace_s(), 70 s with the default caps, under the plist's ExitTimeOut of 80 s) for the case
        where that path never starts.
Every crossing writes ONE line (memguard: <what> | fp_mb rss_mb workers workers_mb inflight streams
embed mps_driver_mb mps_cur_mb asr_worker asr_worker_mb threads soft hard uptime_s; fp_mb and rss_mb
are OmniSeek's own process, workers_mb the process-lane workers together, asr_worker_mb the speech
worker (shown, not counted), and <what> names the sum), so a restart can be matched to what held the
memory. A restart ends the workers with the process (anyio ends them with the
event loop).
A threshold has to hold for several consecutive samples before it acts, so a short spike (a matrix
rebuild, one large forward) does not cost a restart. Both are disabled by setting them to 0.

Fail-open throughout: a platform without a footprint counter (not macOS), no loadable omo_proc
(omniseek.procs) or any reading error disables the guard and logs once; it never raises into the service.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, Optional

from omniseek import procs

log = logging.getLogger("omniseek.core.memguard")

# Ceilings, MB of phys_footprint of OmniSeek's process PLUS its process-lane workers (omniseek_footprint_mb),
# DERIVED FROM THE MACHINE BUDGET (10-11;
# derived again 11-04, once the workers were counted). The host machine has 16384 MB. Over 10-08 to 10-11 (memlog, 4427 samples) the
# wired pages alone were 7.69 GiB at p50, the admission rule keeps 15% (2458 MB) free, and the p50 of 9
# Copilot workers at 504 MB each is 4536 MB: 16384 - 7875 - 2458 - 4536 = 1515 MB is left for OmniSeek,
# less than its own healthy footprint (p50 2352). The machine is already in deficit at p50 (swap p50
# 6.6 GiB), so every MB OmniSeek holds above its healthy steady state is paid in swap, MB for MB. Hence:
#   soft = W0 + one worker, rounded DOWN to a 256 MB step. W0 = the largest steady footprint of a clean
#          production eye without ASR: 3261 MB for its own process (the local replay settled at 3071),
#          plus its process-lane workers, which since 11-04 do the parsing and the ranking's text scoring
#          and stay until 5 idle minutes end them: PROC_LANE_TOKENS = 2 workers at the size they keep after
#          broad searches, about 340 MB each live (read 11-04 01:00 after the proc-lane deploy), 680 MB.
#          W0 = 3261 + 680 = 3941 MB. (3261 is a process that had never loaded ASR, so moving ASR out of
#          the process, 2026-11-10, leaves W0 as it is.) One worker (504 MB, the p50 Copilot process) is the most OmniSeek may
#          hold over its steady state before it acts: 3941 + 504 = 4445, rounded down to 4352 MB
#          (4.25 GiB; it was 3584 before the workers were counted, the same derivation without the 680).
#          Over soft the guard releases device caches and, if still over, restarts at the next idle moment
#          (no call in flight).
#   hard = soft + the largest legitimate transient above the settled level WITHOUT ASR (948 MB, a repeated
#          broad search: 2860 settled, 3808 peak), rounded UP to a 256 MB step: 4352 + 948 = 5300, so
#          5376 MB (5.25 GiB; was 4608). Nothing is added for the workers' own transient: their 340 MB was
#          read after broad searches, i.e. it already holds what a search leaves in them. Over hard the
#          guard restarts now, idle or not.
#          The 680 MB the workers add is paid like the rest: the budget above left 1515 MB for OmniSeek at
#          p50, so W0 was already over it, now by 680 MB more, in swap. The lever on that is the lane's
#          size (PROC_LANE_TOKENS, ASYNC-CORE-DESIGN.md D14), not this line.
#   No ASR allowance (2026-11-10). Until then hard rose by 3840 MB (to 9216) while an ASR model was
#          loaded in this process, and soft waited for its idle unload. Recognition now runs in a speech
#          worker process with its own cap (eye.asr_sched.worker_cap_mb), so OmniSeek's own footprint has
#          no ASR term and the plain lines above hold at all times.
# PROVISIONAL: soft and hard since 11-04, because the workers' 340 MB is one live reading of two workers.
# Calibration plan for both: every crossing writes one line with fp, RSS, in flight and the MPS
# allocations; after deploy, (a) a soft restart more often than once a day means W0 moved and the
# memory path behind it needs a look before the line does; (b) a hard restart means a transient above
# 948 MB, and the call in flight names it; (c) the line's workers_mb is the workers' part: settling well
# above 680 (two workers) means their steady size moved, and W0 with it.
# OMNISEEK_MEM_SOFT_MB / OMNISEEK_MEM_HARD_MB override these (0 disables); the service launcher
# (scripts/services.py) does not set them, so these defaults are what runs.
SOFT_MB_DEFAULT = 4352
HARD_MB_DEFAULT = 5376
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
# PROVISIONAL. The fallback for exit_grace_s() when the lifecycle caps are turned off (0), the only case
# in which nothing else bounds the exit. Derivation: the lifespan drain budget is 5 s
# (lifecycle.drain_all); the clean exits measured in the restart drill took 1 to 6 s from SIGTERM; 30 s
# is 5x the slowest. Calibration plan: count the "memguard: forced exit(1)" lines after deploy; any hit
# on a soft (idle) restart means a clean exit took longer than the grace and the drain needs a look.
EXIT_GRACE_S = 30.0

def _rusage(pid: Optional[int] = None) -> Optional[dict]:
    """{"footprint", "resident"} in bytes for `pid` (default: this process), read through
    omo_proc (the one place that asks the kernel about processes; omniseek.procs finds it), or
    None when it cannot be read (no copy of omo_proc, a reading fault). Linux has no footprint (None)."""
    try:
        lib = procs.lib()
        return None if lib is None else lib.rusage(pid or os.getpid())
    except Exception:  # noqa: BLE001 -- a reading fault disables the guard, never the service
        return None


def footprint_mb(pid: Optional[int] = None) -> Optional[float]:
    """phys_footprint of `pid` (default: this process) in MB, or None where unavailable."""
    info = _rusage(pid)
    fp = None if info is None else info.get("footprint")
    return None if fp is None else fp / (1024 * 1024)


def lane_pids() -> list:
    """The process lane's workers that are alive now and children of this process. The candidates are
    the pids the workers reported (eye.portal.LANE_PIDS); a pid that ended, or that now belongs to a
    process that is not our child (a reused number), leaves that set here."""
    portal = sys.modules.get("omniseek.core.portal")
    if portal is None:
        return []
    lib = procs.lib()
    if lib is None:
        return []
    me = os.getpid()
    out = []
    for pid in list(portal.LANE_PIDS):
        try:
            info = lib.short_info(pid)
        except Exception:  # noqa: BLE001 -- unreadable: not counted, kept for the next sample
            continue
        # None: ended, or another user's process (our own children are always readable); a live worker
        # that was dropped by mistake comes back with its next result
        if info is None or info.get("ppid") != me or info.get("zombie"):
            portal.LANE_PIDS.discard(pid)
        else:
            out.append(pid)
    return out


def workers_mb(pids: Optional[list] = None) -> float:
    """phys_footprint of the process lane's live workers together, in MB (0 when there are none)."""
    return sum(footprint_mb(p) or 0.0 for p in (lane_pids() if pids is None else pids))


def omniseek_footprint_mb() -> Optional[float]:
    """What the thresholds are compared with: this process's footprint plus its process-lane workers'.
    None when this process's own footprint cannot be read (the guard is then off)."""
    own = footprint_mb()
    return None if own is None else own + workers_mb()


def resident_mb(pid: Optional[int] = None) -> Optional[float]:
    """Resident set size (RSS) of `pid` in MB, or None where unavailable. Logged next to the footprint
    on every crossing: RSS leaves out swapped and compressed pages and the GPU memory the footprint
    counts, so the two together tell "swapped out" from "given back"."""
    info = _rusage(pid)
    rss = None if info is None else info.get("resident")
    return None if rss is None else rss / (1024 * 1024)


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


_streams = 0   # open GET listening streams (not counted as in flight, see _is_listening_stream)


def streams() -> int:
    return _streams


def _stream_delta(d: int) -> None:
    global _streams
    with _inflight_lock:
        _streams = max(0, _streams + d)


def _is_listening_stream(scope) -> bool:
    """A GET with Accept: text/event-stream is the MCP client's long-lived listening stream: it stays
    open for the life of the client window and carries no tool call. Counting it as in flight kept the
    count above zero for hours on 10-11 (the live access log has 3246 such GETs), so the soft restart
    scheduled at 14:37:07 never found an idle moment and the process went on to a hard restart."""
    if scope.get("method") != "GET":
        return False
    for k, v in scope.get("headers") or ():
        if k.lower() == b"accept" and b"text/event-stream" in v.lower():
            return True
    return False


class InflightMiddleware:
    """Counts HTTP requests from arrival until the final response body chunk (or an exception).
    Listening streams (see _is_listening_stream) are counted apart, in streams()."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        if _is_listening_stream(scope):
            _stream_delta(1)
            try:
                return await self.app(scope, receive, send)
            finally:
                _stream_delta(-1)
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


def exit_grace_s() -> float:
    """Seconds the restart's own watchdog waits before forcing the exit. A SIGTERM to self goes through
    the same uvicorn handler as a deploy's, so eye.lifecycle's caps already bound the exit: in-flight
    calls get up to drain_cap_s() to finish, the lifespan shutdown up to shutdown_cap_s(), each with a
    BACKSTOP_EXTRA_S backstop (30 + 30 + 5 = 65 s). This watchdog only covers the case where that path
    never starts (no handler installed, a dead event loop), so it waits for the whole lifecycle bound
    plus one more BACKSTOP_EXTRA_S: 70 s by default, under the plist's ExitTimeOut (80 s). With the
    lifecycle caps turned off (0) nothing else bounds the exit, and EXIT_GRACE_S applies."""
    try:
        from omniseek.core import lifecycle
        drain, shut = lifecycle.drain_cap_s(), lifecycle.shutdown_cap_s()
        if drain > 0 and shut > 0:
            return drain + shut + 2 * lifecycle.BACKSTOP_EXTRA_S
    except Exception:  # noqa: BLE001
        pass
    return EXIT_GRACE_S


def _restart_self() -> None:
    _arm_exit_watchdog(exit_grace_s())
    os.kill(os.getpid(), signal.SIGTERM)


def _asr_worker() -> tuple:
    """(pid, footprint MB) of the speech worker this process started, or (None, None) when none runs
    (never imports asr_sched: if it is not imported, there is none). Shown on the line, not counted:
    the worker has its own cap."""
    sched = sys.modules.get("omniseek.core.asr_sched")
    w = getattr(getattr(sched, "POOL", None), "worker", None)
    if w is None or not w.alive():
        return None, None
    fp = footprint_mb(w.pid)
    return w.pid, None if fp is None else round(fp)


def snapshot() -> dict:
    """What the crossing line records: footprint and RSS, calls in flight and open listening streams,
    the large objects (embedder, MPS allocations, the speech worker), threads and uptime. Never raises."""
    out: dict = {}
    try:
        fp, rss = footprint_mb(), resident_mb()
        out["fp_mb"] = None if fp is None else round(fp)
        out["rss_mb"] = None if rss is None else round(rss)
        pids = lane_pids()
        out["workers"] = len(pids)
        out["workers_mb"] = round(workers_mb(pids))
        out["inflight"] = inflight()
        out["streams"] = streams()
        emb = sys.modules.get("omniseek.core.recall.embed")
        out["embed"] = "loaded" if emb is not None and getattr(emb, "_model", None) is not None else "none"
        torch = sys.modules.get("torch")
        if torch is not None and torch.backends.mps.is_available():
            out["mps_driver_mb"] = round(torch.mps.driver_allocated_memory() / 2**20)
            out["mps_cur_mb"] = round(torch.mps.current_allocated_memory() / 2**20)
        pid, mb = _asr_worker()
        out["asr_worker"] = pid or "none"
        if mb is not None:
            out["asr_worker_mb"] = mb
        out["threads"] = threading.active_count()
    except Exception as exc:  # noqa: BLE001
        out["snapshot_error"] = str(exc)[:80]
    return out


def _fmt(snap: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in snap.items())


class MemGuard:
    def __init__(self, soft_mb: int, hard_mb: int, *,
                 read: Callable[[], Optional[float]] = omniseek_footprint_mb,
                 busy: Callable[[], int] = inflight,
                 release: Callable[[], None] = _release_device_caches,
                 restart: Callable[[], None] = _restart_self,
                 clock: Callable[[], float] = time.monotonic,
                 min_uptime_s: float = MIN_UPTIME_S,
                 snap: Callable[[], dict] = snapshot):
        self.soft_mb, self.hard_mb = soft_mb, hard_mb
        self._read, self._busy, self._release, self._restart = read, busy, release, restart
        self._snap = snap
        self._clock = clock
        self._born = clock()
        self.min_uptime_s = min_uptime_s
        self.soft_hits = 0
        self.hard_hits = 0
        self.low_hits = 0         # consecutive samples under soft
        self.released = False
        self.pending = False      # soft restart decided, waiting for an idle moment
        self.fired: Optional[str] = None
        self.checks = 0
        self.last_mb: Optional[float] = None

    def _line(self, what: str, mb: float) -> None:
        """The one line per crossing: what happened, then the snapshot (footprint, RSS, in flight,
        streams, embedder, MPS allocations, speech worker, threads) and the uptime."""
        snap = {}
        try:
            snap = dict(self._snap() or {})
        except Exception as exc:  # noqa: BLE001
            snap = {"snapshot_error": str(exc)[:80]}
        snap.setdefault("fp_mb", round(mb))
        snap["soft"], snap["hard"] = self.soft_mb, self.hard_mb
        snap["uptime_s"] = round(self._clock() - self._born)
        log.warning("memguard: %s | %s", what, _fmt(snap))

    def check(self) -> Optional[str]:
        """One sample. Returns "hard" / "soft" when it restarted, else None."""
        if self.fired:
            return None
        mb = self._read()
        self.checks += 1
        self.last_mb = mb
        if mb is None:
            return None
        hard = self.hard_mb
        if self.checks % LOG_EVERY == 1:  # the boot-time first sample stays out of the one INFO boot line
            (log.debug if self.checks == 1 else log.info)("memguard: footprint %.0f MB (soft %d, hard %d, in-flight %d)",
                     mb, self.soft_mb, hard, self._busy())
        self.hard_hits = self.hard_hits + 1 if hard and mb >= hard else 0
        self.soft_hits = self.soft_hits + 1 if self.soft_mb and mb >= self.soft_mb else 0
        # A decision is cleared only after the footprint has stayed under soft for as many samples as
        # it took to make it: one sample dipping under the line (a forward just ended, an allocation
        # freed for a moment) no longer cancels a scheduled restart.
        self.low_hits = 0 if self.soft_hits else self.low_hits + 1
        if self.low_hits >= SOFT_CONSECUTIVE:
            self.released = self.pending = False
        old_enough = self._clock() - self._born >= self.min_uptime_s
        if self.hard_hits >= HARD_CONSECUTIVE:
            if not old_enough:
                log.warning("memguard: footprint %.0f MB over hard %d MB within %.0fs of start; "
                            "not restarting (would loop)", mb, hard, self.min_uptime_s)
                return None
            return self._fire("hard", mb)
        if self.soft_hits >= SOFT_CONSECUTIVE and not self.pending:
            if not self.released:
                self._line(f"footprint {mb:.0f} MB over soft {self.soft_mb} MB; releasing device caches", mb)
                self._release()
                self.released = True
                self.soft_hits = 0
                return None
            if not old_enough:
                log.warning("memguard: footprint %.0f MB over soft %d MB within %.0fs of start; "
                            "not restarting (would loop)", mb, self.soft_mb, self.min_uptime_s)
                return None
            self.pending = True
            self._line(f"footprint {mb:.0f} MB still over soft {self.soft_mb} MB after a cache release; "
                       "restart scheduled for the next idle moment", mb)
        if self.pending:
            # only while this sample is still over soft: the footprint may have come back under the
            # line since the decision, and then there is nothing to restart for
            if self.soft_hits and self._busy() == 0:
                return self._fire("soft", mb)
        return None

    def _fire(self, kind: str, mb: float) -> str:
        self.fired = kind
        self._line(f"{kind} restart at footprint {mb:.0f} MB: SIGTERM to self, launchd restarts the service", mb)
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
        log.debug("memguard: disabled (no phys_footprint reading on this platform)")
        return None
    guard = MemGuard(soft, hard)
    t = threading.Thread(target=guard.run, args=(_STOP,), name="memguard", daemon=True)
    t.start()
    try:
        from omniseek.core import lifecycle
        lifecycle.register_loop("memguard", _STOP, t)
    except Exception:  # noqa: BLE001
        pass
    log.debug("memguard: started (soft %d MB, hard %d MB, every %.0fs)", soft, hard, CHECK_INTERVAL_S)
    return t
