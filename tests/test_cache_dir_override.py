"""OMNISEEK_CACHE_DIR moves OmniSeek's disk cache (2026-10-10).

The deploy smoke runs beside the live service as the same user; it sets this variable so its checks
read and write a throwaway directory instead of the service's cache. cache.py reads the variable
once at import, so each case imports it in a fresh interpreter.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = ("from omniseek.core import cache; from platformdirs import user_cache_dir; "
          "import pathlib; print(cache.CACHE_DIR); "
          "print(pathlib.Path(user_cache_dir('omniseek', appauthor=False)) / 'omniseek_cache')")


_NAMES = ("OMNISEEK_CACHE_DIR", "OMNISEEK_CACHE_DIR")


def _probe(extra_env: dict) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _NAMES}
    env.update(extra_env)
    env["PYTHONPATH"] = str(ROOT / "src")
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True,
                         timeout=120, check=True).stdout.splitlines()
    return out[0], out[1]


class CacheDirOverride(unittest.TestCase):
    def test_unset_keeps_the_user_cache_dir(self):
        got, default = _probe({})
        self.assertEqual(got, default)

    def test_empty_value_keeps_the_user_cache_dir(self):
        got, default = _probe({"OMNISEEK_CACHE_DIR": ""})
        self.assertEqual(got, default)

    def test_set_moves_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, default = _probe({"OMNISEEK_CACHE_DIR": tmp})
            self.assertEqual(got, tmp)
            self.assertNotEqual(got, default)
            env = {k: v for k, v in os.environ.items() if k not in _NAMES}
            env.update({"OMNISEEK_CACHE_DIR": tmp, "PYTHONPATH": str(ROOT / "src")})
            subprocess.run([sys.executable, "-c",
                            "from omniseek.core import cache; cache.set('k', 1, ttl=60); "
                            "assert cache.get('k') == 1"],
                           env=env, check=True, timeout=120)
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)


@unittest.skipUnless(importlib.util.find_spec("omniseek._legacy_env") is not None,
                     "transition alias layer absent (the public build ships no legacy names)")
class LegacyCacheDirName(unittest.TestCase):
    def test_legacy_name_still_moves_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            got, _ = _probe({"OMNISEEK_CACHE_DIR": tmp})
            self.assertEqual(got, tmp)

    def test_new_name_wins_over_the_legacy_one(self):
        with tempfile.TemporaryDirectory() as new, tempfile.TemporaryDirectory() as old:
            got, _ = _probe({"OMNISEEK_CACHE_DIR": new, "OMNISEEK_CACHE_DIR": old})
            self.assertEqual(got, new)


if __name__ == "__main__":
    unittest.main()
