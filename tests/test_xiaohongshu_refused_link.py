"""A note link the platform refuses must stop at the browser, never fall through to the signed API.

What happened (2026-09-26, mini): an App share link (xsec_token CB..., xsec_source=app_share) for a
note older than about 60 days opened in the logged-in 9224 browser and was server-redirected to
/404/sec_... and then /404. The mainland adapter read that as an empty page and fell through to the
signed feed call, which carried a hardcoded xsec_source=pc_feed and drew HTTP 461 / code 300031;
the signed breaker opened, and omniseek_read reported only "breaker open", so the caller never learned
that the platform had refused the link. Every 461 in the incident black box came down this path,
and each one is a risk signal on an account the platform has already warned.

The contract pinned here:
  - a /404 landing is reported as ``refused:`` together with the address, by both adapters, and
    nothing else is asked about that link;
  - the signed fallback runs only when the browser never got the page open AND the link carries an
    xsec_token AND it is not an App share link (xsec_source=app_share); the request sends the
    hardcoded pc_feed, which with a search-minted token is the combination measured live 2026-06-18;
  - a page that did open (an empty shell, or a flow that died after goto) never reaches it.

Some tests here REPRODUCE the defect (they fail on fa8ca04) and some GUARD behaviour that fa8ca04
already had and this change must keep (search-token links still get the signed fallback, with
pc_feed). Each test's docstring says which.

STATE ISOLATION: the mainland adapter writes the incident black box, the daily budget ledger and
the note cache. setUpModule points _INCIDENT_PATH and _DAILY_STATE_PATH at a temp dir, every test
runs on a temp cache dir, and _bump_daily is stubbed, so this suite touches none of the real state
files on the machine that runs it. No browser and no network: cdp_call is replaced by a stub that
drives the adapter's REAL note flow against a fake page, and the signed interface is a recorder.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniseek.core import cache, fetcher, web_fallback
from omniseek.core.sources.walled import _human
from omniseek.core.sources.walled import xiaohongshu_cn_source as CN
from omniseek.core.sources.walled import xiaohongshu_source as INTL

NOTE_ID = "0123456789abcdef01234567"
SHARE_TOKEN = "CBfakeShareToken"
CN_SHARE = (f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
            f"?xsec_token={SHARE_TOKEN}&xsec_source=app_share")
SEARCH_TOKEN = "ABfakeSearchToken"
# the two shapes a search-minted link takes: the DOM search card (blank xsec_source, as in the smoke
# fixture) and _note_url's (xsec_source=pc_search)
CN_SEARCH_CARD = (f"https://www.xiaohongshu.com/search_result/{NOTE_ID}"
                  f"?xsec_token={SEARCH_TOKEN}&xsec_source=")
CN_SEARCH_API = (f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
                 f"?xsec_token={SEARCH_TOKEN}&xsec_source=pc_search")

_ISO = None
_REAL = {}


def setUpModule():
    global _ISO
    _ISO = tempfile.TemporaryDirectory()
    for name, fname in (("_INCIDENT_PATH", "xhs-cn-incidents.jsonl"),
                        ("_DAILY_STATE_PATH", "xhs-cn-daily-budget.json")):
        _REAL[name] = getattr(CN, name)
        setattr(CN, name, Path(_ISO.name) / fname)


def tearDownModule():
    for name, value in _REAL.items():
        setattr(CN, name, value)
    if _ISO is not None:
        _ISO.cleanup()


class _Locator:
    """Every selector matches nothing: no captcha, no login overlay, no note body, no search box."""

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False

    def inner_text(self, **_kw):
        return ""

    def get_attribute(self, _name):
        return None


_EMPTY_SHELL = ("<html><body><main><div id='detail-title'></div>"
                "<div id='detail-desc'></div></main></body></html>")


class _Page:
    """Just enough of a Playwright page for the two note flows.

    ``land`` is where the server's redirect chain ends, i.e. what ``page.url`` reads once goto has
    returned (goto follows server redirects itself). ``goto_error`` makes the navigation fail, and
    ``content_error`` makes the page die after it had opened."""

    def __init__(self, land=None, goto_error=None, content_error=None, html=_EMPTY_SHELL):
        self.url = "about:blank"
        self.frames = [self]
        self.gotos = []
        self._land = land
        self._goto_error = goto_error
        self._content_error = content_error
        self._html = html

    def on(self, _event, _handler):
        pass

    def goto(self, url, **_kw):
        self.gotos.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        self.url = self._land or url

    def locator(self, _selector):
        return _Locator()

    def evaluate(self, *_a, **_kw):
        return None

    def content(self):
        if self._content_error is not None:
            raise self._content_error
        return self._html


def _drive(page):
    """cdp_call stand-in that runs the adapter's own flow against ``page``."""
    def _cdp_call(callback, **_kw):
        return callback(page)
    return _cdp_call


def _unreachable(*_a, **_kw):
    """cdp_call stand-in for a browser that cannot be reached: it fails before any tab exists, so
    the flow never runs. The message has the shape of Playwright's connect failure."""
    raise RuntimeError("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9224")


class _Signed:
    """Records every call into the signed interface (every edith request goes through _signed_post
    or _signed_get). The feed answer carries no items, so a read that gets this far ends there."""

    def __init__(self):
        self.calls = []

    def post(self, path, payload, _cookies):
        self.calls.append(("POST", path, dict(payload)))
        return {"code": 0, "data": {"items": []}}

    def get(self, path, params, _cookies):
        self.calls.append(("GET", path, dict(params)))
        return {"code": 0, "data": {}}


def _noop(*_a, **_kw):
    return None


class _OfflineCase(unittest.TestCase):
    """Temp cache dir and zero human delays (the delays are real sleeps)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [patch.object(cache, "CACHE_DIR", Path(self._tmp.name))]
        self._patches += [patch.object(_human, n, _noop)
                          for n in ("read_dwell", "scroll_like_reading", "action_pause", "short_pause")]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


class _MainlandCase(_OfflineCase):
    _GLOBALS = {"_tripped_until": 0.0, "_signed_tripped_until": 0.0, "_trip_streak": 0,
                "_signed_trip_streak": 0, "_last_signal": "", "_last_signed_signal": "",
                # no human gap between flows: the previous test's 5-15s gap would otherwise be slept
                "_browser_last_flow": 0.0, "_browser_next_gap": 0.0}

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(CN, n) for n in self._GLOBALS}
        for n, v in self._GLOBALS.items():
            setattr(CN, n, v)
        self.signed = _Signed()
        more = [patch.object(CN, "_SEALED", False),  # the mainland read path as it returns on restore
                patch.object(CN, "_bump_daily", _noop),
                patch.object(CN, "_note_browser_cdp", _noop),
                patch.object(CN, "_DEPS_OK", True),        # the signed fallback is armed
                patch.object(CN, "_signed_ready", lambda: (True, "")),
                patch.object(CN, "_get_cookies", lambda force=False: {}),
                patch.object(CN, "_signed_post", self.signed.post),
                patch.object(CN, "_signed_get", self.signed.get),
                patch.object(CN, "_xhs_load_comments", _noop)]
        for p in more:
            p.start()
        self._patches += more

    def tearDown(self):
        super().tearDown()
        for n, v in self._saved.items():
            setattr(CN, n, v)

    def _read(self, url, cdp):
        """omniseek_read's own path: fetcher.fetch_url_with_reason, with this adapter as the only reader."""
        with patch.object(CN, "cdp_call", cdp), \
             patch.object(fetcher, "_adapters", {"xiaohongshu_cn": CN.XiaohongshuCNAdapter()}), \
             patch.object(web_fallback, "read_via_fallback", lambda _url: None):
            return fetcher.fetch_url_with_reason(url)


class MainlandRefusedLinkTests(_MainlandCase):
    def test_a_404_landing_is_refused_and_the_signed_api_is_never_asked(self):
        """REPRODUCES: fa8ca04 read the /404 page as an empty shell and sent the signed feed call."""
        page = _Page(land="https://www.xiaohongshu.com/404/sec_Fk3xQ9?src=app&xsec_token=CBleak")
        doc, reason = self._read(CN_SHARE, _drive(page))
        self.assertIsNone(doc)
        self.assertEqual(self.signed.calls, [], "a refused link must never reach the signed API")
        self.assertTrue((reason or "").startswith("xiaohongshu_cn: refused:"), reason)
        self.assertIn("https://www.xiaohongshu.com/404/sec_Fk3xQ9", reason)
        self.assertNotIn("CBleak", reason, "the landing's query string must not ride into the reason")
        self.assertEqual(page.gotos, [CN_SHARE], "exactly one navigation")
        # a verdict on one link: neither breaker may open over it
        self.assertFalse(CN._tripped())
        self.assertFalse(CN._signed_tripped())

    def test_an_empty_shell_stops_at_the_browser(self):
        """REPRODUCES: fa8ca04 fell through to the signed API after an opened page gave nothing."""
        doc, reason = self._read(CN_SHARE, _drive(_Page()))
        self.assertIsNone(doc)
        self.assertEqual(self.signed.calls, [], "the page opened, so the signed API is not asked")
        self.assertIn("not the token case", reason or "")

    def test_a_flow_that_dies_after_the_page_opened_does_not_fall_back(self):
        """REPRODUCES: fa8ca04 fell through to the signed API when the flow died after goto."""
        page = _Page(content_error=RuntimeError("Target page, context or browser has been closed"))
        doc, reason = self._read(CN_SHARE, _drive(page))
        self.assertIsNone(doc)
        self.assertEqual(self.signed.calls, [])
        self.assertIn("after the note page opened", reason or "")
        # and it was THIS failure (the page dying at the end), not some earlier stub gap: the black
        # box row (sandboxed by setUpModule) carries the exception text
        last = json.loads(CN._INCIDENT_PATH.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["kind"], "browser_fetch_error")
        self.assertIn("has been closed", last["exc"])


class MainlandSignedFallbackTests(_MainlandCase):
    def test_an_unreachable_browser_never_signs_a_share_link(self):
        """REPRODUCES: fa8ca04 sent an App share token to the signed API (as pc_feed)."""
        doc, reason = self._read(CN_SHARE, _unreachable)
        self.assertIsNone(doc)
        self.assertEqual(self.signed.calls, [], "a share link is read through the browser only")
        self.assertIn("share link", reason or "")

    def test_an_unreachable_browser_without_a_token_never_calls_the_signed_api(self):
        """REPRODUCES: fa8ca04 sent a signed feed call with an empty xsec_token."""
        doc, reason = self._read(f"https://www.xiaohongshu.com/explore/{NOTE_ID}", _unreachable)
        self.assertIsNone(doc)
        self.assertEqual(self.signed.calls, [])
        self.assertIn("carries no xsec_token", reason or "")

    def test_an_unreachable_browser_signs_a_search_card_link_with_pc_feed(self):
        """GUARDS: a search-card link (blank xsec_source) keeps the signed fallback, as pc_feed."""
        doc, _reason = self._read(CN_SEARCH_CARD, _unreachable)
        self.assertIsNone(doc)                        # the recorder's feed answer has no items
        self.assertEqual([c[1] for c in self.signed.calls], [CN._FEED], self.signed.calls)
        payload = self.signed.calls[0][2]
        self.assertEqual(payload["xsec_source"], "pc_feed")
        self.assertEqual(payload["xsec_token"], SEARCH_TOKEN)

    def test_an_unreachable_browser_signs_a_pc_search_link_with_pc_feed(self):
        """GUARDS: a pc_search link keeps the signed fallback, still sent as pc_feed."""
        self._read(CN_SEARCH_API, _unreachable)
        self.assertEqual([c[1] for c in self.signed.calls], [CN._FEED], self.signed.calls)
        self.assertEqual(self.signed.calls[0][2]["xsec_source"], "pc_feed")
        self.assertEqual(self.signed.calls[0][2]["xsec_token"], SEARCH_TOKEN)

    def test_a_goto_that_times_out_counts_as_never_opened(self):
        """GUARDS: a goto that times out never opened the page, so a search link still falls back."""
        page = _Page(goto_error=TimeoutError("Page.goto: Timeout 30000ms exceeded."))
        self._read(CN_SEARCH_API, _drive(page))
        self.assertEqual(page.gotos, [CN_SEARCH_API], "the flow reached goto, and goto is what failed")
        self.assertEqual([c[1] for c in self.signed.calls], [CN._FEED], self.signed.calls)
        self.assertEqual(self.signed.calls[0][2]["xsec_source"], "pc_feed")


class InternationalRefusedLinkTests(_OfflineCase):
    _GLOBALS = {"_backoff_until": 0.0, "_last_live_call": 0.0, "_next_interval": 0.0,
                "_consec_cdp_err": 0}

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(INTL, n) for n in self._GLOBALS}
        for n, v in self._GLOBALS.items():
            setattr(INTL, n, v)
        p = patch.object(INTL, "_load_comments", _noop)
        p.start()
        self._patches.append(p)

    def tearDown(self):
        super().tearDown()
        for n, v in self._saved.items():
            setattr(INTL, n, v)

    def test_a_404_landing_is_refused_without_tripping_the_account_backoff(self):
        """REPRODUCES: on a 404 page with no search box fa8ca04 saw a logout and tripped the 6h backoff."""
        url = f"https://www.rednote.com/explore/{NOTE_ID}?xsec_token={SHARE_TOKEN}&xsec_source=app_share"
        page = _Page(land="https://www.rednote.com/404/sec_Fk3xQ9")
        with patch.object(INTL, "cdp_call", _drive(page)), \
             patch.object(fetcher, "_adapters", {"xiaohongshu": INTL.XiaohongshuAdapter()}), \
             patch.object(web_fallback, "read_via_fallback", lambda _url: None):
            doc, reason = fetcher.fetch_url_with_reason(url)
        self.assertIsNone(doc)
        self.assertTrue((reason or "").startswith("xiaohongshu: refused:"), reason)
        self.assertIn("https://www.rednote.com/404/sec_Fk3xQ9", reason)
        # A refusal is the platform's word on this link. Read as a login wall it would trip the
        # 6h account backoff and darken every other read.
        self.assertEqual(INTL._backoff_until, 0.0)


if __name__ == "__main__":
    unittest.main()
