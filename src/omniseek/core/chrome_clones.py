"""Delete the app-bundle clones that Google Chrome leaves behind on macOS (2026-10-05).

What they are. At startup every Google Chrome browser process clones its own app bundle into
``<user dir>/X/com.google.Chrome.code_sign_clone/code_sign_clone.XXXXXX`` (an APFS clone; the main
executable inside is a HARD LINK to the running binary), so its code signature still validates after
an update replaces /Applications/Google Chrome.app. Only a normal shutdown removes it: the browser's
last destructor starts a ``--type=code-sign-clone-cleanup`` helper that waits for the browser to exit
and then deletes the directory (chrome/browser/mac/code_sign_clone_manager.mm). A browser that ends
any other way leaves its clone behind until the machine reboots. Measured on the live host 2026-10-05:
``launchctl kill TERM`` (how the cdp-reaper stops an idle browser) leaves it every time, while the
CDP command Browser.close lets Chrome remove it. The live host had collected 200 of them in two months.

What this module does. One pass over the clone root, run by the cdp-reaper job (every 10 minutes):
  * only the direct children of the root whose name is exactly ``code_sign_clone.`` plus six letters
    or digits, that are real directories (not symlinks) and whose resolved path is still in the root;
  * a clone is IN USE, and kept, when a process has a file under it open or mapped. One full lsof,
    read two ways. (A) By path: a name lsof prints under the clone. (B) By file identity (device +
    inode) of the clone's main executable, because that executable is a hard link of the running
    binary and lsof prints just one of its names (under /Applications, or under any other clone of
    the same binary; measured on the live host). Every clone of the current version shares one
    executable with every running browser of that version, so (B) alone would keep every leaked
    clone for as long as any such browser runs. (B) therefore counts a process only when it is the
    clone's maker (it started at most MAKER_WINDOW_S before the clone was born; measured: the clone
    appears 0.3 s after the browser starts), or when the executable is no longer the one installed
    in Google Chrome.app (an old version still running, like a desktop Chrome left open across an
    update: every clone of its binary is kept, since those clones are now its only copies);
  * a clone born less than an hour ago is kept (the browser that made it may still be starting);
  * everything else is deleted, one log line each (name, birth time, seconds taken). Any error is
    logged and the pass goes on; nothing here raises to the caller.
If lsof or ps cannot be read (it fails, or its output does not even list this process), nothing is
deleted.

``python -m <this module> --dry-run`` (or running the file directly) prints what one pass would do
without deleting anything. On any host other than macOS the pass does nothing.

Deliberately self-contained (standard library only, Python 3.9 compatible): the same file is run by
hand on the live host for the dry run before it is deployed.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

CLONE_PARENT = "com.google.Chrome.code_sign_clone"
NAME_RE = re.compile(r"^code_sign_clone\.[A-Za-z0-9]{6}$")
ROOT_PREFIX = "/private/var/folders/"   # Chrome refuses any clone dir outside this (ValidateTempDir)
MIN_AGE_S = 3600                         # spec: never touch a clone born within the last hour
# How long before a clone's birth its maker may have started. Confirmed 2026-10-06 from the clone
# watch on the live host (2026-10-05 11:15 to 2026-10-06 04:30, four CDP services): of 11 clones, 10 were
# born within 3 s of their browser's start and one 19 s after it (that browser's second clone), so
# 300 s keeps more than ten times the widest gap seen. Too wide only keeps a leaked clone longer; too
# narrow could free the clone of a running browser (harmless until Chrome updates while that browser
# is still up). Recheck if a pass ever deletes a clone whose maker was alive.
MAKER_WINDOW_S = 300
MAKER_SLACK_S = 2                        # ps start times have 1 s resolution
TIME_BUDGET_S = 30.0                     # job budget 120 s; one clone took 0.12 s to delete on the live host
LSOF_TIMEOUT_S = 30                      # one full lsof took 0.17 s on the live host
PS_TIMEOUT_S = 10
MIN_PASS_S = 15                          # with less time than this left, a pass does not start
INSTALLED_EXES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))


def _tool(name: str, path: str) -> str:
    """The macOS system path of a tool when it is there, so a job started with a short PATH works."""
    return path if os.path.exists(path) else name


FileId = Tuple[int, int]                 # (st_dev, st_ino)


def clone_root() -> Optional[str]:
    """The directory Chrome puts its clones in, resolved, or None when it cannot be worked out.

    Chrome asks the private ``_dirhelper`` for the per-user "X" directory, the sibling of the
    per-user temp directory "T" that ``getconf DARWIN_USER_TEMP_DIR`` prints."""
    try:
        out = subprocess.run([_tool("getconf", "/usr/bin/getconf"), "DARWIN_USER_TEMP_DIR"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("chrome-clones: getconf failed: %s", exc)
        return None
    if not out:
        return None
    temp_dir = os.path.realpath(out.rstrip("/"))
    root = os.path.join(os.path.dirname(temp_dir), "X", CLONE_PARENT)
    if not root.startswith(ROOT_PREFIX):
        log.warning("chrome-clones: unexpected clone root %s", root)
        return None
    return root


class OpenFile:
    __slots__ = ("pid", "cmd", "fd", "dev", "ino", "name")

    def __init__(self, pid: int, cmd: str, fd: str, dev: Optional[int], ino: Optional[int],
                 name: str):
        self.pid, self.cmd, self.fd, self.dev, self.ino, self.name = pid, cmd, fd, dev, ino, name


def parse_lsof(text: str) -> List[OpenFile]:
    """Parse ``lsof -F pcfDin`` output: one record per open file, with the process it belongs to."""
    files: List[OpenFile] = []
    pid, cmd = -1, ""
    cur: Optional[dict] = None

    def flush() -> None:
        if cur is not None and cur.get("name") is not None:
            files.append(OpenFile(pid, cmd, cur.get("fd", ""), cur.get("dev"), cur.get("ino"),
                                  cur["name"]))

    for line in text.splitlines():
        if not line:
            continue
        key, val = line[0], line[1:]
        if key == "p":
            flush()
            cur = None
            try:
                pid = int(val)
            except ValueError:
                pid = -1
            cmd = ""
        elif key == "c":
            cmd = val
        elif key == "f":
            flush()
            cur = {"fd": val}
        elif cur is None:
            continue
        elif key == "D":
            try:
                cur["dev"] = int(val, 16) if val.lower().startswith("0x") else int(val)
            except ValueError:
                pass
        elif key == "i":
            try:
                cur["ino"] = int(val)
            except ValueError:
                pass
        elif key == "n":
            cur["name"] = val
    flush()
    return files


def read_lsof(timeout: float = LSOF_TIMEOUT_S) -> Optional[List[OpenFile]]:
    """One full lsof of this user's processes, or None when it cannot be trusted."""
    try:
        out = subprocess.run([_tool("lsof", "/usr/sbin/lsof"), "-nP", "-w", "-FpcfDin"],
                             capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("chrome-clones: lsof failed: %s", exc)
        return None
    files = parse_lsof(out)
    if not any(f.pid == os.getpid() for f in files):
        log.warning("chrome-clones: lsof output does not list this process; deleting nothing")
        return None
    return files


def parse_ps_starts(text: str) -> Dict[int, float]:
    """Parse ``ps -o pid=,lstart=`` (C locale) into {pid: start time, epoch seconds}."""
    starts: Dict[int, float] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s*$", line)
        if not m:
            continue
        try:
            t = time.mktime(time.strptime(re.sub(r"\s+", " ", m.group(2)), "%a %b %d %H:%M:%S %Y"))
        except (ValueError, OverflowError):
            continue
        starts[int(m.group(1))] = t
    return starts


def read_ps_starts(timeout: float = PS_TIMEOUT_S) -> Optional[Dict[int, float]]:
    """Start time of every process, or None when ps cannot be trusted."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        out = subprocess.run([_tool("ps", "/bin/ps"), "-axww", "-o", "pid=,lstart="],
                             capture_output=True, text=True, timeout=timeout, env=env).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("chrome-clones: ps failed: %s", exc)
        return None
    starts = parse_ps_starts(out)
    if os.getpid() not in starts:
        log.warning("chrome-clones: ps output does not list this process; deleting nothing")
        return None
    return starts


def installed_exe_ids(paths: Iterable[str] = INSTALLED_EXES) -> Set[FileId]:
    """File identities of the installed Google Chrome executables (what a new browser would run)."""
    ids: Set[FileId] = set()
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        ids.add((st.st_dev, st.st_ino))
    return ids


def _exe_ids(clone_dir: str) -> Set[FileId]:
    """File identities of the executables in each bundle's Contents/MacOS inside one clone."""
    ids: Set[FileId] = set()
    try:
        bundles = os.listdir(clone_dir)
    except OSError:
        return ids
    for b in bundles:
        macos = os.path.join(clone_dir, b, "Contents", "MacOS")
        try:
            names = os.listdir(macos)
        except OSError:
            continue
        for n in names:
            try:
                st = os.stat(os.path.join(macos, n), follow_symlinks=False)
            except OSError:
                continue
            ids.add((st.st_dev, st.st_ino))
    return ids


def _birth(path: str) -> Optional[float]:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return getattr(st, "st_birthtime", None) or st.st_mtime


def _iso(t: Optional[float]) -> Optional[str]:
    return datetime.datetime.fromtimestamp(t).isoformat(timespec="seconds") if t else None


def plan(root: str, open_files: Iterable[OpenFile], *, now: float,
         starts: Optional[Dict[int, float]] = None, installed: Optional[Set[FileId]] = None,
         min_age_s: float = MIN_AGE_S, maker_window_s: float = MAKER_WINDOW_S,
         birth_of: Callable[[str], Optional[float]] = _birth,
         exe_ids_of: Callable[[str], Set[FileId]] = _exe_ids) -> Dict[str, list]:
    """Decide, without deleting, what happens to each direct child of ``root``.

    ``starts`` maps pid to start time (epoch s); ``installed`` holds the file identities of the
    installed Chrome executables. Returns lists ``delete`` / ``in_use`` / ``too_new`` / ``skipped``
    of dicts with at least ``name``; ``delete`` entries carry ``path`` and ``birth``, ``in_use``
    entries carry the holding ``pids`` and ``why`` (path / maker / old binary)."""
    starts = starts or {}
    installed = installed or set()
    res: Dict[str, list] = {"delete": [], "in_use": [], "too_new": [], "skipped": []}
    root_real = os.path.realpath(root)
    try:
        entries = sorted(os.listdir(root_real))
    except OSError as exc:
        res["skipped"].append({"name": root_real, "why": "cannot list root: %s" % exc})
        return res
    opened = list(open_files)
    cmd_of = {f.pid: f.cmd for f in opened}
    by_id: Dict[FileId, Set[int]] = {}
    for f in opened:
        if f.dev is not None and f.ino is not None:
            by_id.setdefault((f.dev, f.ino), set()).add(f.pid)
    for name in entries:
        path = os.path.join(root_real, name)
        if not NAME_RE.match(name):
            res["skipped"].append({"name": name, "why": "name"})
            continue
        if os.path.islink(path):
            res["skipped"].append({"name": name, "why": "symlink"})
            continue
        if os.path.dirname(os.path.realpath(path)) != root_real:
            res["skipped"].append({"name": name, "why": "resolves outside the root"})
            continue
        if not os.path.isdir(path):
            res["skipped"].append({"name": name, "why": "not a directory"})
            continue
        born = birth_of(path)
        prefix = os.path.join(path, "")
        holders: Dict[int, str] = {}
        for f in opened:
            if f.name == path or f.name.startswith(prefix):
                holders.setdefault(f.pid, "path")
        for fid in exe_ids_of(path):
            for pid in by_id.get(fid, ()):
                if fid not in installed:
                    holders.setdefault(pid, "old binary")
                elif born is None:
                    holders.setdefault(pid, "maker")   # cannot date the clone: keep it
                elif pid in starts and \
                        born - maker_window_s <= starts[pid] <= born + MAKER_SLACK_S:
                    holders.setdefault(pid, "maker")
        if holders:
            res["in_use"].append({"name": name, "birth": _iso(born), "pids": sorted(holders),
                                  "why": sorted(set(holders.values())),
                                  "cmds": sorted(set(cmd_of.get(p, "") for p in holders))})
            continue
        if born is None or now - born < min_age_s:
            res["too_new"].append({"name": name, "birth": _iso(born)})
            continue
        res["delete"].append({"name": name, "path": path, "birth": _iso(born)})
    return res


def _delete(path: str) -> None:
    shutil.rmtree(path)


def sweep(*, dry_run: bool = False, root: Optional[str] = None,
          open_files: Optional[List[OpenFile]] = None,
          starts: Optional[Dict[int, float]] = None, installed: Optional[Set[FileId]] = None,
          now: Optional[float] = None, min_age_s: float = MIN_AGE_S,
          time_budget_s: float = TIME_BUDGET_S, time_left_s: Optional[float] = None,
          birth_of: Callable[[str], Optional[float]] = _birth,
          exe_ids_of: Callable[[str], Set[FileId]] = _exe_ids,
          delete: Callable[[str], None] = _delete) -> dict:
    """One cleanup pass. Never raises; returns a small summary for the job's result. Every input
    left as None is read from the machine (root, lsof, ps, the installed executables).
    ``time_left_s`` is how long the caller can still wait for the whole pass (None: no limit beyond
    the tools' own timeouts and ``time_budget_s`` for deleting); below MIN_PASS_S nothing starts."""
    try:
        if time_left_s is not None and time_left_s < MIN_PASS_S:
            return {"skipped": "no time left (%.0f s)" % time_left_s}
        t_end = time.monotonic() + (time_left_s if time_left_s is not None else 3600.0)
        return _sweep(dry_run=dry_run, root=root, open_files=open_files, starts=starts,
                      installed=installed, now=now, min_age_s=min_age_s,
                      time_budget_s=time_budget_s, t_end=t_end, birth_of=birth_of,
                      exe_ids_of=exe_ids_of, delete=delete)
    except Exception as exc:  # noqa: BLE001 -- a cleanup must never break the job that runs it
        log.warning("chrome-clones: pass failed: %r", exc)
        return {"error": repr(exc)}


def _sweep(*, dry_run, root, open_files, starts, installed, now, min_age_s, time_budget_s, t_end,
           birth_of, exe_ids_of, delete) -> dict:
    if root is None:
        if sys.platform != "darwin":
            return {"skipped": "not-darwin"}
        root = clone_root()
        if root is None:
            return {"skipped": "no clone root"}
    if not os.path.isdir(root):
        return {"skipped": "no clone dir", "root": root}
    if open_files is None:
        open_files = read_lsof(timeout=min(LSOF_TIMEOUT_S, max(1.0, t_end - time.monotonic())))
        if open_files is None:
            return {"skipped": "lsof unreadable", "root": root}
    if starts is None:
        starts = read_ps_starts(timeout=min(PS_TIMEOUT_S, max(1.0, t_end - time.monotonic())))
        if starts is None:
            return {"skipped": "ps unreadable", "root": root}
    if installed is None:
        installed = installed_exe_ids()
    p = plan(root, open_files, now=time.time() if now is None else now, starts=starts,
             installed=installed, min_age_s=min_age_s, birth_of=birth_of, exe_ids_of=exe_ids_of)
    out = {"root": root, "dry_run": dry_run,
           "in_use": [c["name"] for c in p["in_use"]],
           "too_new": [c["name"] for c in p["too_new"]],
           "skipped": ["%s (%s)" % (c["name"], c["why"]) for c in p["skipped"]]}
    if dry_run:
        out["would_delete"] = [c["name"] for c in p["delete"]]
        out["detail"] = p
        return out
    deleted, errors, left = [], [], []
    stop_at = min(time.monotonic() + time_budget_s, t_end - 2.0)
    for c in p["delete"]:
        if time.monotonic() > stop_at:
            left.append(c["name"])
            continue
        t0 = time.monotonic()
        try:
            delete(c["path"])
        except Exception as exc:  # noqa: BLE001 -- one bad clone must not stop the others
            errors.append("%s: %r" % (c["name"], exc))
            log.warning("chrome-clones: could not delete %s (born %s): %r", c["name"], c["birth"], exc)
            continue
        took = time.monotonic() - t0
        deleted.append(c["name"])
        log.info("chrome-clones: deleted %s (born %s) in %.2fs", c["name"], c["birth"], took)
    if left:
        log.info("chrome-clones: time budget used up; %d left for the next pass", len(left))
    out.update(deleted=deleted, errors=errors, left_for_next_pass=left)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    dry = "--dry-run" in args
    res = sweep(dry_run=dry)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
