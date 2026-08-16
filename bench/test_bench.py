"""Offline tests for the benchmark harness."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from convert_candidates import convert_candidates
from gen_report import generate_report
from judges import (
    dynamic_structural,
    judge,
    identifier_in_topk,
    normalized_containment,
    wilson_interval,
)
from run import (
    DormantExtraError,
    MCPInvoker,
    _aggregate_pass,
    _majority_status,
    _parser,
    _required_extras,
    _run_rep,
)
from schema import TaskValidationError, canonicalize_task, load_task_file


def _arm_socket_guard() -> None:
    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def connect(self, address):
            host = address[0] if isinstance(address, tuple) and address else ""
            if host not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError(f"offline test attempted non-loopback socket: {host}")
            return super().connect(address)

        def connect_ex(self, address):
            host = address[0] if isinstance(address, tuple) and address else ""
            if host not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError(f"offline test attempted non-loopback socket: {host}")
            return super().connect_ex(address)

    socket.socket = GuardedSocket


_arm_socket_guard()


def _good_task(**overrides):
    task = {
        "id": "s3-test-001",
        "suite": "s3-crosslingual",
        "claim": "a returned document contains the phrase",
        "input": {"tool": "omniseek_search", "args": {"query": "classic paper"}},
        "ground_truth": {"type": "normalized_containment", "value": "A phrase"},
        "liveness_probe": {"url": "https://example.test/probe"},
        "added_in": "bench-v1.0",
    }
    task.update(overrides)
    return task


class JudgeTests(unittest.TestCase):
    def test_normalized_containment_strips_cjk_fullwidth_punctuation_and_whitespace(self):
        self.assertTrue(
            normalized_containment(
                "  「量子，计算」 ",
                "答案：量子 计算！",
            )
        )
        self.assertFalse(normalized_containment("alpha beta", "alpha gamma"))

    def test_doi_arxiv_and_url_identifiers_are_normalized(self):
        self.assertTrue(
            identifier_in_topk(
                "https://doi.org/10.1234/ABC",
                [{"metadata": {"doi": "10.1234/abc"}}],
                1,
            )
        )
        self.assertTrue(
            identifier_in_topk(
                "arXiv:1602.03837v2",
                [{"url": "https://arxiv.org/abs/1602.03837v1"}],
                1,
            )
        )
        self.assertTrue(
            identifier_in_topk(
                "https://example.test/paper/",
                [{"url": "http://example.test/paper"}],
                1,
            )
        )
        self.assertTrue(
            identifier_in_topk(
                "arXiv:1602.03837v2",
                [{"url": "https://arxiv.org/pdf/1602.03837v5.pdf"}],
                1,
            )
        )
        self.assertFalse(identifier_in_topk("10.1234/missing", [{"id": "other"}], 1))

    def test_similarity_fallback_reports_the_branch_that_passed(self):
        exact = judge(
            _good_task(),
            {"text": "A phrase"},
        )
        self.assertTrue(exact["passed"])
        self.assertEqual(exact["branch"], "normalized_containment")

        fallback = judge(
            _good_task(
                ground_truth={
                    "type": "normalized_containment",
                    "value": "the quick brown fox",
                },
                fallback_similarity=0.8,
            ),
            {"text": "the quick brown f0x jumps"},
        )
        self.assertTrue(fallback["passed"])
        self.assertEqual(fallback["branch"], "fallback_similarity")

        failed = judge(
            _good_task(
                ground_truth={
                    "type": "normalized_containment",
                    "value": "the quick brown fox",
                },
                fallback_similarity=0.99,
            ),
            {"text": "the slow green turtle"},
        )
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["branch"], "fallback_similarity")

    def test_meta_assertion_dsl_checks_exists_gt_equals(self):
        task = _good_task(
            ground_truth={
                "type": "meta_assertion",
                "assertions": [
                    {"path": "_meta.searched", "gt": 0},
                    {"path": "_meta.empty", "exists": True},
                    {"path": "_meta.mode", "equals": "ranked"},
                ],
            }
        )
        result = {
            "_meta": {"searched": 2, "empty": [], "mode": "ranked"},
        }
        outcome = judge(task, result)
        self.assertTrue(outcome["passed"])
        self.assertEqual(outcome["branch"], "meta_assertion")

    def test_repeat_task_requires_seen_before_field_on_second_document(self):
        task = load_task_file(HERE / "tasks" / "s6-memory" / "s6-memory-001.json")[0]
        result = {
            "calls": [
                {"documents": [{"metadata": {"seen_before": False}}]},
                {
                    "documents": [{"metadata": {}}],
                    "_meta": {"deduped": {"in": 2, "out": 1}},
                },
            ]
        }
        outcome = judge(task, result)
        self.assertFalse(outcome["passed"])

    def test_repeat_task_accepts_seen_before_field_with_false_value(self):
        task = load_task_file(HERE / "tasks" / "s6-memory" / "s6-memory-001.json")[0]
        result = {
            "calls": [
                {"documents": [{"metadata": {"seen_before": False}}]},
                {"documents": [{"metadata": {"seen_before": False}}]},
            ]
        }
        outcome = judge(task, result)
        self.assertTrue(outcome["passed"])

    def test_semantic_scholar_retries_one_429(self):
        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        class Client:
            def __init__(self):
                self.calls = 0

            def get(self, _url):
                self.calls += 1
                if self.calls == 1:
                    return Response(429, {})
                return Response(200, {"citationCount": 10})

        task = _good_task(
            kind="citation_conflict",
            ground_truth={
                "type": "dynamic_structural",
                "upstream_of_record": {"api": "semantic_scholar"},
                "ratio_floor": 2.0,
            },
        )
        client = Client()
        outcome = dynamic_structural(
            task,
            [
                {"citation_count": 10, "kind": "arxiv"},
                {"citation_count": 30, "kind": "doi"},
            ],
            http_client=client,
        )
        self.assertTrue(outcome["passed"])
        self.assertEqual(client.calls, 2)

    def test_citation_conflict_reads_provenance_stamped_signal_values(self):
        class Response:
            status_code = 200

            def json(self):
                return {"message": {"is-referenced-by-count": 30}}

        class Client:
            def get(self, _url):
                return Response()

        task = _good_task(
            kind="citation_conflict",
            ground_truth={
                "type": "dynamic_structural",
                "upstream_of_record": {"api": "crossref"},
                "ratio_floor": 2.0,
            },
        )
        outcome = dynamic_structural(
            task,
            [
                {
                    "signals": {
                        "citations": {
                            "value": 10,
                            "computed_by": "source:openalex/cited_by",
                        }
                    }
                },
                {"citation_count": 30, "source": "crossref"},
            ],
            http_client=Client(),
        )
        self.assertTrue(outcome["passed"])

    def test_dynamic_retraction_unwraps_paper_enrich_results(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "message": {
                        "updated-by": [
                            {"type": "correction"},
                            {"type": "retraction"},
                        ]
                    }
                }

        class Client:
            def get(self, _url):
                return Response()

        task = _good_task(
            kind="retraction",
            ground_truth={
                "type": "dynamic_structural",
                "upstream_of_record": {"api": "crossref"},
            },
        )
        outcome = dynamic_structural(
            task,
            {
                "results": [
                    {
                        "integrity": {
                            "retracted": True,
                            "notices": ["correction", "retraction"],
                        }
                    }
                ]
            },
            http_client=Client(),
        )
        self.assertTrue(outcome["passed"])

    def test_arxiv_oa_task_accepts_live_html_probe(self):
        class Response:
            status_code = 200

            def json(self):
                raise ValueError("HTML response")

        class Client:
            def get(self, _url):
                return Response()

        task = _good_task(
            kind="oa_fulltext",
            ground_truth={
                "type": "dynamic_structural",
                "upstream_of_record": {"api": "unpaywall"},
            },
            liveness_probe={"url": "https://arxiv.org/abs/1602.03837"},
        )
        outcome = dynamic_structural(
            task,
            {
                "results": [
                    {
                        "is_oa": True,
                        "pdf_url": "https://arxiv.org/pdf/1602.03837.pdf",
                    }
                ]
            },
            http_client=Client(),
        )
        self.assertTrue(outcome["passed"])


class SchemaTests(unittest.TestCase):
    def test_schema_accepts_good_task_and_canonicalizes_eye_prefix(self):
        task = canonicalize_task(
            _good_task(
                input={
                    "eye_tool": "eye_search",
                    "args": {"query": "classic paper"},
                }
            )
        )
        self.assertEqual(task["input"]["tool"], "omniseek_search")
        self.assertNotIn("eye_tool", task["input"])

    def test_schema_rejects_missing_liveness_probe(self):
        with self.assertRaises(TaskValidationError):
            canonicalize_task(_good_task(liveness_probe={}))

    def test_schema_loads_single_task_and_array_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            single = root / "one.json"
            single.write_text(json.dumps(_good_task()), encoding="utf-8")
            array = root / "many.json"
            array.write_text(json.dumps([_good_task(id="s3-test-002")]), encoding="utf-8")
            self.assertEqual(len(load_task_file(single)), 1)
            self.assertEqual(len(load_task_file(array)), 1)


class StatisticsTests(unittest.TestCase):
    def test_wilson_interval_known_values(self):
        low, high = wilson_interval(50, 100)
        self.assertAlmostEqual(low, 0.40382982859014716, places=12)
        self.assertAlmostEqual(high, 0.5961701714098528, places=12)
        self.assertEqual(wilson_interval(0, 0), (0.0, 0.0))


class RunnerContractTests(unittest.TestCase):
    def test_warmup_uses_sources_then_cache_only_search_and_is_untimed_for_scoring(self):
        calls = []
        invoker = object.__new__(MCPInvoker)

        async def fake_call(tool, args, timeout_s):
            calls.append((tool, args, timeout_s))
            return {}

        invoker.call = fake_call
        elapsed = asyncio.run(invoker.warmup())
        self.assertGreaterEqual(elapsed, 0.0)
        self.assertEqual(
            calls,
            [
                ("omniseek_sources", {}, 600.0),
                (
                    "omniseek_search",
                    {"query": "benchmark neutral warmup", "staleness": "cache_only"},
                    600.0,
                ),
            ],
        )

    def test_no_warmup_flag_is_available(self):
        self.assertFalse(_parser().parse_args([]).no_warmup)
        self.assertTrue(_parser().parse_args(["--no-warmup"]).no_warmup)

    def test_dynamic_upstream_skip_is_not_a_failure(self):
        self.assertEqual(
            _majority_status([{"status": "skip", "passed": False}]),
            "skipped",
        )

    def test_pdf_read_task_declares_pdf_extra(self):
        task = _good_task(
            suite="s4-depth",
            input={
                "tool": "omniseek_read",
                "args": {"target": "https://example.test/paper.pdf"},
            },
        )
        self.assertIn("pdf", _required_extras(task))

    def test_dynamic_upstream_skip_is_excluded_from_denominator(self):
        task = _good_task()
        records = {
            task["id"]: {
                "passes": [
                    {
                        "pass": 1,
                        "status": "skipped",
                        "reps": [{"status": "skip", "passed": False}],
                    }
                ]
            }
        }
        aggregate = _aggregate_pass(
            records,
            [task],
            1,
            stale_ids=set(),
            dormant_suites=set(),
        )
        self.assertEqual(aggregate[task["suite"]]["total"], 0)

    def test_normal_degraded_tool_payload_marks_extra_dormant(self):
        class Invoker:
            async def call(self, _tool, _args, _timeout_s):
                return {"error": "install omniseek[asr] to enable transcription"}

        task = _good_task(
            suite="s1-audio",
            input={
                "tool": "omniseek_transcribe",
                "args": {"url": "https://example.test/audio"},
            },
        )
        with self.assertRaises(DormantExtraError):
            asyncio.run(_run_rep(Invoker(), task, 120.0))


class ConversionAndReportTests(unittest.TestCase):
    def test_convert_candidates_writes_canonical_per_task_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_file = root / "candidates.json"
            candidate_file.write_text(
                json.dumps(
                    [
                        _good_task(
                            id="s3-candidate-001",
                            input={
                                "eye_tool": "eye_read",
                                "args": {"target": "https://example.test"},
                            },
                        )
                    ]
                ),
                encoding="utf-8",
            )
            out_dir = root / "tasks"
            written = convert_candidates(candidate_file, out_dir)
            self.assertEqual(len(written), 1)
            saved = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["input"]["tool"], "omniseek_read")

    def test_report_generator_uses_json_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = {
                "env": {"vantage": "local", "python": "3.13"},
                "suites": {
                    "s6-memory": {
                        "n": 2,
                        "passes": [
                            {
                                "successes": 1,
                                "total": 2,
                                "rate": 0.5,
                                "wilson_95": [0.09453120573423058, 0.9054687942657694],
                            }
                        ],
                        "pooled": {
                            "successes": 1,
                            "total": 2,
                            "rate": 0.5,
                            "wilson_95": [0.09453120573423058, 0.9054687942657694],
                        },
                        "noise_band": None,
                        "latency_ms": {"p50": 12.0, "p90": 14.0},
                        "stale_count": 0,
                        "dormant": None,
                    }
                },
                "stale": ["s6-stale-001"],
                "dormant": [],
            }
            result_file = root / "run.json"
            result_file.write_text(json.dumps(results), encoding="utf-8")
            report_file = root / "RESULTS.md"
            generate_report(result_file, report_file)
            report = report_file.read_text(encoding="utf-8")
            self.assertIn("s6-memory", report)
            self.assertIn("s6-stale-001", report)
            self.assertIn("Conflict of interest", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
