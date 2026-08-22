"""Fixture tests for the pre-registered S3 section 8 rebuild."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
FIXTURES = ROOT / "tests" / "fixtures" / "s3"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

from judges import judge, normalized_containment  # noqa: E402
from run import _task_record  # noqa: E402
from schema import TaskValidationError, canonicalize_task, load_task_file  # noqa: E402


class S3RebuildTests(unittest.TestCase):
    def _load(self, filename: str) -> dict:
        return load_task_file(FIXTURES / filename)[0]

    def _raw(self, filename: str = "s3-valid.json") -> dict:
        return json.loads((FIXTURES / filename).read_text(encoding="utf-8"))

    def test_s3_containment_accepts_canonical_key(self):
        task = self._load("s3-valid.json")
        outcome = judge(task, {"text": "The source reports Café 43% of results."})
        self.assertTrue(outcome["passed"])
        self.assertEqual(outcome["branch"], "normalized_containment")

    def test_s3_containment_accepts_a_registered_form(self):
        task = self._load("s3-valid.json")
        outcome = judge(task, {"text": "The source reports cafe 43 percent of results."})
        self.assertTrue(outcome["passed"])

    def test_s3_containment_fails_when_all_forms_are_absent(self):
        task = self._load("s3-valid.json")
        outcome = judge(task, {"text": "The source reports Café 42% of results."})
        self.assertFalse(outcome["passed"])

    def test_normalization_absorbs_composed_and_decomposed_accent_renderings(self):
        self.assertTrue(normalized_containment("café", "CAFE\u0301"))

    def test_normalization_absorbs_literal_backslash_percent_rendering(self):
        self.assertTrue(normalized_containment("43%", r"43\%"))

    def test_normalization_does_not_absorb_interleaved_line_number_digits(self):
        self.assertFalse(normalized_containment("12345", "12 7 345"))

    def test_normalization_preserves_numeric_separators_to_avoid_collisions(self):
        self.assertFalse(normalized_containment("12345", "12.345"))

    def test_s3_schema_preserves_all_section_8_fields(self):
        task = self._load("s3-valid.json")
        self.assertEqual(task["accessibility_tier"], "T2")
        self.assertEqual(task["key_language"], "en")
        self.assertEqual(task["accepted_forms"], ["cafe 43 percent of results"])
        self.assertEqual(len(task["render_routes"]), 3)
        self.assertEqual(task["funnel_id"], "need-fixture-001")

    def test_s3_schema_rejects_t3_retrieval_task(self):
        with self.assertRaises(TaskValidationError):
            self._load("s3-invalid-t3.json")

    def test_s3_schema_rejects_identifier_judge(self):
        with self.assertRaises(TaskValidationError):
            self._load("s3-invalid-identifier.json")

    def test_identifier_judge_is_refused_even_if_called_directly_for_s3(self):
        task = self._raw("s3-invalid-identifier.json")
        outcome = judge(task, [{"id": "10.1234/fixture"}])
        self.assertFalse(outcome["passed"])
        self.assertIn("refused", outcome["detail"])

    def test_s3_schema_validates_section_8_field_types(self):
        invalid_values = {
            "accessibility_tier": "T4",
            "key_language": [],
            "accepted_forms": "cafe 43 percent of results",
            "render_routes": ["publisher_html", 4, "omniseek_reader"],
            "funnel_id": "",
        }
        for field, invalid_value in invalid_values.items():
            with self.subTest(field=field):
                raw = self._raw()
                raw[field] = invalid_value
                with self.assertRaises(TaskValidationError):
                    canonicalize_task(raw)

    def test_s3_schema_requires_three_render_routes(self):
        raw = self._raw()
        raw["render_routes"] = ["publisher_html", "pdf_text_extraction"]
        with self.assertRaises(TaskValidationError):
            canonicalize_task(raw)

    def test_runner_records_section_8_metadata(self):
        task = self._load("s3-valid.json")
        record = _task_record(task)
        for field in (
            "accessibility_tier",
            "key_language",
            "accepted_forms",
            "render_routes",
            "funnel_id",
        ):
            self.assertEqual(record[field], task[field])


if __name__ == "__main__":
    unittest.main(verbosity=2)
