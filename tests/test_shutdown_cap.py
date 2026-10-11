"""eye.lifecycle shutdown cap: a lifespan shutdown that does not finish (a background loop that ignores
its stop Event, FastMCP's session manager waiting on a worker thread that never returns, the event
loop blocked by a synchronous call, native code holding the GIL) ends within the cap, leaves one
WARNING naming what is still running, and exits non-zero. A normal shutdown is untouched.

The in-process tests inject the exit function; the subprocess tests run a real uvicorn server on an
ephemeral port, stop it the way a user stops it on that platform and time the exit: SIGTERM on
macOS / Linux, Ctrl+Break on Windows (there send_signal(SIGTERM) is TerminateProcess, a hard kill
that runs no shutdown code at all, so it cannot test the cap). Only OMNISEEK_CACHE_DIR is set (a
temp dir)."""

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from omniseek.core import lifecycle

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


_CHILD = r'''
import asyncio, contextlib, logging, os, sys, threading, time
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
import uvicorn
from starlette.applications import Starlette
from omniseek.core import lifecycle

MODE = sys.argv[1]
CAP = float(sys.argv[2])

if MODE == "normal":
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

app = Starlette(lifespan=lifecycle.compose_lifespan(fake_fastmcp, cap_s=CAP, drain_timeout=0.3))


class CappedServer(uvicorn.Server):   # the same three lines as serve_http._CappedServer
    async def startup(self, sockets=None):
        lifecycle.cap_lifespan_shutdown(self.lifespan, cap_s=CAP)
        if MODE == "loop_blocked_early":
            # a callback that blocks the loop thread is already queued when uvicorn sends
            # lifespan.shutdown, so our lifespan code never gets the loop back (the 10-10 / 10-11 shape)
            capped = self.lifespan.shutdown
            async def shutdown_behind_a_blocker():
                asyncio.get_running_loop().call_soon(time.sleep, 3600)
                await capped()
            self.lifespan.shutdown = shutdown_behind_a_blocker
        await super().startup(sockets=sockets)


CappedServer(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="info")).run()
'''

# How a user stops the server gracefully, per platform. uvicorn's HANDLED_SIGNALS are SIGINT and
# SIGTERM, plus SIGBREAK on win32. On Windows a child started in its own process group cannot get
# Ctrl+C (Windows disables it for a new group), but Ctrl+Break reaches it as SIGBREAK. After a
# graceful stop uvicorn re-raises the signal it caught with the default action: -SIGTERM on POSIX,
# exit code 3 on Windows (the C runtime's default for SIGBREAK).
if sys.platform == "win32":
    _STOP_SIGNAL = signal.CTRL_BREAK_EVENT
    _POPEN_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
    _CLEAN_EXIT_CODES = (0, 3)
else:
    _STOP_SIGNAL = signal.SIGTERM
    _POPEN_FLAGS = 0
    _CLEAN_EXIT_CODES = (0, -signal.SIGTERM)


class SubprocessCapTests(unittest.TestCase):
    CAP = 2.0

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="x.", dir=os.environ.get("TMPDIR"))
        cls._script = os.path.join(cls._tmp, "child.py")
        with open(cls._script, "w") as f:
            f.write(_CHILD)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _run(self, mode, budget):
        env = dict(os.environ)
        env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
        env["OMNISEEK_CACHE_DIR"] = self._tmp
        p = subprocess.Popen([sys.executable, self._script, mode, str(self.CAP)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                             creationflags=_POPEN_FLAGS)
        lines = []
        ready = threading.Event()

        def pump():
            for line in p.stdout:
                lines.append(line)
                if "Application startup complete" in line:
                    ready.set()
        threading.Thread(target=pump, daemon=True).start()
        try:
            self.assertTrue(ready.wait(60), "".join(lines))
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
        self.assertLess(elapsed, self.CAP)
        self.assertNotIn("shutdown cap hit", out)
        self.assertIn("lifespan shutdown done in", out)
        self.assertIn("Finished server process", out)


if __name__ == "__main__":
    unittest.main()
