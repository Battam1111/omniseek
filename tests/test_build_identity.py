import unittest
import io
import subprocess
import tarfile
from pathlib import Path

from omniseek.core.contracts.build_identity import validate_build_id

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.x` puts the repo root there
    from _repo_only import requires_source_repo
except ImportError:
    from tests._repo_only import requires_source_repo

ROOT = Path(__file__).resolve().parents[1]


class BuildIdentityTests(unittest.TestCase):
    def test_accepts_exact_lowercase_commit(self):
        self.assertEqual(validate_build_id("a" * 40), "a" * 40)

    def test_rejects_archive_placeholder(self):
        with self.assertRaisesRegex(ValueError, "archive-substituted"):
            validate_build_id("$Format:%H$")

    def test_rejects_malformed_or_uppercase_identity(self):
        for value in ("", "a" * 39, "A" * 40, "g" * 40):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_build_id(value)

    # Only THIS one needs the git working tree; the three above are pure validator logic and must
    # keep running from a release, where they still guard the shipped contract.
    @requires_source_repo
    def test_git_archive_substitutes_the_exact_archived_commit(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        archive = subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                "HEAD",
                "src/omniseek/core/contracts/build_id.py",
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as payload:
            source = payload.extractfile("src/omniseek/core/contracts/build_id.py")
            self.assertIsNotNone(source)
            text = source.read().decode("utf-8")
        self.assertIn(f'EYE_BUILD_ID = "{head}"', text)


if __name__ == "__main__":
    unittest.main()
