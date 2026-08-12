import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.x` puts the repo root there
    from _repo_only import requires_source_repo
except ImportError:
    from tests._repo_only import requires_source_repo


@requires_source_repo
class DeployPayloadTests(unittest.TestCase):
    def _bash(self) -> str:
        if os.name == "nt":
            git = Path(shutil.which("git") or "")
            candidate = git.parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        bash = shutil.which("bash")
        if not bash:
            self.fail("bash is required to verify the deploy payload")
        return bash

    def test_archive_and_manifest_have_the_same_export_semantics(self):
        root = Path(__file__).resolve().parents[1]
        bash = self._bash()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "payload.tar"
            archive_arg = str(archive_path).replace("\\", "/")
            archive = subprocess.run(
                [bash, "scripts/deploy_payload.sh", "archive", archive_arg],
                cwd=root,
                capture_output=True,
                check=False,
            )
            self.assertEqual(archive.returncode, 0, archive.stderr.decode("utf-8", "replace"))
            manifest = subprocess.run(
                [bash, "scripts/deploy_payload.sh", "manifest", archive_arg],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(manifest.returncode, 0, manifest.stderr)
            with tarfile.open(archive_path) as payload:
                archived_files = sorted(
                    member.name for member in payload.getmembers() if member.isfile()
                )
            manifested_files = sorted(line for line in manifest.stdout.splitlines() if line)
        self.assertEqual(manifested_files, archived_files)
        self.assertNotIn("scripts/debug/import_creds_from_env.py", manifested_files)
        self.assertNotIn("scripts/debug/setup_creds.sh", manifested_files)

        deploy = (root / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('bash scripts/deploy_payload.sh archive "$ARCHIVE"', deploy)
        self.assertIn('bash scripts/deploy_payload.sh manifest "$ARCHIVE"', deploy)
        self.assertNotIn("git ls-tree -r --name-only HEAD --", deploy)


if __name__ == "__main__":
    unittest.main()
