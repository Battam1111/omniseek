import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# The note flow returns FIVE values: (status, html, images, cdata, (video_url, origin)). A stub
# whose arity or slot-5 SHAPE is wrong does NOT exercise the branch under test: it raises inside
# the adapter, and the adapter's own error path returns exactly the None / ("error", None) these
# tests assert, so the assertion passes on the wrong path. That is not hypothetical: both stubs
# below were four-tuples for a while and both tests were green without ever reaching the branch
# they name. Keep the shape honest or the test is theatre. (A note with no video is
# (None, "unresolved"), what _cdp.video_with_origin returns when neither the DOM nor the wire
# sniffer produced a URL.)
_NO_VIDEO = (None, "unresolved")

# STATE ISOLATION (2026-08-12). This suite drives the xhs_cn guard, and the guard APPENDS to the
# incident black box. Unisolated it wrote into ~/.omniseek/state/xhs-cn-incidents.jsonl on every
# run, so the file whose whole job is to answer "what did a real 461 look like" filled up with
# rows no incident produced. setUpModule redirects it once for everything below; a temp dir per
# module keeps the check cheap and leaves nothing behind.
_ISO_TMP = None
_ISO_REAL = None


def setUpModule():
    global _ISO_TMP, _ISO_REAL
    from omniseek.core.sources.walled import xiaohongshu_cn_source as _xcn
    _ISO_TMP = tempfile.TemporaryDirectory()
    _ISO_REAL = _xcn._INCIDENT_PATH
    _xcn._INCIDENT_PATH = Path(_ISO_TMP.name) / "xhs-cn-incidents.jsonl"


def tearDownModule():
    from omniseek.core.sources.walled import xiaohongshu_cn_source as _xcn
    if _ISO_REAL is not None:
        _xcn._INCIDENT_PATH = _ISO_REAL
    if _ISO_TMP is not None:
        _ISO_TMP.cleanup()


def _stream_entry(height, *, master="https://sns-video-v4.xhscdn.com/stream/1/110/301/x.mp4",
                  fmt="mp4", default=0, weight=62, backups=()):
    """One entry shaped like the real thing. Field names and the nesting below come from a LIVE
    signed response observed 2026-08-12, not from memory: master_url + backup_urls + format +
    height/width + quality_type + default_stream + weight."""
    e = {"master_url": master, "backup_urls": list(backups), "format": fmt, "height": height,
         "width": int(height * 9 / 16), "quality_type": "HD", "default_stream": default,
         "weight": weight, "size": 14_000_000}
    if master is None:
        e.pop("master_url")
    return e


def _video_card(stream, note_type="video"):
    return {"type": note_type, "video": {"media": {"stream": stream}}}


class SignedVideoExtractionTests(unittest.TestCase):
    """The signed fallback's half of the video contract.

    It reads image_list, which on a video note is only the cover frame. Until 2026-08-12 that meant
    a video note served through this path (9224 browser down, or an explicit &xhs_full=1 deep drill)
    came back with no stream, no hint, and no way for a later reader to know it had been a video.
    """

    def setUp(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as m
        self.m = m

    def test_picks_the_tallest_stream_when_the_site_marks_no_default(self):
        """The observed case exactly: three HD mp4 entries across two buckets, none marked default,
        every weight identical, heights 1280 / 1280 / 1920. Only height can discriminate, and taller
        is right because OmniSeek's own hint sends the agent to READ THE FRAMES."""
        nc = _video_card({"EF4": [_stream_entry(1280, master="https://x/ef4.mp4")],
                          "EF5": [_stream_entry(1280, master="https://x/ef5a.mp4"),
                                  _stream_entry(1920, master="https://x/ef5b.mp4")],
                          "EF6": [], "EF7": []})
        self.assertEqual(self.m._signed_video(nc), ("https://x/ef5b.mp4", True))

    def test_a_site_marked_default_beats_a_taller_stream(self):
        """When the site does express a preference it wins: it knows which stream it serves."""
        nc = _video_card({"EF5": [_stream_entry(720, master="https://x/small.mp4", default=1),
                                  _stream_entry(1920, master="https://x/big.mp4")]})
        self.assertEqual(self.m._signed_video(nc), ("https://x/small.mp4", True))

    def test_falls_back_to_a_backup_url_when_the_master_is_missing(self):
        nc = _video_card({"EF5": [_stream_entry(1080, master=None,
                                                backups=["https://bak/a.mp4", "https://bak/b.mp4"])]})
        self.assertEqual(self.m._signed_video(nc), ("https://bak/a.mp4", True))

    def test_a_non_mp4_entry_is_refused(self):
        """Both downstream readers fetch the file; an entry in some other container is not a URL we
        can promise works, and a video note with no usable stream is still a video note."""
        nc = _video_card({"EF5": [_stream_entry(1080, master="https://x/a.m3u8", fmt="m3u8")]})
        self.assertEqual(self.m._signed_video(nc), (None, True))

    def test_a_video_note_with_no_stream_is_still_reported_as_a_video_note(self):
        """THE DENOMINATOR. Without this the note is indistinguishable from an image note, and
        'the signed path never resolves a stream' can never be told from 'no video note came'."""
        self.assertEqual(self.m._signed_video(_video_card({"EF5": [], "EF4": []})), (None, True))
        self.assertEqual(self.m._signed_video({"type": "video"}), (None, True))

    def test_an_image_note_grows_no_video_keys(self):
        self.assertEqual(self.m._signed_video({"type": "normal", "image_list": [{"url_default": "u"}]}),
                         (None, False))

    def test_a_stream_without_the_video_type_still_counts_as_video(self):
        """type is the primary signal, but a payload that carries a playable stream is a video note
        whatever it calls itself; believing the label over the evidence would undercount."""
        nc = _video_card({"EF5": [_stream_entry(1080)]}, note_type="normal")
        url, is_video = self.m._signed_video(nc)
        self.assertTrue(url and is_video)

    def test_the_signed_document_stamps_video_src_signed(self):
        """The origin must say WHICH mechanism produced the URL. dom / wire / signed are three
        different answers and the whole point of the field is that they stay distinguishable."""
        from omniseek.core.sources.walled import _cdp
        self.assertEqual(_cdp.video_metadata("https://x/a.mp4", "signed", has_player=True),
                         {"video_url": "https://x/a.mp4", "video_src": "signed"})


class XiaohongshuReadContractTests(unittest.TestCase):
    def test_search_index_replays_the_cached_snippet_for_eye_read(self):
        from omniseek.core import cache
        from omniseek.core.sources.api import search_index_source as module

        url = "https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567"
        row = {
            "url": url,
            "title": "indexed note",
            "snippet": "the indexed body",
        }
        venue = module._SearchVenue(
            name="xiaohongshu_search",
            description="test",
            site="xiaohongshu.com",
            url_filter=r"/(?:explore|discovery/item)/[0-9a-f]",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cache, "CACHE_DIR", Path(tmp)):
                with patch.object(module, "search_web", return_value=[row]):
                    docs = venue.search("indexed", limit=1)
                self.assertEqual(len(docs), 1)
                reread = venue.fetch_url(url)

        self.assertIsNotNone(reread)
        self.assertEqual(reread.source, "xiaohongshu_search")
        self.assertEqual(reread.content, "the indexed body")
        self.assertEqual(reread.metadata["read_depth"], "search-index-snippet")

    def test_search_index_drops_non_note_mobile_question_urls(self):
        from omniseek.core.sources.api import search_index_source as module

        config_path = Path(module.__file__).with_name("search_index_sites.json")
        rows = json.loads(config_path.read_text(encoding="utf-8"))
        row = next(r for r in rows if r["name"] == "xiaohongshu_search")
        self.assertNotRegex(row["url_filter"], r"mobile/question")

    def test_search_index_refetches_when_a_cached_snapshot_violates_the_filter(self):
        from omniseek.core import cache
        from omniseek.core.normalize import Document
        from omniseek.core.sources.api import search_index_source as module

        venue = module._SearchVenue(
            name="xiaohongshu_search",
            description="test",
            site="xiaohongshu.com",
            url_filter=r"/(?:explore|discovery/item)/[0-9a-f]{16,}",
        )
        old_url = "https://www.xiaohongshu.com/mobile/question/319144"
        new_url = "https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567"
        old_doc = Document(source=venue.name, source_id=old_url, url=old_url,
                                   title="old", content="old snippet")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cache, "CACHE_DIR", Path(tmp)):
                q = f"site:{venue.site} stale"
                cache.set_docs(cache.make_key("search_index", venue.name, q, 1), [old_doc], ttl=1800)
                with patch.object(module, "search_web", return_value=[{
                    "url": new_url, "title": "new", "snippet": "new snippet"
                }]) as search:
                    docs = venue.search("stale", limit=1)

        search.assert_called_once()
        self.assertEqual(docs[0].url, new_url)

    def test_international_detail_rejects_an_empty_shell(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        html = "<html><body><main><div id='detail-title'></div><div id='detail-desc'></div></main></body></html>"
        # FIVE elements, slot five a (url, origin) PAIR. The note flow grew a video slot on
        # 2026-08-12 and this stub was left at four, so the unpack raised ValueError, the adapter's
        # own except swallowed it, and the result was None: which is exactly what this test asserts.
        # It went on passing while never once reaching the empty-shell branch it is named after. A
        # stub that lags the contract does not merely fail to test, it actively certifies.
        with patch.object(module, "cdp_call",
                          return_value=("ok", html, [], {"list": [], "declared": None}, _NO_VIDEO)), \
             patch.object(module, "_note_cdp_result"):
            with patch.object(module.diag, "note") as _diag:
                doc = module.XiaohongshuAdapter()._fetch_url_live(
                    "https://www.rednote.com/search_result/0123456789abcdef01234567"
                )

        self.assertIsNone(doc)
        # and prove it got there THROUGH the branch, not through a swallowed exception
        # Assert the EVENT NAME, not merely that diag fired: the swallowed-exception path also
        # emits a diag note, so `called` alone is satisfied by the very failure this guards against.
        _events = [c.args[0] for c in _diag.call_args_list if c.args]
        self.assertTrue(any("missing_xsec_token" in e for e in _events),
                        f"the empty-shell branch was never reached; diag saw {_events}")

    def test_international_adapter_does_not_claim_mainland_urls(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        url = "https://www.xiaohongshu.com/search_result/0123456789abcdef01234567"
        with patch.object(module.XiaohongshuAdapter, "_fetch_url_live") as live:
            doc = module.XiaohongshuAdapter().fetch_url(url)

        self.assertIsNone(doc)
        live.assert_not_called()

    def test_international_health_is_transport_only_and_does_not_navigate(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        with patch.object(module, "cdp_health", return_value=(True, "ok")), \
             patch.object(module, "cdp_call", side_effect=AssertionError("health must not navigate")):
            healthy, status = module.XiaohongshuAdapter().health_check()

        self.assertTrue(healthy)
        self.assertIn("CDP reachable", status)

    def test_mainland_detail_rejects_an_empty_shell(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        html = "<html><body><main><div id='detail-title'></div><div id='detail-desc'></div></main></body></html>"
        # FIVE elements, slot five a (url, origin) PAIR, for the same reason as the international
        # case above: at four the unpack raised, _browser_fetch's own except returned
        # ("error", None), and that is what this test asserts. It passed for runs it never actually
        # performed. Worse here: the swallowed error was recorded as a browser fault, so every run
        # wrote a forged browser_fetch_error row into the incident black box (setUpModule redirects
        # it now, and the unpack has since moved out of that try, so a shape bug is loud).
        with patch.object(module, "_bump_daily"), \
             patch.object(module, "cdp_call",
                          return_value=("ok", html, [], {"list": [], "declared": None}, _NO_VIDEO)), \
             patch.object(module, "_note_browser_cdp"), \
             patch.object(module, "_content_with_media", side_effect=lambda body, images: body):
            with patch.object(module.diag, "note") as _diag:
                status, doc = module._browser_fetch(
                    "0123456789abcdef01234567", "", "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
                )

        self.assertEqual(status, "error")
        self.assertIsNone(doc)
        # Assert the EVENT NAME, not merely that diag fired: the swallowed-exception path also
        # emits a diag note, so `called` alone is satisfied by the very failure this guards against.
        _events = [c.args[0] for c in _diag.call_args_list if c.args]
        self.assertTrue(any("missing_xsec_token" in e for e in _events),
                        f"the empty-shell branch was never reached; diag saw {_events}")

    def test_fallback_rejects_a_known_xiaohongshu_shell(self):
        from omniseek.core import cache
        from omniseek.core import web_fallback

        url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
        html = (
            "<html><head><title>小红书 - 你的生活兴趣社区</title></head>"
            "<body><main><a href='/explore?channel_id=homefeed_recommend'>发现</a>"
            + ("<a href='/red_video'>RED</a>" * 120)
            + "</main></body></html>"
        )
        response = {
            "ok": True,
            "status": 200,
            "content_type": "text/html",
            "text": html,
            "blocked_reason": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cache, "CACHE_DIR", Path(tmp)):
                with patch.object(web_fallback.safeurl, "safe_fetch", return_value=response):
                    with patch.object(web_fallback, "_jina_markdown", return_value=None):
                        doc = web_fallback.read_via_fallback(url)

        self.assertIsNone(doc)

    def test_fallback_never_launders_a_known_xiaohongshu_deep_link(self):
        from omniseek.core import cache
        from omniseek.core import web_fallback

        url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
        response = {
            "ok": False,
            "status": 0,
            "content_type": "",
            "text": "",
            "blocked_reason": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cache, "CACHE_DIR", Path(tmp)):
                with patch.object(web_fallback.safeurl, "safe_fetch", return_value=response), \
                     patch.object(
                         web_fallback,
                         "_jina_markdown",
                         return_value=("Title: 小红书 - 你的生活兴趣社区\\n\\n登录后推荐更懂你的笔记"),
                     ):
                    doc = web_fallback.read_via_fallback(url)

        self.assertIsNone(doc)

    def test_eye_read_reason_prefers_the_walled_adapter_diagnostic(self):
        from omniseek.core import diag
        from omniseek.core import fetcher
        from omniseek.core import web_fallback

        class RiskAdapter:
            name = "xiaohongshu_cn"
            fetch_timeout = 1.0

            @staticmethod
            def fetch_url(url):
                diag.note(
                    "xiaohongshu_cn.breaker",
                    url=url,
                    body="风控 breaker OPEN: http_461_captcha; no live call was made",
                )
                return None

        def refused_fallback(url):
            diag.note(
                "web_fallback",
                url=url,
                body="known walled deep link was not claimed; generic web fallback refused",
            )
            return None

        url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
        with patch.object(fetcher, "_adapters", {"xiaohongshu_cn": RiskAdapter()}), \
             patch.object(web_fallback, "read_via_fallback", side_effect=refused_fallback):
            doc, reason = fetcher.fetch_url_with_reason(url)

        self.assertIsNone(doc)
        self.assertIn("xiaohongshu_cn", reason)
        self.assertIn("http_461_captcha", reason)
        self.assertNotIn("generic web fallback refused", reason)

    def test_mainland_breaker_records_the_first_risk_signal_for_eye_read(self):
        from omniseek.core import diag
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        old_state = (module._tripped_until, module._trip_streak, module._last_signal)
        try:
            module._tripped_until = 0.0
            module._trip_streak = 0
            module._last_signal = ""
            diag.enable()
            module._trip("http_461_captcha")
            notes = diag.drain()
        finally:
            module._tripped_until, module._trip_streak, module._last_signal = old_state
            diag.drain()

        self.assertTrue(notes)
        self.assertEqual(notes[-1]["helper"], "xiaohongshu_cn.breaker")
        self.assertIn("http_461_captcha", notes[-1]["body"])


class XiaohongshuCNSignedPostureTests(unittest.TestCase):
    """The 2026-08-11 HTTP 461 defect, pinned as contract.

    小红书 mints a PER-HOST acw_tc, so the live 9224 jar carries three of them at once
    (edith / www / so). The old provider flattened the jar name-wise, so which token reached
    edith depended on jar order, and a wrong-host token draws a 461. Worse, that 461 opened a
    single global breaker that also darkened the healthy PRIMARY browser path for 1h/4h/24h.
    """

    @staticmethod
    def _jar(order):
        by_dom = {
            "edith.xiaohongshu.com": "acw-EDITH",
            "www.xiaohongshu.com": "acw-WWW",
            "so.xiaohongshu.com": "acw-SO",
        }
        jar = [{"name": "acw_tc", "domain": d, "value": by_dom[d], "expires": 4102444800.0}
               for d in order]
        jar.append({"name": "web_session", "domain": ".xiaohongshu.com",
                    "value": "sess", "expires": 4102444800.0})
        jar.append({"name": "unrelated", "domain": ".example.com", "value": "no", "expires": -1})
        return jar

    def test_edith_scoped_acw_tc_wins_regardless_of_jar_order(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        orders = [
            ["edith.xiaohongshu.com", "www.xiaohongshu.com", "so.xiaohongshu.com"],
            ["www.xiaohongshu.com", "so.xiaohongshu.com", "edith.xiaohongshu.com"],
            ["so.xiaohongshu.com", "edith.xiaohongshu.com", "www.xiaohongshu.com"],
        ]
        for order in orders:
            cookies, exp = module._cookies_for_host(self._jar(order), module._SIGNED_HOST)
            self.assertEqual(cookies["acw_tc"], "acw-EDITH", f"jar order {order}")
            self.assertEqual(cookies["web_session"], "sess")   # parent-domain cookie still rides
            self.assertNotIn("unrelated", cookies)             # a foreign domain never leaks in
            self.assertIn("acw_tc", exp)

    def test_host_cookies_for_other_hosts_never_reach_edith(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        jar = [{"name": "acw_tc", "domain": "www.xiaohongshu.com", "value": "acw-WWW", "expires": -1}]
        cookies, _exp = module._cookies_for_host(jar, module._SIGNED_HOST)
        self.assertNotIn("acw_tc", cookies)  # honestly absent beats silently wrong

    def test_signed_ready_refuses_a_dead_or_missing_anti_crawl_token(self):
        import time

        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        old = (dict(module._cookies), dict(module._cookie_exp), module._cookies_at)
        try:
            with patch.object(module, "_get_cookies", lambda force=False: {}):
                module._cookies = {}
                module._cookie_exp = {}
                ok, why = module._signed_ready()
                self.assertFalse(ok)
                self.assertIn("acw_tc", why)

                module._cookies = {"acw_tc": "v"}
                module._cookie_exp = {"acw_tc": time.time() + 5}
                ok, why = module._signed_ready()
                self.assertFalse(ok)
                self.assertIn("left", why)

                module._cookies = {"acw_tc": "v"}
                module._cookie_exp = {"acw_tc": time.time() + 3600}
                ok, why = module._signed_ready()
                self.assertTrue(ok)
                self.assertEqual(why, "")
        finally:
            module._cookies, module._cookie_exp, module._cookies_at = old

    def _reset_breakers(self, module):
        module._tripped_until = 0.0
        module._signed_tripped_until = 0.0
        module._trip_streak = 0
        module._signed_trip_streak = 0
        module._last_signal = ""
        module._last_signed_signal = ""

    def test_a_signed_461_darkens_only_the_signed_fallback(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        old = (module._tripped_until, module._signed_tripped_until, module._trip_streak,
               module._signed_trip_streak, module._last_signal, module._last_signed_signal)
        try:
            self._reset_breakers(module)
            with self.assertRaises(module.XhsRiskSignal):
                module._guard(461, {})
            self.assertTrue(module._signed_tripped(), "the signed fallback must go dark")
            self.assertFalse(module._tripped(),
                             "a 461 on our forged edith call must NOT darken the primary browser path")
        finally:
            (module._tripped_until, module._signed_tripped_until, module._trip_streak,
             module._signed_trip_streak, module._last_signal, module._last_signed_signal) = old

    def test_account_level_signals_still_darken_everything(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        old = (module._tripped_until, module._signed_tripped_until, module._trip_streak,
               module._signed_trip_streak, module._last_signal, module._last_signed_signal)
        for status, body in ((200, {"code": -1, "success": False}), (200, {"code": 300012})):
            try:
                self._reset_breakers(module)
                with self.assertRaises(module.XhsRiskSignal):
                    module._guard(status, body)
                self.assertTrue(module._tripped(), f"{body} is account-level: everything dark")
                self.assertTrue(module._signed_tripped())
            finally:
                (module._tripped_until, module._signed_tripped_until, module._trip_streak,
                 module._signed_trip_streak, module._last_signal, module._last_signed_signal) = old


class XiaohongshuCNDailyBudgetTests(unittest.TestCase):
    """The daily volume cap must survive the process turning over.

    MediaCrawler #769: for this WARNED account the binding constraint is cumulative VOLUME, not
    per-request rate, which makes _DAILY_REQ_CAP the most load-bearing guard in the module. Until
    2026-08-11 it lived only in module globals, so every eye-http restart (launchd KeepAlive, a
    deploy, a crash) silently handed the account a fresh 150-touch budget.
    """

    def setUp(self):
        from omniseek.core.sources.walled import xiaohongshu_cn_source as module

        self.module = module
        self._saved = (module._DAILY_STATE_PATH, module._DAILY_REQ_CAP, module._daily_count,
                       module._daily_key, module._daily_ledger_warned, module._tripped_until,
                       module._trip_streak, module._last_signal)
        self._tmp = tempfile.TemporaryDirectory()
        module._DAILY_STATE_PATH = Path(self._tmp.name) / "xhs-cn-daily-budget.json"
        self._restart()

    def tearDown(self):
        (self.module._DAILY_STATE_PATH, self.module._DAILY_REQ_CAP, self.module._daily_count,
         self.module._daily_key, self.module._daily_ledger_warned, self.module._tripped_until,
         self.module._trip_streak, self.module._last_signal) = self._saved
        self._tmp.cleanup()

    def _restart(self):
        """Put the module back into the state a FRESH import would leave it in: the ledger on
        disk is untouched, exactly as a real eye-http restart leaves things."""
        self.module._daily_count = 0
        self.module._daily_key = ""
        self.module._daily_ledger_warned = False
        self.module._tripped_until = 0.0
        self.module._trip_streak = 0
        self.module._last_signal = ""

    def test_a_restart_does_not_re_grant_the_daily_budget(self):
        for _ in range(7):
            self.module._bump_daily()
        self.assertEqual(self.module._daily_count, 7)

        self._restart()
        self.module._bump_daily()
        self.assertEqual(self.module._daily_count, 8,
                         "a restart must continue the day's count, never restart it")
        self.assertEqual(self.module._daily_spent(), 8)

    def test_the_cap_trips_on_spend_accumulated_across_restarts(self):
        self.module._DAILY_REQ_CAP = 3
        for _ in range(3):
            self.module._bump_daily()          # budget exactly spent in "process A"

        self._restart()                        # ... and the service turns over
        with self.assertRaises(self.module.XhsRiskSignal):
            self.module._bump_daily()          # the 4th touch of the DAY, not of the process
        self.assertTrue(self.module._tripped(), "a breached daily cap is account-level: all dark")

    def test_yesterdays_ledger_never_charges_today(self):
        self.module._DAILY_STATE_PATH.write_text(
            json.dumps({"date": "2000-01-01", "count": 149}), encoding="utf-8")
        self.module._bump_daily()
        self.assertEqual(self.module._daily_count, 1, "a stale day must roll over, not carry")
        self.assertEqual(self.module._daily_spent(), 1)

    def test_an_unusable_ledger_degrades_to_in_memory_counting_and_never_raises(self):
        # the state path is a DIRECTORY: every read and every write raises underneath.
        self.module._DAILY_STATE_PATH.mkdir(parents=True)
        for _ in range(2):
            self.module._bump_daily()          # must not raise: retrieval outranks the ledger
        self.assertEqual(self.module._daily_count, 2)
        self.assertTrue(self.module._daily_ledger_warned, "the degradation is announced once")

    def test_a_second_process_cannot_spend_the_same_budget_twice(self):
        for _ in range(4):
            self.module._bump_daily()
        # another process wrote a HIGHER count for today while this one was idle
        self.module._DAILY_STATE_PATH.write_text(
            json.dumps({"date": self.module._today(), "count": 40}), encoding="utf-8")
        self.module._bump_daily()
        self.assertEqual(self.module._daily_count, 41,
                         "the ledger reconciles upward; a stale reader never rewinds the spend")


class XiaohongshuDualHostNavTests(unittest.TestCase):
    """_goto_note_dual_host: a tokened note read survives a rednote.com-only network
    outage by retrying once on xiaohongshu.com (guest-readable), and nothing else."""

    @staticmethod
    def _page(first_error=None):
        calls = []

        class _Page:
            def goto(self, url, **kwargs):
                calls.append(url)
                if len(calls) == 1 and first_error is not None:
                    raise first_error

        return _Page(), calls

    def test_net_error_retries_the_same_note_on_the_sibling_host(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        url = "https://www.rednote.com/search_result/0123456789abcdef01234567?xsec_token=tok&xsec_source="
        page, calls = self._page(RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED at " + url))
        module._goto_note_dual_host(page, url)

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[1],
            "https://www.xiaohongshu.com/search_result/0123456789abcdef01234567?xsec_token=tok&xsec_source=",
        )

    def test_goto_timeout_also_earns_the_retry(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        url = "https://www.rednote.com/search_result/0123456789abcdef01234567?xsec_token=tok&xsec_source="
        page, calls = self._page(RuntimeError("Timeout 30000ms exceeded."))
        module._goto_note_dual_host(page, url)

        self.assertEqual(len(calls), 2)
        self.assertIn("www.xiaohongshu.com", calls[1])

    def test_non_network_errors_reraise_without_a_retry(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        url = "https://www.rednote.com/search_result/0123456789abcdef01234567?xsec_token=tok&xsec_source="
        page, calls = self._page(ValueError("target closed for an unrelated reason"))
        with self.assertRaises(ValueError):
            module._goto_note_dual_host(page, url)
        self.assertEqual(len(calls), 1)

    def test_non_rednote_urls_never_cross_hosts(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        url = "https://example.com/whatever"
        page, calls = self._page(RuntimeError("net::ERR_CONNECTION_CLOSED"))
        with self.assertRaises(RuntimeError):
            module._goto_note_dual_host(page, url)
        self.assertEqual(len(calls), 1)

    def test_success_navigates_exactly_once(self):
        from omniseek.core.sources.walled import xiaohongshu_source as module

        url = "https://www.rednote.com/search_result/0123456789abcdef01234567?xsec_token=tok&xsec_source="
        page, calls = self._page()
        module._goto_note_dual_host(page, url)
        self.assertEqual(calls, [url])


class EmptyDetailDiagnosticTests(unittest.TestCase):
    """A blank note page must say WHY it was blank.

    The old diagnostic, "detail navigation succeeded but returned no title/body/media/comments",
    is true and useless: it tells the caller a page was empty, not what to do instead. A real
    caller hit it on a bare /explore/<id>, concluded the message must be generated by the MCP tool
    handler (it is not; it is this line), and nearly patched the wrong layer. Controlled A/B on
    2026-08-12: the SAME note id read fine with its xsec_token and produced this branch without it.
    """

    ADAPTERS = ("xiaohongshu_source.py", "xiaohongshu_cn_source.py")

    def _src(self, name):
        return (Path(__file__).resolve().parents[1] / "src" / "omniseek" / "core" / "sources"
                / "walled" / name).read_text(encoding="utf-8")

    def test_the_no_token_case_is_told_apart_and_named(self):
        for f in self.ADAPTERS:
            s = self._src(f)
            self.assertIn("missing_xsec_token", s, f"{f}: no distinct event for the token case")
            self.assertIn('"xsec_token=" not in', s, f"{f}: does not branch on token presence")

    def test_the_message_points_at_the_fix_not_just_the_symptom(self):
        """Naming a cause without naming the remedy still leaves the caller guessing."""
        for f in self.ADAPTERS:
            s = self._src(f)
            self.assertIn("omniseek_search", s, f"{f}: does not tell the caller where a tokened URL comes from")

    def test_the_tokened_failure_says_it_is_NOT_the_token_case(self):
        """A URL that DOES carry a token and still comes back empty is a different problem
        (deleted, private, lost login). Saying so stops the next reader from chasing the token."""
        for f in self.ADAPTERS:
            s = self._src(f)
            self.assertIn("not the token case", s, f"{f}: the two failures are not told apart")


if __name__ == "__main__":
    unittest.main()
