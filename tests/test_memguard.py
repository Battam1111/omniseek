"""eye.memguard: the footprint ceiling that restarts the HTTP service the way a deploy does. All
readings, the idle signal, the restart and the clock are injected; nothing here signals a process."""

import asyncio
import sys
import threading
import unittest

from omniseek.core import memguard


class _Rig:
    def __init__(self, readings, busy=0, uptime=10_000.0):
        self.readings = list(readings)
        self.busy = busy
        self.released = 0
        self.restarts = 0
        self.now = 0.0
        self.guard = memguard.MemGuard(
            1000, 2000, read=self._read, busy=lambda: self.busy, release=self._release,
            restart=self._restart, clock=lambda: self.now, min_uptime_s=600.0)
        self.now = uptime

    def _read(self):
        return self.readings.pop(0)

    def _release(self):
        self.released += 1

    def _restart(self):
        self.restarts += 1

    def run(self, n):
        return [self.guard.check() for _ in range(n)]


class MemGuardDecisionTests(unittest.TestCase):
    def test_below_soft_never_acts(self):
        rig = _Rig([500, 999, 800, 999])
        self.assertEqual(rig.run(4), [None] * 4)
        self.assertEqual((rig.released, rig.restarts), (0, 0))

    def test_short_spike_over_soft_is_ridden_out(self):
        rig = _Rig([1500, 1500, 900, 1500, 1500, 900])
        rig.run(6)
        self.assertEqual((rig.released, rig.restarts), (0, 0))

    def test_soft_releases_first_then_restarts_when_idle(self):
        rig = _Rig([1500] * 6, busy=0)
        out = rig.run(6)
        self.assertEqual(rig.released, 1)                 # after 3 consecutive samples
        self.assertEqual(out[:5], [None] * 5)
        self.assertEqual(out[5], "soft")                  # 3 more samples still over -> idle -> fire
        self.assertEqual(rig.restarts, 1)

    def test_release_that_works_cancels_restart(self):
        rig = _Rig([1500] * 3 + [800] * 5)
        rig.run(8)
        self.assertEqual((rig.released, rig.restarts), (1, 0))

    def test_soft_restart_waits_for_idle(self):
        rig = _Rig([1500] * 10, busy=2)
        self.assertEqual(rig.run(8), [None] * 8)
        self.assertTrue(rig.guard.pending)
        rig.busy = 0
        self.assertEqual(rig.guard.check(), "soft")
        self.assertEqual(rig.restarts, 1)

    def test_pending_cleared_if_footprint_falls(self):
        rig = _Rig([1500] * 6 + [900, 1500], busy=1)
        rig.run(7)
        self.assertFalse(rig.guard.pending)
        rig.busy = 0
        rig.run(1)
        self.assertEqual(rig.restarts, 0)

    def test_hard_restarts_even_when_busy(self):
        rig = _Rig([2500, 2500], busy=5)
        self.assertEqual(rig.run(2), [None, "hard"])
        self.assertEqual((rig.released, rig.restarts), (0, 1))

    def test_fires_once(self):
        rig = _Rig([2500] * 5)
        rig.run(5)
        self.assertEqual(rig.restarts, 1)

    def test_young_process_does_not_restart_loop(self):
        rig = _Rig([2500] * 4 + [1500] * 6, uptime=100.0)
        rig.run(10)
        self.assertEqual(rig.restarts, 0)

    def test_zero_thresholds_disable(self):
        g = memguard.MemGuard(0, 0, read=lambda: 99999.0, busy=lambda: 0, release=lambda: None,
                              restart=self.fail, clock=lambda: 1e9)
        for _ in range(10):
            self.assertIsNone(g.check())

    def test_unreadable_footprint_is_a_noop(self):
        g = memguard.MemGuard(1, 1, read=lambda: None, busy=lambda: 0, release=self.fail,
                              restart=self.fail, clock=lambda: 1e9)
        for _ in range(5):
            self.assertIsNone(g.check())

    def test_run_loop_stops_on_event(self):
        stop = threading.Event()
        stop.set()
        g = memguard.MemGuard(1000, 2000, read=lambda: 10.0, busy=lambda: 0, release=lambda: None,
                              restart=self.fail, clock=lambda: 1e9)
        g.run(stop)       # returns immediately, no restart

    def test_exit_watchdog_forces_exit_after_grace(self):
        codes = []
        fired = threading.Event()

        def fake_exit(code):
            codes.append(code)
            fired.set()

        from unittest.mock import patch
        raw = []
        with self.assertLogs("omniseek.core.memguard", level="WARNING") as cm, \
                patch.object(memguard.os, "write", side_effect=lambda fd, b: raw.append((fd, b)) or len(b)):
            t = memguard._arm_exit_watchdog(0.05, exit_fn=fake_exit)
            self.assertTrue(t.daemon)
            self.assertTrue(fired.wait(5))
            t.join(5)
        self.assertEqual(codes, [1])
        self.assertIn("forcing exit", "\n".join(cm.output))
        self.assertEqual(raw, [(2, b"memguard: forced exit(1) 0s after SIGTERM\n")])

    def test_restart_self_arms_watchdog_before_sigterm(self):
        from unittest.mock import patch
        order = []
        with patch.object(memguard, "_arm_exit_watchdog", side_effect=lambda g: order.append(("arm", g))), \
                patch.object(memguard.os, "kill", side_effect=lambda pid, sig: order.append(("kill", sig))):
            memguard._restart_self()
        self.assertEqual(order, [("arm", memguard.EXIT_GRACE_S), ("kill", memguard.signal.SIGTERM)])

    def test_env_thresholds(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"OMNISEEK_MEM_SOFT_MB": "0", "OMNISEEK_MEM_HARD_MB": "0"}):
            self.assertIsNone(memguard.start())
        with patch.dict("os.environ", {"X": "1"}):
            self.assertEqual(memguard._env_mb("OMNISEEK_MEM_SOFT_MB_UNSET", 7), 7)
        with patch.dict("os.environ", {"OMNISEEK_MEM_SOFT_MB": "abc"}):
            self.assertEqual(memguard._env_mb("OMNISEEK_MEM_SOFT_MB", 7), 7)


    def test_defaults_follow_the_memory_budget(self):
        # eye-mem-3: peak budget 4.5 GB (4608 MB); soft = budget + 0.5 GB, hard = soft + 1.5 GB.
        self.assertEqual(memguard.SOFT_MB_DEFAULT, 4608 + 512)
        self.assertEqual(memguard.HARD_MB_DEFAULT, memguard.SOFT_MB_DEFAULT + 1536)
        from pathlib import Path
        services = Path(__file__).resolve().parents[1] / "scripts" / "services.py"
        if services.exists():
            text = services.read_text(encoding="utf-8")
            self.assertNotIn("OMNISEEK_MEM_SOFT_MB", text)
            self.assertNotIn("OMNISEEK_MEM_HARD_MB", text)

@unittest.skipUnless(sys.platform == "darwin", "phys_footprint is a macOS reading")
class FootprintReadingTests(unittest.TestCase):
    def test_reads_this_process_and_tracks_an_allocation(self):
        before = memguard.footprint_mb()
        self.assertIsNotNone(before)
        self.assertGreater(before, 1)
        blob = bytearray(200 * 1024 * 1024)
        for i in range(0, len(blob), 4096):
            blob[i] = 1
        after = memguard.footprint_mb()
        self.assertGreater(after - before, 150)
        del blob


class InflightMiddlewareTests(unittest.TestCase):
    def _run(self, app, scope):
        seen = []
        mw = memguard.InflightMiddleware(app)

        async def receive():
            return {"type": "http.request"}

        async def send(msg):
            seen.append((msg["type"], memguard.inflight()))

        asyncio.run(mw(scope, receive, send))
        return seen

    def test_counts_until_last_body_chunk(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"a", "more_body": True})
            await send({"type": "http.response.body", "body": b"b"})

        seen = self._run(app, {"type": "http"})
        self.assertEqual([n for _t, n in seen], [1, 1, 1])  # recorded before the counter drops
        self.assertEqual(memguard.inflight(), 0)

    def test_exception_still_decrements(self):
        async def app(scope, receive, send):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self._run(app, {"type": "http"})
        self.assertEqual(memguard.inflight(), 0)

    def test_lifespan_not_counted(self):
        async def app(scope, receive, send):
            await send({"type": "lifespan.startup.complete"})

        seen = self._run(app, {"type": "lifespan"})
        self.assertEqual(seen, [("lifespan.startup.complete", 0)])


if __name__ == "__main__":
    unittest.main()
