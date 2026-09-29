"""Mirror-owned (test_mirror_*: never synced, never overwritten): the image publishing workflow in this
repository's own .github/. Split out of test_truthful_status.py on 2026-09-29, whose engine test now comes
from upstream with the sync."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MirrorTruthfulStatusTests(unittest.TestCase):
    def test_publish_workflow_has_duplicate_version_noop_and_revision_label(self):
        workflow = (
            ROOT / ".github" / "workflows" / "publish-image.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Check whether release version is already registered", workflow)
        self.assertIn("steps.registry.outputs.exists", workflow)
        self.assertIn("org.opencontainers.image.revision=${{ github.sha }}", workflow)


if __name__ == "__main__":
    unittest.main()
