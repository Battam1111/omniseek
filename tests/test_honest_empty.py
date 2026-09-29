"""The honest-empty contract: an empty result cannot mean two things (a source that failed outright
raises; a partial one returns what it has, with a note). Written in the public mirror (OmniSeek) and
moved here on 2026-09-29, because the code it tests is this tree's; the sync carries it back."""
from __future__ import annotations

import asyncio
import contextlib
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


@contextlib.contextmanager
def _quiet_backend():
    """Start the shared web-search backend from a COLD ledger for one test.

    The backend paces and circuit-breaks itself (2026-09-10): its DDG gate sleeps up to
    _DDG_MIN_INTERVAL after ANY previous call (a call that failed still reserved a slot), and a
    tripped breaker refuses to send at all. A test about failure SURFACING must neutralize both,
    or it measures the pacer instead: this suite's third test lost its 2s fetch deadline to the
    2.5s gate left armed by the two tests before it, and read as 'timed_out' instead of 'errored'.
    """
    with contextlib.ExitStack() as stack:
        for name in ("_ddg_last_call", "_brave_last_call", "_ddg_cooldown_until", "_brave_cooldown_until"):
            stack.enter_context(mock.patch.object(_search_backend, name, 0.0))
        yield


class HonestEmptyTests(unittest.TestCase):
    def test_ddg_sync_total_failure_raises(self) -> None:
        with _quiet_backend(), mock.patch.object(_search_backend, "_get_client", side_effect=OSError("offline")):
            with self.assertRaisesRegex(RuntimeError, "DDG request failed"):
                _search_backend._ddg("query", 1)

    def test_ddg_async_total_failure_raises(self) -> None:
        async def run() -> None:
            with _quiet_backend(), mock.patch.object(_search_backend, "_aget_client", side_effect=OSError("offline")):
                with self.assertRaisesRegex(RuntimeError, "DDG request failed"):
                    await _search_backend._addg("query", 1)

        asyncio.run(run())

    def test_search_backend_failure_reaches_fetcher_outcome(self) -> None:
        adapter = _search_backend_source()
        with (
            _quiet_backend(),
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
            mock.patch.object(xiaoyuzhou_source.http, "direct", side_effect=OSError("offline")),
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
