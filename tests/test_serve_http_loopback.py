"""The HTTP service on its loopback default must accept a real MCP client.

A stranger's first install (2026-10-10) bound 127.0.0.1 with the SDK's DNS-rebinding check on and
an EMPTY allowlist, so every client got "421 Invalid Host header" while /healthz said ok. /healthz
never reaches the MCP transport, so only a real initialize + tools/list proves the path works.

One server subprocess (its own token in a temp credentials dir handed over explicitly, a temp cache dir,
temp port; HOME is left alone), the module's own `app` under uvicorn
(not main(): no warmer, scheduler or recall writer, so no network). Then:
  - the SDK client does initialize + tools/list over 127.0.0.1 and over localhost;
  - a foreign Host still gets 421 and a foreign Origin 403 (the protection stayed on);
  - a non-loopback bind keeps the check off, as before.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
_BOOT_TIMEOUT_S = 180  # a cold import of the whole server measured ~34s on the deploy host; 5x headroom


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _token_dir(tmp: Path, token: str) -> Path:
    """The test's own credentials dir (never under any HOME) holding omniseek_http.json, mode 600."""
    cred = tmp / "creds"
    cred.mkdir()
    tok = cred / "omniseek_http.json"
    tok.write_text(json.dumps({"token": token}))
    os.chmod(tok, 0o600)
    return cred


def _creds_at(cred: Path) -> str:
    """Child-process prelude: point OmniSeek's credentials dir at ``cred`` before serve_http reads its
    token (serve_http takes the token path from auth.CREDS_DIR at import)."""
    return f"import pathlib, omniseek.core.auth as _a; _a.CREDS_DIR = pathlib.Path({str(cred)!r}); "


def _client():
    try:
        from mcp.client.streamable_http import streamablehttp_client as c
    except ImportError:  # mcp 2.x renamed it
        from mcp.client.streamable_http import streamable_http_client as c
    return c


class LoopbackHttpAcceptsRealClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        cls.token = "t" * 40
        cred = _token_dir(Path(cls.home.name), cls.token)
        cls.port = _free_port()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "OMNISEEK_CACHE_DIR": str(Path(cls.home.name) / "cache"),
               "OMNISEEK_HTTP_HOST": "127.0.0.1", "OMNISEEK_HTTP_PORT": str(cls.port)}
        code = (_creds_at(cred) + "import uvicorn, omniseek.serve_http as s; "
                "uvicorn.run(s.app, host=s.HOST, port=s.PORT, log_level='warning')")
        cls.log = tempfile.TemporaryFile(mode="w+")
        cls.proc = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                                    stdout=cls.log, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                break
            try:
                if httpx.get(f"http://127.0.0.1:{cls.port}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        cls._stop()
        cls.log.seek(0)
        raise RuntimeError(f"server did not come up on {cls.port}:\n{cls.log.read()[-3000:]}")

    @classmethod
    def _stop(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(15)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(5)

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        cls.log.close()
        cls.home.cleanup()

    def _handshake(self, host: str):
        async def go():
            url = f"http://{host}:{self.port}/mcp"
            async with _client()(url, headers={"Authorization": f"Bearer {self.token}"}) as streams:
                from mcp import ClientSession
                async with ClientSession(streams[0], streams[1]) as s:
                    init = await asyncio.wait_for(s.initialize(), 30)
                    tools = await asyncio.wait_for(s.list_tools(), 30)
                    return init, tools
        return asyncio.run(go())

    def test_initialize_and_list_tools_over_127_0_0_1(self):
        init, tools = self._handshake("127.0.0.1")
        self.assertTrue(init.serverInfo.name)
        self.assertGreater(len(tools.tools), 0)

    def test_initialize_and_list_tools_over_localhost(self):
        init, tools = self._handshake("localhost")
        self.assertTrue(init.serverInfo.name)
        self.assertGreater(len(tools.tools), 0)

    def _raw_init(self, **headers):
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}
        return httpx.post(f"http://127.0.0.1:{self.port}/mcp", json=body, timeout=30,
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/json, text/event-stream", **headers})

    def test_foreign_host_still_rejected(self):
        r = self._raw_init(Host=f"evil.example:{self.port}")
        self.assertEqual(r.status_code, 421, r.text)

    def test_foreign_origin_still_rejected(self):
        r = self._raw_init(Origin="http://evil.example")
        self.assertEqual(r.status_code, 403, r.text)
        r = self._raw_init(Origin=f"http://localhost:{self.port + 1}")
        self.assertEqual(r.status_code, 403, r.text)


class TransportSecurityTracksBind(unittest.TestCase):
    """Read the settings in a child process: importing serve_http loads a token and reconfigures the
    process-global FastMCP, which must not leak into the other suites of this run."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            cred = _token_dir(Path(tmp), "t" * 40)
            code = (_creds_at(cred) + "import json, omniseek.serve_http as s; "
                    "print(json.dumps({h: s._transport_security(h, 8765)"
                    ".model_dump() for h in ('0.0.0.0', '127.0.0.1', 'localhost', '::1')}))")
            out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True,
                                 text=True, timeout=_BOOT_TIMEOUT_S,
                                 env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                                      "OMNISEEK_CACHE_DIR": str(Path(tmp) / "cache")})
        if out.returncode != 0:
            raise RuntimeError(out.stderr[-3000:])
        cls.by_host = json.loads(out.stdout.strip().splitlines()[-1])

    def test_non_loopback_bind_keeps_check_off(self):
        self.assertFalse(self.by_host["0.0.0.0"]["enable_dns_rebinding_protection"])

    def test_loopback_bind_allows_only_this_port(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            s = self.by_host[host]
            self.assertTrue(s["enable_dns_rebinding_protection"])
            self.assertIn("127.0.0.1:8765", s["allowed_hosts"])
            self.assertIn("localhost:8765", s["allowed_hosts"])
            self.assertFalse(any(h.endswith(":*") for h in s["allowed_hosts"] + s["allowed_origins"]))


if __name__ == "__main__":
    unittest.main()
