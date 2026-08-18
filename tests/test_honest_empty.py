from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import auth, fetcher
from omniseek.core.sources.api import _base as api_base
from omniseek.core.sources.api import _search_backend

with mock.patch.object(auth, "write_template"):
    from omniseek.core.sources.scrape import xiaoyuzhou_source


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HonestEmptyTests(unittest.TestCase):
    def test_ddg_sync_total_failure_raises(self) -> None:
        with mock.patch.object(_search_backend, "_get_client", side_effect=OSError("offline")):
            with self.assertRaisesRegex(RuntimeError, "DDG request failed"):
                _search_backend._ddg("query", 1)

    def test_ddg_async_total_failure_raises(self) -> None:
        async def run() -> None:
            with mock.patch.object(_search_backend, "_aget_client", side_effect=OSError("offline")):
                with self.assertRaisesRegex(RuntimeError, "DDG request failed"):
                    await _search_backend._addg("query", 1)

        asyncio.run(run())

    def test_search_backend_failure_reaches_fetcher_outcome(self) -> None:
        adapter = _search_backend_source()
        with (
            mock.patch.object(fetcher, "get_adapter", return_value=adapter),
            mock.patch.object(_search_backend, "_brave_key", return_value=None),
            mock.patch.object(_search_backend, "_get_client", side_effect=OSError("offline")),
        ):
            outcome = fetcher.fetch_outcome(
                adapter.name,
                "query",
                limit=1,
                fresh=True,
                deadline_s=2,
            )

        self.assertEqual(outcome.state, "errored")
        self.assertIn("DDG request failed", outcome.reason)
        self.assertTrue(outcome.captures)

    def test_xiaoyuzhou_total_failure_raises(self) -> None:
        adapter = xiaoyuzhou_source.XiaoyuzhouAdapter()
        with (
            mock.patch.object(adapter, "_podcasts", return_value=[{"id": "pod", "name": "pod"}]),
            mock.patch.object(xiaoyuzhou_source.httpx, "get", side_effect=OSError("offline")),
        ):
            with self.assertRaisesRegex(OSError, "offline"):
                adapter.search("query")

    def test_xiaoyuzhou_partial_results_are_returned_with_a_note(self) -> None:
        adapter = xiaoyuzhou_source.XiaoyuzhouAdapter()
        doc = adapter._ep_to_doc({"eid": "episode-1", "title": "one"}, "pod")
        with (
            mock.patch.object(
                adapter,
                "_fetch_podcast",
                side_effect=[[doc], OSError("offline")],
            ),
            mock.patch.object(xiaoyuzhou_source, "diag") as diag,
        ):
            result = adapter.search("", limit=10)

        self.assertEqual(result, [doc])
        diag.note.assert_called_once()
        self.assertIn("partial", diag.note.call_args.kwargs["body"])

    def test_base_api_missing_probe_is_local_configuration_gap(self) -> None:
        class Probe(api_base.BaseAPIAdapter, register=False):
            name = "_honest_empty_probe"
            description = "test adapter"

            def _raw_fetch(self, _query: str, _limit: int) -> list:
                return []

            def _to_document(self, _raw):
                return None

        with mock.patch.object(api_base.http, "get") as get:
            healthy, detail = Probe().health_check()

        self.assertIsNone(healthy)
        self.assertIn("our adapter configuration", detail)
        get.assert_not_called()

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


def _search_backend_source():
    return _search_backend_source_type(
        "_honest_empty_search_backend",
        "search-index",
        "example.invalid",
    )


def _search_backend_source_type(name: str, description: str, site: str):
    from omniseek.core.sources.api.search_index_source import _SearchVenue

    return _SearchVenue(name=name, description=description, site=site)


if __name__ == "__main__":
    unittest.main()
