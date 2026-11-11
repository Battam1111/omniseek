"""Per-process scratch: every eye process keeps its temporary files in a directory of its own, and what a
process left behind is removed once that process is gone.

Why (2026-11-10): the shared user temp directory had collected OmniSeek's leftovers from processes that
ended mid-call: 38 MB decoded WAVs of transcriptions cut off by a restart, empty download directories of
failed fetches, a parser's temporary directory. A file removed in a ``finally`` is gone after every call
that returns or raises, but not after a process that is killed (a memory restart, a deploy past its
drain cap): that path never runs. So the cleanup that cannot be skipped is keyed by the process, not by
the call: ``install()`` points this process's TMPDIR (and Python's tempfile) at ``<scratch>/<pid>``,
and every ``install()`` and ``sweep()`` removes the directory of each pid under ``<scratch>`` that is no
longer running. Child processes (ffmpeg, yt-dlp, the browser driver, the speech worker) inherit TMPDIR.

MPSGraph (Apple's graph compiler under torch's MPS backend) creates one empty directory per compiled
graph in the user temp directory, named ``mpsgraph-<pid>-<time>-<n>`` under
``com.apple.MetalPerformanceShadersGraph``, and never removes them; that directory had reached 65535
entries. It does not follow TMPDIR: with TMPDIR pointed at a writable directory and the user temp
directory not writable, it still tried the user temp directory and logged "Error creating directory"
(2026-11-10, torch 2.12, macOS; the computation itself went on). So it cannot be given a directory of its
own; instead, when a pid's scratch is swept, the empty ``mpsgraph-<pid>-*`` directories of that pid are
removed with it. Only empty directories (``rmdir``) are touched, and only those of pids that ran eye
code, so nothing another program wrote is removed; a pid reused by another program after an eye process
ended can at worst lose empty directories of its own.

What the sweep recognizes as OmniSeek's (``sweep()``, run by every ``install()``), and only that:

1. A pid with a directory under ``<scratch>`` that is no longer running: its scratch directory goes, and
   its empty MPSGraph directories.
2. A pid that OmniSeek's own service logs (``where.logs()``) name as an eye server process (the server's
   "Started server process [<pid>]" line) and that is no longer running: its empty MPSGraph directories.
   This covers eye processes that ran before per-process scratch existed or ended before claiming it
   (2026-11-10: 4786 empty directories of 8 ended eye servers); a pid whose log lines have rotated away
   is no longer recognized and is left alone.
3. An empty directory directly in the user temp directory whose name starts with ``TEMP_PREFIX``, the
   prefix every temporary directory OmniSeek makes carries (download directories of failed fetches, a
   parser's directory of a process killed mid-read). Eye servers make theirs under ``<scratch>``, so one
   left in the user temp directory comes from an older version or from eye code run outside a server
   (a script); such a directory caught empty in the instant between being made and being written fails
   that call with an error instead of leaving a leftover.

Anything else in the user temp directory (the browser driver's ``playwright-artifacts-*``, Chrome's
``com.google.Chrome.*``, MPSGraph directories of pids not recognized above) carries no mark of OmniSeek
and is not touched.

Fail-open: any error here is logged and ignored; temporary files never stop the service.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from omniseek import where

log = logging.getLogger(__name__)

MPSGRAPH_DIR = "com.apple.MetalPerformanceShadersGraph"
TEMP_PREFIX = "omniseek-"
_SERVER_STARTED = re.compile(rb"Started server process \[(\d+)\]")
_CS_DARWIN_USER_TEMP_DIR = 65537


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def dir_for(pid: Optional[int] = None) -> Path:
    return where.scratch() / str(pid or os.getpid())


def user_temp_dir() -> Optional[Path]:
    """The system's per-user temp directory (confstr _CS_DARWIN_USER_TEMP_DIR), whatever TMPDIR says;
    None off macOS."""
    try:
        libc = ctypes.CDLL(None)
        buf = ctypes.create_string_buffer(1024)
        n = libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, len(buf))
        return Path(buf.value.decode()) if 0 < n <= len(buf) and buf.value else None
    except Exception:  # noqa: BLE001
        return None


def mpsgraph_sweep(pids) -> int:
    """Remove the empty MPSGraph directories of the pids in ``pids`` (one pid or several; see the module
    note). The directory is listed once. Returns how many went."""
    pids = {pids} if isinstance(pids, int) else set(pids)
    base = user_temp_dir()
    if base is None or not pids:
        return 0
    d = base / MPSGRAPH_DIR
    marks = tuple(f"mpsgraph-{pid}-" for pid in pids)
    try:
        with os.scandir(d) as it:
            names = [e.name for e in it if e.name.startswith(marks)]
    except OSError:
        return 0
    n = 0
    for name in names:
        try:
            os.rmdir(d / name)
            n += 1
        except OSError:
            pass
    return n


def sweep_pid(pid: int) -> dict:
    """Remove what ended process ``pid`` left: its scratch directory and its MPSGraph directories."""
    shutil.rmtree(dir_for(pid), ignore_errors=True)
    return {"pid": pid, "mpsgraph_dirs": mpsgraph_sweep(pid)}


def logged_server_pids() -> set:
    """Pids OmniSeek's service logs name as eye server processes (rule 2 of the module note)."""
    out: set = set()
    try:
        files = [e.path for e in os.scandir(where.logs()) if e.is_file() and ".err" in e.name]
    except OSError:
        return out
    for f in files:
        try:
            with open(f, "rb") as fh:
                for line in fh:
                    if b"Started server process" in line:
                        m = _SERVER_STARTED.search(line)
                        if m:
                            out.add(int(m.group(1)))
        except OSError:
            continue
    return out


def stray_sweep() -> int:
    """Remove the empty eye-made directories directly in the user temp directory (rule 3 of the module
    note). Returns how many went."""
    base = user_temp_dir()
    if base is None:
        return 0
    try:
        with os.scandir(base) as it:
            names = [e.name for e in it if e.name.startswith(TEMP_PREFIX) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return 0
    n = 0
    for name in names:
        try:
            os.rmdir(base / name)   # only an empty directory goes
            n += 1
        except OSError:
            pass
    return n


def sweep() -> list:
    """Sweep what the module note's three rules recognize as OmniSeek's. Returns one entry per swept pid
    (rule 1) plus, when rules 2 and 3 removed anything, one entry {"logged_pids", "mpsgraph_dirs"} and
    one {"stray_dirs"}."""
    out = []
    me = os.getpid()
    try:
        with os.scandir(where.scratch()) as it:
            pids = [int(e.name) for e in it if e.name.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        if pid != me and not alive(pid):
            out.append(sweep_pid(pid))
    logged = {p for p in logged_server_pids() - set(pids) if p != me and not alive(p)}
    n_logged = mpsgraph_sweep(logged)
    if n_logged:
        out.append({"logged_pids": sorted(logged), "mpsgraph_dirs": n_logged})
    n_stray = stray_sweep()
    if n_stray:
        out.append({"stray_dirs": n_stray})
    if out:
        log.info("scratch: swept %d ended process(es), %d MPSGraph dirs (%d of ended eye servers named in the "
                 "logs), %d empty eye temp dirs", sum(1 for x in out if "pid" in x),
                 sum(x.get("mpsgraph_dirs", 0) for x in out), n_logged, n_stray)
    return out


def install() -> Optional[Path]:
    """Give this process its own temp directory and sweep the ones of ended processes. Call it at the
    start of a long-lived eye process, before anything creates a temporary file."""
    try:
        sweep()
        mine = dir_for()
        if mine.exists():   # left by an ended process that had this pid
            shutil.rmtree(mine, ignore_errors=True)
        mine.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(mine) + os.sep
        tempfile.tempdir = str(mine)
        return mine
    except Exception as exc:  # noqa: BLE001
        log.warning("scratch: not installed (%s); temporary files stay in the system temp directory", exc)
        return None


def claim(pid: int) -> None:
    """Mark ``pid`` (a child that runs eye code, such as the speech worker) as one whose leftovers the
    sweep removes once it has ended."""
    try:
        dir_for(pid).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("scratch: cannot claim %d (%s)", pid, exc)
