"""orcid / sec_financials / discord_communities / wechat fan out concurrently (2026-08-30).

Batch 3 of the serial-egress sweep. All four are ``explicit_only`` (named drills, never broad
fan-out), which is why they were last — but a named drill is precisely where a human is sitting
there waiting, so summing latencies hurts most visibly.

Two of the four are async and two are SYNC-ONLY (no ``asearch`` twin at all), so they cannot use
``asyncio.gather``: their fan-out is a ``ThreadPoolExecutor`` over COPIED contextvars Contexts, the
shape gov_open_data and feishu_jobs already use. The copied context is not decoration: the
per-request cache flags (fresh / cache_only) ride contextvars, and a bare worker thread drops them,
so a ``fresh=True`` request would quietly read stale cache. There is a test for exactly that below,
because it is the kind of defect nothing else would ever surface.

orcid is the one with a real trap. Its fan-out cap counts SUCCESSES, not attempts (a record that
fails to fetch is skipped and the walk moves on), so a single gather over every candidate would
fire requests the serial version never would. It is fixed the way europepmc was: each round asks
for exactly the shortfall. The budget tests below pin both numbers — records returned AND requests
made — because that is the pair a careless gather breaks.

Every concurrency test asserts on WALL CLOCK, never on "was gather called". A structural test keeps
passing the moment someone puts an await back inside the loop, which is exactly how gov_open_data
regressed the first time.
"""
import asyncio
import time
import unittest
from contextvars import copy_context
from unittest import mock

from omniseek.core import cache
from omniseek.core.sources.api import orcid_source as orc
from omniseek.core.sources.api import sec_financials_source as sec
from omniseek.core.sources.walled import discord_communities_source as disc
from omniseek.core.sources.walled import wechat_source as wx


# ────────────────────────────────────────────────────────────── shared fakes

class _Resp:
    """Minimal stand-in for an httpx.Response: a JSON payload, a body, a status."""

    def __init__(self, *, payload=None, body: bytes = b"", status: int = 200):
        self._payload = payload
        self.content = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# ═══════════════════════════════════════════════════════════════════ orcid

def _oid(i: int) -> str:
    return f"0000-0000-0000-{i:04d}"


def _search_payload(n: int) -> dict:
    return {"expanded-result": [{"orcid-id": _oid(i)} for i in range(n)]}


def _oid_from_record_url(url: str) -> str:
    """f"{API_BASE}/{oid}/record" -> oid."""
    return url[len(orc.API_BASE) + 1:-len("/record")]


class OrcidConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """The per-iD /record fan-out runs concurrently; the expanded-search that produces the iDs
    stays first and alone, because that dependency is real."""

    N = 8  # == _MAX_FANOUT, so limit=8 exercises the full fan-out

    def setUp(self):
        self.adapter = orc.OrcidAdapter()
        self.assertEqual(orc._MAX_FANOUT, self.N, "test sized against _MAX_FANOUT")

    def _mock(self, record_fn):
        """Route the expanded-search to a fixed payload and every /record to ``record_fn(oid)``."""
        async def route(url, **kwargs):
            if url == orc.SEARCH_URL:
                return _search_payload(self.N)
            return await record_fn(_oid_from_record_url(url))
        return mock.patch.object(orc.http, "aget_json", side_effect=route)

    async def test_records_are_fetched_concurrently(self):
        """Eight 100ms records must finish well under the serial 800ms."""
        async def slow(oid):
            await asyncio.sleep(0.1)
            return {"oid": oid}

        with self._mock(slow):
            started = time.perf_counter()
            out = await self.adapter._araw_fetch("q", self.N)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), self.N)
        serial = 0.1 * self.N
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_order_is_restored_when_the_first_iD_answers_last(self):
        """expanded-search relevance order IS the contract (rank stays False precisely because the
        fan-out is supposed to preserve the server's order)."""
        async def varied(oid):
            i = int(oid[-4:])
            await asyncio.sleep(0.02 * (self.N - i))  # the FIRST candidate is the SLOWEST
            return {"oid": oid}

        with self._mock(varied):
            out = await self.adapter._araw_fetch("q", self.N)

        self.assertEqual([o for o, _ in out], [_oid(i) for i in range(self.N)])

    async def test_one_record_failing_keeps_the_others(self):
        dead = _oid(2)

        async def one_dead(oid):
            return None if oid == dead else {"oid": oid}

        with self._mock(one_dead):
            out = await self.adapter._araw_fetch("q", self.N)

        self.assertEqual(len(out), self.N - 1)
        self.assertNotIn(dead, [o for o, _ in out])
        self.assertEqual([o for o, _ in out], [_oid(i) for i in range(self.N) if i != 2])

    async def test_one_record_raising_keeps_the_others(self):
        angry = _oid(0)

        async def one_raises(oid):
            if oid == angry:
                raise RuntimeError("record exploded")
            return {"oid": oid}

        with self._mock(one_raises):
            out = await self.adapter._araw_fetch("q", self.N)

        self.assertEqual(len(out), self.N - 1)
        self.assertNotIn(angry, [o for o, _ in out])

    async def test_every_record_failing_returns_an_empty_list_not_none(self):
        """None is reserved for a failed expanded-search (see below). Every RECORD failing is an
        empty batch, which is what the serial version returned too."""
        async def all_dead(oid):
            return None

        with self._mock(all_dead):
            self.assertEqual(await self.adapter._araw_fetch("q", self.N), [])

    async def test_a_failed_expanded_search_still_returns_none(self):
        """The one None in this adapter's contract, unchanged: the fan-out never even starts."""
        calls = []

        async def route(url, **kwargs):
            calls.append(url)
            return None  # search endpoint unreachable

        with mock.patch.object(orc.http, "aget_json", side_effect=route):
            self.assertIsNone(await self.adapter._araw_fetch("q", self.N))
        self.assertEqual(calls, [orc.SEARCH_URL], "no record should be fetched without an iD list")

    async def test_a_failed_record_does_not_consume_the_fan_out_budget(self):
        """The trap. The cap counts SUCCESSES: with the first two candidates dead and a cap of 3,
        the serial version attempted five candidates and returned three records. Both numbers must
        survive concurrency."""
        attempted: list[str] = []

        async def two_dead(oid):
            attempted.append(oid)
            return None if oid in (_oid(0), _oid(1)) else {"oid": oid}

        with self._mock(two_dead):
            out = await self.adapter._araw_fetch("q", 3)

        self.assertEqual(len(out), 3, "the cap counts successes, so three records are still due")
        self.assertEqual([o for o, _ in out], [_oid(2), _oid(3), _oid(4)])
        self.assertEqual(len(attempted), 5, "two misses + three hits = five attempts, as serial")
        self.assertEqual(sorted(attempted), sorted(_oid(i) for i in range(5)))

    async def test_when_every_candidate_fails_each_is_attempted_exactly_once(self):
        """The other half of the request-count contract: concurrency must not re-ask, and must not
        stop early either."""
        attempted: list[str] = []

        async def all_dead(oid):
            attempted.append(oid)
            return None

        with self._mock(all_dead):
            out = await self.adapter._araw_fetch("q", 3)

        self.assertEqual(out, [])
        self.assertEqual(sorted(attempted), sorted(_oid(i) for i in range(self.N)))
        self.assertEqual(len(attempted), len(set(attempted)), "a candidate was asked for twice")

    async def test_a_full_batch_costs_exactly_the_cap_in_requests(self):
        """No overshoot when everything succeeds: the cap is the request count, not a floor."""
        attempted: list[str] = []

        async def ok(oid):
            attempted.append(oid)
            return {"oid": oid}

        with self._mock(ok):
            out = await self.adapter._araw_fetch("q", 3)

        self.assertEqual(len(out), 3)
        self.assertEqual(len(attempted), 3, "asked for more records than the cap allows")

    async def test_duplicate_iDs_are_still_deduped_first_wins(self):
        """The pre-pass must reproduce the serial loop's ``seen`` set exactly."""
        async def route(url, **kwargs):
            if url == orc.SEARCH_URL:
                return {"expanded-result": [{"orcid-id": _oid(0)}, {"orcid-id": _oid(0)},
                                            {"orcid-id": _oid(1)}, {"not-an-id": 1}, "junk"]}
            return {"oid": _oid_from_record_url(url)}

        with mock.patch.object(orc.http, "aget_json", side_effect=route):
            out = await self.adapter._araw_fetch("q", 8)

        self.assertEqual([o for o, _ in out], [_oid(0), _oid(1)])


# ══════════════════════════════════════════════════════════ sec_financials

_IDENT = {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}
_SUBMISSIONS = {"name": "Apple Inc.", "sicDescription": "Electronic Computers",
                "filings": {"recent": {}}}
_FACTS = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
    {"end": "2025-09-27", "val": 364980000000, "fy": 2025, "fp": "FY",
     "form": "10-K", "filed": "2025-11-01"}]}}}}}


class SecFinancialsConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """submissions and the ~4MB companyfacts are two unrelated endpoints; the both-None guard in
    the code is the proof neither feeds the other."""

    def setUp(self):
        self.adapter = sec.SECFinancialsAdapter()

    @staticmethod
    def _is_submissions(url: str) -> bool:
        return "/submissions/" in url

    async def test_the_two_endpoints_are_fetched_concurrently(self):
        async def slow(url, **kwargs):
            await asyncio.sleep(0.25)
            return _SUBMISSIONS if SecFinancialsConcurrencyTests._is_submissions(url) else _FACTS

        with mock.patch.object(sec.http, "aget_json", side_effect=slow):
            started = time.perf_counter()
            doc = await self.adapter._abuild_doc(_IDENT)
            elapsed = time.perf_counter() - started

        self.assertIsNotNone(doc)
        serial = 0.25 * 2
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    async def test_the_replies_are_not_swapped_when_they_arrive_out_of_order(self):
        """The 2-way unpack IS this source's ordering contract: the slow submissions reply must
        still land in ``sub`` and the fast companyfacts in ``facts``, never the other way round."""
        async def varied(url, **kwargs):
            if SecFinancialsConcurrencyTests._is_submissions(url):
                await asyncio.sleep(0.05)          # the FIRST target is the SLOWEST
                return _SUBMISSIONS
            return _FACTS

        with mock.patch.object(sec.http, "aget_json", side_effect=varied):
            doc = await self.adapter._abuild_doc(_IDENT)

        self.assertEqual(doc.author, "Apple Inc.")                    # from submissions
        self.assertIn("Electronic Computers", doc.content)            # from submissions
        self.assertTrue(doc.metadata["raw_facts_available"])          # from companyfacts
        self.assertIn("Total assets", doc.content)                    # from companyfacts

    async def test_one_endpoint_failing_still_yields_a_doc(self):
        for dead in ("submissions", "companyfacts"):
            with self.subTest(dead=dead):
                async def one_dead(url, **kwargs):
                    is_sub = SecFinancialsConcurrencyTests._is_submissions(url)
                    if (dead == "submissions") == is_sub:
                        return None
                    return _FACTS if not is_sub else _SUBMISSIONS

                with mock.patch.object(sec.http, "aget_json", side_effect=one_dead):
                    doc = await self.adapter._abuild_doc(_IDENT)

                self.assertIsNotNone(doc, "one dead endpoint must not sink the other's answer")
                self.assertEqual(doc.metadata["raw_facts_available"], dead == "submissions")

    async def test_one_endpoint_raising_still_yields_a_doc(self):
        async def facts_explode(url, **kwargs):
            if not SecFinancialsConcurrencyTests._is_submissions(url):
                raise RuntimeError("companyfacts exploded")
            return _SUBMISSIONS

        with mock.patch.object(sec.http, "aget_json", side_effect=facts_explode):
            doc = await self.adapter._abuild_doc(_IDENT)

        self.assertIsNotNone(doc)
        self.assertEqual(doc.author, "Apple Inc.")
        self.assertFalse(doc.metadata["raw_facts_available"])

    async def test_both_endpoints_failing_returns_none(self):
        """The do-not-fabricate contract, unchanged."""
        async def all_dead(url, **kwargs):
            return None

        with mock.patch.object(sec.http, "aget_json", side_effect=all_dead):
            self.assertIsNone(await self.adapter._abuild_doc(_IDENT))

    async def test_both_endpoints_raising_returns_none(self):
        async def all_explode(url, **kwargs):
            raise RuntimeError("data.sec.gov is down")

        with mock.patch.object(sec.http, "aget_json", side_effect=all_explode):
            self.assertIsNone(await self.adapter._abuild_doc(_IDENT))

    async def test_one_request_per_endpoint(self):
        """Concurrency must not become extra requests: SEC fair-access counts them."""
        calls: list[str] = []

        async def counting(url, **kwargs):
            calls.append(url)
            return _SUBMISSIONS if SecFinancialsConcurrencyTests._is_submissions(url) else _FACTS

        with mock.patch.object(sec.http, "aget_json", side_effect=counting):
            await self.adapter._abuild_doc(_IDENT)

        self.assertEqual(len(calls), 2)
        self.assertEqual(len([c for c in calls if self._is_submissions(c)]), 1)


# ═════════════════════════════════════════════════ discord_communities (sync)

_GUILDS = [{"id": f"g{i}", "name": f"server{i}"} for i in range(4)]


def _channels_of(gid: str) -> list[dict]:
    return [{"id": f"{gid}c0", "type": 0, "name": "research"},
            {"id": f"{gid}c1", "type": 2, "name": "voice"},        # dropped: not text/announcement
            {"id": f"{gid}c2", "type": 5, "name": "announce"}]


class DiscordDiscoveryConcurrencyTests(unittest.TestCase):
    """Sync-only source: no asearch twin exists, so the fan-out is a thread pool, not gather."""

    def setUp(self):
        self.adapter = disc.DiscordCommunitiesAdapter()
        self.sets: list = []
        for target, repl in (("get", lambda *a, **k: None),
                             ("set", lambda *a, **k: self.sets.append(a))):
            p = mock.patch.object(disc.cache, target, repl)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _gid_of(url: str) -> str:
        return url.split("/guilds/")[1].split("/channels")[0]

    def _mock(self, chan_fn):
        def route(url, **kwargs):
            if url.endswith("/users/@me/guilds"):
                return _Resp(payload=_GUILDS)
            return chan_fn(DiscordDiscoveryConcurrencyTests._gid_of(url))
        return mock.patch.object(disc.httpx, "get", side_effect=route)

    def test_guilds_are_listed_concurrently(self):
        """Four 100ms guilds must finish well under the serial 400ms. A thread pool drops the wall
        clock exactly like gather does."""
        def slow(gid):
            time.sleep(0.1)
            return _Resp(payload=_channels_of(gid))

        with self._mock(slow):
            started = time.perf_counter()
            out = self.adapter._discover_channels("tok")
            elapsed = time.perf_counter() - started

        self.assertEqual(len(out), len(_GUILDS) * 2)  # 2 of the 3 channel types survive
        serial = 0.1 * len(_GUILDS)
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    def test_order_is_restored_when_the_first_guild_answers_last(self):
        def varied(gid):
            time.sleep(0.02 * (len(_GUILDS) - int(gid[1:])))  # the FIRST guild is the SLOWEST
            return _Resp(payload=_channels_of(gid))

        with self._mock(varied):
            out = self.adapter._discover_channels("tok")

        self.assertEqual([c["guild_id"] for c in out],
                         [g["id"] for g in _GUILDS for _ in range(2)])
        self.assertEqual([c["channel_id"] for c in out],
                         [f"{g['id']}c{i}" for g in _GUILDS for i in (0, 2)])

    def test_the_guild_list_is_still_fetched_before_anything_else(self):
        """A real dependency: the per-guild calls need ids that only the guild list carries."""
        seen: list[str] = []

        def route(url, **kwargs):
            seen.append(url)
            if url.endswith("/users/@me/guilds"):
                return _Resp(payload=_GUILDS)
            return _Resp(payload=_channels_of(self._gid_of(url)))

        with mock.patch.object(disc.httpx, "get", side_effect=route):
            self.adapter._discover_channels("tok")

        self.assertTrue(seen[0].endswith("/users/@me/guilds"))
        self.assertEqual(len(seen), 1 + len(_GUILDS), "one call per guild, plus the listing")

    def test_a_failed_guild_list_returns_empty_without_caching(self):
        """Unchanged: an unreadable guild list is a real failure, and must not pin an empty
        discovery for 30 minutes."""
        def boom(url, **kwargs):
            raise RuntimeError("no route")

        with mock.patch.object(disc.httpx, "get", side_effect=boom):
            self.assertEqual(self.adapter._discover_channels("tok"), [])
        self.assertEqual(self.sets, [], "a dead guild list must not write the cache")

    def test_one_guild_returning_junk_keeps_the_others(self):
        dead = "g2"

        def one_junk(gid):
            return _Resp(payload={"message": "Missing Access", "code": 50001}) if gid == dead \
                else _Resp(payload=_channels_of(gid))

        with self._mock(one_junk):
            out = self.adapter._discover_channels("tok")

        self.assertEqual(len(out), (len(_GUILDS) - 1) * 2)
        self.assertNotIn(dead, [c["guild_id"] for c in out])

    def test_one_guild_raising_keeps_the_others(self):
        angry = "g0"

        def one_raises(gid):
            if gid == angry:
                raise RuntimeError("guild exploded")
            return _Resp(payload=_channels_of(gid))

        with self._mock(one_raises):
            out = self.adapter._discover_channels("tok")

        self.assertEqual(len(out), (len(_GUILDS) - 1) * 2)
        self.assertNotIn(angry, [c["guild_id"] for c in out])

    def test_every_guild_failing_returns_empty_and_still_caches(self):
        """The other half of the old contract: the guild list DID answer, so the empty result is a
        real answer and keeps its 30-minute cache."""
        def all_raise(gid):
            raise RuntimeError("guild exploded")

        with self._mock(all_raise):
            self.assertEqual(self.adapter._discover_channels("tok"), [])
        self.assertEqual(len(self.sets), 1, "a real (if empty) discovery still caches")

    def test_the_cache_flags_reach_the_worker_threads(self):
        """The quiet bug a bare thread pool introduces: fresh / cache_only ride contextvars, so a
        worker started WITHOUT a copied Context reads the defaults and a fresh=True request is
        silently served stale cache. Nothing else would ever surface this."""
        seen_fresh: list[bool] = []

        def probe_flag(gid):
            seen_fresh.append(cache._fresh_var.get())
            return _Resp(payload=_channels_of(gid))

        def run_with_fresh():
            cache.set_fresh(True)                      # contained in this copied context
            with self._mock(probe_flag):
                self.adapter._discover_channels("tok")

        copy_context().run(run_with_fresh)

        self.assertEqual(seen_fresh, [True] * len(_GUILDS),
                         "worker threads lost the per-request cache flags")


_CHANNELS = [{"server": "MyLab", "guild_id": f"g{i}", "channel_id": f"c{i}", "label": f"room{i}"}
             for i in range(5)]
_TS = "2026-08-30T00:00:00+00:00"   # IDENTICAL on every message: forces the sort into its tie case


def _messages_of(cid: str, n: int = 2) -> list[dict]:
    return [{"id": f"{cid}m{j}", "content": f"hello from {cid}", "timestamp": _TS,
             "author": {"username": "someone"}} for j in range(n)]


class DiscordChannelPullConcurrencyTests(unittest.TestCase):
    """The SECOND fan-out in this file, and the larger one: channels outnumber guilds and each pull
    asks for PER_CHANNEL messages. The audit missed it; leaving it serial next to a concurrent
    _discover_channels would also have read as a deliberate choice to the next person through."""

    def setUp(self):
        self.adapter = disc.DiscordCommunitiesAdapter()
        for target, repl in (("get", lambda *a, **k: None), ("set", lambda *a, **k: None)):
            p = mock.patch.object(disc.cache, target, repl)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.adapter, "_config", lambda: ("tok", list(_CHANNELS)))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _cid_of(url: str) -> str:
        return url.split("/channels/")[1].split("/messages")[0]

    def _mock(self, pull_fn):
        def route(url, **kwargs):
            return pull_fn(DiscordChannelPullConcurrencyTests._cid_of(url))
        return mock.patch.object(disc.httpx, "get", side_effect=route)

    def test_channels_are_pulled_concurrently(self):
        """Five 100ms channels must finish well under the serial 500ms."""
        def slow(cid):
            time.sleep(0.1)
            return _Resp(payload=_messages_of(cid))

        with self._mock(slow):
            started = time.perf_counter()
            docs = self.adapter.search("", limit=100)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(docs), len(_CHANNELS) * 2)
        serial = 0.1 * len(_CHANNELS)
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    def test_order_is_restored_when_the_first_channel_answers_last(self):
        """Every message here carries the SAME timestamp, so the newest-first sort is entirely in
        its tie case — and a stable sort's tie order is exactly the channel order the fan-out
        produced. That is what makes channel order a contract rather than an accident."""
        def varied(cid):
            time.sleep(0.02 * (len(_CHANNELS) - int(cid[1:])))   # the FIRST channel is the SLOWEST
            return _Resp(payload=_messages_of(cid))

        with self._mock(varied):
            docs = self.adapter.search("", limit=100)

        self.assertEqual([d.source_id for d in docs],
                         [f"{ch['channel_id']}m{j}" for ch in _CHANNELS for j in range(2)])

    def test_one_channel_denied_keeps_the_others(self):
        """403 = bot not in server / missing perms. _pull's own branch, unchanged."""
        dead = "c2"

        def one_denied(cid):
            return _Resp(payload=[], status=403) if cid == dead else _Resp(payload=_messages_of(cid))

        with self._mock(one_denied):
            docs = self.adapter.search("", limit=100)

        self.assertEqual(len(docs), (len(_CHANNELS) - 1) * 2)
        self.assertNotIn(dead, [d.metadata["channel_id"] for d in docs])

    def test_one_channel_raising_keeps_the_others(self):
        angry = "c0"

        def one_raises(cid):
            if cid == angry:
                raise RuntimeError("channel exploded")
            return _Resp(payload=_messages_of(cid))

        with self._mock(one_raises):
            docs = self.adapter.search("", limit=100)

        self.assertEqual(len(docs), (len(_CHANNELS) - 1) * 2)
        self.assertNotIn(angry, [d.metadata["channel_id"] for d in docs])

    def test_every_channel_failing_returns_an_empty_list(self):
        def all_raise(cid):
            raise RuntimeError("channel exploded")

        with self._mock(all_raise):
            self.assertEqual(self.adapter.search("", limit=100), [])

    def test_one_pull_per_channel(self):
        calls: list[str] = []

        def counting(cid):
            calls.append(cid)
            return _Resp(payload=_messages_of(cid))

        with self._mock(counting):
            self.adapter.search("", limit=100)

        self.assertEqual(sorted(calls), sorted(ch["channel_id"] for ch in _CHANNELS))

    def test_the_cache_flags_reach_the_worker_threads(self):
        seen_fresh: list[bool] = []

        def probe_flag(cid):
            seen_fresh.append(cache._fresh_var.get())
            return _Resp(payload=_messages_of(cid))

        def run_with_fresh():
            cache.set_fresh(True)                      # contained in this copied context
            with self._mock(probe_flag):
                self.adapter.search("", limit=100)

        copy_context().run(run_with_fresh)

        self.assertEqual(seen_fresh, [True] * len(_CHANNELS),
                         "worker threads lost the per-request cache flags")


# ══════════════════════════════════════════════════════════════ wechat (sync)

_FEEDS = [("PaperWeekly", "https://wechat2rss.xlab.app/feed/a.xml"),
          ("机器之心", "https://wechat2rss.xlab.app/feed/b.xml"),
          ("量子位", "https://wechat2rss.xlab.app/feed/c.xml"),
          ("wewerss:MP_X", "https://selfhosted.invalid/feeds/MP_X.atom")]  # a DIFFERENT host


def _rss(tag: str, n_items: int = 1) -> bytes:
    items = "".join(
        f"<item><title>post {tag}{i}</title>"
        f"<link>https://mp.weixin.qq.com/s/{tag}{i}</link>"
        f"<description>body {tag}{i}</description></item>" for i in range(n_items))
    return (f"<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>"
            f"<title>AI寒武纪</title>{items}</channel></rss>").encode("utf-8")


class WechatFeedConcurrencyTests(unittest.TestCase):
    """Sync-only source: no asearch twin exists, so the fan-out is a thread pool, not gather.
    The feeds are not even same-host once an operator adds a self-hosted wewe-rss base."""

    def setUp(self):
        self.adapter = wx.WechatAdapter()
        for target, repl in (("get", lambda *a, **k: None), ("set", lambda *a, **k: None)):
            p = mock.patch.object(wx.cache, target, repl)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.adapter, "_all_feeds", lambda: list(_FEEDS))
        p.start()
        self.addCleanup(p.stop)
        # The sogou breadth merge is a separate source and a separate concern; neutralise it so
        # these tests measure only the feed fan-out.
        import omniseek.core.fetcher as fetcher
        p = mock.patch.object(fetcher, "get_adapter", lambda name: None)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _tag_of(url: str) -> str:
        return next(name for name, u in _FEEDS if u == url)

    def _mock(self, feed_fn):
        def route(url, **kwargs):
            return feed_fn(WechatFeedConcurrencyTests._tag_of(url))
        return mock.patch.object(wx.httpx, "get", side_effect=route)

    def test_feeds_are_pulled_concurrently(self):
        """Four 100ms feeds must finish well under the serial 400ms."""
        def slow(name):
            time.sleep(0.1)
            return _Resp(body=_rss(str(_FEEDS.index(next(f for f in _FEEDS if f[0] == name)))))

        with self._mock(slow):
            started = time.perf_counter()
            docs = self.adapter.search("", limit=10)
            elapsed = time.perf_counter() - started

        self.assertEqual(len(docs), len(_FEEDS))
        serial = 0.1 * len(_FEEDS)
        self.assertLess(elapsed, serial * 0.6,
                        f"took {elapsed:.3f}s; serial would be ~{serial:.1f}s -> still sequential")

    def test_order_is_restored_when_the_first_feed_answers_last(self):
        """Feed order is what the STABLE score sort falls back on for ties, so it is a contract."""
        def varied(name):
            i = [f[0] for f in _FEEDS].index(name)
            time.sleep(0.02 * (len(_FEEDS) - i))       # the FIRST feed is the SLOWEST
            return _Resp(body=_rss(str(i)))

        with self._mock(varied):
            docs = self.adapter.search("", limit=10)

        # A wewerss:* placeholder is replaced by the feed's own <title>; every other account name
        # passes through. Both must land on the right doc.
        expected = [n if not n.startswith("wewerss:") else "AI寒武纪" for n, _ in _FEEDS]
        self.assertEqual([d.author for d in docs], expected)
        self.assertEqual([d.url for d in docs],
                         [f"https://mp.weixin.qq.com/s/{i}0" for i in range(len(_FEEDS))])

    def test_one_feed_failing_keeps_the_others(self):
        dead = _FEEDS[2][0]

        def one_dead(name):
            return _Resp(status=503) if name == dead \
                else _Resp(body=_rss(str([f[0] for f in _FEEDS].index(name))))

        with self._mock(one_dead):
            docs = self.adapter.search("", limit=10)

        self.assertEqual(len(docs), len(_FEEDS) - 1)
        self.assertNotIn(dead, [d.author for d in docs])

    def test_one_feed_raising_keeps_the_others(self):
        angry = _FEEDS[0][0]

        def one_raises(name):
            if name == angry:
                raise RuntimeError("feed exploded")
            return _Resp(body=_rss(str([f[0] for f in _FEEDS].index(name))))

        with self._mock(one_raises):
            docs = self.adapter.search("", limit=10)

        self.assertEqual(len(docs), len(_FEEDS) - 1)
        self.assertNotIn(angry, [d.author for d in docs])

    def test_every_feed_failing_returns_an_empty_list(self):
        """This source returns docs, never None: all-dead is [] and always was."""
        def all_raise(name):
            raise RuntimeError("feed exploded")

        with self._mock(all_raise):
            self.assertEqual(self.adapter.search("", limit=10), [])
        with self._mock(all_raise):
            self.assertEqual(self.adapter.search("transformer", limit=10), [])

    def test_one_get_per_feed(self):
        calls: list[str] = []

        def counting(name):
            calls.append(name)
            return _Resp(body=_rss(str([f[0] for f in _FEEDS].index(name))))

        with self._mock(counting):
            self.adapter.search("", limit=10)

        self.assertEqual(sorted(calls), sorted(n for n, _ in _FEEDS))

    def test_the_cache_flags_reach_the_worker_threads(self):
        """Same quiet bug as discord: without a copied Context the workers read the contextvar
        defaults, so a fresh=True drill would be served stale cache."""
        seen_fresh: list[bool] = []

        def probe_flag(name):
            seen_fresh.append(cache._fresh_var.get())
            return _Resp(body=_rss(str([f[0] for f in _FEEDS].index(name))))

        def run_with_fresh():
            cache.set_fresh(True)                      # contained in this copied context
            with self._mock(probe_flag):
                self.adapter.search("", limit=10)

        copy_context().run(run_with_fresh)

        self.assertEqual(seen_fresh, [True] * len(_FEEDS),
                         "worker threads lost the per-request cache flags")


# ═══════════════════════════════════════════════════════ structural backstop

class NoSerialRegressionTests(unittest.TestCase):
    """A cheap shape check under the wall-clock tests above, which remain the real guard.

    It earns its place for the two SYNC sources: `grep asyncio.gather` is the usual proof a
    fan-out was fixed, and it does not apply to them at all — a reviewer scanning for gather
    would read their absence as "unfixed" or, worse, a later editor would "restore" a loop.
    """

    def test_the_async_fan_outs_use_gather(self):
        import inspect
        for label, fn in (("orcid", orc.OrcidAdapter._araw_fetch),
                          ("sec_financials", sec.SECFinancialsAdapter._abuild_doc)):
            with self.subTest(source=label):
                src = inspect.getsource(fn)
                self.assertIn("asyncio.gather", src, "the async fan-out no longer uses gather")

    def test_the_sync_fan_outs_use_a_context_carrying_thread_pool(self):
        import inspect
        for label, fn in (("discord_communities/_discover_channels",
                           disc.DiscordCommunitiesAdapter._discover_channels),
                          ("discord_communities/search",
                           disc.DiscordCommunitiesAdapter.search),
                          ("wechat", wx.WechatAdapter.search)):
            with self.subTest(source=label):
                src = inspect.getsource(fn)
                self.assertIn("ThreadPoolExecutor", src,
                              "the sync fan-out is no longer concurrent")
                self.assertIn("copy_context", src,
                              "worker threads must run under a COPIED context or they lose the "
                              "per-request cache flags")

    def test_orcid_no_longer_awaits_a_record_inside_its_candidate_walk(self):
        """The exact regression shape: one await per candidate, back inside the loop."""
        import inspect
        src = inspect.getsource(orc.OrcidAdapter._araw_fetch)
        self.assertNotIn("for item in results:\n            if len(pairs) >= n:", src,
                         "the budget walk is egressing one candidate at a time again")


if __name__ == "__main__":
    unittest.main()
