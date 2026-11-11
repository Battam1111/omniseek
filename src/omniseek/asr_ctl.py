"""Cancel or list OmniSeek's transcriptions from outside OmniSeek: ``python <this file> cancel --job NAME``.

Every eye process that has run a transcription listens on a Unix socket of its own,
``<asr-run>/ctl/<pid>``, which only this user can reach (omniseek.core.asr_sched). This asks each of them:

  cancel --job NAME   drop NAME from every transcription it asked for; each transcription left with no
                      caller is stopped (dropped from the queue, its download or decode killed, the speech
                      worker killed if it was that one's turn, its temporary files removed). Prints the
                      number stopped as the first word of the first line, then one line per eye process.
                      Exit 0 when every live eye process answered (no eye process: "0", exit 0); 1 when
                      one did not answer.
  status              one JSON object per eye process: queue, running transcriptions, worker, counters.

Standard library only, loadable by file path with any Python 3.9 or later, so a caller outside OmniSeek's
environment (a dispatcher stopping a job) runs it without importing OmniSeek. A socket whose process has
ended is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _load_where():
    """omniseek.where by file path: it imports only the standard library, while importing the omniseek
    package itself installs OmniSeek's log masking, which needs Python 3.10."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("omniseek_where", Path(__file__).resolve().with_name("where.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


where = _load_where()

TIMEOUT_S = 5.0


def ctl_dir() -> Path:
    return where.asr_run() / "ctl"


def _sockets() -> list:
    d = ctl_dir()
    try:
        return sorted(p for p in d.iterdir() if p.name.isdigit())
    except OSError:
        return []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _ask(path: Path, req: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_S)
    try:
        s.connect(str(path))
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode() or "{}")
    finally:
        s.close()


def ask_all(req: dict) -> list:
    """[(pid, answer or None, error or None)] for every eye process with a control socket."""
    out = []
    for p in _sockets():
        pid = int(p.name)
        if not _alive(pid):
            try:
                p.unlink()
            except OSError:
                pass
            continue
        try:
            out.append((pid, _ask(p, req), None))
        except (OSError, ValueError) as exc:
            out.append((pid, None, f"{type(exc).__name__}: {exc}"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cancel or list OmniSeek's transcriptions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cancel", help="stop every transcription a job asked for")
    c.add_argument("--job", required=True, help="the caller name the transcriptions were tagged with")
    sub.add_parser("status", help="print each eye process's transcription state as JSON")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        for pid, ans, err in ask_all({"op": "status"}):
            print(json.dumps(ans if ans is not None else {"pid": pid, "error": err}, ensure_ascii=False))
        return 0
    res = ask_all({"op": "cancel", "caller": a.job})
    total = sum(int((ans or {}).get("cancelled") or 0) for _, ans, _ in res)
    failed = [(pid, err or (ans or {}).get("error")) for pid, ans, err in res
              if err or (ans or {}).get("error")]
    print(f"{total} transcription(s) of {a.job} stopped in {len(res)} eye process(es)")
    for pid, ans, err in res:
        if err or (ans or {}).get("error"):
            print(f"  eye {pid}: no answer ({err or ans.get('error')})")
        else:
            print(f"  eye {pid}: {ans.get('cancelled', 0)} stopped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
