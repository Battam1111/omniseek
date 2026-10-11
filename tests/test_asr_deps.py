"""Transcription dependency errors and the closed-loop portal leak (stranger report item 4).

1. A missing torch must yield the install command for THIS platform (no CUDA wheel index on a Mac),
   say how much disk the first call needs, and fire before any audio is fetched.
2. portal.submit on a loop that has closed (the stdio session ended while the background shadow
   probe still ran) must not leak "coroutine ... was never awaited" RuntimeWarnings.
"""
import asyncio
import gc
import logging
import re
import threading
import unittest
import warnings
from unittest import mock

from omniseek.core import asr, fetcher, portal

_DASH = re.compile(r"[\u2013\u2014]| -- ")


class TorchHintTest(unittest.TestCase):
    def test_macos_gets_plain_pip_no_cuda_index(self):
        hint = asr._torch_install_hint("Darwin", cuda=False)
        self.assertIn("pip install torch torchaudio", hint)
        self.assertNotIn("download.pytorch.org/whl/cu", hint)
        # even if something CUDA-looking were on PATH, a Mac never gets a CUDA index
        self.assertNotIn("whl/cu", asr._torch_install_hint("Darwin", cuda=True))

    def test_linux_without_gpu_gets_plain_pip(self):
        hint = asr._torch_install_hint("Linux", cuda=False)
        self.assertTrue(hint.startswith("pip install torch torchaudio"))
        self.assertNotIn("whl/cu", hint)

    def test_cuda_gets_cuda_index(self):
        hint = asr._torch_install_hint("Linux", cuda=True)
        self.assertIn("pip install torch torchaudio", hint)
        self.assertIn("download.pytorch.org/whl/cu", hint)
        self.assertIn("download.pytorch.org/whl/cu", asr._torch_install_hint("Windows", cuda=True))
        self.assertEqual(asr._torch_install_hint("Windows", cuda=False), "pip install torch torchaudio")

    def test_message_names_everything_and_disk(self):
        msg = asr._asr_install_message(["funasr", "imageio_ffmpeg", "torch", "torchaudio"],
                                       "Darwin", False)
        self.assertIn("pip install 'omniseek[asr]'", msg)
        self.assertIn("pip install torch torchaudio", msg)
        self.assertIn("0.9 GB", msg)
        self.assertNotIn("whl/cu", msg)
        only_torch = asr._asr_install_message(["torch", "torchaudio"], "Darwin", False)
        self.assertNotIn("omniseek[asr]", only_torch)
        for system in ("Darwin", "Linux", "Windows"):
            for cuda in (False, True):
                m = asr._asr_install_message(["torch"], system, cuda)
                self.assertIsNone(_DASH.search(m), m)

    def test_transcribe_fails_before_fetching_audio(self):
        with mock.patch.object(asr, "_missing_asr_deps", return_value=["torch", "torchaudio"]), \
                mock.patch.object(asr, "_decode_to_wav") as dec, \
                mock.patch.object(asr, "_ytdlp_download") as ytdl, \
                mock.patch.object(asr.cache, "get", return_value=None):
            rec = asr.transcribe_url("https://example.com/episode.mp3")
        dec.assert_not_called()
        ytdl.assert_not_called()
        self.assertEqual(rec["transcript"], "")
        self.assertIn("pip install torch torchaudio", rec["error"])
        self.assertIn("0.9 GB", rec["error"])


class FunasrStdoutTest(unittest.TestCase):
    """Under stdio, stdout is the JSON-RPC channel: funasr's print()s must not reach it."""

    def test_model_load_and_generate_print_to_stderr_not_stdout(self):
        import io
        import sys
        import types

        class _AutoModel:
            def __init__(self, **kw):
                print("funasr version: 1.4.16.")

            def generate(self, **kw):
                print("rtf_avg: 0.03")
                return [{"text": "hello"}]

        def fake_require(name, extra):
            print("Notice: ffmpeg is not installed. torchaudio is used to load audio")
            return types.SimpleNamespace(AutoModel=_AutoModel)

        out, err = io.StringIO(), io.StringIO()
        saved = asr._model
        asr._model = None
        try:
            with mock.patch.object(asr._optdep, "require", fake_require), \
                    mock.patch.object(asr, "_model_cached", return_value=True), \
                    mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
                text = asr._transcribe_wav("x.wav", None)
        finally:
            asr._model = saved
        self.assertEqual(text, "hello")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("funasr version", err.getvalue())
        self.assertIn("rtf_avg", err.getvalue())


def _bound_then_closed_loop():
    loop = asyncio.new_event_loop()

    def run():
        loop.run_until_complete(asyncio.sleep(0))
        portal.bind(loop)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    loop.close()
    return loop


async def _never_runs():
    return {}, {}


class PortalClosedLoopTest(unittest.TestCase):
    def setUp(self):
        self._saved = (portal._loop, portal._loop_thread_ident)

    def tearDown(self):
        portal._loop, portal._loop_thread_ident = self._saved

    def _assert_no_unawaited(self, fn):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fn()
            gc.collect()
        leaked = [str(w.message) for w in caught
                  if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)]
        self.assertEqual(leaked, [])

    def test_submit_on_closed_loop_refuses_without_leak(self):
        _bound_then_closed_loop()

        def go():
            with self.assertRaises(portal.PortalClosed):
                portal.submit(_never_runs(), timeout=1)
        self._assert_no_unawaited(go)

    def test_loop_closing_during_handoff_closes_both_coroutines(self):
        loop = asyncio.new_event_loop()
        portal._loop, portal._loop_thread_ident = loop, -1

        def closing_race(coro, lp):
            lp.close()
            raise RuntimeError("Event loop is closed")

        def go():
            with mock.patch.object(portal.asyncio, "run_coroutine_threadsafe", closing_race):
                with self.assertRaises(portal.PortalClosed):
                    portal.submit(_never_runs(), timeout=1)
        self._assert_no_unawaited(go)

    def test_shadow_probe_logs_shutdown_race_at_debug(self):
        with self.assertLogs(fetcher.logger, level=logging.DEBUG) as cm:
            fetcher._shadow_log_failure("async fan-out shadow probe",
                                        portal.PortalClosed("portal event loop is closed"))
        self.assertTrue(all(r.levelno == logging.DEBUG for r in cm.records))
        with self.assertLogs(fetcher.logger, level=logging.WARNING):
            fetcher._shadow_log_failure("async fan-out shadow probe", RuntimeError("boom"))


if __name__ == "__main__":
    unittest.main()
