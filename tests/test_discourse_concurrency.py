"""discourse_forums fetches its three instances concurrently (2026-08-29).

Why this file exists: the async twin awaited the three forums one after another, so the source's
latency was the SUM of three unrelated hosts (measured from the mini: 1.4-6.8s + 3.0s + 2.4s).
That sum was the entire reason it missed the search fan-out deadline and was the last timing-out
source after that day's memory work. Nothing was broken; it was just serial.

The timing test is the load-bearing one. Everything else here holds the invariant that going
concurrent changed ONLY the latency: same instances, same order, same degrade behaviour.
"""
import asyncio
import time
import unittest
from unittest import mock

from omniseek.core.sources.scrape import discourse_forums_source as mod


class DiscourseConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = mod.DiscourseForumsAdapter()
        self.n_instances = len(mod.INSTANCES)
        self.assertGreaterEqual(self.n_instances, 2, "the test is meaningless with one instance")

    @staticmethod
    def _host_of(url: str) -> str:
        return next(h for h in mod.INSTANCES if h in url)

    async def test_instances_are_fetched_concurrently(self):
        """Three 100ms fetches must finish in well under the serial 300ms.

        This asserts on wall-clock on purpose. A structural assertion (counting gather calls) would
        keep passing if someone reintroduced an await inside the loop, which is exactly the bug."""
        async def slow(url, **kwargs):
            await asyncio.sleep(0.1)
            return {"topics": []}

        with mock.patch.object(mod.http, "aget_json", side_effect=slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), self.n_instances)
        serial = 0.1 * self.n_instances
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_is_restored_when_replies_arrive_out_of_order(self):
        """Concurrency must not leak into the result order: the downstream scorer and every
        recorded output were produced by the serial version's INSTANCES order, so the fastest
        forum answering first must not reshuffle anything."""
        hosts = list(mod.INSTANCES)
        delays = {h: 0.02 * (len(hosts) - i) for i, h in enumerate(hosts)}  # first is SLOWEST

        async def varied(url, **kwargs):
            host = self._host_of(url)
            await asyncio.sleep(delays[host])
            return {"host": host}

        with mock.patch.object(mod.http, "aget_json", side_effect=varied):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([h for h, _ in out], hosts)

    async def test_one_instance_returning_nothing_keeps_the_others(self):
        dead = list(mod.INSTANCES)[-1]

        async def one_dead(url, **kwargs):
            return None if self._host_of(url) == dead else {"topics": []}

        with mock.patch.object(mod.http, "aget_json", side_effect=one_dead):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(len(out), self.n_instances - 1)
        self.assertNotIn(dead, [h for h, _ in out])

    async def test_one_instance_raising_keeps_the_others(self):
        """gather(return_exceptions=True): the serial loop could not raise here, so a raise is
        unexpected by construction. It must still not take the healthy forums down with it."""
        angry = list(mod.INSTANCES)[0]

        async def one_raises(url, **kwargs):
            if self._host_of(url) == angry:
                raise RuntimeError("instance exploded")
            return {"topics": []}

        with mock.patch.object(mod.http, "aget_json", side_effect=one_raises):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(len(out), self.n_instances - 1)
        self.assertNotIn(angry, [h for h, _ in out])

    async def test_every_instance_failing_returns_none(self):
        """The None-return contract the base layer degrades to [] on. Unchanged from serial."""
        async def all_dead(url, **kwargs):
            return None

        with mock.patch.object(mod.http, "aget_json", side_effect=all_dead):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_get_per_instance(self):
        """'Gently' means one GET per forum. Concurrency must not become extra requests."""
        calls: list[str] = []

        async def counting(url, **kwargs):
            calls.append(self._host_of(url))
            return {"topics": []}

        with mock.patch.object(mod.http, "aget_json", side_effect=counting):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), sorted(mod.INSTANCES))


if __name__ == "__main__":
    unittest.main()
