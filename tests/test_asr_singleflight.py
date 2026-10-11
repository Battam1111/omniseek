"""ASR single flight per cache key (incident 2026-10-11: the same 87-minute omniseek_transcribe re-issued
after each omniseek_gather warming started a fresh run every time, until memguard restarted OmniSeek with
three transcriptions in flight). Every stage that does real work is a counting fake, so the tests
assert how many runs actually STARTED, not only what the calls returned. No network, no model."""
import os
import threading
import time
import unittest
from unittest import mock

from omniseek.core import asr

URL = "https://www.bilibili.com/video/BV1pu411o7BE"


class _Counting:
    """Fakes for fetch / decode / generate that count calls; the generate blocks on a gate."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = {"fetch": 0, "decode": 0, "wav": 0, "segments": 0}
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.fail = None

    def _bump(self, k):
        with self.lock:
            self.calls[k] += 1

    def fetch(self, url):
        self._bump("fetch")
        return "/tmp/fake-audio.m4a", {"source": "bilibili", "title": "t"}

    def decode(self, src, start_s, dur_s):
        self._bump("decode")
        return "/tmp/fake.wav"

    def wav(self, wav, language):
        self._bump("wav")
        self.entered.set()
        if not self.gate.wait(10):
            raise AssertionError("test gate never opened")
        if self.fail is not None:
            raise self.fail
        return "你好世界"

    def segments(self, wav, language):
        self._bump("segments")
        return [{"start": 0.0, "end": 1.0, "text": "你好世界"}]


class SingleFlight(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.f = _Counting()
        patches = [
            mock.patch.object(asr, "_missing_asr_deps", return_value=[]),
            mock.patch.object(asr, "_bilibili_audio", side_effect=self.f.fetch),
            mock.patch.object(asr, "_decode_to_wav", side_effect=self.f.decode),
            mock.patch.object(asr, "_transcribe_wav", side_effect=self.f.wav),
            mock.patch.object(asr, "_transcribe_segments", side_effect=self.f.segments),
            mock.patch.object(asr, "_rm"),
            mock.patch.object(asr, "_remember_transcript"),
            mock.patch.object(asr.os.path, "getsize", return_value=asr._SR * 2 * 5220),
            mock.patch.object(asr.cache, "get", side_effect=lambda k: self.store.get(k)),
            mock.patch.object(asr.cache, "set",
                              side_effect=lambda k, v, ttl=None: self.store.__setitem__(k, v)),
            mock.patch.object(asr._netguard, "security_block_reason", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(asr._inflight, {}, "a previous test leaked an in-flight job")
        self.runs0 = asr.runs_started()

    def tearDown(self):
        self.f.gate.set()
        asr._inflight.clear()

    def _call(self, out, i, **kw):
        out[i] = asr.transcribe_url(URL, language="zh", segments=True, **kw)

    def _spawn(self, n, out, **kw):
        ts = [threading.Thread(target=self._call, args=(out, i), kwargs=kw) for i in range(n)]
        for t in ts:
            t.start()
        return ts

    def test_repeat_calls_while_running_start_exactly_one_run(self):
        out = {}
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_FOLLOW_WAIT_S": "10"}):
            leader = self._spawn(1, out)
            self.assertTrue(self.f.entered.wait(5))
            followers = [threading.Thread(target=self._call, args=(out, i)) for i in (1, 2, 3)]
            for t in followers:
                t.start()
            time.sleep(0.3)
            self.f.gate.set()
            for t in leader + followers:
                t.join(10)
        self.assertEqual(self.f.calls, {"fetch": 1, "decode": 1, "wav": 1, "segments": 1})
        self.assertEqual(asr.runs_started() - self.runs0, 1)
        self.assertEqual(out[0]["transcript"], "你好世界")
        for i in (1, 2, 3):
            self.assertTrue(out[i].get("joined"), out[i])
            self.assertEqual(out[i]["transcript"], "你好世界")
            self.assertEqual(out[i]["segments"], out[0]["segments"])
        self.assertEqual(asr._inflight, {})
        again = asr.transcribe_url(URL, language="zh", segments=True)
        self.assertTrue(again["cached"])
        self.assertEqual(self.f.calls["wav"], 1, "a call after the run finished must hit the cache")

    def test_follower_past_its_wait_gets_running_receipt_and_starts_nothing(self):
        out = {}
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_FOLLOW_WAIT_S": "0.05"}):
            leader = self._spawn(1, out)
            self.assertTrue(self.f.entered.wait(5))
            for i in (1, 2, 3):   # the incident's three re-issues, one after another
                self._call(out, i)
        for i in (1, 2, 3):
            r = out[i]
            self.assertEqual(r["status"], "running")
            self.assertTrue(r["already_running"])
            self.assertEqual(r["transcript"], "")
            self.assertNotIn("error", r)
            self.assertEqual(r["phase"], "transcribing")
            self.assertEqual(r["audio_seconds"], 5220)
            self.assertIn("already running", r["note"])
            self.assertIn("will NOT start a second run", r["note"])
        self.assertEqual(self.f.calls["fetch"], 1)
        self.assertEqual(self.f.calls["wav"], 1)
        self.f.gate.set()
        leader[0].join(10)
        self.assertEqual(out[0]["transcript"], "你好世界")
        self.assertTrue(asr.transcribe_url(URL, language="zh", segments=True)["cached"])
        self.assertEqual(asr.runs_started() - self.runs0, 1)

    def test_leader_error_reaches_followers_and_frees_the_key(self):
        self.f.fail = RuntimeError("mps oom")
        out = {}
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_FOLLOW_WAIT_S": "10"}):
            leader = self._spawn(1, out)
            self.assertTrue(self.f.entered.wait(5))
            follower = threading.Thread(target=self._call, args=(out, 1))
            follower.start()
            time.sleep(0.2)
            self.f.gate.set()
            leader[0].join(10)
            follower.join(10)
        self.assertIn("mps oom", out[0]["error"])
        self.assertIn("mps oom", out[1]["error"])
        self.assertEqual(self.f.calls["wav"], 1)
        self.assertEqual(asr._inflight, {})
        self.assertEqual(self.store, {}, "an error must not be cached")
        self.f.fail = None
        retry = asr.transcribe_url(URL, language="zh", segments=True)
        self.assertEqual(retry["transcript"], "你好世界")
        self.assertEqual(self.f.calls["wav"], 2, "after a failed run a new call must run again")

    def test_leader_crash_still_frees_the_key(self):
        with mock.patch.object(asr, "_transcribe_uncached", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                asr.transcribe_url(URL, language="zh", segments=True)
        self.assertEqual(asr._inflight, {})

    def test_distinct_keys_each_run_once(self):
        out = {}
        self.f.gate.set()
        ts = [threading.Thread(target=lambda i=i: out.__setitem__(
            i, asr.transcribe_url(URL, language="zh", start=str(60 * i), duration="60")))
            for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(self.f.calls["wav"], 3)
        self.assertEqual(asr.runs_started() - self.runs0, 3)
        self.assertTrue(all(out[i]["transcript"] == "你好世界" for i in range(3)))


class GatherWarmingHint(unittest.TestCase):
    def test_transcribe_warming_hint_says_already_running_and_no_second_run(self):
        from omniseek import server as srv
        fn = srv.omniseek_gather
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(**kw):
            gate.wait(10)
            return {"transcript": "x"}
        with mock.patch.dict(srv._GATHER_TOOLS, {"omniseek_transcribe": slow}):
            out = fn(calls=[{"tool": "omniseek_transcribe", "args": {"url": URL}}], wait_s=1)
        r = out["results"][0]
        self.assertEqual(r["status"], "warming")
        self.assertIn("already running", r["hint"])
        self.assertIn("will NOT start a second run", r["hint"])
        self.assertNotIn("staleness", r["hint"])
        self.assertIn("staleness", srv._GATHER_WARMING_HINT_DEFAULT)


if __name__ == "__main__":
    unittest.main()
