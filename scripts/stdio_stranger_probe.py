"""The stranger's first ten minutes, as a CI gate.

Boots the installed package the way a fresh ``pip install omniseek`` user does (MCP over
stdio), performs the real protocol handshake, lists tools, and makes one cache-only search
call. The one assertion that matters: EVERY call RETURNS. The 0.1.1 first-search deadlock
(the lazy rank/recall import chain racing the startup shadow probe's imports on the
event-loop thread, fatal on Windows) hung exactly here with zero log lines, which is why
this probe runs on a Windows runner too, and why it drains stderr on a thread: an undrained
pipe blocks the child on its own boot logging, and the probe would accuse the wrong party.
"""

import json
import os
import subprocess
import sys
import threading
import time

INIT_TIMEOUT_S = 240   # cold CI runner: first boot byte-compiles the world
CALL_TIMEOUT_S = 120


def main() -> int:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "omniseek.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        for raw in proc.stderr:
            stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            del stderr_tail[:-40]

    threading.Thread(target=_drain_stderr, daemon=True).start()

    replies: dict[int, dict] = {}

    def _drain_stdout() -> None:
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and "id" in msg:
                replies[msg["id"]] = msg

    threading.Thread(target=_drain_stdout, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    def wait_reply(rid: int, budget_s: float, label: str) -> dict:
        t0 = time.monotonic()
        while rid not in replies:
            if proc.poll() is not None:
                _fail(f"{label}: server process exited with {proc.returncode}")
            if time.monotonic() - t0 > budget_s:
                _fail(f"{label}: no reply within {budget_s:.0f}s (the deadlock class this gate exists for)")
            time.sleep(0.3)
        print(f"ok  {label} in {time.monotonic() - t0:.1f}s")
        return replies[rid]

    def _fail(why: str) -> None:
        print(f"FAIL  {why}")
        print("---- server stderr tail ----")
        for line in stderr_tail[-25:]:
            print(" ", line)
        proc.kill()
        raise SystemExit(1)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                     "clientInfo": {"name": "stranger-probe", "version": "0"}}})
    init = wait_reply(1, INIT_TIMEOUT_S, "initialize")
    if "result" not in init:
        _fail(f"initialize returned an error: {init.get('error')}")

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = wait_reply(2, CALL_TIMEOUT_S, "tools/list")["result"]["tools"]
    if len(tools) < 15:
        _fail(f"tools/list returned only {len(tools)} tools")
    print(f"ok  {len(tools)} tools listed")

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "omniseek_search",
                     "arguments": {"query": "stranger smoke", "staleness": "cache_only"}}})
    reply = wait_reply(3, CALL_TIMEOUT_S, "omniseek_search (cache_only)")
    if "result" not in reply:
        _fail(f"search returned an error: {reply.get('error')}")
    body = json.loads(reply["result"]["content"][0]["text"])
    print(f"ok  search returned: count={body.get('count')} (a cold cache may honestly hold 0)")

    proc.kill()
    print("STRANGER PROBE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
