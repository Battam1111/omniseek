"""Real MCP handshake probe for an installed OmniSeek (CI regression gate).

Run after `pip install .`:

    python .github/workflows/scripts/mcp_client_probe.py

What it checks, each with the official MCP Python client (no hand-rolled JSON-RPC):

  1. stdio: launch the `omniseek` console script, then initialize + tools/list.
  2. HTTP: start `python -m omniseek.serve_http` on the loopback address with a throwaway
     token, then initialize + tools/list once via http://127.0.0.1:<port>/mcp and once via
     http://localhost:<port>/mcp, with the bearer token.

A /healthz answer is only used to know the server is up; it never counts as a pass, because
/healthz is served before the MCP transport and its Host check, so it stays green while every
real client gets 421. The server and the stdio child run with a temporary HOME, so the probe
never touches the caller's ~/.omniseek. Exit code 0 only when every check passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

INIT_TIMEOUT_S = 180
EXPECTED_TOOL = "omniseek_search"


def _flatten(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(_flatten(sub))
        return out
    return [exc]


def _describe(exc: BaseException) -> str:
    return "\n      ".join(f"{type(e).__name__}: {e}" for e in _flatten(exc))


async def _handshake(session: ClientSession) -> str:
    init = await asyncio.wait_for(session.initialize(), INIT_TIMEOUT_S)
    tools = await asyncio.wait_for(session.list_tools(), INIT_TIMEOUT_S)
    names = [t.name for t in tools.tools]
    if EXPECTED_TOOL not in names:
        raise RuntimeError(f"tools/list returned {len(names)} tools without {EXPECTED_TOOL}: {names}")
    return (f"server={init.serverInfo.name} {init.serverInfo.version} "
            f"protocol={init.protocolVersion} tools={len(names)}")


def _child_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


async def probe_stdio(home: Path) -> tuple[bool, str]:
    exe = shutil.which("omniseek")
    if not exe:
        return False, "the `omniseek` command is not on PATH (console script missing?)"
    params = StdioServerParameters(command=exe, args=[], env=_child_env(home))
    logpath = home / "stdio_server.log"
    with open(logpath, "w") as errlog:
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    return True, await _handshake(session)
        except BaseException as exc:  # noqa: BLE001
            errlog.flush()
            tail = logpath.read_text(errors="replace")[-2000:]
            return False, f"{_describe(exc)}\n      server stderr tail:\n{tail}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _raw_initialize(url: str, token: str) -> str:
    """One plain POST so a failure shows the server's own status line and body."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "omniseek-ci-probe", "version": "0"}},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return f"HTTP {resp.status}"
    except urllib.error.HTTPError as err:
        return f"HTTP {err.code} {err.read()[:200].decode(errors='replace')}"
    except Exception as err:  # noqa: BLE001
        return f"{type(err).__name__}: {err}"


async def probe_http_host(host: str, port: int, token: str) -> tuple[bool, str]:
    url = f"http://{host}:{port}/mcp"
    try:
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (
                read, write, _):
            async with ClientSession(read, write) as session:
                return True, f"{url} {await _handshake(session)}"
    except BaseException as exc:  # noqa: BLE001
        return False, (f"{url}\n      {_describe(exc)}\n"
                       f"      raw POST initialize: {_raw_initialize(url, token)}")


def _start_http(home: Path, port: int) -> tuple[subprocess.Popen, str, Path]:
    cred = home / ".omniseek" / "credentials"
    cred.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tok_file = cred / "omniseek_http.json"
    tok_file.write_text(json.dumps({"token": token}))
    os.chmod(tok_file, 0o600)
    env = _child_env(home)
    env["OMNISEEK_HTTP_HOST"] = "127.0.0.1"
    env["OMNISEEK_HTTP_PORT"] = str(port)
    logpath = home / "serve_http.log"
    log = open(logpath, "w")  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-m", "omniseek.serve_http"], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    return proc, token, logpath


def _wait_healthz(proc: subprocess.Popen, port: int, deadline_s: float = INIT_TIMEOUT_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


async def main() -> int:
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory(prefix="omniseek-probe-") as tmp:
        home = Path(tmp)
        print("== stdio: omniseek (console script) ==", flush=True)
        ok, msg = await probe_stdio(home)
        print(f"  {'PASS' if ok else 'FAIL'} stdio initialize + tools/list: {msg}", flush=True)
        results.append(("stdio", ok))

        port = _free_port()
        print(f"== HTTP: python -m omniseek.serve_http on 127.0.0.1:{port} ==", flush=True)
        proc, token, logpath = _start_http(home, port)
        try:
            if not _wait_healthz(proc, port):
                tail = logpath.read_text(errors="replace")[-2000:]
                print(f"  FAIL start: server never answered /healthz (exit={proc.poll()}); "
                      f"log tail:\n{tail}", flush=True)
                results.append(("http-start", False))
            else:
                print("  up (/healthz answered; not counted as a pass)", flush=True)
                for host in ("127.0.0.1", "localhost"):
                    ok, msg = await probe_http_host(host, port, token)
                    print(f"  {'PASS' if ok else 'FAIL'} HTTP {host} initialize + tools/list: {msg}",
                          flush=True)
                    results.append((f"http-{host}", ok))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    failed = [name for name, ok in results if not ok]
    print(f"== {len(results) - len(failed)}/{len(results)} passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
