"""Offline tests for the benchmark harness."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import httpx

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import gen_report
import run as bench_run
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
    _liveness_probe,
)
from schema import (
    TaskValidationError,
    canonicalize_task,
    load_task_file,
    load_tasks,
)


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


def _results_fixture():
    receipt = [
        {
            "tool": "WebSearch",
            "query": "fixture query for baseline receipt",
            "date": "2026-08-16",
            "first_page_hit": False,
        },
        {
            "tool": "WebSearch",
            "query": "second fixture query",
            "date": "2026-08-15",
            "first_page_hit": False,
        },
    ]
    return {
        "schema_version": "bench-v1.0",
        "run": {
            "git_sha7": "fixture1",
            "passes": 1,
            "reps": 1,
            "suites": ["s3-crosslingual", "s6-memory"],
        },
        "env": {
            "omniseek_version": "fixture-version",
            "utc": "2026-08-16T12:00:00+00:00",
            "vantage": "offline-fixture",
            "extras_detected": {"recall": True},
            "python": "3.13.0",
            "platform": "fixture-platform",
        },
        "tasks": {
            "s3-fixture-001": {
                "id": "s3-fixture-001",
                "suite": "s3-crosslingual",
                "claim": "fixture claim with a recorded receipt",
                "search_resistance_prefilter": receipt,
                "liveness": None,
                "passes": [],
            },
            "s6-fixture-001": {
                "id": "s6-fixture-001",
                "suite": "s6-memory",
                "claim": "fixture claim without a web baseline",
                "liveness": None,
                "passes": [],
            },
        },
        "suites": {
            "s3-crosslingual": {
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
                "noise_band": 0.0,
                "latency_ms": {"p50": 12.0, "p90": 18.0},
                "stale_count": 1,
                "dormant": False,
                "dormant_note": None,
            },
            "s6-memory": {
                "n": 0,
                "passes": [
                    {
                        "successes": 0,
                        "total": 0,
                        "rate": 0.0,
                        "wilson_95": [0.0, 0.0],
                    }
                ],
                "pooled": {
                    "successes": 0,
                    "total": 0,
                    "rate": 0.0,
                    "wilson_95": [0.0, 0.0],
                },
                "noise_band": None,
                "latency_ms": {"p50": None, "p90": None},
                "stale_count": 1,
                "dormant": True,
                "dormant_note": "required optional extra missing",
            },
        },
        "stale": [
            {"id": "s3-fixture-001", "class": "dead"},
            {"id": "s6-fixture-001", "class": "dead"},
        ],
        "dormant": ["s6-memory"],
    }


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

    def test_schema_rejects_invalid_liveness_probe(self):
        with self.assertRaises(TaskValidationError):
            canonicalize_task(_good_task(liveness_probe={}))

    def test_schema_accepts_probe_absent_or_null(self):
        absent = _good_task()
        absent.pop("liveness_probe")
        self.assertNotIn("liveness_probe", canonicalize_task(absent))
        self.assertIsNone(canonicalize_task(_good_task(liveness_probe=None)).get("liveness_probe"))

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

    def test_default_command_resolves_next_to_the_active_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir()
            python_name = "python.exe" if os.name == "nt" else "python"
            command_name = "omniseek.exe" if os.name == "nt" else "omniseek"
            python = scripts / python_name
            command = scripts / command_name
            python.touch()
            command.touch()
            with mock.patch.object(bench_run.sys, "executable", str(python)):
                self.assertEqual(
                    bench_run._resolve_command("omniseek"),
                    str(command.resolve()),
                )

    def test_probe_absent_task_runs_without_network(self):
        class FakeInvoker:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return None

            async def call(self, _tool, _args, _timeout_s):
                return {"text": "A phrase"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks_dir = root / "tasks"
            tasks_dir.mkdir()
            task = _good_task()
            task.pop("liveness_probe")
            (tasks_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
            output = root / "result.json"
            with mock.patch.object(bench_run, "MCPInvoker", FakeInvoker):
                exit_code = bench_run.main(
                    [
                        "--suites",
                        "s3-crosslingual",
                        "--reps",
                        "1",
                        "--passes",
                        "1",
                        "--tasks-dir",
                        str(tasks_dir),
                        "--out",
                        str(output),
                        "--no-warmup",
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["stale"], [])
            self.assertEqual(payload["suites"]["s3-crosslingual"]["pooled"]["total"], 1)

    def test_liveness_probe_classifies_429_as_rate_limited(self):
        class Response:
            status_code = 429

        class Client:
            def get(self, _url):
                return Response()

        probe = _liveness_probe(_good_task(), Client())
        self.assertFalse(probe["alive"])
        self.assertEqual(probe["class"], "rate_limited")

    def test_liveness_probe_classifies_timeout_separately(self):
        class Client:
            def get(self, _url):
                raise httpx.ReadTimeout("synthetic timeout")

        probe = _liveness_probe(_good_task(), Client())
        self.assertFalse(probe["alive"])
        self.assertEqual(probe["class"], "timeout")

    def test_absolute_windows_out_path_with_backslashes_is_preserved(self):
        raw = r"C:\Users\bench\AppData\Local\Temp\bench-final.json"
        args = _parser().parse_args(["--out", raw])
        output = bench_run._output_path(args.out, "abc1234")
        self.assertEqual(str(output), raw)
        self.assertEqual(output.suffix.casefold(), ".json")
        if os.name == "nt":
            self.assertTrue(output.is_absolute())

    def test_mid_run_client_exception_writes_partial_json_and_exits_quickly(self):
        class FailingInvoker:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return None

            async def call(self, _tool, _args, _timeout_s):
                raise ExceptionGroup(
                    "outer client task group",
                    [
                        ExceptionGroup(
                            "inner client task group",
                            [RuntimeError("synthetic mid-run failure")],
                        )
                    ],
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks_dir = root / "tasks"
            tasks_dir.mkdir()
            (tasks_dir / "task.json").write_text(
                json.dumps(_good_task()),
                encoding="utf-8",
            )
            output = root / "partial.json"
            stderr = io.StringIO()
            started = time.monotonic()
            with (
                mock.patch.object(bench_run, "MCPInvoker", FailingInvoker),
                mock.patch.object(
                    bench_run,
                    "_liveness_probe",
                    return_value={
                        "alive": True,
                        "status_code": 200,
                        "url": "https://example.test/probe",
                    },
                ),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = bench_run.main(
                    [
                        "--suites",
                        "s3-crosslingual",
                        "--reps",
                        "1",
                        "--passes",
                        "1",
                        "--tasks-dir",
                        str(tasks_dir),
                        "--out",
                        str(output),
                        "--no-warmup",
                    ]
                )
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(exit_code, 1)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            detail = payload["error"]["detail"]
            self.assertIn("phase=task s3-test-001 rep 1", detail)
            self.assertIn("tool=omniseek_search", detail)
            self.assertIn("RuntimeError('synthetic mid-run failure')", detail)
            self.assertNotIn("ExceptionGroup", detail)
            self.assertIn(detail, stderr.getvalue())

    def test_teardown_kills_the_server_after_the_hard_bound(self):
        released = asyncio.Event()

        class HangingStack:
            async def __aexit__(self, _exc_type, _exc, _traceback):
                await released.wait()

        class FakeProcess:
            killed = False

            def kill(self):
                self.killed = True
                released.set()

            async def wait(self):
                return 0

        invoker = object.__new__(MCPInvoker)
        invoker._stack = HangingStack()
        invoker._process = FakeProcess()
        invoker._last_tool = "omniseek_search"

        async def terminate():
            invoker._process.kill()

        invoker._terminate_process = terminate
        started = time.monotonic()
        with mock.patch.object(bench_run, "TEARDOWN_TIMEOUT", 0.01):
            asyncio.run(invoker.close())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(invoker._process.killed)


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


class VisualizationContractTests(unittest.TestCase):
    def test_runner_copies_receipts_verbatim_and_omits_absent_receipts(self):
        receipt = [
            {
                "tool": "WebSearch",
                "query": "exact fixture query",
                "date": "2026-08-16",
                "first_page_hit": False,
            }
        ]

        class FakeInvoker:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return None

            async def call(self, _tool, _args, _timeout_s):
                return {"text": "A phrase"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks_dir = root / "tasks"
            tasks_dir.mkdir()
            with_receipt = _good_task(
                id="s3-receipt-001",
                search_resistance_prefilter=receipt,
            )
            without_receipt = _good_task(
                id="s6-no-receipt-001",
                suite="s6-memory",
            )
            for task in (with_receipt, without_receipt):
                task.pop("liveness_probe")
                (tasks_dir / f"{task['id']}.json").write_text(
                    json.dumps(task),
                    encoding="utf-8",
                )
            output = root / "result.json"
            with mock.patch.object(bench_run, "MCPInvoker", FakeInvoker):
                exit_code = bench_run.main(
                    [
                        "--suites",
                        "s3-crosslingual,s6-memory",
                        "--reps",
                        "1",
                        "--passes",
                        "1",
                        "--tasks-dir",
                        str(tasks_dir),
                        "--out",
                        str(output),
                        "--no-warmup",
                    ]
                )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            receipt_record = payload["tasks"]["s3-receipt-001"]
            self.assertIn("search_resistance_prefilter", receipt_record)
            self.assertEqual(
                receipt_record["search_resistance_prefilter"],
                receipt,
            )
            self.assertNotIn(
                "search_resistance_prefilter",
                payload["tasks"]["s6-no-receipt-001"],
            )

    def test_chart_rejects_a_number_missing_from_results_json(self):
        renderer = getattr(gen_report, "render_results_svg", None)
        self.assertIsNotNone(
            renderer,
            "gen_report.render_results_svg must exist before chart tests can run",
        )
        with mock.patch.object(gen_report, "_json_numbers", return_value=set()):
            with self.assertRaises(ValueError):
                renderer(_results_fixture(), theme="light")

    def test_chart_rejects_a_missing_stale_count(self):
        results = _results_fixture()
        results["suites"]["s3-crosslingual"].pop("stale_count", None)
        with self.assertRaises(ValueError):
            gen_report.render_results_svg(results, theme="light")

    def test_chart_svg_is_well_formed_with_one_suite_group_and_dormant_row(self):
        renderer = getattr(gen_report, "render_results_svg", None)
        self.assertIsNotNone(
            renderer,
            "gen_report.render_results_svg must exist before chart tests can run",
        )
        svg = renderer(_results_fixture(), theme="light")
        root = ET.fromstring(svg)
        groups = [
            element
            for element in root.iter()
            if "data-suite" in element.attrib
        ]
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {element.attrib["data-suite"] for element in groups},
            {"s3-crosslingual", "s6-memory"},
        )
        live_group = next(
            element
            for element in groups
            if element.attrib["data-suite"] == "s3-crosslingual"
        )
        live_text = " ".join(
            element.text or ""
            for element in live_group.iter()
            if element.text
        )
        self.assertIn("1/2", live_text)
        self.assertIn("0.5000", live_text)
        self.assertIn("stale 1", live_text)
        dormant_group = next(
            element
            for element in groups
            if element.attrib["data-suite"] == "s6-memory"
        )
        text = " ".join(
            element.text or ""
            for element in dormant_group.iter()
            if element.text
        ).casefold()
        self.assertIn("sense dormant", text)
        self.assertIn("stale 1", text)
        self.assertFalse(
            any(
                element.tag.rsplit("}", 1)[-1] == "rect"
                for element in dormant_group.iter()
            )
        )

    def test_report_writes_both_svg_variants_and_embeds_picture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_file = root / "run.json"
            report_file = root / "RESULTS.md"
            result_file.write_text(
                json.dumps(_results_fixture()),
                encoding="utf-8",
            )
            generate_report(result_file, report_file)
            report = report_file.read_text(encoding="utf-8")
            self.assertTrue((root / "results-light.svg").exists())
            self.assertTrue((root / "results-dark.svg").exists())
            self.assertIn("<picture>", report)
            self.assertIn("results-light.svg", report)
            self.assertIn("results-dark.svg", report)

    def test_report_describes_baseline_not_applicable_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_file = root / "run.json"
            report_file = root / "RESULTS.md"
            result_file.write_text(
                json.dumps(_results_fixture()),
                encoding="utf-8",
            )
            generate_report(result_file, report_file)
            report = report_file.read_text(encoding="utf-8")
            self.assertIn("baseline not applicable", report)
            self.assertIn("s6-memory", report)
            self.assertIn("server's own memory contract", report)
            self.assertIn("fixture query for baseline receipt", report)
            self.assertIn("first-page non-hits", report)

    def test_chart_shows_a_stale_count_on_a_dormant_row(self):
        results = _results_fixture()
        results["suites"]["s6-memory"]["stale_count"] = 2
        results["stale"].append({"id": "s6-fixture-002", "class": "timeout"})
        svg = gen_report.render_results_svg(results, theme="dark")
        root = ET.fromstring(svg)
        dormant_group = next(
            element
            for element in root.iter()
            if element.attrib.get("data-suite") == "s6-memory"
        )
        text = " ".join(
            element.text or "" for element in dormant_group.iter() if element.text
        )
        self.assertIn("sense dormant", text)
        self.assertIn("stale 2", text)

    def test_chart_draws_a_dormant_suite_that_recorded_no_pooled_numbers(self):
        results = _results_fixture()
        results["suites"]["s6-memory"].pop("pooled")
        results["suites"]["s6-memory"].pop("passes")
        svg = gen_report.render_results_svg(results, theme="light")
        root = ET.fromstring(svg)
        dormant_group = next(
            element
            for element in root.iter()
            if element.attrib.get("data-suite") == "s6-memory"
        )
        text = " ".join(
            element.text or "" for element in dormant_group.iter() if element.text
        )
        self.assertIn("sense dormant", text)

    def test_table_reports_a_dormant_suite_as_not_scored_rather_than_zero(self):
        # The chart draws a dormant sense as dormant; the table used to print a pooled
        # rate of zero for the same suite, which reads as "failed everything" and
        # contradicts the chart on the same page.
        scored_with_pooled_zeros = gen_report._suite_values(
            "s1-audio",
            {
                "n": 0,
                "pooled": {
                    "successes": 0,
                    "total": 0,
                    "rate": 0.0,
                    "wilson_95": [0.0, 0.0],
                },
                "stale_count": 0,
                "dormant": True,
                "dormant_note": "required optional extra missing: asr",
            },
            {0.0},
        )
        self.assertEqual(scored_with_pooled_zeros[1], "not scored")

        # The runner always writes stale_count, but a suite that never ran may carry no
        # pooled block at all; that must not raise on the way to the table.
        scored_without_pooled = gen_report._suite_values(
            "s1-audio",
            {
                "stale_count": 0,
                "dormant": True,
                "dormant_note": "required optional extra missing: asr",
            },
            {0.0},
        )
        self.assertEqual(scored_without_pooled[0], "n/a")
        self.assertEqual(scored_without_pooled[1], "not scored")

    def test_both_svg_themes_are_well_formed_and_free_of_em_dash(self):
        results = _results_fixture()
        for theme in ("light", "dark"):
            svg = gen_report.render_results_svg(results, theme=theme)
            ET.fromstring(svg)
            self.assertNotIn("\N{EM DASH}", svg)
            self.assertNotIn("<script", svg)
            self.assertIn('width="', svg)
            self.assertIn('viewBox="', svg)
        with self.assertRaises(ValueError):
            gen_report.render_results_svg(results, theme="sepia")

    def test_task_loader_rejects_a_malformed_receipt(self):
        task = _good_task(
            id="s3-bad-receipt-001",
            search_resistance_prefilter=[
                {"tool": "WebSearch", "query": "q", "date": "2026-08-16"}
            ],
        )
        with self.assertRaises(TaskValidationError):
            canonicalize_task(task)
        task["search_resistance_prefilter"][0]["first_page_hit"] = False
        self.assertEqual(
            canonicalize_task(task)["search_resistance_prefilter"],
            task["search_resistance_prefilter"],
        )
        engine_task = _good_task(
            id="s3-engine-receipt-001",
            search_resistance_prefilter=[
                {
                    "engine": "Bing",
                    "query": "q",
                    "date": "2026-08-16",
                    "first_page_hit": False,
                }
            ],
        )
        self.assertEqual(
            canonicalize_task(engine_task)["search_resistance_prefilter"],
            engine_task["search_resistance_prefilter"],
        )

    def test_published_tasks_all_carry_loadable_receipts(self):
        tasks = load_tasks(HERE / "tasks")
        carried = [
            task for task in tasks if "search_resistance_prefilter" in task
        ]
        self.assertTrue(carried, "at least one published task must carry a receipt")
        for task in carried:
            for receipt in task["search_resistance_prefilter"]:
                self.assertIsInstance(receipt["first_page_hit"], bool)
        for task in tasks:
            if task["suite"] in {"s5-scholar", "s6-memory"}:
                self.assertNotIn("search_resistance_prefilter", task)

    def test_workflow_publishes_both_svg_variants(self):
        workflow = (HERE.parent / ".github" / "workflows" / "bench.yml").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            workflow,
            r'cp "\$RUNNER_TEMP/results-light\.svg" "\$publish_dir/bench/results-light\.svg"',
        )
        self.assertRegex(
            workflow,
            r'cp "\$RUNNER_TEMP/results-dark\.svg" "\$publish_dir/bench/results-dark\.svg"',
        )

    def test_website_contract_uses_offline_safe_live_results_page(self):
        website = HERE.parent / "site" / "bench.html"
        self.assertTrue(website.exists(), "site/bench.html must exist")
        html = website.read_text(encoding="utf-8")
        self.assertIn(
            "https://raw.githubusercontent.com/Battam1111/omniseek/health-data/bench/latest.json",
            html,
        )
        self.assertIn("Unable to load benchmark results", html)
        self.assertIn("health-data", html)
        self.assertIn('id="chart"', html)
        self.assertIn('id="baseline"', html)
        self.assertNotRegex(html, r"<script[^>]+src=")
        index = (HERE.parent / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="bench.html"', index)


if __name__ == "__main__":
    unittest.main(verbosity=2)
