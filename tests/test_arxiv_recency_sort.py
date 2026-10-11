"""arxiv: a recency query comes back newest first (OmniSeek showcase R12, 2026-10-11).

R12 asked "speculative decoding" for the past week; the arxiv bucket led with a 2022 paper and
mixed 2023 to 2026, because the adapter always sent ``sortBy=relevance``. Relevance stays the
default (the house convention for the paper lookups), and a query that names a time window is
rewritten into arXiv's native form: the window becomes a ``submittedDate`` range ANDed onto the
topic and the sort becomes ``submittedDate`` descending. Offline: the egress is stubbed and the
parameters it receives are checked, on both the sync and the async path.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest import mock

from omniseek.core.sources.api import arxiv_source as ax

TODAY = date(2026, 10, 11)


def _params(query, limit=10):
    return ax._search_params(query, limit, today=TODAY)


class DefaultOrderTests(unittest.TestCase):
    def test_plain_topic_keeps_relevance_and_the_query_verbatim(self):
        p = _params("speculative decoding")
        self.assertEqual(p["search_query"], "speculative decoding")
        self.assertEqual((p["sortBy"], p["sortOrder"]), ("relevance", "descending"))
        self.assertEqual(p["max_results"], 10)

    def test_native_query_without_a_window_is_untouched(self):
        p = _params("ti:llm AND cat:cs.CL", 500)
        self.assertEqual(p["search_query"], "ti:llm AND cat:cs.CL")
        self.assertEqual(p["sortBy"], "relevance")
        self.assertEqual(p["max_results"], 100)


class RecencyWindowTests(unittest.TestCase):
    WEEK = "submittedDate:[202610040000 TO 202610112359]"

    def test_english_past_n_days(self):
        p = _params("speculative decoding past 7 days")
        self.assertEqual(p["search_query"], f"(all:speculative AND all:decoding) AND {self.WEEK}")
        self.assertEqual((p["sortBy"], p["sortOrder"]), ("submittedDate", "descending"))

    def test_other_window_spellings_give_the_same_week(self):
        for q in ("speculative decoding in the last 7 days", "speculative decoding last 1 week",
                  "speculative decoding days:7", "最近 7 天 speculative decoding",
                  "speculative decoding since:2026-10-04"):
            with self.subTest(q=q):
                p = _params(q)
                self.assertEqual(p["search_query"],
                                 f"(all:speculative AND all:decoding) AND {self.WEEK}")
                self.assertEqual(p["sortBy"], "submittedDate")

    def test_native_topic_is_grouped_and_phrase_kept(self):
        p = _params("ti:llm AND cat:cs.CL past 2 weeks")
        self.assertEqual(p["search_query"],
                         "(ti:llm AND cat:cs.CL) AND submittedDate:[202609270000 TO 202610112359]")
        p = _params('"speculative decoding" days:3')
        self.assertEqual(p["search_query"],
                         '(all:"speculative decoding") AND submittedDate:[202610080000 TO 202610112359]')

    def test_window_alone_lists_everything_in_it(self):
        p = _params("days:1")
        self.assertEqual(p["search_query"], "submittedDate:[202610100000 TO 202610112359]")
        self.assertEqual(p["sortBy"], "submittedDate")

    def test_native_range_is_passed_through_with_a_date_sort(self):
        q = "all:diffusion AND submittedDate:[202610010000 TO 202610112359]"
        p = _params(q)
        self.assertEqual(p["search_query"], q)
        self.assertEqual(p["sortBy"], "submittedDate")
        p = _params("cat:cs.LG AND lastUpdatedDate:[202610010000 TO 202610112359]")
        self.assertEqual(p["sortBy"], "lastUpdatedDate")

    def test_bad_since_date_falls_back_to_relevance(self):
        p = _params("diffusion since:2026-13-45")
        self.assertEqual(p["search_query"], "diffusion since:2026-13-45")
        self.assertEqual(p["sortBy"], "relevance")


class EgressTests(unittest.TestCase):
    """Both egress paths send the parameters _search_params builds."""

    def test_sync_and_async_send_the_same_params(self):
        sent = {}

        def fake_get(url, params=None, **_):
            sent["sync"] = dict(params)
            return None

        async def fake_aget(url, params=None, **_):
            sent["async"] = dict(params)
            return None

        q = "speculative decoding past 7 days"
        a = ax.ArxivAdapter()
        with mock.patch.object(ax, "_arxiv_get_text", fake_get), \
                mock.patch.object(ax, "_arxiv_aget_text", fake_aget):
            self.assertEqual(a._raw_fetch(q, 10), [])
            self.assertEqual(asyncio.run(a._araw_fetch(q, 10)), [])
        self.assertEqual(sent["sync"], sent["async"])
        self.assertEqual(sent["sync"]["sortBy"], "submittedDate")
        self.assertIn("submittedDate:[", sent["sync"]["search_query"])
        self.assertTrue(sent["sync"]["search_query"].startswith("(all:speculative AND all:decoding) AND "))


if __name__ == "__main__":
    unittest.main()
