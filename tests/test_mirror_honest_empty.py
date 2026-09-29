"""Mirror-owned (test_mirror_*: never synced, never overwritten): the public health sweep's honest
classes, which live in this repository's own scripts/ (health_sweep.py, gen_health_page.py). Split out of
test_honest_empty.py on 2026-09-29, whose engine tests now come from upstream with the sync."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MirrorHonestEmptyTests(unittest.TestCase):
    def test_health_sweep_classifies_http_401_and_403_as_blocked(self) -> None:
        sweep = _load_script("honest_empty_health_sweep", "health_sweep.py")

        self.assertEqual(
            sweep.classify_probe(False, "HTTP 401 Unauthorized"),
            ("blocked", "HTTP 401 Unauthorized"),
        )
        self.assertEqual(
            sweep.classify_probe(False, "HTTP 403 Forbidden"),
            ("blocked", "HTTP 403 Forbidden"),
        )

    def test_health_sweep_never_publishes_a_latency_for_a_skipped_probe(self) -> None:
        """A skipped probe has no verdict, so it must carry no latency.

        classify_probe maps healthy=None to "skipped", and the row builder used to pass the
        measured latency straight through. The page validator rejects that combination, so a
        single such source failed the whole published sweep (seen 2026-08-24 and 2026-09-07).
        """
        sweep = _load_script("honest_empty_health_sweep_latency", "health_sweep.py")
        entry = {"name": "s", "domains": ["general"], "access_tier": "free"}

        skipped = sweep.probe_row(entry, None, "no opinion", 812.0)
        self.assertEqual(skipped["status"], "skipped")
        self.assertIsNone(skipped["latency_ms"])

        measured = sweep.probe_row(entry, True, "", 812.0)
        self.assertEqual(measured["status"], "up")
        self.assertEqual(measured["latency_ms"], 812.0)

    def test_health_summary_and_page_keep_blocked_out_of_down(self) -> None:
        sweep = _load_script("honest_empty_health_sweep_summary", "health_sweep.py")
        page = _load_script("honest_empty_health_page", "gen_health_page.py")
        rows = [
            {"status": "blocked", "detail": "HTTP 403 Forbidden"},
            {"status": "down", "detail": "HTTP 503"},
        ]
        summary = sweep.build_summary(rows)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["down"], 1)

        payload = {
            "generated_utc": "2026-08-17T00:00:00Z",
            "vantage": "test",
            "omniseek_version": "0.2.0",
            "sweep_seconds": 0,
            "sources": [
                {
                    "name": "blocked-source",
                    "domain": "general",
                    "tier": "free",
                    "status": "blocked",
                    "latency_ms": 1,
                    "detail": "HTTP 403 Forbidden",
                },
                {
                    "name": "down-source",
                    "domain": "general",
                    "tier": "free",
                    "status": "down",
                    "latency_ms": 2,
                    "detail": "HTTP 503",
                },
            ],
            "summary": {
                "up": 0,
                "degraded": 0,
                "rate_limited": 0,
                "blocked": 1,
                "down": 1,
                "skipped": 0,
                "skipped_policy": 0,
                "skipped_capability": 0,
                "skipped_budget": 0,
                "total": 2,
            },
        }
        rendered = page.render_page(payload)
        self.assertIn("Blocked: 1", rendered)
        self.assertIn("Blocked means", rendered)
        self.assertIn("| blocked-source | free | blocked | 1 ms | HTTP 403 Forbidden |", rendered)


if __name__ == "__main__":
    unittest.main()
