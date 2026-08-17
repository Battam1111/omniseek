"""Offline contract tests for the source health publishing scripts."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HealthSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sweep = _load("health_sweep", "health_sweep.py")

    def test_classifies_live_probe_messages(self) -> None:
        self.assertEqual(self.sweep.classify_probe(True, "OK"), ("up", ""))
        self.assertEqual(
            self.sweep.classify_probe(True, "OK (HTTP 429; API alive)"),
            ("rate_limited", "OK (HTTP 429; API alive)"),
        )
        self.assertEqual(
            self.sweep.classify_probe(True, "4/5 feeds OK (degraded; dead: example.com)"),
            ("degraded", "4/5 feeds OK (degraded; dead: example.com)"),
        )
        self.assertEqual(
            self.sweep.classify_probe(False, "HTTP 503 upstream unavailable"),
            ("down", "HTTP 503 upstream unavailable"),
        )

    def test_classifies_unavailable_probe_as_skipped(self) -> None:
        self.assertEqual(
            self.sweep.classify_probe(None, "PyMuPDF missing: No module named 'fitz'"),
            ("skipped", "PyMuPDF missing: No module named 'fitz'"),
        )

    def test_pdf_optional_dependency_absence_is_not_down(self) -> None:
        from omniseek.core.sources.scrape.pdf_source import PdfAdapter

        real_import = __import__

        def missing_fitz(name, *args, **kwargs):
            if name == "fitz":
                raise ModuleNotFoundError("No module named 'fitz'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=missing_fitz):
            healthy, detail = PdfAdapter().health_check()

        self.assertIsNone(healthy)
        self.assertIn("PyMuPDF missing", detail)

    def test_classification_caps_detail_at_200_characters(self) -> None:
        status, detail = self.sweep.classify_probe(False, "x" * 250)
        self.assertEqual(status, "down")
        self.assertEqual(detail, "x" * 200)

    def test_sweep_skips_unconfigured_and_retired_sources(self) -> None:
        catalog = [
            {
                "name": "public",
                "domains": ["papers"],
                "access_tier": "free",
                "needs_credentials": False,
                "explicit_only": False,
                "retired": False,
            },
            {
                "name": "keyed",
                "domains": ["jobs"],
                "access_tier": "keyed",
                "needs_credentials": True,
                "explicit_only": False,
                "retired": False,
            },
            {
                "name": "named",
                "domains": [],
                "access_tier": "free",
                "needs_credentials": False,
                "explicit_only": True,
                "retired": False,
            },
            {
                "name": "retired",
                "domains": ["general"],
                "access_tier": "free",
                "needs_credentials": False,
                "explicit_only": False,
                "retired": True,
            },
        ]
        adapters = {row["name"]: object() for row in catalog}

        rows, _elapsed = self.sweep.sweep_sources(
            catalog,
            get_adapter=adapters.__getitem__,
            probe_adapter=lambda _adapter, _deadline: (True, "OK", 12),
            credentials_configured=lambda _name: False,
            budget_seconds=10,
        )

        self.assertEqual([row["name"] for row in rows], ["keyed", "named", "public"])
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["keyed"]["status"], "skipped")
        self.assertEqual(by_name["keyed"]["detail"], "requires operator credentials")
        self.assertIsNone(by_name["keyed"]["latency_ms"])
        self.assertEqual(by_name["named"]["domain"], "general")
        self.assertEqual(by_name["named"]["detail"], "explicit-only")
        self.assertEqual(by_name["public"]["status"], "up")
        self.assertEqual(by_name["public"]["latency_ms"], 12)

    def test_zero_budget_marks_every_probeable_source_skipped(self) -> None:
        catalog = [
            {
                "name": "a",
                "domains": ["general"],
                "access_tier": "free",
                "needs_credentials": False,
                "explicit_only": False,
                "retired": False,
            },
            {
                "name": "b",
                "domains": ["general"],
                "access_tier": "free",
                "needs_credentials": False,
                "explicit_only": False,
                "retired": False,
            },
        ]

        rows, _elapsed = self.sweep.sweep_sources(
            catalog,
            get_adapter=lambda _name: object(),
            probe_adapter=lambda _adapter, _deadline: self.fail("probe must not run"),
            credentials_configured=lambda _name: False,
            budget_seconds=0,
        )

        self.assertEqual(
            [(row["status"], row["detail"], row["latency_ms"]) for row in rows],
            [
                ("skipped", "sweep budget exhausted", None),
                ("skipped", "sweep budget exhausted", None),
            ],
        )

    def test_summary_has_all_status_counts(self) -> None:
        rows = [
            {"status": "up"},
            {"status": "degraded"},
            {"status": "rate_limited"},
            {"status": "down"},
            {"status": "skipped", "detail": "explicit-only"},
            {"status": "skipped", "detail": "sweep budget exhausted"},
            {"status": "skipped", "detail": "PyMuPDF missing: No module named 'fitz'"},
            {"status": "up"},
        ]
        self.assertEqual(
            self.sweep.build_summary(rows),
            {
                "up": 2,
                "degraded": 1,
                "rate_limited": 1,
                "down": 1,
                "skipped": 3,
                "skipped_policy": 1,
                "skipped_capability": 1,
                "skipped_budget": 1,
                "total": 8,
            },
        )


class HealthPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = _load("gen_health_page", "gen_health_page.py")

    def _payload(self) -> dict:
        return {
            "generated_utc": "2026-08-16T12:00:00Z",
            "vantage": "local-verify",
            "omniseek_version": "0.1.3",
            "sweep_seconds": 1.25,
            "sources": [
                {
                    "name": "alpha",
                    "domain": "papers",
                    "tier": "free",
                    "status": "up",
                    "latency_ms": 12,
                    "detail": "",
                },
                {
                    "name": "beta",
                    "domain": "jobs",
                    "tier": "keyed",
                    "status": "down",
                    "latency_ms": 34,
                    "detail": "HTTP 503 - unavailable",
                },
            ],
            "summary": {
                "up": 1,
                "degraded": 0,
                "rate_limited": 0,
                "down": 1,
                "skipped": 0,
                "skipped_policy": 0,
                "skipped_capability": 0,
                "skipped_budget": 0,
                "total": 2,
            },
        }

    def test_render_uses_json_values_and_exact_vantage_sentence(self) -> None:
        page = self.page.render_page(self._payload())

        self.assertIn(
            "Checked from GitHub Actions runners; a residential or maintainer deployment "
            "typically reaches more. One probe per source per run; this is a health signal, "
            "not an availability guarantee.",
            page,
        )
        self.assertIn("Vantage: local-verify", page)
        self.assertIn("OmniSeek version: 0.1.3", page)
        self.assertIn("Sweep duration: 1.25 seconds", page)
        self.assertIn(
            "Up: 1 | Degraded: 0 | Rate limited: 0 | Down: 1 | Skipped: 0 | Total: 2",
            page,
        )
        self.assertIn(
            "Skipped breakdown: policy=0 | capability absent=0 | sweep budget=0",
            page,
        )
        self.assertIn("| alpha | free | up | 12 ms |  |", page)
        self.assertIn("| beta | keyed | down | 34 ms | HTTP 503 - unavailable |", page)
        self.assertNotIn("\N{EM DASH}", page)

    def test_render_rejects_missing_summary_number(self) -> None:
        payload = self._payload()
        del payload["summary"]["down"]
        with self.assertRaises(ValueError):
            self.page.render_page(payload)

    def test_cli_writes_page_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            source = directory / "health.json"
            output = directory / "README.md"
            source.write_text(json.dumps(self._payload()), encoding="utf-8")

            result = self.page.main([str(source), "--out", str(output)])

            self.assertEqual(result, 0)
            self.assertIn("# OmniSeek source health", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
