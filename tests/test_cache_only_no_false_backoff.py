"""A cache-only collect is not a live failure, on every logged-in source that 4b3ee36 did not cover.

4b3ee36 fixed the international 小红书 source: a cache-only collect (omniseek_search staleness=cache_only,
the poll-safe pickup half of fire-then-collect) that missed the cache used to enter the live slot, get
CacheOnlyMiss out of cdp_call, and count that as a browser failure toward the account backoff. The
2026-10-08 audit of the other walled sources found three more of the same class:

  - xiaohongshu_cn: the miss charged the shared daily cap and the pacing gap, was counted by
    _note_browser_cdp(False) toward the 风控 cooldown, wrote a false incident, and then fell through to
    the signed API, whose raw curl_cffi egress the cache-only guard in http.py does not cover.
    The source is sealed today, so this was latent; the tests run it unsealed, as it returns on restore.
  - yipinsanfendi: the miss took the egress gate, slept up to _YIPIN_MIN_GAP_S, and stamped
    _YIPIN_LAST, so the next REAL search waited a full gap for an egress that never happened.
  - zhihu_users: the collect swallows CacheOnlyMiss per cold handle, then wrote its partial (often
    empty) answer under the query key, which the next live search served as the full answer.

Each test below fails on 4b3ee36 and passes after the fix. No browser and no network.
"""
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from omniseek.core import cache, diag
from omniseek.core.sources.walled import _cdp
from omniseek.core.sources.walled import xiaohongshu_cn_source as cn
from omniseek.core.sources.walled import yipinsanfendi_source as yipin
from omniseek.core.sources.walled import zhihu_users_source as zu
from omniseek.core.sources.walled._base import BaseCDPAdapter

_ISO = None
_REAL = {}


def setUpModule():
    global _ISO
    _ISO = tempfile.TemporaryDirectory()
    for name, fname in (("_INCIDENT_PATH", "xhs-cn-incidents.jsonl"),
                        ("_DAILY_STATE_PATH", "xhs-cn-daily-budget.json")):
        _REAL[name] = getattr(cn, name)
        setattr(cn, name, Path(_ISO.name) / fname)


def tearDownModule():
    for name, value in _REAL.items():
        setattr(cn, name, value)
    if _ISO is not None:
        _ISO.cleanup()


def _boom(*_a, **_kw):
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


# ── xiaohongshu_cn ──────────────────────────────────────────────────────────────────────────────
NOTE = "0123456789abcdef01234567"
CN_URL = f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=ABfake&xsec_source=pc_search"


class _CnCase(unittest.TestCase):
    _GLOBALS = {"_tripped_until": 0.0, "_signed_tripped_until": 0.0, "_trip_streak": 0,
                "_signed_trip_streak": 0, "_last_signal": "", "_last_signed_signal": "",
                "_browser_last_flow": 0.0, "_browser_next_gap": 0.0}

    def setUp(self):
        self._saved = {n: getattr(cn, n) for n in self._GLOBALS}
        for n, v in self._GLOBALS.items():
            setattr(cn, n, v)
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.object(cache, "CACHE_DIR", Path(self._tmp.name)),
                         mock.patch.object(cn, "_INCIDENT_PATH", Path(self._tmp.name) / "incidents.jsonl"),
                         mock.patch.object(cn, "_SEALED", False),
                         mock.patch.object(cn, "_BROWSER_OK", True),
                         mock.patch.object(cn, "_DEPS_OK", True),
                         mock.patch.object(cn, "_signed_ready", lambda: (True, "")),
                         mock.patch.object(cn, "_get_cookies", lambda force=False: {}),
                         mock.patch.object(cn, "_signed_post", _boom),
                         mock.patch.object(cn, "_signed_get", _boom),
                         mock.patch.object(cn, "_bump_daily", _boom)]
        for p in self._patches:
            p.start()
        self.a = cn.XiaohongshuCNAdapter()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        for n, v in self._saved.items():
            setattr(cn, n, v)
        self._tmp.cleanup()

    def _assert_nothing_counted(self):
        self.assertEqual(cn._trip_streak, 0)
        self.assertEqual(cn._tripped_until, 0.0)
        self.assertEqual(cn._signed_tripped_until, 0.0)
        self.assertEqual(cn._browser_last_flow, 0.0, "the pacing gap was stamped for no egress")
        self.assertFalse(cn._INCIDENT_PATH.exists(), "a cache-only miss wrote an incident")


class CnCacheOnlyTests(_CnCase):
    def test_search_miss_makes_no_live_touch(self):
        with _cache_only(), mock.patch.object(cn, "cdp_call", _boom), _captured() as box:
            for _ in range(5):
                self.assertEqual(self.a.search("咖啡", limit=5), [])
        self._assert_nothing_counted()
        self.assertIn("xiaohongshu_cn.cache_only", _helpers(box))

    def test_read_miss_makes_no_live_touch(self):
        with _cache_only(), mock.patch.object(cn, "cdp_call", _boom), _captured() as box:
            for _ in range(5):
                self.assertIsNone(self.a.fetch_url(CN_URL))
        self._assert_nothing_counted()
        self.assertIn("xiaohongshu_cn.cache_only", _helpers(box))

    def test_signed_paths_make_no_live_touch(self):
        with _cache_only(), _captured() as box:
            self.assertEqual(self.a._search_signed("咖啡", 5), [])
            self.assertIsNone(self.a._fetch_signed(CN_URL))
        self._assert_nothing_counted()
        self.assertIn("xiaohongshu_cn.cache_only", _helpers(box))

    def test_browser_entry_points_refuse_before_the_slot(self):
        with _cache_only(), mock.patch.object(cn, "cdp_call", _boom), \
                mock.patch.object(cn, "_browser_slot", mock.Mock(acquire=_boom, release=_boom)):
            self.assertEqual(cn._browser_search("咖啡", 5), ("cache_only", []))
            self.assertEqual(cn._browser_fetch(NOTE, "ABfake", CN_URL), ("cache_only", None))
        self._assert_nothing_counted()

    def test_a_cache_only_miss_raised_inside_cdp_call_is_not_a_browser_failure(self):
        # The belt: cache_only() read False at the guard but cdp_call still refused (the context
        # variable is what cdp_call checks). Nothing may count, and the signed API is never asked.
        def _miss(*_a, **_kw):
            raise _cdp.CacheOnlyMiss("cache-only: live CDP suppressed")
        with mock.patch.object(cn, "_bump_daily", lambda: None), \
                mock.patch.object(cn, "cdp_call", _miss), \
                mock.patch.object(cn, "_note_browser_cdp", _boom):
            self.assertEqual(cn._browser_search("咖啡", 5), ("cache_only", []))
            self.assertEqual(cn._browser_fetch(NOTE, "ABfake", CN_URL), ("cache_only", None))
            self.assertEqual(self.a.search("咖啡", limit=5), [])
            self.assertIsNone(self.a.fetch_url(CN_URL))
        self.assertEqual(cn._trip_streak, 0)
        self.assertEqual(cn._tripped_until, 0.0)
        self.assertEqual(cn._browser_last_flow, 0.0, "the pacing gap was stamped for no egress")
        self.assertFalse(cn._INCIDENT_PATH.exists())

    def test_a_real_browser_failure_still_counts(self):
        # GUARD: the fix must not blind the breaker to a real failure.
        def _dead(*_a, **_kw):
            raise RuntimeError("connect ECONNREFUSED 127.0.0.1:9224")
        seen = []
        with mock.patch.object(cn, "_bump_daily", lambda: None), \
                mock.patch.object(cn, "cdp_call", _dead), \
                mock.patch.object(cn, "_note_browser_cdp", seen.append):
            status, _ = cn._browser_search("咖啡", 5)
        self.assertEqual(status, "error")
        self.assertEqual(seen, [False])
        self.assertTrue(cn._INCIDENT_PATH.exists(), "a real failure must still reach the black box")
        self.assertGreater(cn._browser_last_flow, 0.0)


# ── yipinsanfendi ───────────────────────────────────────────────────────────────────────────────
class YipinCacheOnlyTests(unittest.TestCase):
    def setUp(self):
        self._last = yipin._YIPIN_LAST[0]

    def tearDown(self):
        yipin._YIPIN_LAST[0] = self._last

    def test_cache_only_skips_the_egress_gate(self):
        a = yipin.YipinsanfendiAdapter()
        stamp = time.monotonic()  # a real search just ran: the gate would make the next one wait
        yipin._YIPIN_LAST[0] = stamp

        def _miss(*_a, **_kw):
            raise _cdp.CacheOnlyMiss("cache-only: live CDP suppressed")
        with _cache_only(), mock.patch.object(BaseCDPAdapter, "_run", _miss), \
                mock.patch.object(yipin.time, "sleep", _boom):
            with self.assertRaises(_cdp.CacheOnlyMiss):
                a._run(lambda page: None, "https://example.invalid/")
        self.assertEqual(yipin._YIPIN_LAST[0], stamp, "a suppressed call stamped the egress gap")

    def test_live_runs_still_take_the_gate(self):
        # GUARD: outside cache-only mode the pacing is unchanged.
        a = yipin.YipinsanfendiAdapter()
        yipin._YIPIN_LAST[0] = time.monotonic()
        slept = []
        with mock.patch.object(BaseCDPAdapter, "_run", lambda self, cb, url: "ok"), \
                mock.patch.object(yipin.time, "sleep", slept.append):
            self.assertEqual(a._run(lambda page: None, "https://example.invalid/"), "ok")
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)
        self.assertGreater(yipin._YIPIN_LAST[0], 0)


# ── zhihu_users ─────────────────────────────────────────────────────────────────────────────────
class ZhihuUsersCacheOnlyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(cache, "CACHE_DIR", Path(self._tmp.name))
        self._p.start()
        self.a = zu.ZhihuUsersAdapter()
        self.users = [{"handle": "h1", "display_name": "甲"}, {"handle": "h2", "display_name": "乙"}]

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _key(self, query, limit):
        return cache.make_key("zhihu_users", "search", query, limit, len(self.users))

    def test_cache_only_collect_does_not_write_the_query_key(self):
        def _cold(_handle, _name):
            raise _cdp.CacheOnlyMiss("cache-only: live CDP suppressed")
        writes = []
        with _cache_only(), mock.patch.object(self.a, "_load_users", lambda: self.users), \
                mock.patch.object(self.a, "_fetch_user_posts", _cold), \
                mock.patch.object(zu.cache, "set_docs", lambda *a, **k: writes.append(a)):
            self.assertEqual(self.a.search("大模型", limit=5), [])
        self.assertEqual(writes, [], "a cache-only collect wrote its partial answer under the query key")

    def test_live_search_still_writes_the_query_key(self):
        # GUARD: outside cache-only mode the query cache is written as before.
        writes = []
        with mock.patch.object(self.a, "_load_users", lambda: self.users), \
                mock.patch.object(self.a, "_fetch_user_posts", lambda h, n: []), \
                mock.patch.object(zu.cache, "set_docs", lambda key, *a, **k: writes.append(key)):
            self.a.search("大模型", limit=5)
        self.assertEqual(writes, [self._key("大模型", 5)])


if __name__ == "__main__":
    unittest.main()
