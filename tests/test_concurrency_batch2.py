"""ml_collective, books_openlibrary_ia, tieba and hackernews fan out concurrently (2026-08-30).

Batch 2 of the serial-egress sweep. All four awaited independent external targets one after
another on the ASYNC path, which is the path the fetcher dispatches to whenever an adapter has
asearch, so each source's latency was the SUM of its targets rather than its slowest one.

Three of the four are not loops but UNROLLED consecutive awaits (two calls written out one after
the other), which is why a loop-shaped scan missed them:

  * books_openlibrary_ia : openlibrary.org + archive.org, two unrelated hosts, both slow.
  * tieba                : the thread search + the forum search, two endpoints, one host.
  * hackernews           : the story query + the comment query against the same Algolia index.
  * ml_collective        : a real loop over three index pages of one static site.

ml_collective additionally carried a comment declaring the serial fan-out DELIBERATE, to keep the
``uniq.setdefault`` dedup order byte-identical to the sync twin. The concern was real and the
conclusion was wrong: replaying the gathered results in INDEX_PATHS order preserves that order
exactly, which is what test_dedup_attribution_follows_index_paths_order_not_completion_order pins.

The timing tests are the load-bearing ones and they assert on WALL CLOCK on purpose. A structural
assertion (counting gather calls) keeps passing the moment someone reintroduces an await inside the
loop, and that is exactly how gov_open_data regressed after its sync twin had already been fixed.
"""
import asyncio
import contextlib
import time
import unittest
from unittest import mock

from omniseek.core.sources.api import hackernews_source as hn
from omniseek.core.sources.scrape import books_openlibrary_ia_source as books
from omniseek.core.sources.scrape import ml_collective_source as mlc
from omniseek.core.sources.scrape import tieba_source as tieba

# Per-target delay for the wall-clock tests. Sized so the gap between the concurrent time and the
# 0.6*serial threshold is several times the OS timer granularity (~16ms on Windows), otherwise the
# assertion measures the host's jitter rather than the code. The two-target sources need the longer
# delay because their concurrent/serial ratio is the least forgiving one there is.
PAIR_DELAY = 0.3   # two-target sources:   serial 0.6s, threshold 0.36s, concurrent ~0.31s
TRIO_DELAY = 0.1   # three-target sources: serial 0.3s, threshold 0.18s, concurrent ~0.11s


@contextlib.contextmanager
def no_doc_cache(mod):
    """Neutralise the adapter's own disk doc-cache so a test never reads or writes real cache.

    Only for the two adapters whose fan-out lives INSIDE asearch (ml_collective, hackernews);
    the other two are tested at _araw_fetch, below the cache."""
    with mock.patch.object(mod.cache, "get_docs", lambda key: None), \
         mock.patch.object(mod.cache, "set_docs", lambda *a, **k: None):
        yield


# ══════════════════════════════════════════════════════════════════ ml_collective (3 index pages)
class MLCollectiveConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Fan-out over INDEX_PATHS inside asearch.

    Note the failure contract differs from the other three: _ascrape_index degrades to [] rather
    than None, and asearch returns a doc LIST, so 'everything failed' is an empty list, not None.
    """

    def setUp(self):
        self.adapter = mlc.MLCollectiveAdapter()
        self.paths = list(mlc.INDEX_PATHS)
        self.assertGreaterEqual(len(self.paths), 2, "the test is meaningless with one index page")

    @staticmethod
    def _row(path, slug, date=None):
        return {"url": f"https://mlcollective.org{path}{slug}/", "title": f"Research Jam {slug}",
                "date": date, "source_path": path}

    async def _asearch(self, fake_index, query="jam", limit=10):
        with no_doc_cache(mlc), mock.patch.object(self.adapter, "_ascrape_index", fake_index):
            return await self.adapter.asearch(query, limit)

    async def test_index_pages_are_fetched_concurrently(self):
        """Three TRIO_DELAY-slow index pages must finish well under their serial sum."""
        async def slow(path):
            await asyncio.sleep(TRIO_DELAY)
            return [self._row(path, "a")]

        started = time.perf_counter()
        docs = await self._asearch(slow)
        elapsed = time.perf_counter() - started

        self.assertEqual(len(docs), len(self.paths))
        serial = TRIO_DELAY * len(self.paths)
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_dedup_attribution_follows_index_paths_order_not_completion_order(self):
        """THE test the old 'keep it sequential' comment was written for.

        uniq.setdefault gives a shared URL to whichever index page claims it FIRST, so if the
        gather leaked completion order into the replay, the fastest page would steal the
        attribution. Here the FIRST path is the SLOWEST, and it must still win."""
        shared = {"url": "https://mlcollective.org/abs/shared/", "title": "Research Jam shared",
                  "date": None}
        delays = {p: 0.02 * (len(self.paths) - i) for i, p in enumerate(self.paths)}  # first SLOWEST

        async def varied(path):
            await asyncio.sleep(delays[path])
            return [{**shared, "source_path": path}, self._row(path, path.strip("/"))]

        docs = await self._asearch(varied)

        self.assertEqual(docs[0].url, shared["url"])
        self.assertEqual(docs[0].metadata["index_path"], self.paths[0],
                         "the shared URL was attributed to the FASTEST page, not the first one")
        # ...and the rest keep INDEX_PATHS order too (undated items sort stably).
        self.assertEqual([d.metadata["index_path"] for d in docs[1:]], self.paths)

    async def test_one_index_page_yielding_nothing_keeps_the_others(self):
        dead = self.paths[-1]

        async def one_dead(path):
            return [] if path == dead else [self._row(path, "a")]

        docs = await self._asearch(one_dead)

        self.assertEqual(len(docs), len(self.paths) - 1)
        self.assertNotIn(dead, [d.metadata["index_path"] for d in docs])

    async def test_one_index_page_raising_keeps_the_others(self):
        """_ascrape_index swallows a dead page into [], so a raise is unexpected by construction.
        It must still not take the healthy pages down."""
        angry = self.paths[0]

        async def one_raises(path):
            if path == angry:
                raise RuntimeError("index page exploded")
            return [self._row(path, "a")]

        docs = await self._asearch(one_raises)

        self.assertEqual(len(docs), len(self.paths) - 1)
        self.assertNotIn(angry, [d.metadata["index_path"] for d in docs])

    async def test_every_index_page_failing_returns_no_docs(self):
        """This adapter has no None contract: an all-miss is an empty doc list. Unchanged."""
        async def all_dead(path):
            return []

        self.assertEqual(await self._asearch(all_dead), [])

    async def test_every_index_page_raising_returns_no_docs(self):
        async def all_raise(path):
            raise RuntimeError("everything exploded")

        self.assertEqual(await self._asearch(all_raise), [])

    async def test_one_fetch_per_index_page(self):
        """Concurrency must not become extra requests."""
        calls = []

        async def counting(path):
            calls.append(path)
            return [self._row(path, "a")]

        await self._asearch(counting)
        self.assertEqual(sorted(calls), sorted(self.paths))


# ══════════════════════════════════════════════════════ books_openlibrary_ia (2 unrelated hosts)
class BooksOpenLibraryIAConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = books.BooksOpenLibraryIAAdapter()

    @staticmethod
    def _surface_of(url):
        return "ol" if url == books.OL_URL else "ia"

    async def test_both_surfaces_are_fetched_concurrently(self):
        """Two PAIR_DELAY-slow surfaces must finish well under their serial sum."""
        async def slow(url, **kwargs):
            await asyncio.sleep(PAIR_DELAY)
            return {"surface": self._surface_of(url)}

        with mock.patch.object(books.http, "aget_json", side_effect=slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(out["ol"], {"surface": "ol"})
        self.assertEqual(out["ia"], {"surface": "ia"})
        serial = PAIR_DELAY * 2
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_the_two_results_are_not_swapped_when_archive_answers_first(self):
        """Unrolled awaits have no target list to restore against, so the failure mode is the two
        payloads landing in each other's slot. Open Library is made the SLOW one here."""
        async def varied(url, **kwargs):
            surface = self._surface_of(url)
            await asyncio.sleep(0.04 if surface == "ol" else 0.0)
            return {"surface": surface}

        with mock.patch.object(books.http, "aget_json", side_effect=varied):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(out["ol"], {"surface": "ol"})
        self.assertEqual(out["ia"], {"surface": "ia"})
        # the budget split is computed before either call and must survive untouched
        self.assertEqual((out["ol_n"], out["ia_n"]), (5, 5))

    async def test_one_surface_failing_keeps_the_other(self):
        for dead in ("ol", "ia"):
            with self.subTest(dead=dead):
                async def one_dead(url, **kwargs):
                    surface = self._surface_of(url)
                    return None if surface == dead else {"surface": surface}

                with mock.patch.object(books.http, "aget_json", side_effect=one_dead):
                    out = await self.adapter._araw_fetch("q", 10)

                alive = "ia" if dead == "ol" else "ol"
                self.assertIsNone(out[dead])
                self.assertEqual(out[alive], {"surface": alive})

    async def test_one_surface_raising_keeps_the_other(self):
        """aget_json returns None on failure, so a raise is unexpected by construction; it must
        degrade to that same None instead of losing the healthy surface."""
        for angry in ("ol", "ia"):
            with self.subTest(angry=angry):
                async def one_raises(url, **kwargs):
                    surface = self._surface_of(url)
                    if surface == angry:
                        raise RuntimeError("surface exploded")
                    return {"surface": surface}

                with mock.patch.object(books.http, "aget_json", side_effect=one_raises):
                    out = await self.adapter._araw_fetch("q", 10)

                alive = "ia" if angry == "ol" else "ol"
                self.assertIsNone(out[angry])
                self.assertEqual(out[alive], {"surface": alive})

    async def test_both_surfaces_failing_returns_none(self):
        """The None contract the base degrades to [] on. Unchanged from serial."""
        async def all_dead(url, **kwargs):
            return None

        with mock.patch.object(books.http, "aget_json", side_effect=all_dead):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_both_surfaces_raising_returns_none(self):
        async def all_raise(url, **kwargs):
            raise RuntimeError("both exploded")

        with mock.patch.object(books.http, "aget_json", side_effect=all_raise):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_get_per_surface(self):
        calls = []

        async def counting(url, **kwargs):
            calls.append(self._surface_of(url))
            return {"surface": self._surface_of(url)}

        with mock.patch.object(books.http, "aget_json", side_effect=counting):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), ["ia", "ol"])


# ═══════════════════════════════════════════════════════════ tieba (2 endpoints, same host)
class TiebaConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = tieba.TiebaAdapter()

    @staticmethod
    def _endpoint_of(url):
        return "threads" if url == tieba.THREAD_URL else "forums"

    async def test_both_endpoints_are_fetched_concurrently(self):
        """Two PAIR_DELAY-slow endpoints must finish well under their serial sum.

        Same host, so 'gently' is a fair question to ask: gently means two GETs per search, which
        is what this issues either way, and the adapter carries no rate limit or politeness gap."""
        async def slow(url, params):
            await asyncio.sleep(PAIR_DELAY)
            return {"no": 0, "endpoint": self._endpoint_of(url)}

        with mock.patch.object(self.adapter, "_aget_json", slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(out["threads"]["endpoint"], "threads")
        self.assertEqual(out["forums"]["endpoint"], "forums")
        serial = PAIR_DELAY * 2
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_the_two_results_are_not_swapped_when_the_forum_search_answers_first(self):
        """The thread search is made the SLOW one: its payload must still land under 'threads'."""
        async def varied(url, params):
            endpoint = self._endpoint_of(url)
            await asyncio.sleep(0.04 if endpoint == "threads" else 0.0)
            return {"no": 0, "endpoint": endpoint}

        with mock.patch.object(self.adapter, "_aget_json", varied):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(out["threads"]["endpoint"], "threads")
        self.assertEqual(out["forums"]["endpoint"], "forums")

    async def test_one_endpoint_failing_keeps_the_other(self):
        for dead in ("threads", "forums"):
            with self.subTest(dead=dead):
                async def one_dead(url, params):
                    endpoint = self._endpoint_of(url)
                    return None if endpoint == dead else {"no": 0, "endpoint": endpoint}

                with mock.patch.object(self.adapter, "_aget_json", one_dead):
                    out = await self.adapter._araw_fetch("q", 10)

                alive = "forums" if dead == "threads" else "threads"
                self.assertIsNone(out[dead])
                self.assertEqual(out[alive]["endpoint"], alive)

    async def test_one_endpoint_raising_keeps_the_other(self):
        """_aget_json swallows its own failures into None, so a raise is unexpected by
        construction; it must not cost us the other endpoint's payload."""
        for angry in ("threads", "forums"):
            with self.subTest(angry=angry):
                async def one_raises(url, params):
                    endpoint = self._endpoint_of(url)
                    if endpoint == angry:
                        raise RuntimeError("endpoint exploded")
                    return {"no": 0, "endpoint": endpoint}

                with mock.patch.object(self.adapter, "_aget_json", one_raises):
                    out = await self.adapter._araw_fetch("q", 10)

                alive = "forums" if angry == "threads" else "threads"
                self.assertIsNone(out[angry])
                self.assertEqual(out[alive]["endpoint"], alive)

    async def test_both_endpoints_failing_returns_none(self):
        async def all_dead(url, params):
            return None

        with mock.patch.object(self.adapter, "_aget_json", all_dead):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_both_endpoints_raising_returns_none(self):
        async def all_raise(url, params):
            raise RuntimeError("both exploded")

        with mock.patch.object(self.adapter, "_aget_json", all_raise):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_get_per_endpoint(self):
        calls = []

        async def counting(url, params):
            calls.append(self._endpoint_of(url))
            return {"no": 0, "endpoint": self._endpoint_of(url)}

        with mock.patch.object(self.adapter, "_aget_json", counting):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), ["forums", "threads"])


# ══════════════════════════════════════════ hackernews (2 Algolia queries, stories + comments)
class HackerNewsConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Fan-out inside asearch. The observable is the DOC list, so the order contract here is
    'stories first, then comments' rather than a payload slot."""

    def setUp(self):
        self.adapter = hn.HackerNewsAdapter()

    @staticmethod
    def _layer_of(kwargs):
        return kwargs["params"]["tags"]

    @staticmethod
    def _hits(layer, n=2):
        if layer == "story":
            return {"hits": [{"objectID": f"s{i}", "title": f"story {i}", "points": 10,
                              "num_comments": 3} for i in range(n)]}
        return {"hits": [{"objectID": f"c{i}", "story_title": f"story for comment {i}",
                          "comment_text": f"<p>comment {i}</p>"} for i in range(n)]}

    async def _asearch(self, fake, query="q", limit=10):
        with no_doc_cache(hn), mock.patch.object(hn.http, "aget_json", side_effect=fake):
            return await self.adapter.asearch(query, limit)

    async def test_both_layers_are_queried_concurrently(self):
        """Two PAIR_DELAY-slow Algolia queries must finish well under their serial sum."""
        async def slow(url, **kwargs):
            await asyncio.sleep(PAIR_DELAY)
            return self._hits(self._layer_of(kwargs))

        started = time.perf_counter()
        docs = await self._asearch(slow)
        elapsed = time.perf_counter() - started

        self.assertEqual(len(docs), 4)
        serial = PAIR_DELAY * 2
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_stories_still_precede_comments_when_comments_answer_first(self):
        """The two mapping loops run after the gather, in the original order, so a fast comment
        reply must not jump ahead of the stories."""
        async def varied(url, **kwargs):
            layer = self._layer_of(kwargs)
            await asyncio.sleep(0.04 if layer == "story" else 0.0)
            return self._hits(layer)

        docs = await self._asearch(varied)

        self.assertEqual([bool(d.metadata.get("is_comment")) for d in docs],
                         [False, False, True, True])
        self.assertEqual([d.source_id for d in docs], ["s0", "s1", "c0", "c1"])

    async def test_one_layer_failing_keeps_the_other(self):
        for dead in ("story", "comment"):
            with self.subTest(dead=dead):
                async def one_dead(url, **kwargs):
                    layer = self._layer_of(kwargs)
                    return None if layer == dead else self._hits(layer)

                docs = await self._asearch(one_dead)

                self.assertEqual(len(docs), 2)
                self.assertTrue(all(bool(d.metadata.get("is_comment")) == (dead == "story")
                                    for d in docs))

    async def test_one_layer_raising_keeps_the_other(self):
        """aget_json returns None on failure, so a raise is unexpected by construction; it must
        degrade to that same None rather than sinking the healthy layer."""
        for angry in ("story", "comment"):
            with self.subTest(angry=angry):
                async def one_raises(url, **kwargs):
                    layer = self._layer_of(kwargs)
                    if layer == angry:
                        raise RuntimeError("layer exploded")
                    return self._hits(layer)

                docs = await self._asearch(one_raises)

                self.assertEqual(len(docs), 2)
                self.assertTrue(all(bool(d.metadata.get("is_comment")) == (angry == "story")
                                    for d in docs))

    async def test_both_layers_failing_returns_no_docs_and_caches_nothing(self):
        """The honest-empty contract: an all-miss returns [] and must NOT be cached as a result."""
        async def all_dead(url, **kwargs):
            return None

        writes = []
        with mock.patch.object(hn.cache, "get_docs", lambda key: None), \
             mock.patch.object(hn.cache, "set_docs", lambda *a, **k: writes.append(a)), \
             mock.patch.object(hn.http, "aget_json", side_effect=all_dead):
            docs = await self.adapter.asearch("q", 10)

        self.assertEqual(docs, [])
        self.assertEqual(writes, [], "an all-layer miss must not be cached")

    async def test_both_layers_raising_returns_no_docs(self):
        async def all_raise(url, **kwargs):
            raise RuntimeError("both exploded")

        self.assertEqual(await self._asearch(all_raise), [])

    async def test_one_query_per_layer(self):
        calls = []

        async def counting(url, **kwargs):
            layer = self._layer_of(kwargs)
            calls.append(layer)
            return self._hits(layer)

        await self._asearch(counting)
        self.assertEqual(sorted(calls), ["comment", "story"])

    async def test_budget_split_is_unchanged(self):
        """Concurrency must not change how many hits each layer is asked for: the split is
        computed before either call and both hitsPerPage values must match the serial version."""
        asked = {}

        async def recording(url, **kwargs):
            layer = self._layer_of(kwargs)
            asked[layer] = kwargs["params"]["hitsPerPage"]
            return self._hits(layer)

        await self._asearch(recording, limit=7)
        self.assertEqual(asked, {"story": 4, "comment": 3})  # ceil(7/2)=4 stories, 3 comments


class NoSerialRegressionTests(unittest.TestCase):
    """A cheap structural backstop for the exact regression shape that has already happened once.

    The wall-clock tests above are the real guard; this one only catches the specific way
    gov_open_data slid back (an await returned to a loop while nothing flagged it)."""

    def test_the_four_async_fan_outs_still_use_gather(self):
        import inspect
        for label, fn, banned in (
            ("ml_collective", mlc.MLCollectiveAdapter.asearch,
             "for it in await self._ascrape_index(path):"),
            ("books_openlibrary_ia", books.BooksOpenLibraryIAAdapter._araw_fetch,
             "ia = await http.aget_json("),
            ("tieba", tieba.TiebaAdapter._araw_fetch,
             "forums = await self._aget_json("),
            ("hackernews", hn.HackerNewsAdapter.asearch,
             "comment_data = await http.aget_json("),
        ):
            src = inspect.getsource(fn)
            with self.subTest(source=label):
                self.assertNotIn(banned, src, "the async fan-out went serial again")
                self.assertIn("asyncio.gather", src, "the async fan-out no longer uses gather")


if __name__ == "__main__":
    unittest.main()
