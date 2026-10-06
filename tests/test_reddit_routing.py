"""Reddit routing by topic group, tight prefix discovery, the Arctic "cannot search" list, and the
reddit-wide browser search (spec 2026-10-05).

Offline: Arctic, the browser search and the disk cache are stubbed in every test. The criteria are
the spec's own; the routing ones read `_resolve_subreddits`, the search ones go through the public
`RedditAdapter().search` and `asearch` entry points so a branch hidden behind an upstream filter
cannot pass unnoticed.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import diag  # noqa: E402
from omniseek.core.sources.api import reddit_source as rs  # noqa: E402

SG = ["askSingapore", "singapore", "singaporefi"]
HK = ["HongKong"]
CANADA = ["AskCanada", "canada", "PersonalFinanceCanada"]
CITIES = ["toronto", "vancouver", "montreal", "Edmonton", "ottawa", "Calgary", "waterloo"]
IMMIGRATION_CA = ["ImmigrationCanada", "CanadaImmigration", "ExpressEntry"]
ACADEMIC = ["PhD", "AskAcademia", "GradSchool", "labrats"]
ML = ["MachineLearning", "compsci", "ArtificialIntelligence"]
JOBS = ["cscareerquestions", "cscareerquestionsEU", "ExperiencedDevs", "csMajors",
        "cscareerquestionsCAD"]
SLOW = '{"data":null,"error":"Timeout. Maybe slow down a bit"}'


def _lower(names):
    return {n.lower() for n in names}


def _no_discovery(*_a, **_k):
    raise AssertionError("discovery must not be called for a routed query")


class _FakeCache:
    """Dict-backed cache.get / cache.set; get_docs always misses, set_docs is recorded."""

    def __init__(self):
        self.store = {}
        self.doc_writes = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None, **_k):
        self.store[key] = value

    def get_docs(self, key):
        return None

    def set_docs(self, key, docs, ttl=None, **_k):
        self.doc_writes.append((key, list(docs)))

    def patches(self):
        return [
            mock.patch.object(rs.cache, "get", self.get),
            mock.patch.object(rs.cache, "set", self.set),
            mock.patch.object(rs.cache, "get_docs", self.get_docs),
            mock.patch.object(rs.cache, "set_docs", self.set_docs),
        ]


def _reset_breaker():
    rs._arctic_cooldown_until = 0.0
    rs._arctic_fail_streak = 0


def _run(mode, query, limit=10):
    adapter = rs.RedditAdapter()
    if mode == "sync":
        return adapter.search(query, limit)
    return asyncio.run(adapter.asearch(query, limit))


def _post(pid, sub, created):
    return {"id": pid, "title": f"t {pid}", "subreddit": sub,
            "permalink": f"/r/{sub}/comments/{pid}/x", "created_utc": created}


class RoutingGroups(unittest.TestCase):
    """Spec item 1: topic groups, fixed priority, locality filter, budget."""

    def resolve(self, query):
        with mock.patch.object(rs, "_discover_subreddits", side_effect=_no_discovery):
            return rs._resolve_subreddits(query, None)

    def test_universal_studios_singapore_goes_to_singapore_subs_only(self):
        res = self.resolve("Universal Studios Singapore tickets")
        self.assertEqual(res[:3], SG)
        self.assertFalse(_lower(res) & _lower(ACADEMIC + ML + JOBS), res)

    def test_singapore_phd_keeps_both_groups_after_the_cap(self):
        res = self.resolve("Singapore PhD stipend")
        self.assertEqual(res, SG + ACADEMIC)

    def test_offer_negotiation_goes_to_career_subs(self):
        res = self.resolve("software engineer offer negotiation")
        self.assertEqual(res[0], "cscareerquestions")
        self.assertTrue({"cscareerquestions", "ExperiencedDevs", "csMajors"} <= set(res), res)
        self.assertTrue(set(res) <= set(JOBS), res)
        self.assertFalse(_lower(res) & _lower(ACADEMIC + ML), res)

    def test_montreal_city_then_canada(self):
        res = self.resolve("Montreal apartment rent")
        self.assertEqual(res[:4], ["montreal"] + CANADA)
        self.assertNotIn("rentnerzeigenaufdinge", _lower(res))

    def test_express_entry_goes_to_canadian_immigration_first(self):
        res = self.resolve("express entry draw")
        self.assertTrue(set(IMMIGRATION_CA) <= set(res), res)
        self.assertIn("IWantOut", res)
        self.assertLess(max(res.index(s) for s in IMMIGRATION_CA), res.index("IWantOut"))
        self.assertNotIn("SingaporePR", res)
        self.assertFalse(_lower(res) & _lower(["drawing", "DrawMyTattoo"]), res)

    def test_singapore_pr_keeps_singapore_immigration_only(self):
        res = self.resolve("Singapore PR application")
        self.assertEqual(res[:3], SG)
        immigration = [s for s in res if s in {"IWantOut", "SingaporePR", *IMMIGRATION_CA}]
        self.assertEqual(immigration[0], "SingaporePR")
        self.assertFalse(set(res) & set(IMMIGRATION_CA), res)

    def test_hong_kong_phrase_and_abbreviation(self):
        self.assertIn("HongKong", self.resolve("Hong Kong MTR fare"))
        self.assertIn("HongKong", self.resolve("HK rent"))

    def test_phd_advisor_is_academic_without_local_subs(self):
        res = self.resolve("phd advisor conflict")
        self.assertEqual(res[0], "PhD")
        self.assertFalse(_lower(res) & _lower(SG + HK + CANADA + CITIES), res)

    def test_pure_finance_query_gets_finance_subs_not_academic(self):
        res = self.resolve("$NVDA earnings")
        self.assertTrue(set(rs._FINANCE_SUBS) <= set(res), res)
        self.assertFalse(_lower(res) & _lower(ACADEMIC), res)

    def test_science_alone_is_not_machine_learning(self):
        with mock.patch.object(rs, "_discover_subreddits", return_value=[]):
            res = rs._resolve_subreddits("science museum tickets", None)
        self.assertFalse(_lower(res) & _lower(ML), res)

    def test_budget_holds_for_every_routed_query(self):
        queries = [
            "Universal Studios Singapore tickets", "Singapore PhD stipend",
            "software engineer offer negotiation", "Montreal apartment rent", "express entry draw",
            "Singapore PR application", "Hong Kong MTR fare", "HK rent", "phd advisor conflict",
            "$NVDA earnings", "toronto vancouver montreal ottawa calgary job offer visa phd ai stock",
            "Singapore Hong Kong Canada PhD machine learning career immigration earnings",
        ]
        for q in queries:
            with self.subTest(q=q):
                res = self.resolve(q)
                self.assertTrue(res)
                self.assertLessEqual(len(res) * len(rs._search_tiers(q, res)), rs._ARCTIC_MAX_REQUESTS)


def _discovery_fixture(rows_by_prefix):
    def arctic(path, params, **_k):
        if path != "/subreddits/search":
            return []
        prefix = params.get("subreddit_prefix", "").lower()
        return [{"display_name": n, "subreddit_type": "public", "over18": False, "subscribers": s}
                for n, s in rows_by_prefix.get(prefix, [])]
    return arctic


class TightDiscovery(unittest.TestCase):
    """Spec item 2: discovery keeps only names equal to a content word or adjacent pair."""

    ROWS = {
        "coffee": [("Coffee", 1_500_000), ("coffeeswap", 60_000)],
        "pour": [("PourPainting", 200_000)],
        "montreal": [("montreal", 319_551), ("montrealhousing", 34_997)],
        "apartment": [("Apartmentliving", 300_000)],
        "rent": [("rentnerzeigenaufdinge", 400_000), ("Renters", 100_000)],
        "draw": [("drawing", 2_000_000), ("DrawMyTattoo", 150_000), ("DrawForMe", 80_000)],
        "express": [("expressjs", 20_000)],
        "entry": [("EntrySports", 9_000)],
        "mechanical": [("MechanicalKeyboards", 1_200_000), ("mechanicalpencils", 90_000)],
        "keyboard": [("keyboards", 300_000), ("KeyboardMemes", 10_000)],
        "switches": [("SwitchesForSale", 6_000)],
    }

    def discover(self, query):
        with mock.patch.object(rs.cache, "get", return_value=None), \
             mock.patch.object(rs.cache, "set"), \
             mock.patch.object(rs, "_arctic_get", side_effect=_discovery_fixture(self.ROWS)):
            return rs._discover_subreddits(query)

    def test_pour_over_coffee_still_finds_coffee_only(self):
        self.assertEqual(self.discover("pour over coffee"), ["Coffee"])

    def test_prefix_noise_is_not_taken(self):
        found = _lower(self.discover("Montreal apartment rent") + self.discover("express entry draw"))
        for noise in ("rentnerzeigenaufdinge", "apartmentliving", "drawing", "drawmytattoo", "renters"):
            self.assertNotIn(noise, found)

    def test_adjacent_pair_plus_s_is_taken(self):
        self.assertIn("MechanicalKeyboards", self.discover("mechanical keyboard switches"))


class _HttpStub:
    """Stub of http.get_json / aget_json for /posts/search. A 422 notes its slow-down body the way the
    real http layer does, so the real refusal detection and breaker in _arctic_get run."""

    def __init__(self, plan, delay=None):
        self.plan = plan          # sub (lower) -> list of posts, or "422"
        self.delay = delay or {}  # sub (lower) -> seconds to wait before answering
        self.calls = []

    def _answer(self, url, params):
        sub = (params or {}).get("subreddit", "").lower()
        self.calls.append(sub)
        outcome = self.plan.get(sub, [])
        if outcome == "422":
            diag.note("http.get", url=url, status=422, body=SLOW)
            return None
        return {"data": list(outcome)}

    def get_json(self, url, params=None, **_k):
        if self.delay.get((params or {}).get("subreddit", "").lower()):
            time.sleep(self.delay[(params or {}).get("subreddit", "").lower()])
        return self._answer(url, params)

    async def aget_json(self, url, params=None, **_k):
        if self.delay.get((params or {}).get("subreddit", "").lower()):
            await asyncio.sleep(self.delay[(params or {}).get("subreddit", "").lower()])
        return self._answer(url, params)

    def count(self, sub):
        return self.calls.count(sub.lower())


class ArcticCannotSearch(unittest.TestCase):
    """Spec item 3: a sub refused while its siblings answered goes on a 24 h list and to the browser."""

    def setUp(self):
        _reset_breaker()
        self.cache = _FakeCache()
        self.cdp_calls = []
        self.cdp_items = []
        for p in self.cache.patches():
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(rs, "_cdp_search", side_effect=self._cdp)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(rs, "_discover_subreddits", side_effect=_no_discovery)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(_reset_breaker)

    def _cdp(self, query, limit, subreddits=None):
        self.cdp_calls.append((query, limit, subreddits))
        return list(self.cdp_items)

    def _with_http(self, stub):
        return mock.patch.multiple(rs.http, get_json=stub.get_json, aget_json=stub.aget_json)

    def test_refused_sub_is_listed_without_moving_the_breaker(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                self.setUp_mode()
                stub = _HttpStub({"asksingapore": [], "singaporefi": [], "singapore": "422"},
                                 delay={"singapore": 0.05})
                with self._with_http(stub):
                    _run(mode, "Universal Studios Singapore tickets", 10)
                self.assertEqual(stub.count("singapore"), 1, stub.calls)
                self.assertGreater(stub.count("askSingapore"), 1, stub.calls)
                self.assertEqual(rs._arctic_fail_streak, 0)
                self.assertFalse(rs._arctic_cooling())
                self.assertEqual(len(self.cdp_calls), 1)
                self.assertEqual(self.cdp_calls[0][2], SG)
                later = _HttpStub({"asksingapore": [], "singaporefi": [], "singapore": []})
                with self._with_http(later):
                    _run(mode, "singapore hawker centre", 10)
                self.assertEqual(later.count("singapore"), 0, later.calls)
                self.assertGreater(later.count("askSingapore"), 0, later.calls)

    def setUp_mode(self):
        _reset_breaker()
        self.cache.store.clear()
        self.cdp_calls.clear()
        self.cdp_items = []

    def test_listed_sub_goes_to_the_browser_after_arctic_results(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                self.setUp_mode()
                mark = getattr(rs, "_mark_arctic_unsearchable", None)
                self.assertTrue(callable(mark), "no way to list a sub Arctic cannot search")
                mark(["singapore"])
                stub = _HttpStub({"asksingapore": [_post("a1", "askSingapore", 20),
                                                   _post("a2", "askSingapore", 10)],
                                  "singaporefi": []})
                self.cdp_items = [_post("a2", "askSingapore", 10), _post("b1", "singapore", 30)]
                with self._with_http(stub):
                    docs = _run(mode, "Universal Studios Singapore tickets", 10)
                self.assertEqual(stub.count("singapore"), 0, stub.calls)
                self.assertEqual([c[2] for c in self.cdp_calls], [["singapore"]])
                self.assertEqual([d.source_id for d in docs], ["a1", "a2", "b1"])
                self.assertIn("cdp-fallback", docs[2].tags)
                self.assertNotIn("cdp-fallback", docs[0].tags)

    def test_no_browser_when_arctic_already_filled_the_page(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                self.setUp_mode()
                rs._mark_arctic_unsearchable(["singapore"])
                stub = _HttpStub({"asksingapore": [_post("a1", "askSingapore", 20),
                                                   _post("a2", "askSingapore", 10)]})
                with self._with_http(stub):
                    docs = _run(mode, "Universal Studios Singapore tickets", 2)
                self.assertEqual(len(docs), 2)
                self.assertEqual(self.cdp_calls, [])

    def test_all_refused_round_counts_toward_breaker_and_lists_nothing(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                self.setUp_mode()
                stub = _HttpStub({"singapore": "422"})
                with self._with_http(stub):
                    _run(mode, "subreddit:singapore tickets", 10)
                self.assertEqual(rs._arctic_fail_streak, 1)
                self.setUp_mode()
                stub = _HttpStub({"singapore": "422", "cscareerquestions": "422"})
                with self._with_http(stub):
                    _run(mode, "subreddit:singapore,cscareerquestions tickets", 10)
                self.assertEqual(rs._arctic_fail_streak, 2)
                _reset_breaker()
                again = _HttpStub({"singapore": []})
                with self._with_http(again):
                    _run(mode, "subreddit:singapore tickets", 10)
                self.assertEqual(again.count("singapore"), 1, "an all-refused round listed the sub")


class SitewideSearch(unittest.TestCase):
    """Spec item 4: nothing resolved for a Latin query means one reddit-wide browser search."""

    def setUp(self):
        _reset_breaker()
        self.addCleanup(_reset_breaker)
        self.cache = _FakeCache()
        for p in self.cache.patches():
            p.start()
            self.addCleanup(p.stop)
        self.arctic_paths = []

        def arctic(path, params, **_k):
            self.arctic_paths.append(path)
            return []

        async def aarctic(path, params, **_k):
            return arctic(path, params)

        for name, fn in (("_arctic_get", arctic), ("_aarctic_get", aarctic)):
            p = mock.patch.object(rs, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_cheapest_uss_tickets_searches_reddit_wide_once(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                self.arctic_paths.clear()
                self.cache.doc_writes.clear()
                items = [_post("w1", "singapore", 5), _post("w2", "travel", 4)]
                with mock.patch.object(rs, "_discover_subreddits", return_value=[]), \
                     mock.patch.object(rs, "_cdp_search", return_value=items) as cdp:
                    docs = _run(mode, "cheapest USS tickets", 10)
                self.assertEqual(cdp.call_count, 1)
                self.assertEqual(cdp.call_args.args[0], "cheapest USS tickets")
                self.assertIsNone(cdp.call_args.args[2] if len(cdp.call_args.args) > 2
                                  else cdp.call_args.kwargs.get("subreddits"))
                self.assertNotIn("/posts/search", self.arctic_paths)
                self.assertEqual([d.source_id for d in docs], ["w1", "w2"])
                for d in docs:
                    self.assertEqual(d.metadata.get("search_via"), "cdp_www_reddit")
                    self.assertEqual(d.metadata.get("search_scope"), "sitewide")
                    self.assertIn("sitewide", d.tags)
                self.assertEqual(len(self.cache.doc_writes), 1)
                self.assertIn("sitewide", self.cache.doc_writes[0][0])

    def test_empty_sitewide_is_not_cached_and_leaves_a_reason(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                self.cache.doc_writes.clear()
                with mock.patch.object(rs, "_discover_subreddits", return_value=[]), \
                     mock.patch.object(rs, "_cdp_search", return_value=[]) as cdp:
                    diag.enable()
                    try:
                        docs = _run(mode, "cheapest USS tickets", 10)
                    finally:
                        notes = diag.drain()
                self.assertEqual(docs, [])
                self.assertEqual(cdp.call_count, 1)
                self.assertEqual(self.cache.doc_writes, [])
                helpers = [n.get("helper") for n in notes]
                self.assertIn("reddit.discovery_empty", helpers)
                self.assertIn("reddit.sitewide_empty", helpers)
                body = " ".join(str(n.get("body") or "") for n in notes
                                if n.get("helper") == "reddit.discovery_empty")
                self.assertIn("all of reddit", body)

    def test_routed_query_gets_one_per_sub_browser_search_and_no_sitewide(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                with mock.patch.object(rs, "_discover_subreddits", side_effect=_no_discovery), \
                     mock.patch.object(rs, "_cdp_search", return_value=[]) as cdp:
                    _run(mode, "Universal Studios Singapore tickets", 10)
                self.assertEqual(cdp.call_count, 1)
                subs = cdp.call_args.args[2] if len(cdp.call_args.args) > 2 \
                    else cdp.call_args.kwargs.get("subreddits")
                self.assertTrue(subs)

    def test_chinese_query_never_reaches_the_browser(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                with mock.patch.object(rs, "_discover_subreddits", return_value=[]), \
                     mock.patch.object(rs, "_cdp_search", return_value=[]) as cdp:
                    self.assertEqual(_run(mode, "环球影城 门票", 10), [])
                self.assertEqual(cdp.call_count, 0)


class CommentRouting(unittest.TestCase):
    """Spec item 5: the comment path resolves subs with the same routing, and has no sitewide search."""

    def _subs_searched(self, query):
        seen = []

        def arctic(path, params, **_k):
            sub = params.get("subreddit")
            if sub and sub not in seen:
                seen.append(sub)
            return []

        with mock.patch.object(rs.cache, "get_docs", return_value=None), \
             mock.patch.object(rs.cache, "set_docs"), \
             mock.patch.object(rs, "_arctic_get", side_effect=arctic), \
             mock.patch.object(rs, "_cdp_search", return_value=[]) as cdp:
            with mock.patch.object(rs, "_discover_subreddits", return_value=[]):
                docs = rs.RedditAdapter().search(query, 5)
        return seen, docs, cdp

    def test_comment_path_uses_the_topic_groups(self):
        _reset_breaker()
        seen, _docs, _cdp = self._subs_searched("comments: Universal Studios Singapore tickets")
        self.assertEqual(seen[:3], SG)

    def test_comment_path_without_subs_returns_empty_without_sitewide(self):
        _reset_breaker()
        diag.enable()
        try:
            seen, docs, cdp = self._subs_searched("comments: cheapest USS tickets")
        finally:
            notes = diag.drain()
        self.assertEqual(docs, [])
        self.assertEqual(seen, [])
        self.assertEqual(cdp.call_count, 0)
        self.assertTrue(any(n.get("helper") == "reddit.discovery_empty" for n in notes))


if __name__ == "__main__":
    unittest.main()
