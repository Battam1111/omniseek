"""A credentials directory this process may not read counts as "no credentials".

The worker sandbox denies ~/.omniseek/credentials by path: every stat or mkdir there raises
PermissionError (EPERM). auth.load() used to let that through, and _openalex builds its User-Agent
from auth.contact_email() at import, so importing OmniSeek died. These tests pin the new contract:
a permission-class failure reads as unconfigured (DEBUG-logged once), a missing directory is created
as before, any other OSError still raises, and a readable directory behaves exactly as it did.

Every case points auth.CREDS_DIR at the test's own temp dir; nothing touches any HOME.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import auth  # noqa: E402

_POSIX = os.name == "posix"


class _CredsDirCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.creds = self.tmp / "parent" / "credentials"
        patcher = mock.patch.object(auth, "CREDS_DIR", self.creds)
        patcher.start()
        self.addCleanup(patcher.stop)
        logged = getattr(auth, "_denied_logged", set())
        saved = set(logged)
        logged.clear()
        self.addCleanup(lambda: (logged.clear(), logged.update(saved)))

    def _lock(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)
        self.addCleanup(os.chmod, path, 0o700)

    def assert_unconfigured(self):
        self.assertIsNone(auth.load("contact"))
        self.assertFalse(auth.is_configured("contact"))
        self.assertEqual(auth.list_configured(), [])
        self.assertEqual(auth.write_template("demo", {"k": "v"}), self.creds / "demo.json.template")
        with mock.patch.dict(os.environ, {"OMNISEEK_CONTACT_EMAIL": ""}):
            self.assertEqual(auth.contact_email(), auth._CONTACT_DEFAULT)


@unittest.skipUnless(_POSIX, "POSIX file modes and shell dispatch only")
class DeniedDirectory(_CredsDirCase):
    """The directory exists (holding a real credential) but this process may not read it."""

    def setUp(self):
        super().setUp()
        self.creds.mkdir(parents=True)
        (self.creds / "contact.json").write_text(json.dumps({"email": "real@example.org"}))
        self._lock(self.creds.parent, 0o000)

    def test_reads_as_unconfigured_and_logs_once(self):
        with self.assertLogs("omniseek.core.auth", level="DEBUG") as logs:
            self.assert_unconfigured()
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertIn("not readable", logs.output[0])
        self.assertIn(str(self.creds), logs.output[0])

    def test_writes_nothing(self):
        auth.write_template("demo", {"k": "v"}, force=True)
        os.chmod(self.creds.parent, 0o700)
        self.assertFalse((self.creds / "demo.json.template").exists())

    def test_ensure_dir_itself_still_raises(self):
        with self.assertRaises(PermissionError):
            auth.ensure_dir()


class DeniedByErrno(_CredsDirCase):
    """The sandbox's exact failure, EPERM, and a bare OSError carrying EACCES, without needing chmod."""

    def test_eperm_from_every_stat_and_mkdir(self):
        err = PermissionError(errno.EPERM, "Operation not permitted", str(self.creds))
        with mock.patch.object(Path, "mkdir", side_effect=err), \
                mock.patch.object(Path, "exists", side_effect=err), \
                self.assertLogs("omniseek.core.auth", level="DEBUG") as logs:
            self.assert_unconfigured()
        self.assertEqual(len(logs.records), 1, logs.output)

    def test_plain_oserror_with_eacces(self):
        class _Plain(OSError):  # an OSError subclass is not remapped to PermissionError by errno
            pass
        err = _Plain(errno.EACCES, "denied")
        self.assertNotIsInstance(err, PermissionError)
        self.assertTrue(auth._is_denied(err))
        self.assertFalse(auth._is_denied(OSError(errno.ENOSPC, "full")))
        with mock.patch.object(Path, "mkdir", side_effect=err), \
                mock.patch.object(Path, "exists", side_effect=err):
            self.assert_unconfigured()


class MissingDirectory(_CredsDirCase):
    def test_created_as_before_and_unconfigured(self):
        self.assertFalse(self.creds.exists())
        self.assertIsNone(auth.load("contact"))
        self.assertTrue(self.creds.is_dir(), "a missing credentials dir is still created on first load")
        self.assertFalse(auth.is_configured("contact"))
        self.assertEqual(auth.list_configured(), [])

    @unittest.skipUnless(_POSIX, "POSIX file modes and shell dispatch only")
    def test_missing_under_unwritable_parent(self):
        self.creds.parent.mkdir()
        self._lock(self.creds.parent, 0o555)
        self.assert_unconfigured()
        self.assertFalse(self.creds.exists())


class ReadableDirectoryUnchanged(_CredsDirCase):
    def test_configured_source_loads(self):
        self.creds.mkdir(parents=True)
        (self.creds / "contact.json").write_text(json.dumps({"email": "real@example.org"}))
        with self.assertNoLogs("omniseek.core.auth", level="DEBUG"):
            self.assertEqual(auth.load("contact"), {"email": "real@example.org"})
            self.assertTrue(auth.is_configured("contact"))
            self.assertEqual(auth.list_configured(), ["contact"])
            self.assertEqual(auth.contact_email(), "real@example.org")
            path = auth.write_template("demo", {"k": "v"})
        self.assertEqual(json.loads(path.read_text()), {"k": "v"})
        auth.write_template("demo", {"k": "other"})
        self.assertEqual(json.loads(path.read_text()), {"k": "v"}, "an existing template is never overwritten")


class OtherErrorsStillRaise(_CredsDirCase):
    def test_parent_is_a_file(self):
        self.creds.parent.write_text("not a directory")
        with self.assertRaises(OSError) as cm:
            auth.load("contact")
        self.assertFalse(auth._is_denied(cm.exception))
        with self.assertRaises(OSError):
            auth.list_configured()


@unittest.skipUnless(_POSIX, "POSIX file modes and shell dispatch only")
class ImportUnderDeniedDir(unittest.TestCase):
    """The original crash: importing _openalex (User-Agent built at import) with a denied creds dir."""

    def test_openalex_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "locked"
            (parent / "credentials").mkdir(parents=True)
            os.chmod(parent, 0o000)
            try:
                code = ("import pathlib, omniseek.core.auth as a; "
                        f"a.CREDS_DIR = pathlib.Path({str(parent / 'credentials')!r}); "
                        "import omniseek.core._openalex as o; print('UA', o.USER_AGENT)")
                env = {k: v for k, v in os.environ.items() if k != "OMNISEEK_CONTACT_EMAIL"}
                env.update({"PYTHONPATH": str(ROOT / "src"), "OMNISEEK_CACHE_DIR": str(Path(tmp) / "cache")})
                res = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                                     capture_output=True, text=True, timeout=120)
            finally:
                os.chmod(parent, 0o700)
        self.assertEqual(res.returncode, 0, res.stderr[-3000:])
        self.assertIn(auth._CONTACT_DEFAULT, res.stdout)


if __name__ == "__main__":
    unittest.main()
