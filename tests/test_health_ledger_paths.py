"""The browser, yt-dlp and curl_cffi paths of the health ledger, and failure messages with their status
(2026-10-04, the four items: items 3 and 4).

Pins, offline (every upstream is a stub; no browser, no network, no real credential):

  (a) the CDP helper (``_cdp.cdp_call``), on both of its paths (one thread per call, and the pool):
      while a health check's ledger is open, the page's main-frame document answers and every 429 of
      an in-page fetch / XHR go into the ledger; a page whose last main document answered 429 is
      refused (``RateLimitedPage``), so a check that only reads the page cannot call it working;
      the site's other traffic (a background XHR that fails, an image, a frame) is not the check's
      and is not recorded; outside a health check nothing is attached to the page at all;
  (b) yt-dlp (``omniseek.core.ytdlp``): every HTTP error answer of an extraction is noted, so a 429
      reads not verified; another error answer stays evidence; outside a check nothing changes;
  (c) the health funnel end to end on sources that read the page alone (zhihu: its URL) or extract
      through yt-dlp (youtube_channels): a 429 is None, no longer True / False;
  (d) failure messages carry the HTTP status: the curl_cffi sources' checks ("HTTP 504", not "fetch
      failed"), the shared curl tier, and an RSS bundle's dead feeds.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import _probe, fetcher, http  # noqa: E402
from omniseek.core.sources.walled import _cdp  # noqa: E402

_HAS_YTDLP = importlib.util.find_spec("yt_dlp") is not None
_HAS_CURL = importlib.util.find_spec("curl_cffi") is not None


# ── a stand-in Playwright: each page load and in-page fetch gets the status the test sets ──────────
class _Resp:
    def __init__(self, url, status, kind, frame, retry_after=None):
        self.url, self.status, self.frame = url, status, frame
        self.headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
        self.request = type("_Req", (), {"resource_type": kind})()


class _Page:
    """``goto`` answers ``doc_status``; ``evaluate`` of a script with ``fetch(`` answers ``fetch_status``;
    ``noise`` lists (kind, status, main_frame?) the site's own traffic fires on every page load."""

    def __init__(self, script):
        self.script, self.main_frame, self.url, self.listeners = script, object(), "about:blank", []
        script.setdefault("pages", []).append(self)

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def _fire(self, resp):
        for event, handler in self.listeners:
            if event == "response":
                handler(resp)
        return resp

    def goto(self, url, **_k):
        self.url = url
        for kind, status, main in self.script.get("noise", ()):
            self._fire(_Resp(url + "/noise", status, kind, self.main_frame if main else object()))
        return self._fire(_Resp(url, self.script["doc_status"], "document", self.main_frame,
                                self.script.get("retry_after")))

    def evaluate(self, js, *_a, **_k):
        if "fetch(" in js:
            self._fire(_Resp(self.url + "/api", self.script["fetch_status"], "fetch", self.main_frame,
                             self.script.get("retry_after")))
        return {"ok": True}

    def close(self):
        return None


class _Playwright:
    def __init__(self, script):
        ctx = type("_Ctx", (), {})()
        ctx.pages = []
        ctx.new_page = lambda: _Page(script)
        browser = type("_Browser", (), {"contexts": [ctx], "is_connected": lambda self_: True})()
        self.chromium = type("_C", (), {"connect_over_cdp": lambda self_, *a, **k: browser})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def start(self):
        return self

    def stop(self):
        return None


class CdpPathsReachTheLedger(unittest.TestCase):
    POOL = False
    _port = 9390

    def setUp(self):
        type(self)._port += 1
        self.cdp_url = f"http://127.0.0.1:{self._port}"   # a port of its own: a fresh gate / pool
        self.script = {"doc_status": 200, "fetch_status": 200}
        self.patches = [mock.patch.object(_cdp, "sync_playwright", lambda: _Playwright(self.script)),
                        mock.patch.object(_cdp, "_browser_instance", lambda url: "stand-in"),
                        mock.patch.object(_cdp, "ensure_browser", lambda url: None),
                        mock.patch.dict(os.environ, {"OMNISEEK_CDP_POOL": "1" if self.POOL else "0"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _call(self, callback, url="https://site.example/home"):
        return _cdp.cdp_call(callback, initial_url=url, timeout=20, cdp_url=self.cdp_url)

    def test_a_a_429_page_is_refused_and_recorded_with_its_retry_after(self):
        self.script.update(doc_status=429, retry_after=30)
        with _probe.watching() as led:
            with self.assertRaises(_cdp.RateLimitedPage) as cm:
                self._call(lambda page: page.url)       # zhihu's check: the URL alone
        self.assertIn("HTTP 429 from site.example, Retry-After 30s", str(cm.exception))
        self.assertEqual(led.held, [("429", "site.example", 30.0)])
        self.assertIsNone(_probe.reread(False, f"RateLimitedPage: {cm.exception}", led)[0])

    def test_a_an_in_page_fetch_429_is_recorded_and_the_result_handed_back(self):
        self.script.update(fetch_status=429)
        with _probe.watching() as led:
            out = self._call(lambda page: page.evaluate("() => fetch('/api/search')"))
        self.assertEqual(out, {"ok": True})
        self.assertEqual([h[:2] for h in led.held], [("429", "site.example")])
        self.assertEqual((led.errors, led.answers), ([], 2))   # the document (200) and the fetch

    def test_a_the_sites_own_traffic_is_not_the_checks(self):
        # a background XHR that fails, an image rate-limited, a frame's document rate-limited
        self.script.update(noise=[("xhr", 404, True), ("image", 429, True), ("document", 429, False),
                                  ("fetch", 500, True)])
        with _probe.watching() as led:
            self.assertEqual(self._call(lambda page: page.url), "https://site.example/home")
        self.assertEqual((led.held, led.errors, led.answers), ([], [], 1))

    def test_a_another_error_page_is_evidence_and_handed_back(self):
        self.script.update(doc_status=503)
        with _probe.watching() as led:
            self.assertEqual(self._call(lambda page: page.url), "https://site.example/home")
        self.assertEqual((led.held, led.errors), ([], [(503, "site.example")]))
        self.assertEqual(_probe.reread(False, "down", led), (False, "down"))

    def test_a_outside_a_health_check_nothing_is_attached_or_refused(self):
        self.script.update(doc_status=429, fetch_status=429)
        self.assertEqual(self._call(lambda page: page.url), "https://site.example/home")
        self.assertEqual(self.script["pages"][-1].listeners, [])
        self.assertEqual(_cdp._watched, {})


class CdpPoolPathReachesTheLedger(CdpPathsReachTheLedger):
    POOL = True
    _port = 9490


# ── yt-dlp ─────────────────────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_HAS_YTDLP, "yt-dlp is not installed (a base dependency; a stripped install)")
class YtDlpReachesTheLedger(unittest.TestCase):
    def _answer(self, status, retry_after=None):
        import yt_dlp
        from yt_dlp.networking.common import Response
        from yt_dlp.networking.exceptions import HTTPError

        def urlopen(self_, req):
            url = getattr(req, "url", None) or str(req)
            headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
            raise HTTPError(Response(io.BytesIO(b""), url, headers, status=status, reason="x"))
        return mock.patch.object(yt_dlp.YoutubeDL, "urlopen", urlopen)

    def test_b_an_http_error_answer_is_noted(self):
        from yt_dlp.networking.exceptions import HTTPError

        from omniseek.core import ytdlp
        with self._answer(429, retry_after=60), _probe.watching() as led:
            with self.assertRaises(HTTPError):
                ytdlp.YoutubeDL({"quiet": True}).urlopen("https://www.youtube.com/results?q=x")
        self.assertEqual(led.held, [("429", "www.youtube.com", 60.0)])
        with self._answer(404), _probe.watching() as led2:
            with self.assertRaises(HTTPError):
                ytdlp.YoutubeDL({"quiet": True}).urlopen("https://slideslive.com/x")
        self.assertEqual((led2.held, led2.errors), ([], [(404, "slideslive.com")]))
        with self._answer(429), self.assertRaises(HTTPError):   # outside a check: just the error
            ytdlp.YoutubeDL({"quiet": True}).urlopen("https://www.youtube.com/x")

    def test_c_a_channel_extraction_rate_limited_reads_not_verified(self):
        from omniseek.core import cache
        from omniseek.core.sources.scrape import youtube_channels_source as yc
        with self._answer(429, retry_after=1), mock.patch.object(cache, "get", lambda *a, **k: None), \
                mock.patch.object(cache, "set", lambda *a, **k: None):
            ok, msg = fetcher._safe_health(yc.YoutubeChannelsAdapter())
        self.assertIsNone(ok, msg)
        self.assertIn("HTTP 429 from www.youtube.com", msg)
        with self._answer(404), mock.patch.object(cache, "get", lambda *a, **k: None), \
                mock.patch.object(cache, "set", lambda *a, **k: None):
            ok, msg = fetcher._safe_health(yc.YoutubeChannelsAdapter())
        self.assertIs(ok, False, msg)


class ZhihuReadsA429PageAsNotVerified(unittest.TestCase):
    def test_c_the_url_alone_no_longer_calls_a_429_page_working(self):
        from omniseek.core.sources.walled import zhihu_source as zs
        script = {"doc_status": 429, "fetch_status": 200, "retry_after": 1}
        with mock.patch.object(_cdp, "sync_playwright", lambda: _Playwright(script)), \
                mock.patch.object(_cdp, "ensure_browser", lambda url: None), \
                mock.patch.dict(os.environ, {"OMNISEEK_CDP_POOL": "0"}), \
                mock.patch.object(zs, "cdp_health", lambda *a, **k: (True, "up")):
            ok, msg = fetcher._safe_health(zs.ZhihuAdapter())
            self.assertIsNone(ok, msg)
            self.assertIn("HTTP 429 from www.zhihu.com", msg)
            script["doc_status"] = 200
            self.assertIs(fetcher._safe_health(zs.ZhihuAdapter())[0], True)


# ── (d) the status in the failure message ──────────────────────────────────────────────────────────
def _curl_resp(status: int, body: bytes = b"{}", url: str = "https://up.example/x"):
    from curl_cffi import requests as creq
    r = creq.Response()
    r.status_code, r.ok, r.url, r.reason = status, 200 <= status < 400, url, "Gateway Timeout"
    r.headers = creq.Headers({"Content-Type": "text/html"})
    r.content = body
    return r


@unittest.skipUnless(_HAS_CURL, "curl_cffi is not installed")
class FailuresSayTheStatus(unittest.TestCase):
    def _answer(self, status, body=b"<html>gateway timeout</html>"):
        from curl_cffi import requests as creq

        def request(self_, method, url, *a, **k):
            return _curl_resp(status, body, url=url)
        return mock.patch.object(creq.Session, "request", request)

    def test_d_the_curl_sources_name_the_status(self):
        from omniseek.core.sources.scrape import (cninfo_source, eastmoney_source, gov_policy_source,
                                                juejin_source)
        for ad in (cninfo_source.CninfoAdapter(), gov_policy_source.GovPolicyAdapter(),
                   juejin_source.JuejinAdapter(), eastmoney_source.EastMoneyAdapter()):
            with self.subTest(source=ad.name), self._answer(504):
                ok, msg = ad.health_check()
                self.assertIs(ok, False, msg)
                self.assertIn("HTTP 504", msg)

    def test_d_a_failure_without_a_response_keeps_its_exception(self):
        from omniseek.core import curl
        self.assertEqual(curl.failure(ValueError("boom")), "ValueError: boom")
        with self._answer(504):
            r = curl.get("https://up.example/x")
        with self.assertRaises(Exception) as cm:
            r.raise_for_status()
        self.assertEqual(curl.failure(cm.exception), "HTTP 504 Gateway Timeout")

    def test_d_the_shared_curl_tier_and_an_rss_bundle_name_the_status(self):
        from omniseek.core.sources.scrape._rss import RSSAdapterBase

        class _Bundle(RSSAdapterBase):
            name = "_status_test_bundle"
            feeds = ["https://feeds.example/jobs.rss"]
            tls_impersonate = True
        with self._answer(504), mock.patch.object(http._netguard, "security_block_reason", lambda u: None):
            ok, msg = _Bundle().health_check()
        self.assertIs(ok, False, msg)
        self.assertEqual(msg, "all 1 feeds failed (feeds.example: HTTP 504)")


if __name__ == "__main__":
    unittest.main()
