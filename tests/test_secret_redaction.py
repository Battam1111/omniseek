"""Contract: no credential leaves OmniSeek's process in text, whichever way the text goes out.

The defect this pins (2026-10-04): httpx logs every request at INFO with its FULL address, so the
live service's organ.eye-http.err carried the OpenAlex key verbatim (``...&api_key=<key> "HTTP/1.1 200 OK"``).
The same address rides in an httpx HTTPStatusError's text, which OmniSeek copies into health
messages, ``_meta.diagnostic``, tool errors, alerts and the watchdog state file.

Pinned here, all offline, with a FAKE key:
  (a) a record written the way httpx writes it (msg with %s, the address as an arg) comes out of
      every entry point's logging setup with ``api_key=***`` and without the value: the stdio
      server's real main(), the HTTP service's setup (basicConfig + the rate-limit filter + the
      uvicorn dictConfig it runs under), and the isolated job child (no setup at all), including
      tracebacks and an uncaught exception in a thread;
  (b) an HTTPStatusError's text is masked in a health message, a diagnostic and a tool result /
      tool error, while a returned document passes through untouched;
  (c) the watchdog state file and the alert text are masked;
  (d) text without a secret is left byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from omniseek import redact  # noqa: E402

FAKE = "FAKEKEY123456"
URL = f"https://api.openalex.org/works?per-page=1&select=id&api_key={FAKE}"


def _status_error(url: str = URL) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(403, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status did not raise")


def _job_fixture_logs_and_raises() -> None:
    """Run by the isolated job child (python -m omniseek.core.job_runner) in test (a)."""
    logging.getLogger("httpx").warning('HTTP Request: %s %s "%s %d %s"',
                                       "GET", httpx.URL(URL), "HTTP/1.1", 429, "Too Many Requests")
    raise _status_error()


def _job_fixture_logs_info() -> None:
    """Run by the isolated job child: an ordinary INFO line, carrying a secret, must reach stderr
    (it was dropped before 2026-10-05) and arrive masked."""
    logging.getLogger("omniseek.core.infra_jobs").info("job-info-marker fetched %s", URL)


def _run_python(code: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = "400"  # the SDK's rich log handler wraps at the terminal width
    cmd = [sys.executable, *args] if not code else [sys.executable, "-c", code]
    return subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=300)


# ── (d) the rule itself ──────────────────────────────────────────────────────────────────────────
class MaskingRule(unittest.TestCase):
    def test_secret_values_are_masked_and_the_rest_is_kept(self):
        self.assertEqual(redact.redact(URL),
                         "https://api.openalex.org/works?per-page=1&select=id&api_key=***")
        for name in ("api_key", "APIKEY", "api-key", "key", "token", "access_token", "auth",
                     "client_secret", "secret", "password", "mailto"):
            with self.subTest(name=name):
                out = redact.redact(f"https://x.example/p?q=a&{name}={FAKE}&page=2")
                self.assertEqual(out, f"https://x.example/p?q=a&{name}=***&page=2")

    def test_headers_are_masked(self):
        for text in (f"Authorization: Bearer {FAKE}", f"{{'Authorization': 'Bearer {FAKE}'}}",
                     f"authorization=token {FAKE}", f"sent Bearer {FAKE} upstream"):
            with self.subTest(text=text):
                out = redact.redact(text)
                self.assertNotIn(FAKE, out)
                self.assertIn("***", out)
        self.assertEqual(redact.redact(f"Authorization: Basic {FAKE}"), "Authorization: Basic ***")

    def test_text_without_a_secret_is_unchanged(self):
        for text in ("https://api.openalex.org/works?per-page=1&select=id&q=large+language+models",
                     "https://bbs.example/search.php?srchtxt=%B2%A9%BA%F3&page=2",
                     "cache key=abc123 is a plain word here, not a query parameter",
                     "https://x.example/plain", "", "no equals sign at all"):
            with self.subTest(text=text):
                self.assertEqual(redact.redact(text), text)

    def test_placeholders_masked_values_and_nested_addresses(self):
        self.assertEqual(redact.redact("GET %s?api_key=%s"), "GET %s?api_key=%s")
        self.assertEqual(redact.redact("https://x?key=<redacted>&token=***"),
                         "https://x?key=<redacted>&token=***")
        nested = f"https://web.archive.org/web/2020/https%3A%2F%2Fa.com%2F%3Fq%3D1%26api_key%3D{FAKE}%26b%3D2"
        self.assertEqual(redact.redact(nested),
                         "https://web.archive.org/web/2020/https%3A%2F%2Fa.com%2F%3Fq%3D1%26api_key%3D***%26b%3D2")

    def test_redact_obj_keeps_identity_when_clean_and_honours_skip(self):
        clean = {"a": [1, "x"], "b": {"c": "https://x.example/?q=1"}}
        self.assertIs(redact.redact_obj(clean), clean)
        doc = {"source_id": "1", "url": f"https://x?key={FAKE}", "content": ""}
        out = redact.redact_obj({"error": URL, "documents": [doc]},
                                skip=lambda n: isinstance(n, dict) and "source_id" in n)
        self.assertNotIn(FAKE, out["error"])
        self.assertIs(out["documents"][0], doc)


# ── (a) logging, through every entry point's setup ───────────────────────────────────────────────
_EMIT = textwrap.dedent(f"""
    import logging, threading, httpx
    URL = {URL!r}
    logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"',
                                    "GET", httpx.URL(URL), "HTTP/1.1", 200, "OK")
    req = httpx.Request("GET", URL)
    try:
        httpx.Response(403, request=req).raise_for_status()
    except httpx.HTTPStatusError as exc:
        logging.getLogger("omniseek.core.fetcher").warning("fetch failed: %s", exc)
        logging.getLogger("omniseek.core.jobs").exception("job raised")
        err = exc
    def boom():
        raise err
    t = threading.Thread(target=boom, name="leaky")
    t.start(); t.join()
""")


class LogsAreMaskedAtEveryEntry(unittest.TestCase):
    def _assert_masked(self, out: str):
        # rich may still wrap a long line; whitespace removed, a wrapped key would rejoin
        flat = re.sub(r"\s+", "", out)
        self.assertNotIn(FAKE, out)
        self.assertNotIn(FAKE, flat)
        self.assertIn("api_key=***", flat)
        self.assertIn("HTTPRequest:GET", flat)

    def test_http_service_setup(self):
        # serve_http imports omniseek.server (whose FastMCP() installs the SDK's rich root handler),
        # then main() runs basicConfig + the rate-limit filter, then uvicorn.run(log_level="info")
        # applies uvicorn's LOGGING_CONFIG. serve_http itself cannot be imported here (it reads the
        # token file under ~/.omniseek at import), so those steps are replayed in that order.
        code = textwrap.dedent("""
            import omniseek.server, logging, logging.config
            logging.basicConfig(level=logging.INFO,
                                format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            from omniseek.core import _lograte
            _lograte.install_on_root()
            from uvicorn.config import LOGGING_CONFIG
            logging.config.dictConfig(LOGGING_CONFIG)
            logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:5", "GET", "/mcp?token=FAKEKEY123456", "1.1", 200)
        """) + _EMIT
        r = _run_python(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        self._assert_masked(out)
        self.assertIn("/mcp?token=***", out)
        self.assertIn("Exception in thread leaky", out)
        self.assertIn("job raised", out)

    def test_stdio_server_main(self):
        code = textwrap.dedent("""
            import logging, sys
            import omniseek.server as s
            def _run_then_emit(*a, **k):
        """) + textwrap.indent(_EMIT, "    ") + textwrap.dedent("""
            s.mcp.run = _run_then_emit
            s._warm_heavy_imports = lambda: None
            s.main()
        """)
        r = _run_python(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_masked(r.stderr)
        self.assertIn("OmniSeek MCP server ready (stdio)", r.stderr)
        self.assertIn("job raised", r.stderr)

    def test_isolated_job_child(self):
        r = _run_python("", "-m", "omniseek.core.job_runner", "tests.test_secret_redaction",
                        "_job_fixture_logs_and_raises")
        self.assertEqual(r.returncode, 1, r.stderr)
        self._assert_masked(r.stderr)
        self.assertIn("HTTPStatusError", r.stderr)
        self.assertIn("Traceback", r.stderr)

    def test_isolated_job_child_keeps_its_info_lines(self):
        r = _run_python("", "-m", "omniseek.core.job_runner", "tests.test_secret_redaction",
                        "_job_fixture_logs_info")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("job-info-marker", r.stderr)
        flat = re.sub(r"\s+", "", r.stderr)
        self.assertNotIn(FAKE, flat)
        self.assertIn("api_key=***", flat)

    def test_in_process_record_keeps_its_shape(self):
        redact.install_log_redaction()
        seen: list[logging.LogRecord] = []

        class _Keep(logging.Handler):
            def emit(self, record):
                seen.append(record)

        h = _Keep()
        lg = logging.getLogger("httpx")
        lg.addHandler(h)
        old = lg.level
        lg.setLevel(logging.INFO)
        try:
            lg.info('HTTP Request: %s %s "%s %d %s"', "GET", httpx.URL(URL), "HTTP/1.1", 200, "OK")
            lg.info("clean %s %d", httpx.URL("https://x.example/?q=1"), 3)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        masked, clean = seen
        self.assertNotIn(FAKE, masked.getMessage())
        self.assertEqual(len(masked.args), 5)
        self.assertEqual(masked.args[3], 200)
        self.assertIsInstance(clean.args[0], httpx.URL, "an arg without a secret keeps its type")


# ── (b) health message, diagnostic, tool result ──────────────────────────────────────────────────
class ReturnedTextIsMasked(unittest.TestCase):
    def test_health_message(self):
        from omniseek.core import fetcher

        class _Raises:
            name = "_redact_probe"

            def health_check(self):
                raise _status_error()

        class _Says:
            name = "_redact_probe2"

            def health_check(self):
                return False, f"HTTP 403 from {URL}"

        for adapter in (_Raises(), _Says()):
            ok, msg, completed = fetcher.health_check_outcome(adapter, timeout=5)
            self.assertIs(ok, False)
            self.assertTrue(completed)
            self.assertNotIn(FAKE, msg)
            self.assertIn("api_key=***", msg)

    def test_diagnostic(self):
        from omniseek.core import diag, fetcher
        diag.enable()
        diag.note("http.get", url=URL, status=403, exc=_status_error())
        captures = diag.drain()
        adapter = SimpleNamespace(name="_redact_diag")
        d = fetcher._build_diagnostic(adapter, docs=[], captures=captures, timed_out=False,
                                      raised=_status_error(), deadline_s=10)
        text = json.dumps(d)
        self.assertNotIn(FAKE, text)
        self.assertIn("api_key=***", d["note"])
        self.assertIn("api_key=<redacted>", d["captures"][0]["url"], "diag keeps its own marker")

    def test_tool_result_and_tool_error(self):
        import omniseek.server as server
        doc = {"source": "s", "source_id": "1", "url": f"https://site.example/a?key={FAKE}",
               "title": "t", "content": "c"}

        def _redact_probe_ok() -> dict:
            """probe"""
            return {"error": str(_status_error()), "_meta": {"diagnostic": {"note": URL}},
                    "documents": [doc]}

        def _redact_probe_raises() -> dict:
            """probe"""
            raise _status_error()

        for fn in (_redact_probe_ok, _redact_probe_raises):
            server.mcp.add_tool(fn)
        try:
            result = asyncio.run(server.mcp.call_tool("_redact_probe_ok", {}))
            blocks, structured = result if isinstance(result, tuple) else (result, None)
            text = "".join(getattr(b, "text", "") for b in blocks)
            self.assertIn("api_key=***", text)
            self.assertEqual(text.count(FAKE), 1, "only the document's own URL keeps its value")
            if structured is not None:
                self.assertEqual(structured["documents"][0]["url"], doc["url"])
                self.assertNotIn(FAKE, structured["error"])
            from mcp.server.fastmcp.exceptions import ToolError
            with self.assertRaises(ToolError) as ctx:
                asyncio.run(server.mcp.call_tool("_redact_probe_raises", {}))
            self.assertNotIn(FAKE, str(ctx.exception))
            self.assertIn("api_key=***", str(ctx.exception))
        finally:
            for fn in (_redact_probe_ok, _redact_probe_raises):
                server.mcp._tool_manager._tools.pop(fn.__name__, None)


# ── (c) watchdog state file and alerts ───────────────────────────────────────────────────────────
class WrittenStateIsMasked(unittest.TestCase):
    def test_watchdog_state_file(self):
        import omniseek.server as server
        from omniseek.core import fetcher, infra_jobs
        answers = {"down_src": (False, str(_status_error()), True),
                   "unverified_src": (None, f"not probed: {URL}", True),
                   "slow_src": (None, f"timeout on {URL}", False)}
        adapters = {n: SimpleNamespace(name=n, explicit_only=False) for n in answers}
        alerts: list = []
        with tempfile.TemporaryDirectory(prefix="omniseek-redact-") as td:
            statefile = Path(td) / "health-watchdog-state.json"
            statefile.write_text(json.dumps({"fails": {"down_src": 1}, "_alerts": {}}), encoding="utf-8")
            with mock.patch.object(infra_jobs, "_HEALTH_STATE", statefile), \
                    mock.patch.object(infra_jobs, "_health_probe", lambda a: answers[a.name]), \
                    mock.patch.object(infra_jobs, "_heal_cdp_chrome", lambda: []), \
                    mock.patch.object(infra_jobs, "_CDP_INSTANCES", {}), \
                    mock.patch.object(infra_jobs, "_alert", lambda t, b="", **k: alerts.append((t, b))), \
                    mock.patch.object(server, "load_sources", lambda: None), \
                    mock.patch.object(fetcher, "all_adapter_names", lambda: sorted(adapters)), \
                    mock.patch.object(fetcher, "get_adapter", adapters.get), \
                    mock.patch.object(fetcher, "retired_reason", lambda a: ""):
                infra_jobs.run_source_health(scope="noncdp")
            raw = statefile.read_text(encoding="utf-8")
        self.assertNotIn(FAKE, raw)
        saved = json.loads(raw)
        self.assertIn("api_key=***", saved["unverified"]["unverified_src"])
        self.assertIn("api_key=***", saved["unmeasured"]["slow_src"])
        self.assertEqual(saved["fails"]["down_src"], 2, "the bookkeeping itself is unchanged")

    def test_save_state_masks_on_disk_and_leaves_the_callers_dict(self):
        from omniseek.core import infra_jobs
        data = {"note": URL, "n": 3}
        with tempfile.TemporaryDirectory(prefix="omniseek-redact-") as td:
            path = Path(td) / "s.json"
            infra_jobs._save_state(path, data)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"note": URL.replace(FAKE, "***"), "n": 3})
        self.assertEqual(data["note"], URL)

    def test_alert_text(self):
        from omniseek.core import notify
        pushed: list = []
        with tempfile.TemporaryDirectory(prefix="omniseek-redact-") as td, \
                mock.patch.object(notify, "_ALERT_DELIVERY_PATH", Path(td) / "alert-delivery.json"), \
                mock.patch.object(notify, "wecom_push", lambda t, b: pushed.append((t, b)) or True):
            notify.alert(f"source down {URL}", f"- openalex: {_status_error()}")
        self.assertNotIn(FAKE, repr(pushed))
        self.assertIn("api_key=***", pushed[0][1])


if __name__ == "__main__":
    unittest.main()
