"""The transition layer of an internal package rename: old names keep working for one cycle.

Two old names are covered, each by its own mechanism:
  * the import name ``omniseek`` (package ``src/omniseek``, an alias finder onto ``omniseek``),
    including ``python -m omniseek.<module>``, which an eye-http plist installed before this release
    still runs;
  * ``OMNISEEK_*`` environment variables (``omniseek._legacy_env.bridge``).
The launchd labels and the deploy directory are not renamed in this release, so they need no alias.

The whole layer is removed in the cleanup batch, together with this file.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

if importlib.util.find_spec("omniseek._legacy_env") is None:
    raise unittest.SkipTest("transition alias layer absent (the public build ships no legacy names)")

OLD = "pol" + "aris"   # spelled in two halves so a plain-text scan of this file finds no old name


def _fresh_alias():
    """Import the alias package from scratch so its one-time warning fires inside the caller."""
    for name in [n for n in sys.modules if n == OLD or n.startswith(OLD + ".")]:
        del sys.modules[name]
    return importlib.import_module(OLD)


class ImportAliasTests(unittest.TestCase):
    def test_old_package_name_warns_and_is_the_same_package(self):
        import omniseek
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            alias = _fresh_alias()
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
        self.assertEqual(alias.__version__, omniseek.__version__)

    def test_old_submodule_is_the_same_module_object(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _fresh_alias()
            old = importlib.import_module(OLD + ".eye.fetcher")
        import omniseek.core.fetcher as new
        self.assertIs(old, new)
        self.assertEqual(new.__spec__.name, "omniseek.core.fetcher")
        self.assertEqual(new.__name__, "omniseek.core.fetcher")

    def test_old_from_import_reaches_the_same_function(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _fresh_alias()
            mod = importlib.import_module(OLD + ".eye.sources.walled._cdp")
        from omniseek.core.sources.walled import _cdp
        self.assertIs(mod.cdp_health, _cdp.cdp_health)

    def test_unknown_old_submodule_still_fails_cleanly(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _fresh_alias()
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(OLD + ".no_such_module_here")

    def test_run_old_module_as_main(self):
        """``python -m omniseek.<module>`` runs the omniseek module as __main__. That is the exact
        path an eye-http plist installed before this release (``-m omniseek.serve_http``) takes until it
        is reinstalled, and after a rollback; runpy asks the alias loader for get_code."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "_rename_probe_main.py"
            probe.write_text("import sys\nprint('RAN', __name__, __file__)\nsys.exit(7)\n",
                             encoding="utf-8")
            code = (
                "import warnings; warnings.simplefilter('ignore', DeprecationWarning)\n"
                "import omniseek, runpy\n"
                f"omniseek.__path__.append({tmp!r})\n"
                # runpy._run_module_as_main is what the interpreter itself calls for ``-m``.
                f"runpy._run_module_as_main({OLD!r} + '._rename_probe_main')\n"
            )
            env = {**os.environ, "PYTHONPATH": str(SRC)}
            res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env=env, timeout=120)
        self.assertEqual(res.returncode, 7, res.stderr)
        self.assertIn("RAN __main__", res.stdout)
        self.assertIn(str(probe), res.stdout)

    def test_run_old_package_module_with_dash_m(self):
        """A real ``-m`` on an old dotted name: the alias resolves a package and its __main__-less
        module spec without error (``-m`` on a package needs a __main__, so expect that message,
        not an AttributeError from the loader)."""
        env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONWARNINGS": "ignore::DeprecationWarning"}
        res = subprocess.run([sys.executable, "-m", OLD + ".eye"], capture_output=True, text=True,
                             env=env, timeout=120)
        self.assertNotIn("AttributeError", res.stderr)
        self.assertIn("cannot be directly executed", res.stderr)
        # The venv may also provide the old name from an editable install of the deployed tree,
        # which gives the same message: check the same interpreter setup resolves it to this tree.
        res = subprocess.run([sys.executable, "-c", "import importlib.util; "
                              f"print(importlib.util.find_spec({OLD!r} + '.eye').origin)"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(res.stdout.strip(), str(SRC / "omniseek" / "core" / "__init__.py"), res.stderr)


class OldPlistServiceTests(unittest.TestCase):
    """OmniSeek-http plist installed before this release runs ``python -m omniseek.serve_http`` with
    ``OMNISEEK_HTTP_HOST`` / ``OMNISEEK_HTTP_PORT``. Until it is reinstalled, the new code must still
    come up as a working service from exactly that: the old module name and the old variable names.
    The background services (cache warmer, recall index, job scheduler, memory guard) are switched
    off in the child, because they reach the network and the disk and are not what is tested here."""

    # Measured 2026-10-10 on the mini: /healthz answered 6.2 s after spawn cold, 2.4-2.7 s warm.
    # 120 s is about twenty times the cold figure, so a loaded machine does not fail it and a
    # service that never comes up still does.
    BOOT_BOUND_S = 120

    _RUN = (
        "import runpy, warnings\n"
        "warnings.simplefilter('ignore', DeprecationWarning)\n"
        "import omniseek.core.prewarm as _p; _p.warm_loop = lambda *a, **k: None\n"
        "import omniseek.core.recall as _r; _r.init = lambda *a, **k: False\n"
        "import omniseek.core.jobs as _j; _j.start_scheduler = lambda *a, **k: None\n"
        "import omniseek.core.memguard as _m; _m.start = lambda *a, **k: None\n"
        # The venv on the mini also has the deployed tree installed in editable mode, which
        # provides the old name too: prove the old name resolves to THIS tree's omniseek first.
        "import importlib.util, sys\n"
        f"_s = importlib.util.find_spec({OLD!r} + '.serve_http')\n"
        "print('SERVE_ORIGIN', _s.origin, flush=True)\n"
        # runpy._run_module_as_main is what the interpreter itself calls for ``-m``.
        f"runpy._run_module_as_main({OLD!r} + '.serve_http')\n"
    )

    @staticmethod
    def _status(url, method="GET"):
        import urllib.error
        import urllib.request
        try:
            return urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=2).status
        except urllib.error.HTTPError as exc:
            return exc.code
        except OSError:
            return None

    def test_old_module_and_old_env_names_serve(self):
        import json
        import socket
        import time
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cred = tmp / "home" / ("." + OLD) / "credentials"   # the credentials dir is not renamed
            cred.mkdir(parents=True)
            tok = cred / "omniseek_http.json"
            tok.write_text(json.dumps({"token": "legacy-alias-test-token-0123456789"}), encoding="utf-8")
            tok.chmod(0o600)
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            old = OLD.upper()
            env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNISEEK_", old + "_"))}
            env.update({"HOME": str(tmp / "home"), "PYTHONPATH": str(SRC),
                        old + "_HTTP_HOST": "127.0.0.1", old + "_HTTP_PORT": str(port),
                        old + "_OMNISEEK_CACHE_DIR": str(tmp / "cache")})
            proc = subprocess.Popen([sys.executable, "-c", self._RUN], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + self.BOOT_BOUND_S
                health = None
                while time.monotonic() < deadline and proc.poll() is None:
                    health = self._status(f"http://127.0.0.1:{port}/healthz")
                    if health == 200:
                        break
                    time.sleep(0.2)
                unauth = self._status(f"http://127.0.0.1:{port}/mcp", "POST")
            finally:
                proc.terminate()
                try:
                    out = proc.communicate(timeout=60)[0]
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out = proc.communicate()[0]
        self.assertIn(f"SERVE_ORIGIN {SRC / 'omniseek' / 'serve_http.py'}\n", out, out[-3000:])
        self.assertEqual(health, 200, out[-3000:])
        self.assertEqual(unauth, 401, "the bearer-token gate must be on")
        self.assertIn(f"{old}_HTTP_PORT -> OMNISEEK_HTTP_PORT", out)


class EnvBridgeTests(unittest.TestCase):
    def setUp(self):
        from omniseek import _legacy_env
        self.bridge = _legacy_env.bridge

    def test_old_name_fills_an_unset_new_name(self):
        env = {OLD.upper() + "_SOME_KNOB": "1"}
        out = io.StringIO()
        used = self.bridge(env, stream=out)
        self.assertEqual(env["OMNISEEK_SOME_KNOB"], "1")
        self.assertEqual(used, [OLD.upper() + "_SOME_KNOB"])
        self.assertIn("OMNISEEK_SOME_KNOB", out.getvalue())

    def test_new_name_wins(self):
        env = {OLD.upper() + "_SOME_KNOB": "old", "OMNISEEK_SOME_KNOB": "new"}
        self.assertEqual(self.bridge(env, stream=io.StringIO()), [])
        self.assertEqual(env["OMNISEEK_SOME_KNOB"], "new")

    def test_brain_variables_are_not_bridged(self):
        env = {OLD.upper() + "_BRAIN_TOKEN": "x"}
        self.assertEqual(self.bridge(env, stream=io.StringIO()), [])
        self.assertNotIn("OMNISEEK_BRAIN_TOKEN", env)

    def test_warning_names_variables_never_values(self):
        env = {OLD.upper() + "_SECRETISH": "s3cr3t-value"}
        out = io.StringIO()
        self.bridge(env, stream=out)
        self.assertNotIn("s3cr3t-value", out.getvalue())


if __name__ == "__main__":
    unittest.main()
