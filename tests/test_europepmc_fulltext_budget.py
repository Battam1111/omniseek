"""europepmc pulls its full texts concurrently WITHOUT changing the budget (2026-08-30).

This one was nearly written off as unfixable. The serial version's budget counts SUCCESSES, not
attempts: a failed pull keeps the abstract and does not consume budget. The first reading was
that concurrency therefore changes the semantics and the fix needs a product decision.

It does not. Asking for exactly the shortfall each round preserves both numbers that matter:
the count of successful full texts, and the total number of requests, because serial and batched
both stop at the attempt that completes the budget. Only the concurrency inside a round is new.

These tests pin exactly that, since the whole argument for the change is that nothing observable
moved except wall clock.
"""
import asyncio
import time
import unittest
from unittest import mock

from omniseek.core.sources.api import europepmc_source as mod


def _raw(n: int) -> dict:
    """A search payload with n eligible OA records."""
    return {"resultList": {"result": [
        {"id": str(i), "title": "T%d" % i, "pmcid": "PMC%d" % i,
         "source": "MED", "isOpenAccess": "Y", "inEPMC": "Y", "hasTextMinedTerms": "Y",
         "abstractText": "abstract %d" % i, "pubYear": "2026"}
        for i in range(n)]}}


class FullTextBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = mod.EuropePMCAdapter()
        self.assertEqual(mod._MAX_FULLTEXT, 3, "these tests assume a budget of 3")

    async def _run(self, n_records, outcomes, delay=0.0):
        """outcomes: callable(pmcid) -> bool, what each pull returns."""
        attempts = []

        async def fake(doc, result):
            attempts.append(result["pmcid"])
            if delay:
                await asyncio.sleep(delay)
            return outcomes(result["pmcid"])

        with mock.patch.object(self.adapter, "_amaybe_fulltext", fake), \
             mock.patch.object(self.adapter, "_fulltext_eligible", lambda r: True):
            docs = await self.adapter._ato_documents(_raw(n_records), "q", n_records)
        return docs, attempts

    async def test_stops_after_three_successes(self):
        """The budget counts successes. With everything succeeding, exactly 3 are attempted."""
        docs, attempts = await self._run(10, lambda pmcid: True)
        self.assertEqual(len(docs), 10, "every record still becomes a doc")
        self.assertEqual(len(attempts), 3, "attempted %r" % attempts)

    async def test_failures_do_not_consume_budget(self):
        """The serial version's defining behaviour: a miss keeps its abstract and the budget
        moves on. Here the first two fail, so it must keep going until 3 have SUCCEEDED."""
        failing = {"PMC0", "PMC1"}
        docs, attempts = await self._run(10, lambda pmcid: pmcid not in failing)
        self.assertEqual(len(docs), 10)
        self.assertEqual(len(attempts), 5, "2 misses + 3 hits = 5 attempts; got %r" % attempts)

    async def test_never_attempts_more_than_the_serial_version_would(self):
        """Everything fails: it should try every eligible record and stop, not loop forever."""
        docs, attempts = await self._run(6, lambda pmcid: False)
        self.assertEqual(len(docs), 6)
        self.assertEqual(len(attempts), 6, "should exhaust the candidates exactly once")

    async def test_fewer_candidates_than_budget_is_fine(self):
        docs, attempts = await self._run(2, lambda pmcid: True)
        self.assertEqual(len(docs), 2)
        self.assertEqual(len(attempts), 2)

    async def test_the_batch_runs_concurrently(self):
        """Three 100ms pulls in one round must finish well under the serial 300ms.

        Asserting on wall clock on purpose: a structural check would keep passing if someone
        put the await back inside the loop."""
        started = time.perf_counter()
        docs, attempts = await self._run(3, lambda pmcid: True, delay=0.1)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(attempts), 3)
        self.assertLess(elapsed, 0.3 * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~0.3s -> still sequential")

    async def test_doc_order_is_result_order(self):
        """Docs are built and appended up front, so concurrency cannot reshuffle them."""
        docs, _ = await self._run(5, lambda pmcid: True)
        self.assertEqual([d.title for d in docs], ["T0", "T1", "T2", "T3", "T4"])

    async def test_a_raising_pull_counts_as_a_miss(self):
        """_amaybe_fulltext swallows its own failures, so a raise is unexpected by construction.
        It must not take the round down, and it must not count toward the budget."""
        async def sometimes_raises(doc, result):
            if result["pmcid"] == "PMC0":
                raise RuntimeError("full text exploded")
            return True

        with mock.patch.object(self.adapter, "_amaybe_fulltext", sometimes_raises), \
             mock.patch.object(self.adapter, "_fulltext_eligible", lambda r: True):
            docs = await self.adapter._ato_documents(_raw(6), "q", 6)
        self.assertEqual(len(docs), 6, "a raising pull must not drop any doc")

    async def test_ineligible_records_are_never_attempted(self):
        attempts = []

        async def fake(doc, result):
            attempts.append(result["pmcid"])
            return True

        with mock.patch.object(self.adapter, "_amaybe_fulltext", fake), \
             mock.patch.object(self.adapter, "_fulltext_eligible",
                               lambda r: r["pmcid"] in {"PMC1", "PMC3"}):
            docs = await self.adapter._ato_documents(_raw(6), "q", 6)
        self.assertEqual(len(docs), 6)
        self.assertEqual(sorted(attempts), ["PMC1", "PMC3"])


if __name__ == "__main__":
    unittest.main()
