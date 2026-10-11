"""Cold first broad search: the two in-process causes that made core academic sources miss the
broad deadline on a new install (stranger report, friction 6).

1. omniseek_search is the one async tool body, so it skipped _threaded's _set_limiter: a session whose
   first call is omniseek_search ran its ~90-source fan-out on AnyIO's default 40 worker tokens.
2. The native-async RSS path parsed every feed (feedparser) and BM25-scored every bundle ON the event
   loop, stalling it for seconds during a cold sweep, so finished academic fetches were not collected
   before the deadline.
3. Every RSS source started all of its feeds at once: one cold query sent 2,210 feed requests into
   the shared 128-connection pool, so the API sources queued behind blog hosts.
4. httpcore imports sniffio inside every async lock; anyio 4.x no longer installs it, and a failed
   import re-scans sys.path each time, on the event loop.
"""
import asyncio
import importlib
import sys
import threading
import time
import unittest
from unittest import mock

import anyio

from omniseek import server
from omniseek.core import fetcher, http
from omniseek.core.sources.scrape import _rss


class EyeSearchRaisesWorkerLimiterTest(unittest.TestCase):
    def test_first_call_eye_search_runs_on_the_raised_limiter(self):
        seen = {}

        async def fake_ranked(*a, **k):
            seen["tokens"] = anyio.to_thread.current_default_thread_limiter().total_tokens
            return [], {"timed_out": [], "empty": [], "errored": {}}

        async def run():
            self.assertEqual(anyio.to_thread.current_default_thread_limiter().total_tokens, 40)
            with mock.patch.object(fetcher, "asearch_ranked", fake_ranked), \
                    mock.patch.object(server, "_stamp_backend_state", lambda *a, **k: None):
                await server.omniseek_search(query="speculative decoding")

        old = server._limiter_set
        server._limiter_set = False
        try:
            asyncio.run(run())
        finally:
            server._limiter_set = old
        self.assertEqual(seen["tokens"], server._THREAD_TOKENS)


class _Resp:
    status_code = 200

    def __init__(self, body: bytes):
        self.content = body


def _feed(tag: str) -> bytes:
    items = "".join(f"<item><title>speculative decoding {tag}{i}</title>"
                    f"<link>https://example.org/{tag}{i}</link>"
                    f"<description>draft model {tag}{i}</description></item>" for i in range(3))
    return (f"<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>"
            f"<title>t</title>{items}</channel></rss>").encode()


class _Feeds(_rss.RSSAdapterBase):
    name = "cold_test_feeds"
    feeds = ["https://example.org/a.xml", "https://example.org/b.xml"]


class RssCpuOffLoopTest(unittest.TestCase):
    def test_parse_and_scoring_run_off_the_event_loop(self):
        threads = {"parse": set(), "score": set()}
        real_parse, real_scores = _rss._parse_or_refuse, _rss.relevance.doc_scores

        def parse(*a, **k):
            threads["parse"].add(threading.get_ident())
            return real_parse(*a, **k)

        def scores(*a, **k):
            threads["score"].add(threading.get_ident())
            return real_scores(*a, **k)

        async def aget(url, **kw):
            return _Resp(_feed(url[-5]))

        async def run():
            loop_thread = threading.get_ident()
            docs = await _Feeds().asearch("speculative decoding", 5)
            return loop_thread, docs

        with mock.patch.object(_rss, "_parse_or_refuse", parse), \
                mock.patch.object(_rss.relevance, "doc_scores", scores), \
                mock.patch.object(_rss.http, "aget", aget), \
                mock.patch.object(_rss.cache, "get_docs", lambda *a, **k: None), \
                mock.patch.object(_rss.cache, "set_docs", lambda *a, **k: None):
            loop_thread, docs = asyncio.run(run())
        self.assertEqual(len(docs), 5)
        self.assertTrue(threads["parse"] and threads["score"])
        self.assertNotIn(loop_thread, threads["parse"])
        self.assertNotIn(loop_thread, threads["score"])


class _ManyFeeds(_rss.RSSAdapterBase):
    name = "cold_test_many_feeds"
    feeds = [f"https://example.org/f{i}.xml" for i in range(200)]


class FeedFanOutBoundedTest(unittest.TestCase):
    def test_feed_fetches_in_flight_never_exceed_the_cap(self):
        state = {"now": 0, "peak": 0, "calls": 0}

        async def aget(url, **kw):
            state["now"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return _Resp(_feed("x"))

        async def run():
            # Two RSS sources sweeping at once share the one per-loop bound.
            return await asyncio.gather(_ManyFeeds().asearch("speculative decoding", 5),
                                        _ManyFeeds().asearch("speculative decoding", 5))

        with mock.patch.object(_rss.http, "aget", aget), \
                mock.patch.object(_rss.cache, "get_docs", lambda *a, **k: None), \
                mock.patch.object(_rss.cache, "set_docs", lambda *a, **k: None):
            asyncio.run(run())
        self.assertEqual(state["calls"], 400)  # every feed still fetched
        self.assertLessEqual(state["peak"], 64)
        self.assertEqual(state["peak"], _rss._FEED_CONCURRENCY)


class SniffioMissingImportIsCheapTest(unittest.TestCase):
    @staticmethod
    async def _library():
        from httpcore._synchronization import current_async_library
        return current_async_library()

    def test_httpcore_library_probe_does_not_rescan_sys_path(self):
        self.assertTrue("sniffio" in sys.modules,  # the http module settled it at import
                        "sniffio neither installed nor settled")
        from httpcore._synchronization import current_async_library
        if sys.modules["sniffio"] is not None:  # installed: nothing to settle, the probe just works
            self.assertEqual(asyncio.run(self._library()), "asyncio")
            return

        async def probe():
            t = time.perf_counter()
            for _ in range(500):
                self.assertEqual(current_async_library(), "asyncio")
            return (time.perf_counter() - t) / 500

        per_call = asyncio.run(probe())
        # A failed import that walks sys.path costs ~50us per call; the settled one ~0.4us.
        self.assertLess(per_call, 10e-6)
        with self.assertRaises(ImportError):
            importlib.import_module("sniffio")
        self.assertIsNotNone(http)


if __name__ == "__main__":
    unittest.main()
