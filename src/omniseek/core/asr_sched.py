"""Transcription scheduling: the speech worker process, the machine-wide limit, the memory admission and
cancellation by the job that asked.

Four things, each answering an incident:

1. The worker (``Pool``). Recognition runs in one child process (omniseek.asr_worker), one job at a time;
   OmniSeek's own process never loads a speech model. A worker lives while it is used and ends when it
   has been idle ``idle_exit_s()``, when its footprint after a job leaves no room for the next job's
   transient, or when a job has to be stopped. Its memory is watched against ``worker_cap_mb()`` while a
   job runs; over the cap the worker is killed and the caller gets an error naming the cap. What the
   models leave in a process (the MPSGraph heap, 1 to 2.5 GB after long files) ends with the worker.
2. The machine-wide limit (``Scheduler``). 10-11 22:3x one job sent 13 slices of a 4 h 22 min video at
   once: 13 downloads and decodes, swap full, free memory 9%, the root disk at 3.2 GB. Now every
   transcription takes one of ``max_segments()`` slots before it downloads anything and keeps it until
   its result is in; the slots are lock files every eye process on the machine shares, so the count is
   the machine's, not one process's. Calls over the limit wait in a queue and get a receipt with their
   place and how to collect the result; the run goes on without the caller.
3. Memory admission. A transcription starts (takes its slot, and later starts a worker) only while the
   kernel's memory pressure level is not critical; otherwise it waits in the queue until the level drops.
   The level is read by omo_proc.pressure().
4. Cancellation by caller. Every transcription remembers which job asked for it (``caller``, from the
   request; see omniseek.server.omniseek_transcribe). ``cancel(caller)`` drops that caller from every
   transcription; one left with no caller is stopped: dropped from the queue, its download or decode
   process killed, the worker killed if it is that transcription's turn, its temporary files removed.
   Nothing half-done is ever in the result cache (a transcript is cached only once it is complete). The
   control socket below and omniseek.asr_ctl make this callable from outside the process.
"""

from __future__ import annotations

import atexit
import fcntl
import itertools
import json
import logging
import os
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from omniseek import procs, where

log = logging.getLogger(__name__)

# ── settings ──────────────────────────────────────────────────────────────────────────────────────

# PROVISIONAL. Transcriptions the whole machine runs at once, counting download, decode and recognition.
# Derivation: recognition is the expensive stage and runs one at a time (one worker per eye process,
# and the inference lock below makes it one per machine): a second recognition would add a second
# worker of up to its cap (5376 MB, 7168 for diarization; CAPS_DEFAULT), while the machine budget (the 10-11 memory readings, section 3.2: 16384
# MB minus 7875 wired at p50, the 15% free reserve 2458 and 9 Copilot workers at 504 MB) leaves 1515 MB
# at p50, less than one worker. With recognition serial, more transcriptions in flight cannot finish
# sooner; they only hold disk (a bilibili slice downloads the whole audio, up to 500 MB, plus a 16 kHz
# WAV of 115 MB per audio hour) and threads. One more than the recognitions keeps the next download and
# decode running while the current one is recognized, so the worker does not wait on the network:
# 1 + 1 = 2. Calibration: each finished transcription logs its queue wait, its fetch time and its
# recognition time; if the fetch regularly takes longer than the recognition before it (the worker
# idles between jobs), raise to 3; if the disk alarms during transcription bursts, lower to 1.
MAX_SEGMENTS_DEFAULT = 2
# Memory caps of the worker process (phys_footprint, MB), one per model family, and the largest transient
# of a job above the loaded models. Derivations (fresh processes; long.jsonl of the 10-11 memory readings, and
# docs/speech-worker for 11-10):
#   SenseVoice (flat and segments): loaded 2874 MB (2843 on 11-10), largest generate peak 5225 MB (10 min
#     clip; 30 and 87 min clips peaked lower; 4227 on 11-10 in the worker), so TRANSIENT 5225 - 2874 = 2351
#     MB; the peak rounded up to 256 MB is the cap: 5376.
#   Diarization (Paraformer-zh + cam++ + ct-punc + fsmn-vad, batch DIAR_BATCH_S = 30): loaded 5105 MB (its
#     load itself peaks at 6334), job peaks 6372 MB (10 min) and 6864 MB (30 min), so TRANSIENT 6864 - 5105
#     = 1759 MB; the cap is 6864 rounded up: 7168. PROVISIONAL: it grows about 25 MB per audio minute
#     (6372 to 6864 from 10 to 30 min), so a diarization much longer than 30 min may pass the cap and is
#     stopped with an error asking for a shorter slice. The 87 min run was not made (resource rule).
# Calibration: every job logs the worker's peak and every kill its footprint; a family whose jobs keep
# ending in "over its memory cap" on slices of 30 min or less needs its cap read again.
CAPS_DEFAULT = {"sv": (5376, 2351), "diar": (7168, 1759)}
_CAP_ENV = {"sv": "OMNISEEK_ASR_WORKER_CAP_MB", "diar": "OMNISEEK_ASR_DIAR_CAP_MB"}
# PROVISIONAL. A job's time limit is START_ALLOWANCE_S plus the audio length times RTF_LIMIT. Derivation:
# a cold worker spent 26 s importing torch and funasr and 6 to 14 s loading SenseVoice (the 10-11 memory
# readings, asr_probe.jsonl), about 40 s; the allowance is three times that. Recognition measured RTF 0.03 on MPS;
# 1.0 (real time) is 33 times that and also covers the CPU fallback, which was not measured. Calibration:
# every job logs its RTF; after 50 jobs set RTF_LIMIT to three times the largest seen.
START_ALLOWANCE_S = 120.0
RTF_LIMIT = 1.0
# The derivation of the idle exit is IDLE_UNLOAD_S_DEFAULT's: the same trade (hold the loaded models
# for the next call of the same session, or give them back), measured on the same transcription gaps.
# PROVISIONAL. Seconds of no job after which the worker ends. Derivation (2026-10-11, memory report
# section 3.1): the 175 transcripts in the live cache (08-11 to 10-11) give 174 gaps between one
# transcription finishing and the next finishing. They split in two: within a session (p50 52 s, p75
# 166 s) and between sessions (p90 3.4 h). 130 of 174 (75%) are <= 180 s, 140 (80%) are <= 600 s, so
# 600 s instead of 180 s saves a reload in 10 of 174 gaps and pays for it by holding about 2.8 GB for 7
# more minutes after every session. Calibration: every worker start logs "asr: worker started N s after
# the last one ended"; after 50, if more than a quarter came within 300 s, raise this toward 600 s.
IDLE_UNLOAD_S_DEFAULT = 180.0
IDLE_POLL_S = 10.0
POLL_S = 0.5            # how often a running job's worker is sampled (memory, time, cancellation)
WAIT_POLL_S = 2.0       # how often a queued transcription re-checks the slots and the memory


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def max_segments() -> int:
    return max(1, int(_env_num("OMNISEEK_ASR_MAX_SEGMENTS", MAX_SEGMENTS_DEFAULT)))


def family(op: str) -> str:
    return "diar" if op == "diar" else "sv"


def worker_cap_mb(op: str = "flat") -> float:
    fam = family(op)
    return _env_num(_CAP_ENV[fam], CAPS_DEFAULT[fam][0])


def idle_exit_s() -> float:
    """Seconds of no job after which the worker ends (0 or negative: it stays until OmniSeek ends)."""
    return _env_num("OMNISEEK_ASR_IDLE_UNLOAD_S", IDLE_UNLOAD_S_DEFAULT)


def rtf_limit() -> float:
    return _env_num("OMNISEEK_ASR_RTF_LIMIT", RTF_LIMIT)


# ── system memory ─────────────────────────────────────────────────────────────────────────────────

def memory_ok() -> tuple:
    """(ok, why): False while the kernel's memory pressure level is critical. Only the level counts, not
    the free percentage: cold pages being compressed lower the free share while the machine runs fine, and
    so it is not read as a sign of strain. The reading is omo_proc.pressure() (through
    omniseek.procs). No reading (omo_proc not loadable,
    not macOS) admits."""
    lib = procs.lib()
    read = getattr(lib, "pressure", None) if lib is not None else None
    if read is None:
        return True, ""
    try:
        level = read().get("level")
    except Exception:  # noqa: BLE001 -- a failed reading never holds a transcription
        return True, ""
    critical = getattr(lib, "PRESSURE_CRITICAL", 4)
    if level is not None and level >= critical:
        return False, f"memory pressure critical (kernel level {level})"
    return True, ""


# ── one transcription's place in the scheduler ────────────────────────────────────────────────────

_ids = itertools.count(1)


class Ticket:
    """One transcription (one single-flight run): who asked, where it is, and how to stop it."""

    def __init__(self, url: str):
        self.id = next(_ids)
        self.url = url
        self.callers: set = set()
        self.ever_callers: set = set()
        self.state = "new"          # new, queued, admitted, done, cancelled
        self.phase = "queued"       # queued, fetching_audio, transcribing, waiting_memory, done
        self.waiting_for = ""
        self.cancelled = threading.Event()
        self.slot_fd: Optional[int] = None
        self.procs: list = []       # download / decode processes, killed on cancel
        self.workdir: Optional[Path] = None
        self.t_submit = time.monotonic()
        self.t_admit: Optional[float] = None

    def add_caller(self, caller: Optional[str]) -> None:
        if caller:
            self.callers.add(caller)
            self.ever_callers.add(caller)

    def watch(self, proc) -> None:
        self.procs.append(proc)
        if self.cancelled.is_set():
            _kill(proc)

    def describe(self) -> dict:
        return {"id": self.id, "url": self.url, "callers": sorted(self.callers), "state": self.state,
                "phase": self.phase, "waiting_for": self.waiting_for,
                "age_s": round(time.monotonic() - self.t_submit, 1)}


def _kill(proc) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


class Cancelled(Exception):
    pass


# ── the machine-wide slots ────────────────────────────────────────────────────────────────────────

def _lockfile(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def _try_flock(path: Path) -> Optional[int]:
    fd = _lockfile(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _unflock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def slot_paths() -> list:
    return [where.asr_run() / "slots" / f"{i}.lock" for i in range(max_segments())]


def slots_busy() -> int:
    """Slots held by any process on the machine right now (probed without taking one)."""
    n = 0
    for p in slot_paths():
        fd = _try_flock(p)
        if fd is None:
            n += 1
        else:
            _unflock(fd)
    return n


class Scheduler:
    def __init__(self):
        self.cv = threading.Condition()
        self.queue: list = []       # Tickets waiting for a slot, first come first served
        self.active: list = []      # Tickets holding a slot
        self.counters = {"submitted": 0, "admitted": 0, "queued": 0, "cancelled": 0,
                         "memory_waits": 0}

    def _try_admit(self, t: Ticket) -> bool:
        """Caller holds cv. Takes a slot if one is free and memory allows."""
        ok, why = memory_ok()
        if not ok:
            if t.waiting_for != "memory":
                self.counters["memory_waits"] += 1
                log.info("asr: transcription %d waits for memory (%s)", t.id, why)
            t.waiting_for = "memory"
            return False
        for p in slot_paths():
            fd = _try_flock(p)
            if fd is not None:
                t.slot_fd = fd
                t.state, t.waiting_for = "admitted", ""
                t.phase = "fetching_audio"
                t.t_admit = time.monotonic()
                self.active.append(t)
                self.counters["admitted"] += 1
                return True
        t.waiting_for = "slot"
        return False

    def submit(self, t: Ticket) -> int:
        """Admit ``t`` now if it can start (returns 0) or queue it (returns its 1-based place)."""
        _ensure_control()
        with self.cv:
            self.counters["submitted"] += 1
            if not self.queue and self._try_admit(t):
                return 0
            t.state = "queued"
            self.queue.append(t)
            self.counters["queued"] += 1
            return len(self.queue)

    def position(self, t: Ticket) -> int:
        with self.cv:
            return self.queue.index(t) + 1 if t in self.queue else 0

    def wait_admitted(self, t: Ticket) -> bool:
        """Block until ``t`` holds a slot (True) or is cancelled (False). Only the head of the queue
        tries the slots, so the queue is served in order."""
        with self.cv:
            while True:
                if t.cancelled.is_set():
                    if t in self.queue:
                        self.queue.remove(t)
                        self.cv.notify_all()
                    return False
                if t.state == "admitted":
                    return True
                if self.queue and self.queue[0] is t and self._try_admit(t):
                    self.queue.pop(0)
                    self.cv.notify_all()
                    return True
                self.cv.wait(WAIT_POLL_S)

    def release(self, t: Ticket) -> None:
        with self.cv:
            if t in self.active:
                self.active.remove(t)
            if t in self.queue:
                self.queue.remove(t)
            _unflock(t.slot_fd)
            t.slot_fd = None
            if t.state != "cancelled":
                t.state = "done"
            t.phase = "done"
            self.cv.notify_all()

    def cancel(self, caller: str) -> list:
        """Drop ``caller`` from every transcription; stop each one left with no caller. Returns the
        stopped transcriptions."""
        stopped = []
        with self.cv:
            for t in list(self.queue) + list(self.active):
                if caller not in t.callers:
                    continue
                t.callers.discard(caller)
                if t.callers:
                    continue
                t.state = "cancelled"
                t.cancelled.set()
                for p in list(t.procs):
                    _kill(p)
                if t in self.queue:
                    self.queue.remove(t)
                stopped.append(t)
                self.counters["cancelled"] += 1
            self.cv.notify_all()
        if stopped:
            log.warning("asr: cancelled %d transcription(s) of %s: %s", len(stopped), caller,
                        ", ".join(f"{t.id} ({t.phase})" for t in stopped))
        return stopped

    def status(self) -> dict:
        with self.cv:
            return {"queue": [dict(t.describe(), position=i + 1) for i, t in enumerate(self.queue)],
                    "active": [t.describe() for t in self.active],
                    "counters": dict(self.counters)}


SCHED = Scheduler()


# ── the worker process ────────────────────────────────────────────────────────────────────────────

class JobError(Exception):
    """A job the worker could not finish; the message is the caller's error text."""


def _footprint_mb(pid: int) -> Optional[float]:
    try:
        from omniseek.core import memguard
        return memguard.footprint_mb(pid)
    except Exception:  # noqa: BLE001
        return None


class _Worker:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.pid = proc.pid
        self.started = time.monotonic()
        self.jobs = 0
        self.peak_mb = 0.0
        self.last_use = time.monotonic()

    def alive(self) -> bool:
        return self.proc.poll() is None


class Pool:
    """This eye process's one speech worker, and the machine-wide inference lock it runs under."""

    def __init__(self):
        self.lock = threading.Lock()          # one job at a time on this process's worker
        self.worker: Optional[_Worker] = None
        self.ended_at = 0.0
        self.counters = {"worker_starts": 0, "jobs": 0, "over_cap": 0, "timeouts": 0, "crashes": 0,
                         "cancel_kills": 0, "recycled": 0, "idle_exits": 0}
        self._idle_stop = threading.Event()
        self._idle_thread: Optional[threading.Thread] = None

    # spawning and ending

    def _spawn(self) -> _Worker:
        from omniseek.core import scratch
        pkg_root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ)
        env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.Popen([sys.executable, "-m", "omniseek.asr_worker"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=env, close_fds=True)
        scratch.claim(proc.pid)
        w = _Worker(proc)
        self.counters["worker_starts"] += 1
        since = f"{time.monotonic() - self.ended_at:.0f} s after the last one ended" if self.ended_at else "first"
        log.info("asr: worker %d started (%s)", w.pid, since)
        return w

    def _end(self, reason: str, kill: bool = False) -> None:
        """Caller holds self.lock. End the worker (close its input; kill it if asked or if it lingers),
        then remove what it left (its scratch and MPSGraph directories)."""
        w, self.worker = self.worker, None
        if w is None:
            return
        try:
            if kill:
                _kill(w.proc)
            else:
                try:
                    w.proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                w.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill(w.proc)
                w.proc.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("asr: worker %d did not end cleanly (%s)", w.pid, exc)
        for f in (w.proc.stdout, w.proc.stdin):
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        self.ended_at = time.monotonic()
        swept = {}
        try:
            from omniseek.core import scratch
            swept = scratch.sweep_pid(w.pid)
        except Exception:  # noqa: BLE001
            pass
        log.info("asr: worker %d ended (%s) after %d job(s), %.0f s; peak %.0f MB; removed %d MPSGraph dirs",
                 w.pid, reason, w.jobs, time.monotonic() - w.started, w.peak_mb,
                 swept.get("mpsgraph_dirs", 0))

    def end(self, reason: str = "requested") -> None:
        with self.lock:
            self._end(reason)

    def worker_info(self) -> Optional[dict]:
        w = self.worker
        if w is None or not w.alive():
            return None
        fp = _footprint_mb(w.pid)
        return {"pid": w.pid, "fp_mb": None if fp is None else round(fp), "jobs": w.jobs,
                "peak_mb": round(w.peak_mb), "age_s": round(time.monotonic() - w.started),
                "idle_s": round(time.monotonic() - w.last_use)}

    # the idle exit

    def _idle_loop(self) -> None:
        while not self._idle_stop.wait(IDLE_POLL_S):
            limit = idle_exit_s()
            if limit <= 0 or not self.lock.acquire(blocking=False):
                continue
            try:
                w = self.worker
                if w is None:
                    self._idle_thread = None
                    return
                idle = time.monotonic() - w.last_use
                if not w.alive():
                    self.counters["crashes"] += 1
                    self._end("found ended")
                elif idle >= limit:
                    self.counters["idle_exits"] += 1
                    self._end(f"idle {idle:.0f}s >= {limit:.0f}s")
            finally:
                self.lock.release()

    def _ensure_idle_watch(self) -> None:
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return
        t = threading.Thread(target=self._idle_loop, name="asr-worker-idle", daemon=True)
        self._idle_thread = t
        t.start()
        try:
            from omniseek.core import lifecycle
            lifecycle.register_loop("asr-worker-idle", self._idle_stop, t)
        except Exception as exc:  # noqa: BLE001
            log.debug("asr: idle watcher not registered with lifecycle (%s)", exc)

    # one job

    def run(self, ticket: Ticket, req: dict, audio_s: float):
        """Run one job on the worker under the machine-wide inference lock. Returns the worker's result;
        raises JobError (with the caller's message) or Cancelled."""
        with self.lock:
            infer_fd = None
            try:
                while infer_fd is None:
                    if ticket.cancelled.is_set():
                        raise Cancelled()
                    infer_fd = _try_flock(where.asr_run() / "infer.lock")
                    if infer_fd is None:
                        ticket.phase = "waiting_recognition"
                        time.sleep(POLL_S)
                if self.worker is not None and not self.worker.alive():
                    self.counters["crashes"] += 1
                    self._end("found ended")
                if self.worker is None:
                    while True:
                        if ticket.cancelled.is_set():
                            raise Cancelled()
                        ok, why = memory_ok()
                        if ok:
                            break
                        ticket.phase = "waiting_memory"
                        ticket.waiting_for = "memory"
                        time.sleep(WAIT_POLL_S)
                    ticket.waiting_for = ""
                    self.worker = self._spawn()
                ticket.phase = "transcribing"
                return self._exchange(ticket, req, audio_s)
            finally:
                _unflock(infer_fd)
                if self.worker is not None:
                    self.worker.last_use = time.monotonic()
                    self._ensure_idle_watch()

    def _exchange(self, ticket: Ticket, req: dict, audio_s: float):
        w = self.worker
        cap = worker_cap_mb(req.get("op", "flat"))
        transient = CAPS_DEFAULT[family(req.get("op", "flat"))][1]
        limit_s = START_ALLOWANCE_S + max(audio_s, 1.0) * rtf_limit()
        req = dict(req, id=ticket.id)
        t0 = time.monotonic()
        try:
            w.proc.stdin.write((json.dumps(req) + "\n").encode())
            w.proc.stdin.flush()
        except OSError:
            self.counters["crashes"] += 1
            self._end("could not take the job", kill=True)
            raise JobError("the speech worker ended before it took the job; call again")
        fd = w.proc.stdout.fileno()
        buf = b""
        job_peak = 0.0
        while True:
            ready, _, _ = select.select([fd], [], [], POLL_S)
            if ready:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    code = w.proc.wait(timeout=10)
                    self.counters["crashes"] += 1
                    w.peak_mb = max(w.peak_mb, job_peak)
                    self._end(f"crashed with exit code {code}")
                    raise JobError(f"the speech worker crashed (exit code {code}) while transcribing; "
                                   "nothing was cached, call again to retry")
                buf += chunk
                if b"\n" in buf:
                    line = buf.split(b"\n", 1)[0]
                    break
            fp = _footprint_mb(w.pid)
            if fp is not None:
                job_peak = max(job_peak, fp)
                if cap > 0 and fp > cap:
                    self.counters["over_cap"] += 1
                    w.peak_mb = max(w.peak_mb, job_peak)
                    self._end(f"over its memory cap: {fp:.0f} MB > {cap:.0f} MB", kill=True)
                    raise JobError(f"transcription stopped: the speech worker went over its memory cap "
                                   f"({fp:.0f} MB > {cap:.0f} MB); "
                                   "try a shorter slice (start/duration)")
            elapsed = time.monotonic() - t0
            if elapsed > limit_s:
                self.counters["timeouts"] += 1
                w.peak_mb = max(w.peak_mb, job_peak)
                self._end(f"over its time limit: {elapsed:.0f} s > {limit_s:.0f} s", kill=True)
                raise JobError(f"transcription stopped: it ran {elapsed:.0f} s, over its limit of "
                               f"{limit_s:.0f} s for {audio_s:.0f} s of audio; call again to retry")
            if ticket.cancelled.is_set():
                self.counters["cancel_kills"] += 1
                w.peak_mb = max(w.peak_mb, job_peak)
                self._end("its transcription was cancelled", kill=True)
                raise Cancelled()
        w.jobs += 1
        self.counters["jobs"] += 1
        w.peak_mb = max(w.peak_mb, job_peak)
        took = time.monotonic() - t0
        after = _footprint_mb(w.pid)
        log.info("asr: worker %d job %d (%s, %.0f s audio) in %.1f s, rtf %.3f, peak %.0f MB, after %s MB",
                 w.pid, ticket.id, req.get("op"), audio_s, took, took / max(audio_s, 1.0), job_peak,
                 "?" if after is None else f"{after:.0f}")
        # The next job of the family may add its transient; a worker already above cap - transient would
        # be killed by it, so it ends now, idle, and the next job gets a fresh one.
        if cap > 0 and after is not None and after > cap - transient:
            self.counters["recycled"] += 1
            self._end(f"footprint {after:.0f} MB leaves no room for another job under {cap:.0f} MB")
        try:
            out = json.loads(line)
        except ValueError:
            raise JobError("the speech worker answered something unreadable; call again")
        if not out.get("ok"):
            raise JobError(out.get("error") or "the speech worker failed")
        return out.get("result")


POOL = Pool()


def _shutdown() -> None:
    # Never waits on POOL.lock: a job may hold it at exit. The worker would also end on its own (it
    # watches its parent), this only makes it immediate.
    POOL._idle_stop.set()
    w = POOL.worker
    if w is not None:
        _kill(w.proc)
    try:
        p = _control_path()
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass


atexit.register(_shutdown)


# ── the control socket <asr-run>/ctl/<pid> (cancel and status from outside; see omniseek.asr_ctl) ──
# A Unix socket: only this user can reach it (the directory is 0700, the socket 0600), so it needs no
# token. Its path is limited to 104 bytes on macOS; the live one is about 45. A process whose path is
# longer (a test or rehearsal home under a long temp path) runs without one and logs why.

_control_started = False
_control_lock = threading.Lock()


def _control_path(pid: Optional[int] = None) -> Path:
    return where.asr_run() / "ctl" / str(pid or os.getpid())


def status() -> dict:
    out = {"pid": os.getpid(), **SCHED.status(), "worker": POOL.worker_info(),
           "pool": dict(POOL.counters), "slots_busy": slots_busy(),
           "limits": {"max_segments": max_segments(),
                      "worker_cap_mb": worker_cap_mb("flat"), "diar_cap_mb": worker_cap_mb("diar"),
                      "idle_exit_s": idle_exit_s()}}
    out["memory_ok"], out["memory_why"] = memory_ok()
    return out


def cancel(caller: str) -> dict:
    stopped = SCHED.cancel(caller)
    return {"pid": os.getpid(), "cancelled": len(stopped), "transcriptions": [t.describe() for t in stopped]}


def _serve(sock: socket.socket) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            data = b""
            while not data.endswith(b"\n") and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.decode() or "{}")
            op = req.get("op")
            if op == "cancel" and isinstance(req.get("caller"), str) and req["caller"]:
                out = cancel(req["caller"])
            elif op == "status":
                out = status()
            else:
                out = {"error": "unknown request (op is cancel with a caller, or status)"}
            conn.sendall((json.dumps(out, ensure_ascii=False) + "\n").encode())
        except Exception as exc:  # noqa: BLE001 -- one bad request never stops the socket
            try:
                conn.sendall((json.dumps({"error": str(exc)[:200]}) + "\n").encode())
            except Exception:  # noqa: BLE001
                pass
        finally:
            conn.close()


def _ensure_control() -> None:
    """Open this process's control socket the first time a transcription is submitted (before that
    there is nothing to cancel). Never raises: without it, cancellation from outside is what is lost,
    and the warning says so."""
    global _control_started
    with _control_lock:
        if _control_started:
            return
        _control_started = True
        path = _control_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            if path.exists() or path.is_symlink():
                path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
            os.chmod(path, 0o600)
            sock.listen(8)
            threading.Thread(target=_serve, args=(sock,), name="asr-control", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            log.warning("asr: control socket %s (%d bytes) not opened (%s); cancellation by job is "
                        "unavailable in this process", path, len(str(path)), exc)


def remove_workdir(t: Ticket) -> None:
    if t.workdir is not None:
        shutil.rmtree(t.workdir, ignore_errors=True)
        t.workdir = None
