from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.x` puts the repo root there
    from _repo_only import requires_source_repo
except ImportError:
    from tests._repo_only import requires_source_repo


@requires_source_repo
class DeployScriptTests(unittest.TestCase):
    def _root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _bash(self) -> str:
        if os.name == "nt":
            git = Path(shutil.which("git") or "")
            candidate = git.parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        bash = shutil.which("bash")
        if not bash:
            self.fail("bash is required to verify deploy.sh")
        return bash

    def test_shell_scripts_parse(self):
        root = self._root()
        for script in ("deploy.sh", "scripts/deploy_payload.sh"):
            result = subprocess.run(
                [self._bash(), "-n", script],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name != "nt", "macOS path resolution is the target behavior")
    def test_public_prepare_accepts_macos_tmp_alias(self):
        root = self._root()
        output = Path(tempfile.gettempdir()) / f"omniseek-public-test-{os.getpid()}"
        result = subprocess.run(
            [self._bash(), "public_prepare.sh", str(output)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(output, ignore_errors=True)
        self.assertNotIn("README link check failed", result.stderr)

    def test_deploy_uses_release_staging_verification_activation_and_rollback(self):
        deploy = (self._root() / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("git status --porcelain", deploy)
        self.assertIn("releases/.staging-", deploy)
        self.assertIn("record-baseline", deploy)
        self.assertIn("baseline healthz", deploy)
        self.assertIn("install", deploy)
        self.assertIn("verify-release", deploy)
        self.assertIn("activate", deploy)
        self.assertIn("rollback", deploy)
        self.assertLess(deploy.index("remote_tx verify-release"), deploy.index("remote_tx activate"))
        self.assertLess(deploy.index("remote_tx activate"), deploy.index("remote_tx rollback"))
        self.assertIn("PYTHONPATH=", deploy)
        self.assertIn(".eye-git.bundle", deploy)
        self.assertIn("launchctl kickstart", deploy)

    def test_deploy_never_extracts_or_prunes_the_active_flat_tree(self):
        deploy = (self._root() / "deploy.sh").read_text(encoding="utf-8")
        self.assertNotIn("tar -C omniseek -x", deploy)
        self.assertNotIn("find src scripts tests docs", deploy)
        self.assertNotIn("rm -rf", deploy)
        self.assertNotIn("scp -q \"$TMP_BUNDLE\" \"$MINI:omniseek/.eye-git.bundle\"", deploy)

    def test_deploy_preserves_failed_release_and_durable_receipts(self):
        deploy = (self._root() / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("ACTIVE_PENDING_HEALTH", deploy)
        self.assertIn("ROLLED_BACK", deploy)
        self.assertNotIn("releases/$BUILD_ID\"", deploy.split("rollback", 1)[1])

    def test_deploy_repairs_compatibility_and_cleans_verified_transfer_staging(self):
        deploy = (self._root() / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("compatibility_alarms", deploy)
        self.assertIn("repair-compatibility", deploy)
        self.assertIn("cleanup-staging", deploy)
        self.assertLess(deploy.index("repair-compatibility"), deploy.index("transfer candidate"))
        self.assertLess(deploy.index("remote_tx verify-release"), deploy.index("cleanup-staging"))
        self.assertLess(deploy.index("cleanup-staging"), deploy.index("atomic activation"))

    def test_deploy_requires_runtime_heartbeat_build_identity_before_verification(self):
        deploy = (self._root() / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("scheduler-heartbeat", deploy)
        self.assertIn("producer.build_id", deploy)
        self.assertIn("RUNTIME BUILD ID MISMATCH", deploy)
        self.assertLess(deploy.index("producer.build_id"), deploy.index("remote_tx verify-active"))


if __name__ == "__main__":
    unittest.main()
