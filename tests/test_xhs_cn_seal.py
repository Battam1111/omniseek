"""The 2026-10-07 seal of the mainland 小红书 source (xiaohongshu_cn, CDP 9224).

the operator: 「小红书还有个国际号能上呢，大陆号先封存吧」. One flag (xiaohongshu_cn_source._SEALED) switches
every entry point off; these tests pin what "off" means everywhere OmniSeek reads it, plus the two
international-adapter fixes that came with the seal (a cache-only collect never counts as a CDP failure,
the 2026-10-07 17:45 backoff trip; a guest wall charges nothing). Everything is offline: cdp_call,
the browser driver, launchctl and the alert channel are faked, and a call that would reach a network
raises in the test instead.

The launchd half (services / sentinel / omniseek_doctor reading a disabled label) is in
test_xhs_cn_seal_launchd.py: it loads the fleet's launchd scripts, which the public mirror does not ship.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from omniseek.core import cache, diag, fetcher, infra_jobs, profile
from omniseek.core.sources.walled import _cdp
from omniseek.core.sources.walled import xiaohongshu_cn_source as cn
from omniseek.core.sources.walled import xiaohongshu_source as xhs

# The cn module appends to an incident black box on some paths; keep the real file untouched
# (the smoke tripwire requires every importer of xiaohongshu_cn_source to redirect _INCIDENT_PATH).
_ISO_TMP = None
_ISO_REAL = None


def setUpModule():
    global _ISO_TMP, _ISO_REAL
    _ISO_TMP = tempfile.TemporaryDirectory()
    _ISO_REAL = cn._INCIDENT_PATH
    cn._INCIDENT_PATH = Path(_ISO_TMP.name) / "xhs-cn-incidents.jsonl"


def tearDownModule():
    if _ISO_REAL is not None:
        cn._INCIDENT_PATH = _ISO_REAL
    if _ISO_TMP is not None:
        _ISO_TMP.cleanup()


def _no_network(*_a, **_kw):
    raise AssertionError("a sealed path reached the browser / network")


@contextmanager
def _open_slot(*_a, **_kw):
    yield (True, "")


@contextmanager
def _captured():
    diag.enable()
    box = []
    try:
        yield box
    finally:
        box.extend(diag.drain())


def _helpers(captures):
    return [c.get("helper") for c in captures]


@contextmanager
def _cache_only():
    token = cache._cache_only_var.set(True)
    try:
        yield
    finally:
        cache._cache_only_var.reset(token)


# ── the mainland adapter itself ──────────────────────────────────────────────────────────────────
class SealedMainlandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(cn._SEALED, "the shipped state is sealed")
        self.a = fetcher.get_adapter("xiaohongshu_cn")
        self.assertIsNotNone(self.a)

    def test_sealed_attribute_names_where_to_go(self):
        self.assertEqual(self.a.sealed, cn._SEALED_MSG)
        self.assertIn("xiaohongshu", cn._SEALED_MSG)

    def test_health_is_not_a_failure(self):
        ok, why = self.a.health_check()
        self.assertIsNone(ok)
        self.assertTrue(why.startswith("sealed: "))

    def test_search_returns_empty_with_a_reason_and_no_network(self):
        with mock.patch.object(cn, "cdp_call", side_effect=_no_network, create=True), \
                mock.patch.object(cache, "get", side_effect=_no_network), _captured() as box:
            self.assertEqual(self.a.search("咖啡", limit=5), [])
        self.assertIn("xiaohongshu_cn.sealed", _helpers(box))

    def test_fetch_url_declines_every_link(self):
        with mock.patch.object(cache, "get", side_effect=_no_network):
            self.assertIsNone(self.a.fetch_url(
                "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=t"))

    def test_unsealed_the_property_is_empty(self):
        with mock.patch.object(cn, "_SEALED", False):
            self.assertEqual(self.a.sealed, "")


# ── the fetcher's reading of the seal ────────────────────────────────────────────────────────────
class FetcherSealTests(unittest.TestCase):
    def test_sealed_reason_accepts_str_and_bool(self):
        self.assertEqual(fetcher.sealed_reason(types.SimpleNamespace(sealed="why")), "why")
        self.assertEqual(fetcher.sealed_reason(types.SimpleNamespace(sealed=True)), "sealed")
        self.assertEqual(fetcher.sealed_reason(types.SimpleNamespace(sealed=False)), "")
        self.assertEqual(fetcher.sealed_reason(types.SimpleNamespace()), "")

    def test_explicit_only_reason_leads_with_the_seal(self):
        r = fetcher._explicit_only_reason(fetcher.get_adapter("xiaohongshu_cn"))
        self.assertTrue(r.startswith("sealed: "), r)

    def test_named_call_returns_empty_with_the_reason(self):
        with mock.patch.object(cn, "cdp_call", side_effect=_no_network, create=True):
            docs, d = fetcher.fetch_one_with_diag("xiaohongshu_cn", "咖啡", limit=3, deadline_s=None)
        self.assertEqual(docs, [])
        self.assertTrue(d.get("sealed"))
        self.assertEqual(d.get("note"), cn._SEALED_MSG)

    def test_broad_sweep_leaves_it_out_and_never_suggests_it(self):
        # The deployment profile is CONSTRUCTED, never read from this host's ~/.omniseek/profile.json.
        # Walled sources are deny-by-default without one, so neither xiaohongshu account would reach the
        # plan at all and the seal would go untested; the live host's own file happens to opt the walled tier
        # in, which is the only reason this ever passed without the patch (it failed in the public
        # mirror's gate, which runs with no profile).
        with mock.patch.object(profile, "_cache", {"walled": {"enabled": True, "bring_your_own": True}}):
            cat = fetcher.get_catalog_snapshot()
            pol = fetcher._build_policy_snapshot(cat)
        self.assertEqual(pol.sealed.get("xiaohongshu_cn"), cn._SEALED_MSG)
        plan = fetcher.build_search_plan(cat, pol, "小红书 xiaohongshu 笔记 咖啡")
        self.assertNotIn("xiaohongshu_cn", plan.broad_live)
        self.assertTrue(plan.excluded["xiaohongshu_cn"].startswith("sealed: "))
        suggested = [e["name"] for e in plan.excluded_relevant]
        self.assertNotIn("xiaohongshu_cn", suggested)
        self.assertIn("xiaohongshu", suggested)  # the international account is what gets suggested

    def test_list_sources_reads_sealed_health(self):
        row = [s for s in fetcher.list_sources() if s["name"] == "xiaohongshu_cn"][0]
        self.assertEqual(row["health"], "sealed")
        self.assertTrue(row["sealed"])
        self.assertEqual(row["sealed_reason"], cn._SEALED_MSG)
        intl = [s for s in fetcher.list_sources() if s["name"] == "xiaohongshu"][0]
        self.assertNotIn("sealed_reason", intl)


# ── _cdp: port 9224 is never started ─────────────────────────────────────────────────────────────
class SealedPortTests(unittest.TestCase):
    def test_ensure_browser_refuses_before_any_probe(self):
        self.assertTrue(_cdp.sealed_port_reason("http://127.0.0.1:9224"))
        with mock.patch.object(_cdp, "cdp_health", side_effect=_no_network), \
                mock.patch.object(_cdp.subprocess, "run", side_effect=_no_network), \
                mock.patch.object(_cdp.sys, "platform", "darwin"):
            with self.assertRaises(RuntimeError) as ctx:
                _cdp.ensure_browser("http://127.0.0.1:9224")
        self.assertIn("9224 sealed", str(ctx.exception))

    def test_other_ports_are_untouched(self):
        self.assertEqual(_cdp.sealed_port_reason("http://127.0.0.1:9223"), "")
        self.assertEqual(_cdp.sealed_port_reason("http://127.0.0.1:9225"), "")

    def test_without_the_seal_9224_behaves_as_before(self):
        saved = _cdp._SEALED_PORTS.pop("9224", None)
        try:
            calls = []
            with mock.patch.object(_cdp, "cdp_health", lambda url: (calls.append(url) or (True, ""))), \
                    mock.patch.object(_cdp, "touch_last_use", lambda url: None), \
                    mock.patch.object(_cdp.sys, "platform", "darwin"):
                _cdp.ensure_browser("http://127.0.0.1:9224")
            self.assertEqual(calls, ["http://127.0.0.1:9224"])
        finally:
            if saved is not None:
                _cdp._SEALED_PORTS["9224"] = saved


# ── the session warmer skips the sealed account ──────────────────────────────────────────────────
class _Refusing:
    def __init__(self, order):
        self.order = order
        self.chromium = self

    def connect_over_cdp(self, url):
        self.order.append(url)
        raise RuntimeError(f"connect ECONNREFUSED {url}")


class WarmerSealTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.state_path = tmp / "session-warmer.json"
        self._patches = [
            mock.patch.object(infra_jobs, "_WARMER_STATE", self.state_path),
            mock.patch.object(infra_jobs, "_MAINT_FLAG", tmp / "cdp-maintenance"),
            mock.patch.object(infra_jobs, "_jsleep", lambda lo, hi: None),
            mock.patch.dict(os.environ, {"WARMER_FORCE": "1", "WARMER_ONLY": ""}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def test_warmer_sealed_reads_the_port_table(self):
        self.assertTrue(infra_jobs._warmer_sealed("http://127.0.0.1:9224"))
        self.assertEqual(infra_jobs._warmer_sealed("http://127.0.0.1:9223"), "")

    def test_9224_is_skipped_without_alert_or_start(self):
        connected, started, sent = [], [], []
        fake = _Refusing(connected)

        class _Session:
            def __enter__(self):
                return fake

            def __exit__(self, *exc):
                return False

        driver = types.ModuleType("patchright.sync_api")
        driver.sync_playwright = lambda: _Session()
        package = types.ModuleType("patchright")
        package.sync_api = driver
        server = types.ModuleType("omniseek.server")

        def _no_forums():
            raise RuntimeError("the forum half is not under test")

        server.load_sources = _no_forums
        with mock.patch.dict(sys.modules, {"patchright": package, "patchright.sync_api": driver,
                                           "omniseek.server": server}), \
                mock.patch.object(_cdp, "ensure_browser", side_effect=started.append), \
                mock.patch.object(infra_jobs, "_alert",
                                  lambda title, body, **_kw: sent.append(title)):
            out = infra_jobs.run_session_warmer()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("http://127.0.0.1:9224", started)
        self.assertNotIn("http://127.0.0.1:9224", connected)
        self.assertEqual(sent, [])
        self.assertEqual(out.get("sealed"), ["大陆号-xiaohongshu"])
        sealed_rows = [r for r in state["last_results"] if r.get("skipped") == "sealed"]
        self.assertEqual([r["label"] for r in sealed_rows], ["大陆号-xiaohongshu"])
        self.assertFalse(infra_jobs._needs_relogin_alert(sealed_rows[0]))


# ── the international adapter (9223): cache-only, guest wall, ownership, media ────────────────────
_NOTE = "https://www.rednote.com/explore/0123456789abcdef01234567?xsec_token=abc"
_IMGS = ["https://sns-webpic-qc.xhscdn.com/202610/1/a.jpg",
         "https://ci.xhscdn.com/notes_pre_post/b.jpg"]


class _BackoffState:
    """Snapshot and restore the module's backoff counters so no test leaks a tripped 小号."""

    def setUp(self):
        self._saved = (xhs._backoff_until, xhs._consec_cdp_err)
        xhs._backoff_until, xhs._consec_cdp_err = 0.0, 0
        self.a = fetcher.get_adapter("xiaohongshu")

    def tearDown(self):
        xhs._backoff_until, xhs._consec_cdp_err = self._saved


class CacheOnlyIsNotACdpFailureTests(_BackoffState, unittest.TestCase):
    def test_cache_only_search_miss_never_enters_the_slot(self):
        with _cache_only(), mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs, "_live_slot", side_effect=_no_network), \
                mock.patch.object(xhs, "_note_cdp_result", side_effect=_no_network), \
                mock.patch.object(xhs, "cdp_call", side_effect=_no_network), _captured() as box:
            for _ in range(5):
                self.assertEqual(self.a.search("咖啡", limit=5), [])
        self.assertEqual(xhs._backoff_until, 0.0)
        self.assertIn("xiaohongshu.cache_only", _helpers(box))

    def test_cache_only_read_miss_never_enters_the_slot(self):
        with _cache_only(), mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs, "_live_slot", side_effect=_no_network), \
                mock.patch.object(xhs, "_note_cdp_result", side_effect=_no_network), \
                mock.patch.object(xhs, "cdp_call", side_effect=_no_network):
            for _ in range(5):
                self.assertIsNone(self.a.fetch_url(_NOTE))
        self.assertEqual(xhs._backoff_until, 0.0)

    def test_a_cache_only_miss_raised_by_cdp_call_is_not_counted(self):
        """The belt behind the braces: if a CacheOnlyMiss still reaches the except (another caller
        sets cache-only mode deeper down), it must not bump the consecutive-failure counter."""
        miss = xhs.CacheOnlyMiss("cache-only mode: live CDP suppressed")
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs.cache, "cache_only", return_value=False), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(xhs, "cdp_call", side_effect=miss):
            for _ in range(xhs._CDP_ERR_THRESHOLD + 2):
                self.assertEqual(self.a.search("咖啡", limit=5), [])
                self.assertIsNone(self.a.fetch_url(_NOTE))
        self.assertEqual(xhs._consec_cdp_err, 0)
        self.assertEqual(xhs._backoff_until, 0.0)

    def test_a_real_cdp_failure_still_trips_the_backoff(self):
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(xhs, "cdp_call", side_effect=TimeoutError("wedged")):
            for _ in range(xhs._CDP_ERR_THRESHOLD):
                self.assertEqual(self.a.search("咖啡", limit=5), [])
        self.assertGreater(xhs._backoff_until, 0.0)


class GuestWallTests(_BackoffState, unittest.TestCase):
    def test_guest_wall_charges_nothing(self):
        url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc"
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(xhs, "_trip_backoff", side_effect=_no_network), \
                mock.patch.object(xhs, "cdp_call",
                                  return_value=("guest_wall", None, [], {"list": [], "declared": None},
                                                (None, "unresolved"))), _captured() as box:
            self.assertIsNone(self.a.fetch_url(url))
        self.assertIn("xiaohongshu.guest_wall", _helpers(box))
        self.assertEqual(xhs._backoff_until, 0.0)


class OwnershipTests(unittest.TestCase):
    URL = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc"

    def test_sealed_the_international_adapter_claims_xiaohongshu_com(self):
        a = fetcher.get_adapter("xiaohongshu")
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(a, "_fetch_url_live", return_value="claimed") as live:
            self.assertEqual(a.fetch_url(self.URL), "claimed")
        live.assert_called_once_with(self.URL)

    def test_unsealed_it_leaves_xiaohongshu_com_to_the_mainland_adapter(self):
        a = fetcher.get_adapter("xiaohongshu")
        with mock.patch.object(cn, "_SEALED", False), \
                mock.patch.object(a, "_fetch_url_live", side_effect=_no_network):
            self.assertIsNone(a.fetch_url(self.URL))


class MediaTests(_BackoffState, unittest.TestCase):
    """omniseek_read of a note returns its image URLs in media (what 思兼 hands to omniseek_view)."""

    def test_image_note_returns_media_and_says_so(self):
        html = ("<html><body><div id='detail-title'>咖啡豆怎么选</div>"
                "<div id='detail-desc'>看图</div></body></html>")
        flow = ("ok", html, list(_IMGS), {"list": [], "declared": None}, (None, "unresolved"))
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs.cache, "set", lambda *a, **k: None), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(xhs, "cdp_call", return_value=flow):
            doc = self.a.fetch_url(_NOTE)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.media, _IMGS)
        self.assertIn("2 张图", doc.content)
        self.assertEqual(doc.to_tool_dict(full=True)["media"], _IMGS)


if __name__ == "__main__":
    unittest.main()
