"""A cache-only collect is not a live failure: Stack Exchange and reddit (the class 4b3ee36 and 0bb2649
fixed on the walled sources).

A cache-only collect (omniseek_search staleness=cache_only, the poll-safe pickup half of fire-then-collect)
that misses the cache must do no live work and count nothing. Two keyless API sources still did:

  - Stack Exchange: http.get_json returns None in cache-only mode without egress, but _se_get /
    _ase_get took the gate for it (a permit and a pacing slot) and then counted the None as a quota
    failure (_se_record(False)), so three cache-only misses opened the shared breaker and the next
    real searches of all six Stack Exchange sources were skipped for _SE_COOLDOWN.
  - reddit: the reddit-wide branch wrote a misleading ``reddit.sitewide_empty`` ("found nothing") for
    a browser search that never ran; the Arctic fan-out slept through its retry backoffs and fed the
    Arctic breaker; the empty answer was written under the query key (and an empty discovery list
    under each probed term), which the next live search served as reddit's answer.

Each test below except the guard rails fails on 4aa082a and passes after the fix. No network.
"""
import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from omniseek.core import _stackexchange as se
from omniseek.core import cache, diag, http
from omniseek.core.sources.api import reddit_source as rd
from omniseek.core.sources.scrape.academia_se_source import AcademiaSEAdapter


def _boom(*_a, **_kw):
    raise AssertionError("a cache-only collect made a live touch")


async def _aboom(*_a, **_kw):
    raise AssertionError("a cache-only collect made a live touch")


@contextmanager
def _cache_only():
    token = cache._cache_only_var.set(True)
    try:
        yield
    finally:
        cache._cache_only_var.reset(token)


@contextmanager
def _captured():
    diag.enable()
    box = []
    try:
        yield box
    finally:
        box.extend(diag.drain())


def _helpers(box):
    return [c.get("helper") for c in box]


class _IsolatedCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._tmp.name)
        self._cache_patch = mock.patch.object(cache, "CACHE_DIR", self._cache_dir)
        self._cache_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        self._tmp.cleanup()

    def _cache_files(self):
        return sorted(self._cache_dir.glob("*.json")) if self._cache_dir.exists() else []


# ── Stack Exchange ──────────────────────────────────────────────────────────────────────────────
class StackExchangeCacheOnlyTests(_IsolatedCache):
    def setUp(self):
        super().setUp()
        self._saved = (se._se_fail_streak, se._se_cooldown_until)
        se._se_fail_streak, se._se_cooldown_until = 0, 0.0

    def tearDown(self):
        se._se_fail_streak, se._se_cooldown_until = self._saved
        super().tearDown()

    def _assert_nothing_counted(self):
        self.assertEqual(se._se_fail_streak, 0, "a cache-only miss was counted as a quota failure")
        self.assertFalse(se._se_cooling(), "cache-only misses opened the quota breaker")

    def test_sync_miss_takes_no_gate_and_counts_nothing(self):
        with _cache_only(), mock.patch.object(se._se_guard, "hold", _boom), \
                mock.patch.object(se.http, "get_json", _boom), _captured() as box:
            for _ in range(se._SE_TRIP_AFTER + 2):
                self.assertIsNone(se._se_get(f"{se.API_BASE}/search/advanced", {"q": "phd"}))
        self._assert_nothing_counted()
        self.assertIn("stackexchange.cache_only", _helpers(box))

    def test_async_miss_takes_no_gate_and_counts_nothing(self):
        async def _run():
            for _ in range(se._SE_TRIP_AFTER + 2):
                self.assertIsNone(await se._ase_get(f"{se.API_BASE}/search/advanced", {"q": "phd"}))
        with _cache_only(), mock.patch.object(se._se_guard, "ahold", _boom), \
                mock.patch.object(se.http, "aget_json", _aboom), _captured() as box:
            asyncio.run(_run())
        self._assert_nothing_counted()
        self.assertIn("stackexchange.cache_only", _helpers(box))

    def test_adapter_collect_leaves_the_next_live_search_unblocked(self):
        a = AcademiaSEAdapter()
        with _cache_only(), mock.patch.object(se.http, "get_json", _boom):
            for _ in range(se._SE_TRIP_AFTER + 2):
                self.assertEqual(a.search("advisor left", limit=3), [])
        self._assert_nothing_counted()
        self.assertEqual(self._cache_files(), [], "a cache-only miss wrote the cache")

    def test_a_real_failure_still_counts(self):
        # GUARD: the fix must not blind the quota breaker to a real 429 / 5xx.
        with mock.patch.object(se.http, "get_json", lambda *a, **k: None):
            for _ in range(se._SE_TRIP_AFTER):
                self.assertIsNone(se._se_get(f"{se.API_BASE}/search/advanced", {"q": "phd"}))
        self.assertEqual(se._se_fail_streak, se._SE_TRIP_AFTER)
        self.assertTrue(se._se_cooling())


# ── reddit ──────────────────────────────────────────────────────────────────────────────────────
SUB_QUERY = "subreddit:PhD advisor left"        # explicit subs: the Arctic fan-out path
SITEWIDE_QUERY = "zxqv kubernetes operator"     # no topic group, no same-name sub: reddit-wide


class RedditCacheOnlyTests(_IsolatedCache):
    def setUp(self):
        super().setUp()
        self._saved = (rd._arctic_fail_streak, rd._arctic_cooldown_until)
        rd._arctic_fail_streak, rd._arctic_cooldown_until = 0, 0.0
        self._patches = [mock.patch.object(rd.http, "get_json", _boom),
                         mock.patch.object(rd.http, "aget_json", _aboom),
                         mock.patch.object(rd, "_cdp_search", _boom),
                         mock.patch.object(rd, "_arctic_record", _boom),
                         mock.patch.object(rd.time, "sleep", _boom)]
        for p in self._patches:
            p.start()
        self.a = rd.RedditAdapter()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        rd._arctic_fail_streak, rd._arctic_cooldown_until = self._saved
        super().tearDown()

    def _assert_clean(self, box):
        helpers = _helpers(box)
        self.assertIn("reddit.cache_only", helpers)
        self.assertNotIn("reddit.sitewide_empty", helpers, "a search that never ran was reported empty")
        self.assertNotIn("reddit.discovery_empty", helpers)
        self.assertEqual(rd._arctic_fail_streak, 0)
        self.assertFalse(rd._arctic_cooling())
        self.assertEqual(self._cache_files(), [], "a cache-only miss wrote the cache")

    def test_sitewide_miss_is_not_reported_empty(self):
        with _cache_only(), _captured() as box:
            self.assertEqual(self.a.search(SITEWIDE_QUERY, limit=5), [])
        self._assert_clean(box)

    def test_async_sitewide_miss_is_not_reported_empty(self):
        with _cache_only(), _captured() as box:
            self.assertEqual(asyncio.run(self.a.asearch(SITEWIDE_QUERY, limit=5)), [])
        self._assert_clean(box)

    def test_fanout_miss_makes_no_live_touch(self):
        with _cache_only(), _captured() as box:
            self.assertEqual(self.a.search(SUB_QUERY, limit=5), [])
        self._assert_clean(box)

    def test_async_fanout_miss_makes_no_live_touch(self):
        with _cache_only(), _captured() as box:
            self.assertEqual(asyncio.run(self.a.asearch(SUB_QUERY, limit=5)), [])
        self._assert_clean(box)

    def test_comment_miss_makes_no_live_touch(self):
        with _cache_only(), _captured() as box:
            self.assertEqual(self.a.search("comments: " + SUB_QUERY, limit=5), [])
            self.assertEqual(self.a.search("comments: " + SITEWIDE_QUERY, limit=5), [])
        self._assert_clean(box)

    def test_arctic_get_itself_refuses_in_cache_only(self):
        async def _run():
            return await rd._aarctic_get("/posts/search", {"subreddit": "PhD"}, retries=2)
        with _cache_only():
            self.assertIsNone(rd._arctic_get("/posts/search", {"subreddit": "PhD"}, retries=2))
            self.assertIsNone(asyncio.run(_run()))

    def test_a_warm_cache_is_still_served(self):
        # GUARD: cache-only reads what is cached; the skip comes only after a miss.
        doc = rd.Document(source="reddit", source_id="t3_x", url="https://www.reddit.com/r/PhD/x",
                                 title="cached", content="cached")
        subs = rd._resolve_subreddits(*rd._parse_subreddits(SUB_QUERY))
        q, _ = rd._parse_subreddits(SUB_QUERY)
        cache.set_docs(cache.make_key("reddit_arctic", "search", q, ",".join(subs), 5), [doc], ttl=600)
        with _cache_only():
            got = self.a.search(SUB_QUERY, limit=5)
        self.assertEqual([d.title for d in got], ["cached"])

    def test_a_real_arctic_failure_still_counts(self):
        # GUARD: a live HTTP failure still feeds the breaker (and retries with backoff).
        seen = []
        with mock.patch.object(rd.http, "get_json", lambda *a, **k: None), \
                mock.patch.object(rd, "_arctic_record", seen.append), \
                mock.patch.object(rd.time, "sleep", lambda s: None):
            self.assertIsNone(rd._arctic_get("/posts/search", {"subreddit": "PhD"}, retries=1))
        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
