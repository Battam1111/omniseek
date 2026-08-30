"""wikidata_wikipedia, v2ex, huggingface_hub and github_awesome_phd fan out concurrently.

Batch 1 of the serial-egress audit's remaining twelve (2026-08-30), following the
gov_open_data / mastodon / europepmc repairs. Each of these four awaited several INDEPENDENT
external targets one after another inside its async twin, which is the path fetcher dispatches to
whenever an adapter has asearch, so each source's latency was the SUM of its targets:

  * wikidata_wikipedia: TWO defects. `_araw_fetch` awaited en.wikipedia.org then www.wikidata.org
    (different hosts, neither reads the other), and `_afetch_articles` then pulled the REST
    summaries one title at a time (five of them at limit=10). Longest serial chain in the audit.
  * v2ex: six node listings, the largest target count of any fan-out in OmniSeek.
  * huggingface_hub: /api/models, /api/datasets, /api/spaces, three independent queries.
  * github_awesome_phd: six repo READMEs (24h cached, so only a cold start paid full price).

The timing tests assert on WALL CLOCK, deliberately. A structural test that counts gather calls
keeps passing the moment someone reintroduces an await inside the loop, and that is exactly how
gov_open_data regressed after its sync twin had already been fixed. Everything else here holds the
invariant that going concurrent changed ONLY the latency: same targets, same request count, same
result order, same degrade behaviour.

What must STAY serial and is asserted as such at the bottom: wikidata's `_afetch_entities` ->
`_afetch_key_claims` -> `_aresolve_labels` chain (each call needs the previous one's entity id /
claim QIDs) and github_awesome_phd's `_afetch_readme` main-then-master fallback.
"""
import asyncio
import inspect
import time
import unittest
from unittest import mock

from omniseek.core.sources.api import huggingface_hub_source as hf
from omniseek.core.sources.scrape import github_awesome_phd_source as gap
from omniseek.core.sources.scrape import v2ex_source as v2ex
from omniseek.core.sources.scrape import wikidata_wikipedia_source as wiki

# Per-target delay for the TWO-target wall-clock tests. Two targets is the least forgiving ratio
# this assertion ever sees: concurrency saves exactly one target's latency while the scheduling
# overhead is roughly fixed, so at 0.1s the gap between the concurrent time and the 0.6*serial
# threshold was under 10ms — inside Windows' ~16ms timer granularity, i.e. the assertion was
# measuring host jitter rather than the code. It flaked once on an otherwise clean run. At 0.3s the
# same 0.6 threshold leaves ~44ms. The 0.6 criterion itself is UNCHANGED and must stay: it is the
# entire point of these tests. Three-or-more-target tests keep 0.1s, where the margin is already
# 80ms or better. (Same manoeuvre and same numbers as batch2's PAIR_DELAY / TRIO_DELAY.)
PAIR_DELAY = 0.3   # two-target: serial 0.6s, threshold 0.36s, concurrent ~0.31s


# ===========================================================================
# wikidata_wikipedia, defect 3a: the two LAYERS (two different hosts) in _araw_fetch
# ===========================================================================
class WikidataLayerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = wiki.WikidataWikipediaAdapter()

    @staticmethod
    def _articles():
        return [{"title": "Article", "extract": "x", "pageid": 1}]

    @staticmethod
    def _entities():
        return [{"id": "Q1", "label": "Entity", "description": "d"}]

    async def test_the_two_layers_are_fetched_concurrently(self):
        """Wikipedia and Wikidata are unrelated hosts; two PAIR_DELAY-slow layers must beat their
        serial sum. See PAIR_DELAY on why two targets needs the longer delay."""
        async def slow_articles(query, n):
            await asyncio.sleep(PAIR_DELAY)
            return self._articles()

        async def slow_entities(query, n):
            await asyncio.sleep(PAIR_DELAY)
            return self._entities()

        with mock.patch.object(self.adapter, "_afetch_articles", slow_articles), \
             mock.patch.object(self.adapter, "_afetch_entities", slow_entities):
            started = time.perf_counter()
            raw = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(raw["articles"]), 1)
        self.assertEqual(len(raw["entities"]), 1)
        serial = PAIR_DELAY * 2
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_layers_are_not_swapped_when_the_article_layer_is_slowest(self):
        """Order restoration for a two-target fan-out: each layer must still land under its own
        key, and _to_documents must still emit every article before every entity."""
        async def slow_articles(query, n):
            await asyncio.sleep(0.05)
            return self._articles()

        async def fast_entities(query, n):
            return self._entities()

        with mock.patch.object(self.adapter, "_afetch_articles", slow_articles), \
             mock.patch.object(self.adapter, "_afetch_entities", fast_entities):
            raw = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([a["title"] for a in raw["articles"]], ["Article"])
        self.assertEqual([e["id"] for e in raw["entities"]], ["Q1"])
        docs = self.adapter._to_documents(raw, "q", 10)
        self.assertEqual([d.title for d in docs], ["Article", "Entity"])

    async def test_one_layer_coming_back_empty_keeps_the_other(self):
        async def no_articles(query, n):
            return []

        async def ok_entities(query, n):
            return self._entities()

        with mock.patch.object(self.adapter, "_afetch_articles", no_articles), \
             mock.patch.object(self.adapter, "_afetch_entities", ok_entities):
            raw = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(raw["articles"], [])
        self.assertEqual(len(raw["entities"]), 1)

    async def test_one_layer_raising_keeps_the_other(self):
        """Both helpers degrade to [] on a request failure, so a raise is unexpected by
        construction. It must still not take the healthy layer down with it."""
        async def angry_articles(query, n):
            raise RuntimeError("wikipedia exploded")

        async def ok_entities(query, n):
            return self._entities()

        with mock.patch.object(self.adapter, "_afetch_articles", angry_articles), \
             mock.patch.object(self.adapter, "_afetch_entities", ok_entities):
            raw = await self.adapter._araw_fetch("q", 10)

        self.assertEqual(raw["articles"], [])
        self.assertEqual(len(raw["entities"]), 1)

    async def test_both_layers_empty_returns_none(self):
        """The None contract the base degrades to []. Unchanged from serial."""
        async def nothing(query, n):
            return []

        with mock.patch.object(self.adapter, "_afetch_articles", nothing), \
             mock.patch.object(self.adapter, "_afetch_entities", nothing):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_both_layers_raising_returns_none(self):
        async def angry(query, n):
            raise RuntimeError("both hosts exploded")

        with mock.patch.object(self.adapter, "_afetch_articles", angry), \
             mock.patch.object(self.adapter, "_afetch_entities", angry):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_call_per_layer(self):
        """Concurrency must not become extra requests."""
        calls: list[str] = []

        async def count_articles(query, n):
            calls.append("articles")
            return self._articles()

        async def count_entities(query, n):
            calls.append("entities")
            return self._entities()

        with mock.patch.object(self.adapter, "_afetch_articles", count_articles), \
             mock.patch.object(self.adapter, "_afetch_entities", count_entities):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), ["articles", "entities"])


# ===========================================================================
# wikidata_wikipedia, defect 3b: the per-TITLE REST summaries in _afetch_articles
# ===========================================================================
class WikidataArticleSummaryConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    N_TITLES = 5  # what limit=10 produces: n_articles = 10 - 10 // 2

    def setUp(self):
        self.adapter = wiki.WikidataWikipediaAdapter()
        self.titles = [f"Title {i}" for i in range(self.N_TITLES)]
        self.search_payload = {"query": {"search": [{"title": t} for t in self.titles]}}

    def _title_of(self, url: str) -> str:
        """The REST summary URL ends in the quoted, underscored title."""
        tail = url[len(wiki.WIKI_REST_SUMMARY):]
        return next(t for t in self.titles if t.replace(" ", "_") == tail)

    async def test_summaries_are_fetched_concurrently(self):
        """The search must stay first (it produces the titles), but five 100ms summaries behind it
        must finish well under the serial 500ms."""
        async def fake(url, **kwargs):
            if url == wiki.WIKI_API:
                return self.search_payload
            await asyncio.sleep(0.1)
            return {"title": self._title_of(url)}

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            started = time.perf_counter()
            out = await self.adapter._afetch_articles("q", self.N_TITLES)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), self.N_TITLES)
        serial = 0.1 * self.N_TITLES
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_is_restored_when_the_first_title_is_slowest(self):
        """Search-hit order is Wikipedia's relevance order and is what _to_documents emits, so the
        fastest summary answering first must not reshuffle anything."""
        delays = {t: 0.02 * (self.N_TITLES - i) for i, t in enumerate(self.titles)}

        async def fake(url, **kwargs):
            if url == wiki.WIKI_API:
                return self.search_payload
            title = self._title_of(url)
            await asyncio.sleep(delays[title])
            return {"title": title}

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            out = await self.adapter._afetch_articles("q", self.N_TITLES)

        self.assertEqual([a["title"] for a in out], self.titles)

    async def test_one_summary_failing_keeps_the_others(self):
        """A missing / disambiguation summary was skipped by the serial loop; it still is."""
        dead = self.titles[-1]
        ambiguous = self.titles[0]

        async def fake(url, **kwargs):
            if url == wiki.WIKI_API:
                return self.search_payload
            title = self._title_of(url)
            if title == dead:
                return None
            if title == ambiguous:
                return {"title": title, "type": "disambiguation"}
            return {"title": title}

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            out = await self.adapter._afetch_articles("q", self.N_TITLES)

        self.assertEqual([a["title"] for a in out], self.titles[1:-1])

    async def test_one_summary_raising_keeps_the_others(self):
        angry = self.titles[0]

        async def fake(url, **kwargs):
            if url == wiki.WIKI_API:
                return self.search_payload
            title = self._title_of(url)
            if title == angry:
                raise RuntimeError("summary exploded")
            return {"title": title}

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            out = await self.adapter._afetch_articles("q", self.N_TITLES)

        self.assertEqual([a["title"] for a in out], self.titles[1:])

    async def test_every_summary_failing_returns_empty(self):
        """_fetch_articles' contract is a list, empty on total failure (it is _araw_fetch that
        turns both-layers-empty into None). Unchanged from serial."""
        async def fake(url, **kwargs):
            return self.search_payload if url == wiki.WIKI_API else None

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            self.assertEqual(await self.adapter._afetch_articles("q", self.N_TITLES), [])

    async def test_the_search_failing_skips_the_summaries_entirely(self):
        """No titles means no summary requests: the search is a real dependency, not a target."""
        calls: list[str] = []

        async def fake(url, **kwargs):
            calls.append(url)
            return None

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            self.assertEqual(await self.adapter._afetch_articles("q", self.N_TITLES), [])

        self.assertEqual(calls, [wiki.WIKI_API])

    async def test_one_get_per_title_plus_the_one_search(self):
        """Concurrency must not become extra requests."""
        calls: list[str] = []

        async def fake(url, **kwargs):
            calls.append(url)
            if url == wiki.WIKI_API:
                return self.search_payload
            return {"title": self._title_of(url)}

        with mock.patch.object(wiki.http, "aget_json", side_effect=fake):
            await self.adapter._afetch_articles("q", self.N_TITLES)

        self.assertEqual(len(calls), self.N_TITLES + 1)
        self.assertEqual(calls.count(wiki.WIKI_API), 1)
        self.assertEqual(sorted(self._title_of(u) for u in calls if u != wiki.WIKI_API),
                         sorted(self.titles))


# ===========================================================================
# v2ex: six node listings on one host
# ===========================================================================
class V2exConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = v2ex.V2exAdapter()
        self.n = len(v2ex.NODES)
        self.assertGreaterEqual(self.n, 2, "the test is meaningless with one node")

    @staticmethod
    def _slug_of(url: str) -> str:
        return url.rsplit("node_name=", 1)[1]

    async def test_nodes_are_fetched_concurrently(self):
        """Six 100ms nodes must finish well under the serial 600ms."""
        async def slow(url, **kwargs):
            await asyncio.sleep(0.1)
            return [{"id": self._slug_of(url)}]

        with mock.patch.object(v2ex.http, "aget_json", side_effect=slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), self.n)
        serial = 0.1 * self.n
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_and_dedup_still_follow_nodes_order(self):
        """NODES order is the merge order AND the tie-break of the id dedup: a topic cross-posted
        to several nodes is attributed to the earliest node. The fastest node answering first must
        not change either."""
        delays = {slug: 0.02 * (self.n - i) for i, slug in enumerate(v2ex.NODES)}  # first is SLOWEST

        async def varied(url, **kwargs):
            slug = self._slug_of(url)
            await asyncio.sleep(delays[slug])
            return [{"id": slug}, {"id": "crosspost", "seen_on": slug}]

        with mock.patch.object(v2ex.http, "aget_json", side_effect=varied):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([t["id"] for t in out if t["id"] != "crosspost"], list(v2ex.NODES))
        cross = [t for t in out if t["id"] == "crosspost"]
        self.assertEqual(len(cross), 1, "the id dedup must still collapse the cross-posted topic")
        self.assertEqual(cross[0]["seen_on"], v2ex.NODES[0],
                         "the first node in NODES must still win the dedup")

    async def test_one_node_failing_keeps_the_others(self):
        dead = v2ex.NODES[-1]

        async def one_dead(url, **kwargs):
            slug = self._slug_of(url)
            return None if slug == dead else [{"id": slug}]

        with mock.patch.object(v2ex.http, "aget_json", side_effect=one_dead):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([t["id"] for t in out], [s for s in v2ex.NODES if s != dead])

    async def test_one_node_raising_keeps_the_others(self):
        angry = v2ex.NODES[0]

        async def one_raises(url, **kwargs):
            slug = self._slug_of(url)
            if slug == angry:
                raise RuntimeError("node exploded")
            return [{"id": slug}]

        with mock.patch.object(v2ex.http, "aget_json", side_effect=one_raises):
            out = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([t["id"] for t in out], [s for s in v2ex.NODES if s != angry])

    async def test_every_node_failing_returns_none(self):
        """The None contract the base degrades to []. Unchanged from serial."""
        async def all_dead(url, **kwargs):
            return None

        with mock.patch.object(v2ex.http, "aget_json", side_effect=all_dead):
            self.assertIsNone(await self.adapter._araw_fetch("q", 10))

    async def test_one_get_per_node(self):
        """Concurrency must not become extra requests."""
        calls: list[str] = []

        async def counting(url, **kwargs):
            calls.append(self._slug_of(url))
            return [{"id": self._slug_of(url)}]

        with mock.patch.object(v2ex.http, "aget_json", side_effect=counting):
            await self.adapter._araw_fetch("q", 10)

        self.assertEqual(sorted(calls), sorted(v2ex.NODES))


# ===========================================================================
# huggingface_hub: models / datasets / spaces
# ===========================================================================
class HuggingFaceHubConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    KINDS = ("models", "datasets", "spaces")

    def setUp(self):
        self.adapter = hf.HuggingFaceHubAdapter()

    @staticmethod
    def _kind_of(url: str) -> str:
        return url.rsplit("/", 1)[1]

    async def test_categories_are_fetched_concurrently(self):
        """Three 100ms listings must finish well under the serial 300ms."""
        async def slow(url, **kwargs):
            await asyncio.sleep(0.1)
            return [{"id": f"acme/{self._kind_of(url)}", "downloads": 5}]

        with mock.patch.object(hf.http, "aget_json", side_effect=slow):
            started = time.perf_counter()
            pairs = await self.adapter._araw_fetch("q", 10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(pairs), len(self.KINDS))
        serial = 0.1 * len(self.KINDS)
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_category_order_is_restored_when_spaces_answers_first(self):
        """Equal download counts leave the stable sort's tie-break as the only ordering, and that
        tie-break IS models -> datasets -> spaces (the docstring says so). The fastest category
        answering first must not reshuffle it."""
        delays = {k: 0.02 * (len(self.KINDS) - i) for i, k in enumerate(self.KINDS)}

        async def varied(url, **kwargs):
            kind = self._kind_of(url)
            await asyncio.sleep(delays[kind])
            return [{"id": f"acme/{kind}", "downloads": 5}]

        with mock.patch.object(hf.http, "aget_json", side_effect=varied):
            pairs = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([k for _, k in pairs], list(self.KINDS))

    async def test_downloads_desc_sort_still_wins_over_category_order(self):
        """The sort is the contract, the category order is only its tie-break."""
        downloads = {"models": 1, "datasets": 99, "spaces": 50}

        async def varied(url, **kwargs):
            kind = self._kind_of(url)
            return [{"id": f"acme/{kind}", "downloads": downloads[kind]}]

        with mock.patch.object(hf.http, "aget_json", side_effect=varied):
            pairs = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([k for _, k in pairs], ["datasets", "spaces", "models"])

    async def test_one_category_failing_keeps_the_others(self):
        dead = "datasets"

        async def one_dead(url, **kwargs):
            kind = self._kind_of(url)
            return None if kind == dead else [{"id": f"acme/{kind}", "downloads": 5}]

        with mock.patch.object(hf.http, "aget_json", side_effect=one_dead):
            pairs = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([k for _, k in pairs], [k for k in self.KINDS if k != dead])

    async def test_one_category_raising_keeps_the_others(self):
        angry = "models"

        async def one_raises(url, **kwargs):
            kind = self._kind_of(url)
            if kind == angry:
                raise RuntimeError("category exploded")
            return [{"id": f"acme/{kind}", "downloads": 5}]

        with mock.patch.object(hf.http, "aget_json", side_effect=one_raises):
            pairs = await self.adapter._araw_fetch("q", 10)

        self.assertEqual([k for _, k in pairs], [k for k in self.KINDS if k != angry])

    async def test_every_category_failing_returns_an_empty_list(self):
        """This adapter's contract is a LIST (the base slices it), not None. Unchanged from serial."""
        async def all_dead(url, **kwargs):
            return None

        with mock.patch.object(hf.http, "aget_json", side_effect=all_dead):
            self.assertEqual(await self.adapter._araw_fetch("q", 10), [])

    async def test_one_get_per_category_with_the_same_per_cat_budget(self):
        """Concurrency must not become extra requests, nor a bigger per-category budget."""
        calls: list[tuple[str, int]] = []

        async def counting(url, **kwargs):
            calls.append((self._kind_of(url), kwargs["params"]["limit"]))
            return [{"id": f"acme/{self._kind_of(url)}", "downloads": 5}]

        limit = 10
        with mock.patch.object(hf.http, "aget_json", side_effect=counting):
            await self.adapter._araw_fetch("q", limit)

        per_cat = max(2, (limit // 3) + 1)
        self.assertEqual(sorted(k for k, _ in calls), sorted(self.KINDS))
        self.assertEqual({n for _, n in calls}, {per_cat})

    async def test_a_category_returning_more_than_per_cat_is_still_truncated(self):
        """The per-category cap lived in the loop body; it must survive the move into the task."""
        limit = 10
        per_cat = max(2, (limit // 3) + 1)

        async def flood(url, **kwargs):
            kind = self._kind_of(url)
            return [{"id": f"acme/{kind}/{i}", "downloads": 5} for i in range(per_cat + 7)]

        with mock.patch.object(hf.http, "aget_json", side_effect=flood):
            pairs = await self.adapter._araw_fetch("q", limit)

        # 3 categories * per_cat items, then truncated to `limit`.
        self.assertEqual(len(pairs), min(limit, per_cat * len(self.KINDS)))
        for kind in self.KINDS:
            self.assertLessEqual(sum(1 for _, k in pairs if k == kind), per_cat)


# ===========================================================================
# github_awesome_phd: six repo READMEs
# ===========================================================================
class GithubAwesomePhdConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    QUERY = "alpha"

    def setUp(self):
        self.adapter = gap.GithubAwesomePhDAdapter()
        self.n = len(gap.AWESOME_REPOS)
        self.assertGreaterEqual(self.n, 2, "the test is meaningless with one repo")
        self.repos = [f"{owner}/{repo}" for owner, repo, _ in gap.AWESOME_REPOS]

    @staticmethod
    def _readme(owner: str, repo: str) -> str:
        """One bullet, identical term count in every repo, so the stable sort's ONLY ordering
        input is the AWESOME_REPOS order the docs were appended in."""
        return f"- [alpha guide](https://example.com/{owner}/{repo}) alpha notes"

    async def test_repos_are_fetched_concurrently(self):
        """Six 100ms READMEs must finish well under the serial 600ms."""
        async def slow(owner, repo):
            await asyncio.sleep(0.1)
            return self._readme(owner, repo)

        with mock.patch.object(self.adapter, "_afetch_readme", slow):
            started = time.perf_counter()
            docs = await self.adapter.asearch(self.QUERY, limit=50)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(docs), self.n)
        serial = 0.1 * self.n
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_is_restored_when_the_first_repo_is_slowest(self):
        """Equal scores leave the final stable sort a no-op, so the doc order IS the fetch order,
        and that must stay AWESOME_REPOS order however the READMEs arrive."""
        delays = {r: 0.02 * (self.n - i) for i, r in enumerate(self.repos)}

        async def varied(owner, repo):
            await asyncio.sleep(delays[f"{owner}/{repo}"])
            return self._readme(owner, repo)

        with mock.patch.object(self.adapter, "_afetch_readme", varied):
            docs = await self.adapter.asearch(self.QUERY, limit=50)

        self.assertEqual([d.metadata["repo"] for d in docs], self.repos)

    async def test_one_repo_failing_keeps_the_others(self):
        dead = gap.AWESOME_REPOS[-1]

        async def one_dead(owner, repo):
            if (owner, repo) == (dead[0], dead[1]):
                return None
            return self._readme(owner, repo)

        with mock.patch.object(self.adapter, "_afetch_readme", one_dead):
            docs = await self.adapter.asearch(self.QUERY, limit=50)

        self.assertEqual([d.metadata["repo"] for d in docs],
                         [r for r in self.repos if r != f"{dead[0]}/{dead[1]}"])

    async def test_one_repo_raising_keeps_the_others(self):
        angry = gap.AWESOME_REPOS[0]

        async def one_raises(owner, repo):
            if (owner, repo) == (angry[0], angry[1]):
                raise RuntimeError("readme fetch exploded")
            return self._readme(owner, repo)

        with mock.patch.object(self.adapter, "_afetch_readme", one_raises):
            docs = await self.adapter.asearch(self.QUERY, limit=50)

        self.assertEqual([d.metadata["repo"] for d in docs],
                         [r for r in self.repos if r != f"{angry[0]}/{angry[1]}"])

    async def test_every_repo_failing_returns_an_empty_list(self):
        """asearch's contract is a list of docs, empty when nothing was readable."""
        async def all_dead(owner, repo):
            return None

        with mock.patch.object(self.adapter, "_afetch_readme", all_dead):
            self.assertEqual(await self.adapter.asearch(self.QUERY, limit=50), [])

    async def test_a_termless_query_still_fetches_nothing(self):
        """The early return is BEFORE the fan-out: a query with no usable terms must stay free."""
        calls: list[str] = []

        async def counting(owner, repo):
            calls.append(f"{owner}/{repo}")
            return self._readme(owner, repo)

        with mock.patch.object(self.adapter, "_afetch_readme", counting):
            self.assertEqual(await self.adapter.asearch("!", limit=50), [])

        self.assertEqual(calls, [])

    async def test_one_fetch_per_repo(self):
        """Concurrency must not become extra requests."""
        calls: list[str] = []

        async def counting(owner, repo):
            calls.append(f"{owner}/{repo}")
            return self._readme(owner, repo)

        with mock.patch.object(self.adapter, "_afetch_readme", counting):
            await self.adapter.asearch(self.QUERY, limit=50)

        self.assertEqual(sorted(calls), sorted(self.repos))


# ===========================================================================
# Structural backstops
# ===========================================================================
class NoSerialRegressionTests(unittest.TestCase):
    """A cheap backstop for the exact regression shape that already happened once: an await put
    back inside the loop. The timing tests above are the real guard; this one just names the
    needle, because gov_open_data's async twin quietly went serial and nothing flagged it."""

    def test_the_repaired_fan_outs_do_not_loop_over_their_targets(self):
        for label, fn, needles in (
            ("wikidata _araw_fetch", wiki.WikidataWikipediaAdapter._araw_fetch,
             ("await self._afetch_entities(",)),
            ("wikidata _afetch_articles", wiki.WikidataWikipediaAdapter._afetch_articles,
             ("for hit in hits[:n]:",)),
            ("v2ex _araw_fetch", v2ex.V2exAdapter._araw_fetch,
             ("for slug in NODES:",)),
            ("huggingface_hub _araw_fetch", hf.HuggingFaceHubAdapter._araw_fetch,
             ('for kind in ("models", "datasets", "spaces"):',)),
            ("github_awesome_phd asearch", gap.GithubAwesomePhDAdapter.asearch,
             ("for owner, repo, desc in AWESOME_REPOS:",)),
        ):
            src = inspect.getsource(fn)
            with self.subTest(target=label):
                self.assertIn("asyncio.gather", src, "the fan-out no longer uses gather")
                for needle in needles:
                    self.assertNotIn(needle, src, "the fan-out is awaiting its targets in series again")


class MustStaySerialTests(unittest.TestCase):
    """The audit's must-stay-serial table, for the two entries that live in these files. Each of
    these is a real dependency (the next request needs the previous answer), so 'fixing' it would
    either break the result or fire requests that can never be used."""

    def test_wikidata_entity_enrichment_chain_stays_serial(self):
        """_afetch_entities needs the search's top QID before it can ask for claims, and
        _afetch_key_claims needs those claims' QIDs before it can resolve their labels."""
        for name, fn in (
            ("_afetch_entities", wiki.WikidataWikipediaAdapter._afetch_entities),
            ("_afetch_key_claims", wiki.WikidataWikipediaAdapter._afetch_key_claims),
        ):
            src = inspect.getsource(fn)
            with self.subTest(target=name):
                self.assertNotIn("asyncio.gather", src,
                                 "this chain is a dependency, not a fan-out: it must stay serial")
        self.assertIn("await self._afetch_key_claims(top_qid)",
                      inspect.getsource(wiki.WikidataWikipediaAdapter._afetch_entities))
        self.assertIn("await self._aresolve_labels(sorted(ids_to_label))",
                      inspect.getsource(wiki.WikidataWikipediaAdapter._afetch_key_claims))

    def test_github_readme_branch_fallback_stays_serial(self):
        """master is only tried BECAUSE main came back empty. Racing them would double the
        requests and pick a winner by timing."""
        src = inspect.getsource(gap.GithubAwesomePhDAdapter._afetch_readme)
        self.assertIn('for branch in ("main", "master"):', src)
        self.assertNotIn("asyncio.gather", src)


if __name__ == "__main__":
    unittest.main()
