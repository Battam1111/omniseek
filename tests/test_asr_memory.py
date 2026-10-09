"""ASR device-memory bound (eye-mem-2): one lock around load + generate + unload, an MPS cache release
after every generate, and an idle unload of the lazy models. Fakes stand in for funasr; no model loads."""
import os
import threading
import time
import unittest
from unittest import mock

from omniseek.core import asr


class _FakeModel:
    def __init__(self, gate=None, log=None):
        self.gate, self.log = gate, log if log is not None else []

    def generate(self, **kw):
        self.log.append(("start", threading.get_ident()))
        if self.gate is not None:
            self.gate.wait(2)
        self.log.append(("end", threading.get_ident()))
        if isinstance(kw.get("input"), list):
            return [{"text": "x"} for _ in kw["input"]]
        return [{"text": "<|zh|>你好", "value": [[0, 500]]}]


class AsrMemoryBound(unittest.TestCase):
    def setUp(self):
        self._saved = (asr._model, asr._vad_model, asr._diar_model, asr._last_use, asr._idle_thread)
        asr._IDLE_STOP.clear()
        self.releases = 0

        def _rel():
            self.releases += 1
        p = mock.patch.object(asr, "_release_device", side_effect=_rel)
        p.start()
        self.addCleanup(p.stop)
        w = mock.patch.object(asr, "_ensure_idle_watcher")
        self.watcher = w.start()
        self.addCleanup(w.stop)

    def tearDown(self):
        (asr._model, asr._vad_model, asr._diar_model, asr._last_use, asr._idle_thread) = self._saved

    def test_release_after_every_generate(self):
        asr._model = _FakeModel()
        self.assertEqual(asr._transcribe_wav("/x.wav", "zh"), "你好")
        self.assertEqual(self.releases, 1)
        self.assertGreater(asr._last_use, 0)
        self.watcher.assert_called()

    def test_release_even_when_generate_raises(self):
        class Boom:
            def generate(self, **kw):
                raise RuntimeError("mps oom")
        asr._model = Boom()
        with self.assertRaises(RuntimeError):
            asr._transcribe_wav("/x.wav", "zh")
        self.assertEqual(self.releases, 1)

    def test_batch_size_constant_is_passed(self):
        seen = {}

        class M(_FakeModel):
            def generate(self, **kw):
                seen.update(kw)
                return [{"text": "a"}]
        asr._model = M()
        with mock.patch.object(asr, "_BATCH_S", 77):
            asr._transcribe_wav("/x.wav", None)
        self.assertEqual(seen["batch_size_s"], 77)

    def test_generates_never_overlap(self):
        log = []
        gate = threading.Event()
        asr._model = _FakeModel(gate=gate, log=log)
        ts = [threading.Thread(target=asr._transcribe_wav, args=("/x.wav", "zh")) for _ in range(3)]
        for t in ts:
            t.start()
        time.sleep(0.2)
        self.assertEqual([e for e, _ in log], ["start"], "a second generate started while one ran")
        gate.set()
        for t in ts:
            t.join(5)
        kinds = [e for e, _ in log]
        self.assertEqual(kinds, ["start", "end"] * 3)
        self.assertEqual(self.releases, 3)

    def test_unload_drops_every_model_and_releases(self):
        asr._model, asr._vad_model, asr._diar_model = _FakeModel(), _FakeModel(), None
        self.assertTrue(asr.unload_models("test"))
        self.assertIsNone(asr._model)
        self.assertIsNone(asr._vad_model)
        self.assertEqual(self.releases, 1)
        self.assertFalse(asr.unload_models("again"))

    def test_unload_waits_for_inflight_generate(self):
        gate = threading.Event()
        log = []
        asr._model = _FakeModel(gate=gate, log=log)
        t = threading.Thread(target=asr._transcribe_wav, args=("/x.wav", "zh"))
        t.start()
        time.sleep(0.1)
        done = []
        u = threading.Thread(target=lambda: done.append(asr.unload_models("race")))
        u.start()
        time.sleep(0.2)
        self.assertEqual(done, [], "unload ran while a generate held the model")
        gate.set()
        t.join(5)
        u.join(5)
        self.assertEqual(done, [True])
        self.assertIsNone(asr._model)


class AsrIdleUnload(unittest.TestCase):
    def setUp(self):
        self._saved = (asr._model, asr._last_use, asr._idle_thread, asr._IDLE_POLL_S)
        asr._IDLE_STOP.clear()
        asr._idle_thread = None
        asr._IDLE_POLL_S = 0.05
        p = mock.patch.object(asr, "_release_device")
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        asr._IDLE_STOP.set()
        t = asr._idle_thread
        if t is not None:
            t.join(2)
        asr._IDLE_STOP.clear()
        (asr._model, asr._last_use, asr._idle_thread, asr._IDLE_POLL_S) = self._saved

    def _wait(self, cond, limit=3.0):
        end = time.monotonic() + limit
        while time.monotonic() < end and not cond():
            time.sleep(0.02)
        return cond()

    def test_models_unloaded_after_idle_and_watcher_exits(self):
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_IDLE_UNLOAD_S": "0.2"}):
            asr._model = _FakeModel()
            asr._transcribe_wav("/x.wav", "zh")
            t = asr._idle_thread
            self.assertIsNotNone(t)
            self.assertIsNotNone(asr._model)
            self.assertTrue(self._wait(lambda: asr._model is None), "model not unloaded when idle")
            self.assertTrue(self._wait(lambda: not t.is_alive()))
            # the next call starts a fresh watcher
            asr._model = _FakeModel()
            asr._transcribe_wav("/x.wav", "zh")
            self.assertIsNot(asr._idle_thread, t)
            self.assertTrue(asr._idle_thread.is_alive())

    def test_recent_use_keeps_model(self):
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_IDLE_UNLOAD_S": "30"}):
            asr._model = _FakeModel()
            asr._transcribe_wav("/x.wav", "zh")
            time.sleep(0.3)
            self.assertIsNotNone(asr._model)

    def test_zero_disables_unload(self):
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_IDLE_UNLOAD_S": "0"}):
            asr._model = _FakeModel()
            asr._transcribe_wav("/x.wav", "zh")
            self.assertIsNone(asr._idle_thread)
            self.assertIsNotNone(asr._model)

    def test_watcher_registered_with_lifecycle_and_stops_on_drain(self):
        from omniseek.core import lifecycle
        with mock.patch.dict(os.environ, {"OMNISEEK_ASR_IDLE_UNLOAD_S": "600"}):
            asr._model = _FakeModel()
            asr._transcribe_wav("/x.wav", "zh")
            names = [n for n, *_ in lifecycle._stops]
            self.assertIn("asr-idle-unload", names)
            t = asr._idle_thread
            asr._IDLE_STOP.set()
            t.join(2)
            self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
