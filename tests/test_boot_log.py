"""A fresh start prints ONE readable INFO line; the detail moves to DEBUG, not away.

Stranger report item 8 (2026-10-10): a lean install's stdio boot opened with 'patchright unavailable'
and 'xhshow/curl_cffi unavailable', then ~100 lines of module names, then 'senses online: none',
and read as a broken install. The boot is replayed here as a lean install sees it (the optional-extra
modules blocked), so the test does not depend on what this venv happens to have installed.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_LEAN_BOOT = textwrap.dedent("""
    import importlib.abc, runpy, sys
    BLOCK = {"patchright", "xhshow", "funasr", "sentence_transformers", "fitz", "pymupdf",
             "rapidocr_onnxruntime", "torch", "torchaudio"}
    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCK:
                raise ModuleNotFoundError(f"No module named '{name}'", name=name)
            return None
    sys.meta_path.insert(0, _Block())
    sys.argv = ["omniseek.server"]
    runpy.run_module("omniseek.server", run_name="__main__")
""")


def _lean_boot(extra_env: dict) -> str:
    with tempfile.TemporaryDirectory() as home:
        env = {k: v for k, v in os.environ.items() if k != "OMNISEEK_LOG_LEVEL"}
        env.update({"HOME": home, "PYTHONPATH": str(ROOT / "src"), "COLUMNS": "400"}, **extra_env)
        proc = subprocess.run([sys.executable, "-c", _LEAN_BOOT], env=env, cwd=str(ROOT),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    return proc.stderr


class BootSummaryLine(unittest.TestCase):
    def test_lists_every_missing_extra_with_its_install_command(self):
        from omniseek import server
        line = server._boot_summary(probe=lambda name: None)
        self.assertNotIn("\n", line)
        self.assertRegex(line, r"\d+ sources available")
        for _sense, _mod, extra in server._SENSE_PROBES:
            self.assertIn(f"pip install 'omniseek[{extra}]'", line)
        self.assertIn("OMNISEEK_LOG_LEVEL=DEBUG", line)
        self.assertIsNone(re.search(r"[\u2013\u2014]| -- ", line))

    def test_all_installed_says_so_and_offers_no_install(self):
        from omniseek import server
        line = server._boot_summary(probe=lambda name: object())
        self.assertIn("Optional capabilities installed", line)
        self.assertNotIn("pip install", line)


class LeanBootLog(unittest.TestCase):
    def test_default_boot_is_one_info_line_without_degraded_notices(self):
        err = _lean_boot({})
        self.assertEqual(len(re.findall(r"\bINFO\b", err)), 1, err)
        self.assertIn("server ready (stdio)", err)
        for noise in ("unavailable", "Registered adapters", "senses online", "WARNING"):
            self.assertNotIn(noise, err)
        self.assertIsNone(re.search(r"[\u2013\u2014]", err), err)

    def test_debug_level_brings_every_detail_back(self):
        err = _lean_boot({"OMNISEEK_LOG_LEVEL": "DEBUG"})
        for detail in ("patchright unavailable", "xhshow/curl_cffi unavailable",
                       "Registered adapters", "senses online: none", "server ready (stdio)"):
            self.assertIn(detail, err)


if __name__ == "__main__":
    unittest.main()
