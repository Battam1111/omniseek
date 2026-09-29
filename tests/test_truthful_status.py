"""A walled source whose browser (CDP) cannot be reached is skipped by the shared health probe, not
reported down. Written in the public mirror (OmniSeek) and moved here on 2026-09-29, because the code it
tests is this tree's; the sync carries it back."""
from __future__ import annotations

import unittest
from unittest import mock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from omniseek.core.sources.walled import _base


class TruthfulStatusTests(unittest.TestCase):
    def test_cdp_unavailable_is_skipped_by_the_shared_walled_health_probe(self):
        class Probe(_base.BaseCDPAdapter, register=False):
            name = "_truthful_status_probe"

            def _search_url(self, _query):
                return "https://example.invalid/search"

            def _flow(self, _page):
                return ""

            def _to_documents(self, _raw, _query, _limit):
                return []

        with mock.patch.object(
            _base,
            "cdp_health",
            return_value=(False, "connection refused"),
        ):
            healthy, detail = Probe().health_check()

        self.assertIsNone(healthy)
        self.assertIn("CDP not reachable", detail)


if __name__ == "__main__":
    unittest.main()
