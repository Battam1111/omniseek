"""gov_open_data and mastodon fan out concurrently (2026-08-30).

Both were serial in their ASYNC twin, which is the path fetcher actually dispatches to whenever
an adapter has asearch. So each source's latency was the SUM of unrelated hosts.

gov_open_data is the worse of the two, and its history is the reason this file exists:

  * 2026-07-25 its SYNC fan-out was fixed for exactly this defect. The docstring records the
    measurement: 21.3s across 8 requests, blowing the broad-search deadline on 62% of searches
    (1231 of 1986), contribution to ranked output exactly ZERO.
  * The async twin was then written as a for-loop, which put the source right back where it
    started while the repaired sync fan-out sat there as dead code.

That is why the timing tests below assert on WALL CLOCK. A structural test (counting gather
calls) keeps passing the moment someone reintroduces an await inside a loop, which is precisely
how this regressed the first time.
"""
import asyncio
import time
import unittest
from unittest import mock

from omniseek.core.sources.scrape import gov_open_data_source as gov
from omniseek.core.sources.scrape import mastodon_source as mast


class GovOpenDataConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = gov.GovOpenDataAdapter()
        self.n = len(gov.PORTALS)
        self.assertGreaterEqual(self.n, 2, "the test is meaningless with one portal")

    async def test_portals_are_fetched_concurrently(self):
        """Three 100ms portals must finish well under the serial 300ms."""
        async def slow(portal, query, limit):
            await asyncio.sleep(0.1)
            return [{"name": portal["id"]}]

        with mock.patch.object(self.adapter, "_afetch_portal", slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), self.n)
        serial = 0.1 * self.n
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_is_restored_when_the_fastest_portal_answers_first(self):
        """Concurrency must not leak into result order: PORTALS order is the contract, and the
        sync twin's zip against PORTALS has always relied on it."""
        delays = {p["id"]: 0.02 * (self.n - i) for i, p in enumerate(gov.PORTALS)}  # first is SLOWEST

        async def varied(portal, query, limit):
            await asyncio.sleep(delays[portal["id"]])
            return [{"name": portal["id"]}]

        with mock.patch.object(self.adapter, "_afetch_portal", varied):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([p["id"] for p, _ in out], [p["id"] for p in gov.PORTALS])

    async def test_one_portal_failing_keeps_the_others(self):
        dead = gov.PORTALS[-1]["id"]

        async def one_dead(portal, query, limit):
            return None if portal["id"] == dead else [{"name": portal["id"]}]

        with mock.patch.object(self.adapter, "_afetch_portal", one_dead):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(len(out), self.n - 1)
        self.assertNotIn(dead, [p["id"] for p, _ in out])

    async def test_one_portal_raising_keeps_the_others(self):
        angry = gov.PORTALS[0]["id"]

        async def one_raises(portal, query, limit):
            if portal["id"] == angry:
                raise RuntimeError("portal exploded")
            return [{"name": portal["id"]}]

        with mock.patch.object(self.adapter, "_afetch_portal", one_raises):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(len(out), self.n - 1)
        self.assertNotIn(angry, [p["id"] for p, _ in out])

    async def test_every_portal_failing_returns_none(self):
        """The None contract the base degrades to [] on. Unchanged from serial."""
        async def all_dead(portal, query, limit):
            return None

        with mock.patch.object(self.adapter, "_afetch_portal", all_dead):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_call_per_portal(self):
        """Concurrency must not become extra requests."""
        calls = []

        async def counting(portal, query, limit):
            calls.append(portal["id"])
            return [{"name": portal["id"]}]

        with mock.patch.object(self.adapter, "_afetch_portal", counting):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), sorted(p["id"] for p in gov.PORTALS))


class MastodonConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = mast.MastodonAdapter()
        self.n = len(mast.INSTANCES)
        self.assertGreaterEqual(self.n, 2, "the test is meaningless with one instance")

    @staticmethod
    def _host_of(url: str) -> str:
        return next(h for h in mast.INSTANCES if h in url)

    async def test_instances_are_fetched_concurrently(self):
        async def slow(url, **kwargs):
            await asyncio.sleep(0.1)
            return [{"id": "1"}]

        with mock.patch.object(mast.http, "aget_json", side_effect=slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), self.n)
        serial = 0.1 * self.n
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_is_restored_when_replies_arrive_out_of_order(self):
        hosts = list(mast.INSTANCES)
        delays = {h: 0.02 * (len(hosts) - i) for i, h in enumerate(hosts)}  # first is SLOWEST

        async def varied(url, **kwargs):
            host = self._host_of(url)
            await asyncio.sleep(delays[host])
            return [{"host": host}]

        with mock.patch.object(mast.http, "aget_json", side_effect=varied):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([h for h, _ in out], hosts)

    async def test_one_instance_returning_nothing_keeps_the_others(self):
        dead = list(mast.INSTANCES)[-1]

        async def one_dead(url, **kwargs):
            return None if self._host_of(url) == dead else [{"id": "1"}]

        with mock.patch.object(mast.http, "aget_json", side_effect=one_dead):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(len(out), self.n - 1)
        self.assertNotIn(dead, [h for h, _ in out])

    async def test_one_instance_raising_keeps_the_others(self):
        angry = list(mast.INSTANCES)[0]

        async def one_raises(url, **kwargs):
            if self._host_of(url) == angry:
                raise RuntimeError("instance exploded")
            return [{"id": "1"}]

        with mock.patch.object(mast.http, "aget_json", side_effect=one_raises):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(len(out), self.n - 1)
        self.assertNotIn(angry, [h for h, _ in out])

    async def test_every_instance_failing_returns_none(self):
        async def all_dead(url, **kwargs):
            return None

        with mock.patch.object(mast.http, "aget_json", side_effect=all_dead):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_get_per_instance(self):
        calls = []

        async def counting(url, **kwargs):
            calls.append(self._host_of(url))
            return [{"id": "1"}]

        with mock.patch.object(mast.http, "aget_json", side_effect=counting):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), sorted(mast.INSTANCES))


class NoSerialRegressionTests(unittest.TestCase):
    """A cheap structural backstop for the specific regression shape that already happened once.

    The timing tests above are the real guard. This one exists because gov_open_data's async twin
    was written as a for-loop while its sync twin was already concurrent, and nothing flagged the
    asymmetry: the source simply got slow again and no test noticed.
    """

    def test_async_twins_do_not_loop_over_their_fan_out_targets(self):
        import inspect
        for mod, adapter, needle in (
            (gov, gov.GovOpenDataAdapter, "for portal in PORTALS:"),
            (mast, mast.MastodonAdapter, "for host, base in INSTANCES.items():"),
        ):
            src = inspect.getsource(adapter._araw_fetch)
            with self.subTest(module=mod.__name__):
                self.assertNotIn(needle, src,
                                 "the async fan-out is looping over its targets again")
                self.assertIn("asyncio.gather", src,
                              "the async fan-out no longer uses gather")


if __name__ == "__main__":
    unittest.main()
