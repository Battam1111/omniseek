"""The walled-source "needs a VNC re-login" alert is sent on a background thread (2026-10-06).

``BaseCDPAdapter._alert_auth_fail`` runs inside an omniseek_search. The push behind it can take up to
~28.5s in the worst case (three 8s tries plus the 1.5s and 3s pauses), and it used to run inline, so
one logged-out source stalled the whole search. The cooldown check and its bookkeeping stay
synchronous; only the push moves to a daemon thread. Held here:
  - with a push that blocks for 5s, _alert_auth_fail returns within 0.5s and the push still runs once;
  - inside the cooldown a second call starts no thread and pushes nothing;
  - a push that raises is swallowed on the thread (best effort, as before).
Every push here is a stand-in for ``infra_jobs._alert``; nothing can leave the machine.
"""
from __future__ import annotations

import threading
import time
import types
import unittest
from unittest.mock import patch

from omniseek.core import infra_jobs
from omniseek.core.sources.walled import _base

_REAL_THREAD = threading.Thread


class AuthAlertBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.src = types.SimpleNamespace(name="auth_alert_test_source")
        self.started: list = []
        started = self.started

        class _CountingThread(_REAL_THREAD):
            def start(self_inner):
                if (self_inner.name or "").startswith("auth-alert-"):
                    started.append(self_inner)
                return super().start()

        for p in (patch.dict(_base._AUTH_BARK_LAST, {}, clear=True),
                  patch.object(threading, "Thread", _CountingThread)):
            p.start()
            self.addCleanup(p.stop)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls: list = []
        self.addCleanup(self.release.set)

    def _slow_alert(self, title, body="", **kw):
        self.calls.append((title, body, kw))
        self.entered.set()
        self.release.wait(5)            # a 5s push; the test releases it early once it has measured

    def test_returns_at_once_and_the_push_runs_once_on_a_daemon_thread(self):
        with patch.object(infra_jobs, "_alert", self._slow_alert):
            t0 = time.monotonic()
            _base.BaseCDPAdapter._alert_auth_fail(self.src)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)
            self.assertTrue(self.entered.wait(5), "the push never ran")
            self.assertEqual(len(self.started), 1)
            self.assertTrue(self.started[0].daemon)
            self.release.set()
            self.started[0].join(5)
        self.assertFalse(self.started[0].is_alive())
        self.assertEqual(len(self.calls), 1)
        title, body, kw = self.calls[0]
        self.assertEqual(title, "auth_alert_test_source 登录态失效")
        self.assertIn("VNC", body)
        self.assertEqual(kw, {"group": "OmniSeek-Health"})

    def test_inside_the_cooldown_no_thread_starts(self):
        with patch.object(infra_jobs, "_alert", self._slow_alert):
            _base.BaseCDPAdapter._alert_auth_fail(self.src)
            self.assertTrue(self.entered.wait(5))
            t0 = time.monotonic()
            _base.BaseCDPAdapter._alert_auth_fail(self.src)
            self.assertLess(time.monotonic() - t0, 0.5)
            self.release.set()
            for t in self.started:
                t.join(5)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("auth_alert_test_source", _base._AUTH_BARK_LAST)

    def test_a_raising_push_is_swallowed_on_the_thread(self):
        hooked: list = []

        def _boom(*a, **k):
            raise RuntimeError("push on fire")

        with patch.object(infra_jobs, "_alert", _boom), \
                patch.object(threading, "excepthook", lambda args: hooked.append(args)):
            _base.BaseCDPAdapter._alert_auth_fail(self.src)
            for t in self.started:
                t.join(5)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(hooked, [])


if __name__ == "__main__":
    unittest.main()
