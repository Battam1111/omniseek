"""eye.lifecycle shutdown cap: a lifespan shutdown that does not finish (a background loop that ignores
its stop Event, FastMCP's session manager waiting on a worker thread that never returns, the event
loop blocked by a synchronous call, native code holding the GIL) ends within the cap, leaves one
WARNING naming what is still running, and exits non-zero. A normal shutdown is untouched.

The wait before that, for open connections and running requests (a request that never answers, a
stream the client never closes), ends at the drain cap: uvicorn cancels them and shuts down normally,
or, when the event loop is blocked and uvicorn's own timeout cannot fire, the drain watchdog forces
the exit.

The in-process tests inject the exit function; the subprocess tests run a real uvicorn server (the
same capped_server_class serve_http uses) on an ephemeral port, stop it the way a user stops it on
that platform and time the exit: SIGTERM on macOS / Linux, Ctrl+Break on Windows (there
send_signal(SIGTERM) is TerminateProcess, a hard kill that runs no shutdown code at all, so it cannot
test the cap). Only OMNISEEK_CACHE_DIR is set (a temp dir)."""

import asyncio
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from omniseek.core import lifecycle

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.x` puts the repo root there
    from _clock import at_least
except ImportError:
    from tests._clock import at_least

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class _Recorder:
    def __init__(self):
        self.codes = []
        self.called = threading.Event()

    def __call__(self, code):
        self.codes.append(code)
        self.called.set()


class InProcessCapTests(unittest.TestCase):
    def setUp(self):
        lifecycle.disarm_shutdown_watchdog()
        self._saved = list(lifecycle._stops)
        lifecycle._stops.clear()

    def tearDown(self):
        lifecycle.disarm_shutdown_watchdog()
        lifecycle._stops[:] = self._saved

    def test_stuck_loop_and_stuck_fastmcp_shutdown_hit_the_cap_with_warning(self):
        release = threading.Event()
        stuck_stop = threading.Event()   # the loop never looks at it: it does not exit on drain
        stuck = threading.Thread(target=release.wait, name="stuck-test-loop", daemon=True)
        stuck.start()
        lifecycle.register_loop("stuck-test-loop", stuck_stop, stuck)

        @contextlib.asynccontextmanager
        async def fastmcp_hangs(app_):
            try:
                yield None
            finally:   # like the session manager awaiting a tool body in a worker thread
                await asyncio.get_running_loop().run_in_executor(None, release.wait)

        rec = _Recorder()
        composed = lifecycle.compose_lifespan(fastmcp_hangs, cap_s=1.0, drain_timeout=0.2,
                                              exit_fn=rec, backstop=False)

        async def serve():
            async with composed("APP"):
                pass

        runner = threading.Thread(target=asyncio.run, args=(serve(),), daemon=True)
        with self.assertLogs("omniseek.core.lifecycle", level="WARNING") as cm:
            t0 = time.monotonic()
            runner.start()
            fired = rec.called.wait(5.0)
            elapsed = time.monotonic() - t0
        release.set()
        runner.join(5.0)

        self.assertTrue(fired, "the cap never fired")
        self.assertEqual(rec.codes, [1])
        self.assertLess(elapsed, 1.0 + 1.5)
        warn = [r for r in cm.output if "shutdown cap hit" in r]
        self.assertEqual(len(warn), 1, cm.output)
        self.assertIn("stuck-test-loop", warn[0])
        self.assertIn("FastMCP session manager shutdown", warn[0])

    def test_normal_shutdown_does_not_fire(self):
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, name="good-loop", daemon=True)
        t.start()
        lifecycle.register_loop("good-loop", stop, t)

        @contextlib.asynccontextmanager
        async def fastmcp_ok(app_):
            yield None

        rec = _Recorder()
        composed = lifecycle.compose_lifespan(fastmcp_ok, cap_s=2.0, drain_timeout=1.0,
                                              exit_fn=rec, backstop=False)

        async def serve():
            async with composed("APP"):
                pass

        with self.assertLogs("omniseek.core.lifecycle", level="INFO") as cm:
            t0 = time.monotonic()
            asyncio.run(serve())
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertFalse(t.is_alive())
        self.assertFalse(rec.called.is_set())
        self.assertFalse([r for r in cm.output if r.startswith("WARNING")], cm.output)
        self.assertTrue(any("lifespan shutdown done in" in r for r in cm.output), cm.output)

    def test_disarmed_before_cap_never_calls_exit(self):
        rec = _Recorder()
        lifecycle.arm_shutdown_watchdog(0.3, exit_fn=rec, backstop=False)
        lifecycle.disarm_shutdown_watchdog()
        self.assertFalse(rec.called.wait(0.6))

    def test_cap_zero_disables(self):
        rec = _Recorder()
        self.assertIsNone(lifecycle.arm_shutdown_watchdog(0, exit_fn=rec, backstop=False))
        self.assertFalse(rec.called.wait(0.2))

    def test_arm_is_idempotent(self):
        rec = _Recorder()
        self.assertIsNotNone(lifecycle.arm_shutdown_watchdog(5, exit_fn=rec, backstop=False))
        self.assertIsNone(lifecycle.arm_shutdown_watchdog(5, exit_fn=rec, backstop=False))

    def test_env_override(self):
        old = os.environ.get("OMNISEEK_SHUTDOWN_CAP_S")
        try:
            os.environ.pop("OMNISEEK_SHUTDOWN_CAP_S", None)
            self.assertEqual(lifecycle.shutdown_cap_s(), lifecycle.SHUTDOWN_CAP_S_DEFAULT)
            os.environ["OMNISEEK_SHUTDOWN_CAP_S"] = "12.5"
            self.assertEqual(lifecycle.shutdown_cap_s(), 12.5)
            os.environ["OMNISEEK_SHUTDOWN_CAP_S"] = "0"
            self.assertEqual(lifecycle.shutdown_cap_s(), 0.0)
            os.environ["OMNISEEK_SHUTDOWN_CAP_S"] = "nope"
            self.assertEqual(lifecycle.shutdown_cap_s(), lifecycle.SHUTDOWN_CAP_S_DEFAULT)
        finally:
            if old is None:
                os.environ.pop("OMNISEEK_SHUTDOWN_CAP_S", None)
            else:
                os.environ["OMNISEEK_SHUTDOWN_CAP_S"] = old


class InProcessDrainCapTests(unittest.TestCase):
    """The connections stage (SIGTERM until uvicorn starts the lifespan shutdown) and its hand-over to
    the lifespan stage, on one clock from the signal. backstop=False: a faulthandler exit would end
    the test process."""

    def setUp(self):
        lifecycle.disarm_shutdown_watchdog()

    def tearDown(self):
        lifecycle.disarm_shutdown_watchdog()

    def test_unprepared_drain_arm_is_a_no_op(self):
        self.assertFalse(lifecycle.arm_drain_watchdog(1.0, backstop=False))
        self.assertIsNone(lifecycle._wd_state["stage"])

    def test_drain_zero_disables(self):
        rec = _Recorder()
        self.assertTrue(lifecycle.prepare_drain_watchdog(exit_fn=rec))
        self.assertFalse(lifecycle.arm_drain_watchdog(0, backstop=False))
        self.assertFalse(rec.called.wait(0.3))

    def test_drain_stage_fires_at_drain_plus_grace(self):
        rec = _Recorder()
        self.assertTrue(lifecycle.prepare_drain_watchdog(exit_fn=rec))
        with self.assertLogs("omniseek.core.lifecycle", level="INFO") as cm:
            t0 = time.monotonic()
            self.assertTrue(lifecycle.arm_drain_watchdog(0.3, grace_s=0.2, backstop=False))
            self.assertFalse(lifecycle.arm_drain_watchdog(0.3, grace_s=0.2, backstop=False))
            fired = rec.called.wait(5.0)
            elapsed = time.monotonic() - t0
        self.assertTrue(fired, cm.output)
        self.assertEqual(rec.codes, [1])
        self.assertGreaterEqual(elapsed, at_least(0.5))
        self.assertLess(elapsed, 0.5 + 1.0)
        self.assertTrue(any("drain cap armed" in r for r in cm.output), cm.output)
        warn = [r for r in cm.output if "shutdown cap hit" in r]
        self.assertEqual(len(warn), 1, cm.output)
        self.assertIn("after SIGTERM", warn[0])
        self.assertIn("waiting for open connections", warn[0])
        self.assertIn("has not yet started the shutdown", warn[0])

    def test_lifespan_stage_takes_over_and_drain_deadline_is_dropped(self):
        rec = _Recorder()
        lifecycle.prepare_drain_watchdog(exit_fn=rec)
        with self.assertLogs("omniseek.core.lifecycle", level="INFO") as cm:
            t0 = time.monotonic()
            lifecycle.arm_drain_watchdog(0.4, grace_s=0.2, backstop=False)   # alone: fires at 0.6
            time.sleep(0.1)
            self.assertIsNotNone(lifecycle.arm_shutdown_watchdog(1.0, exit_fn=rec, backstop=False))
            self.assertFalse(rec.called.wait(0.9))    # the drain deadline (0.6) no longer applies
            fired = rec.called.wait(5.0)
            elapsed = time.monotonic() - t0
        self.assertTrue(fired, cm.output)
        self.assertEqual(rec.codes, [1])
        self.assertGreaterEqual(elapsed, at_least(1.1))   # 0.1 + the 1.0 lifespan cap
        self.assertLess(elapsed, 1.1 + 1.0)
        self.assertTrue(any("SIGTERM to lifespan shutdown" in r for r in cm.output), cm.output)
        warn = [r for r in cm.output if "shutdown cap hit" in r]
        self.assertEqual(len(warn), 1, cm.output)
        self.assertIn("after the lifespan shutdown began", warn[0])

    def test_total_stays_within_drain_plus_cap_after_sigterm(self):
        """A connections stage that ends late (inside its grace) does not add the grace to the
        total: the lifespan stage ends at most drain + cap after the SIGTERM."""
        rec = _Recorder()
        lifecycle.prepare_drain_watchdog(exit_fn=rec)
        t0 = time.monotonic()
        lifecycle.arm_drain_watchdog(0.2, grace_s=5.0, backstop=False)
        time.sleep(1.0)                              # past the drain cap, inside the grace
        with self.assertLogs("omniseek.core.lifecycle", level="INFO"):
            lifecycle.arm_shutdown_watchdog(2.0, exit_fn=rec, backstop=False)
        fired = rec.called.wait(5.0)
        elapsed = time.monotonic() - t0
        self.assertTrue(fired)
        self.assertGreaterEqual(elapsed, at_least(2.2))   # 0.2 + 2.0 from the SIGTERM
        self.assertLess(elapsed, 2.7)                # not 1.0 + 2.0 = 3.0

    def test_shutdown_cap_off_drops_the_drain_deadline(self):
        """OMNISEEK_SHUTDOWN_CAP_S=0 with the drain cap on: once the lifespan shutdown begins, the
        connections stage's deadline must not cut it short."""
        rec = _Recorder()
        lifecycle.prepare_drain_watchdog(exit_fn=rec)
        lifecycle.arm_drain_watchdog(0.3, grace_s=0.2, backstop=False)   # alone: fires at 0.5
        time.sleep(0.1)
        self.assertIsNone(lifecycle.arm_shutdown_watchdog(0, exit_fn=rec, backstop=False))
        self.assertFalse(rec.called.wait(1.0))
        self.assertEqual(lifecycle._wd_state["stage"], "lifespan")
        self.assertIsNone(lifecycle._wd_state["deadline"])

    def test_drain_env_override(self):
        old = os.environ.get("OMNISEEK_DRAIN_CAP_S")
        try:
            os.environ.pop("OMNISEEK_DRAIN_CAP_S", None)
            self.assertEqual(lifecycle.drain_cap_s(), lifecycle.DRAIN_CAP_S_DEFAULT)
            os.environ["OMNISEEK_DRAIN_CAP_S"] = "7"
            self.assertEqual(lifecycle.drain_cap_s(), 7.0)
            os.environ["OMNISEEK_DRAIN_CAP_S"] = "0"
            self.assertEqual(lifecycle.drain_cap_s(), 0.0)
            os.environ["OMNISEEK_DRAIN_CAP_S"] = "x"
            self.assertEqual(lifecycle.drain_cap_s(), lifecycle.DRAIN_CAP_S_DEFAULT)
        finally:
            if old is None:
                os.environ.pop("OMNISEEK_DRAIN_CAP_S", None)
            else:
                os.environ["OMNISEEK_DRAIN_CAP_S"] = old


_CHILD = r'''
import asyncio, contextlib, logging, os, sys, threading, time
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from omniseek.core import lifecycle

MODE = sys.argv[1]
CAP = float(sys.argv[2])
DRAIN = float(sys.argv[3])

if MODE in ("normal", "hang_requests", "drain_loop_blocked", "drain_loop_blocked_late"):
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, name="good-loop", daemon=True)
    t.start()
    lifecycle.register_loop("good-loop", stop, t)
else:
    t = threading.Thread(target=lambda: time.sleep(3600), name="stuck-loop", daemon=True)
    t.start()
    lifecycle.register_loop("stuck-loop", threading.Event(), t)

@contextlib.asynccontextmanager
async def fake_fastmcp(app_):
    try:
        yield None
    finally:
        if MODE == "worker":        # session manager awaiting a tool body in an AnyIO worker thread
            import anyio
            await anyio.to_thread.run_sync(lambda: time.sleep(3600))
        elif MODE == "loop_blocked":  # a synchronous call on the event-loop thread
            time.sleep(3600)
        elif MODE == "gil":          # native code holding the GIL: no Python thread can run
            import ctypes            # PyDLL never releases the GIL around the call
            if sys.platform == "win32":
                ctypes.PyDLL("kernel32").Sleep(3600 * 1000)
            else:
                ctypes.PyDLL(None).sleep(3600)


def started(name):
    print(f"REQUEST STARTED {name}", file=sys.stderr, flush=True)


async def hang(request):             # a tool call that never returns: no response byte is ever sent
    started("hang")
    await asyncio.Event().wait()


async def stream(request):           # an SSE stream the client never closes (MCP's GET stream shape)
    async def body():
        started("stream")
        while True:
            yield b": ping\n\n"
            await asyncio.sleep(0.2)
    return StreamingResponse(body(), media_type="text/event-stream")


async def block(request):            # a synchronous call on the loop thread before the stop signal
    started("block")
    time.sleep(3600)


async def block_late(request):       # the loop blocks only after uvicorn began waiting for connections
    started("block_late")
    await asyncio.sleep(1.0)
    time.sleep(3600)


app = Starlette(routes=[Route("/hang", hang), Route("/stream", stream), Route("/block", block),
                        Route("/block_late", block_late)],
                lifespan=lifecycle.compose_lifespan(fake_fastmcp, cap_s=CAP, drain_timeout=0.3))

Base = lifecycle.capped_server_class(uvicorn.Server, cap_s=CAP)   # the class serve_http uses


class Server(Base):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if MODE == "loop_blocked_early":
            # a callback that blocks the loop thread is already queued when uvicorn sends
            # lifespan.shutdown, so our lifespan code never gets the loop back (the 10-10 / 10-11 shape)
            capped = self.lifespan.shutdown
            async def shutdown_behind_a_blocker():
                asyncio.get_running_loop().call_soon(time.sleep, 3600)
                await capped()
            self.lifespan.shutdown = shutdown_behind_a_blocker


Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="info",
                      timeout_graceful_shutdown=DRAIN or None)).run()
'''

# How a user stops the server gracefully, per platform. uvicorn's HANDLED_SIGNALS are SIGINT and
# SIGTERM, plus SIGBREAK on win32. On Windows a child started in its own process group cannot get
# Ctrl+C (Windows disables it for a new group), but Ctrl+Break reaches it as SIGBREAK. After a
# graceful stop uvicorn re-raises the signal it caught with the default action: -SIGTERM on POSIX,
# exit code 3 on Windows (the C runtime's default for SIGBREAK). _STOP_NAME is the signal the child's
# lifecycle log names.
if sys.platform == "win32":
    _STOP_SIGNAL = signal.CTRL_BREAK_EVENT
    _STOP_NAME = "SIGBREAK"
    _POPEN_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
    _CLEAN_EXIT_CODES = (0, 3)
else:
    _STOP_SIGNAL = signal.SIGTERM
    _STOP_NAME = "SIGTERM"
    _POPEN_FLAGS = 0
    _CLEAN_EXIT_CODES = (0, -signal.SIGTERM)


def _requests_cancelled_at_drain_cap(n_requests, n_streams):
    """How many of the hanging requests uvicorn cancels at the drain cap. uvicorn 0.43.0 and 0.44.0
    send http.disconnect to running requests when the shutdown begins, which ends Starlette's SSE
    stream at once (encode/uvicorn #2829); 0.45.0 reverted that (#2913), so there the stream is
    cancelled at the cap like the request that never answers."""
    import uvicorn
    version = tuple(int(x) for x in re.findall(r"\d+", uvicorn.__version__)[:2])
    if (0, 43) <= version < (0, 45):
        return n_requests
    return n_requests + n_streams


class SubprocessCapTests(unittest.TestCase):
    CAP = 2.0
    DRAIN = 2.0

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="x.", dir=os.environ.get("TMPDIR"))
        cls._script = os.path.join(cls._tmp, "child.py")
        with open(cls._script, "w") as f:
            f.write(_CHILD)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _run(self, mode, budget, requests=()):
        """Start the child, open one connection per path in ``requests`` and wait until each request
        is running in the child, then stop it (_STOP_SIGNAL) and time the exit."""
        env = dict(os.environ)
        env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
        env["OMNISEEK_CACHE_DIR"] = self._tmp
        p = subprocess.Popen([sys.executable, self._script, mode, str(self.CAP), str(self.DRAIN)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                             creationflags=_POPEN_FLAGS)
        lines = []
        ready = threading.Event()
        port = []
        socks = []
        req_seen = {path: threading.Event() for path in requests}

        def pump():
            for line in p.stdout:
                lines.append(line)
                m = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", line)
                if m:
                    port.append(int(m.group(1)))
                if "Application startup complete" in line:
                    ready.set()
                m = re.search(r"REQUEST STARTED (\w+)", line)
                if m and "/" + m.group(1) in req_seen:
                    req_seen["/" + m.group(1)].set()
        threading.Thread(target=pump, daemon=True).start()
        try:
            self.assertTrue(ready.wait(60), "".join(lines))
            for path in requests:
                for _ in range(50):
                    if port:
                        break
                    time.sleep(0.05)
                s = socket.create_connection(("127.0.0.1", port[0]), timeout=5)
                s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                socks.append(s)        # never read, never closed until the child is gone
                self.assertTrue(req_seen[path].wait(10), "".join(lines))
            t0 = time.monotonic()
            p.send_signal(_STOP_SIGNAL)
            try:
                rc = p.wait(budget)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
                self.fail(f"{mode}: still alive {budget}s after {_STOP_SIGNAL!r}\n" + "".join(lines))
            elapsed = time.monotonic() - t0
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(5)
            for s in socks:
                s.close()
        time.sleep(0.2)
        p.stdout.close()
        out = "".join(lines)
        print(f"\n--- {mode}: rc={rc} elapsed={elapsed:.2f}s ---\n{out}", file=sys.stderr)
        return rc, elapsed, out

    def test_worker_thread_never_returns(self):
        rc, elapsed, out = self._run("worker", self.CAP + 10)
        self.assertEqual(rc, 1, out)
        self.assertLess(elapsed, self.CAP + 3)
        self.assertIn("shutdown cap hit", out)
        self.assertIn("stuck-loop", out)
        self.assertIn("FastMCP session manager shutdown", out)
        self.assertIn("forced exit(1)", out)
        self.assertIn("Thread 0x", out)   # faulthandler's all-thread stack dump

    def test_event_loop_blocked(self):
        rc, elapsed, out = self._run("loop_blocked", self.CAP + 10)
        self.assertEqual(rc, 1, out)
        self.assertLess(elapsed, self.CAP + 3)
        self.assertIn("shutdown cap hit", out)
        self.assertIn("MainThread", out)

    def test_event_loop_blocked_before_our_lifespan_code_runs(self):
        rc, elapsed, out = self._run("loop_blocked_early", self.CAP + 10)
        self.assertEqual(rc, 1, out)
        self.assertLess(elapsed, self.CAP + 3)
        self.assertIn("shutdown cap hit", out)
        self.assertIn("event loop not yet back in our lifespan code", out)
        self.assertNotIn("lifecycle drain:", out)   # drain_all never ran, as in the two stalls
        self.assertIn("forced exit(1)", out)

    def test_gil_held_backstop(self):
        rc, elapsed, out = self._run("gil", self.CAP + lifecycle.BACKSTOP_EXTRA_S + 10)
        self.assertEqual(rc, 1, out)
        self.assertLess(elapsed, self.CAP + lifecycle.BACKSTOP_EXTRA_S + 3)
        self.assertIn("Timeout (", out)   # faulthandler.dump_traceback_later's header

    def test_normal_shutdown_exits_clean(self):
        rc, elapsed, out = self._run("normal", self.CAP + 10)
        self.assertIn(rc, _CLEAN_EXIT_CODES, out)   # uvicorn re-raises the signal it caught
        self.assertLess(elapsed, min(self.CAP, self.DRAIN))
        self.assertNotIn("shutdown cap hit", out)
        self.assertNotIn("timeout graceful shutdown exceeded", out)
        self.assertIn("drain cap armed", out)
        self.assertIn(f"{_STOP_NAME} to lifespan shutdown", out)
        self.assertIn("lifespan shutdown done in", out)
        self.assertIn("Finished server process", out)

    def test_connections_that_never_close_end_at_the_drain_cap(self):
        """A request that never answers and an SSE stream the client never closes: uvicorn cancels
        both at the drain cap (the stream ends at once on uvicorn 0.43 / 0.44, see
        _requests_cancelled_at_drain_cap), the lifespan shutdown then runs normally, and the process
        exits cleanly within the drain cap plus a small margin."""
        rc, elapsed, out = self._run("hang_requests", self.DRAIN + self.CAP + 10,
                                     requests=("/hang", "/stream"))
        self.assertIn(rc, _CLEAN_EXIT_CODES, out)
        self.assertGreaterEqual(elapsed, at_least(self.DRAIN))
        self.assertLess(elapsed, self.DRAIN + 1.5)
        self.assertIn("Waiting for connections to close", out)
        n = _requests_cancelled_at_drain_cap(n_requests=1, n_streams=1)
        self.assertIn(f"Cancel {n} running task(s), timeout graceful shutdown exceeded", out)
        self.assertIn(f"{_STOP_NAME} to lifespan shutdown", out)
        self.assertIn("lifecycle drain: all", out)
        self.assertIn("lifespan shutdown done in", out)
        self.assertIn("Finished server process", out)
        self.assertNotIn("shutdown cap hit", out)

    def test_event_loop_blocked_before_uvicorn_notices_the_signal(self):
        """The loop thread is stuck in a synchronous call when the stop signal arrives, so uvicorn
        never starts its shutdown and its own timeout can never fire: the hard drain watchdog ends
        it. On Windows Ctrl+Break does not interrupt that call, so the Python signal handler never
        runs and only the console control handler can arm the watchdog: this is the test that
        fails there without it (the child outlives the budget)."""
        rc, elapsed, out = self._run("drain_loop_blocked",
                                     self.DRAIN + lifecycle.DRAIN_GRACE_S + 10, requests=("/block",))
        self.assertEqual(rc, 1, out)
        self.assertGreaterEqual(elapsed, at_least(self.DRAIN + lifecycle.DRAIN_GRACE_S))
        self.assertLess(elapsed, self.DRAIN + lifecycle.DRAIN_GRACE_S + 2)
        self.assertIn("shutdown cap hit", out)
        self.assertIn(f"after {_STOP_NAME}", out)
        self.assertIn("has not yet started the shutdown", out)
        self.assertIn("forced exit(1)", out)
        self.assertIn("Thread 0x", out)
        self.assertNotIn("Waiting for application shutdown", out)

    def test_event_loop_blocked_while_waiting_for_connections(self):
        rc, elapsed, out = self._run("drain_loop_blocked_late",
                                     self.DRAIN + lifecycle.DRAIN_GRACE_S + 10,
                                     requests=("/block_late",))
        self.assertEqual(rc, 1, out)
        self.assertGreaterEqual(elapsed, at_least(self.DRAIN + lifecycle.DRAIN_GRACE_S))
        self.assertLess(elapsed, self.DRAIN + lifecycle.DRAIN_GRACE_S + 2)
        self.assertIn("Waiting for connections to close", out)
        self.assertIn("shutdown cap hit", out)
        self.assertIn("uvicorn: waiting for open connections and running requests", out)
        self.assertIn("forced exit(1)", out)
        self.assertNotIn("Waiting for application shutdown", out)


if __name__ == "__main__":
    unittest.main()
