"""Contract follow-ups (2026-10-04): 429 and an open breaker are "not verified"; logins are probed.

Pins, offline (every upstream is a stub; no network, no real credential):

  (a) the ledger rules (omniseek.core._probe): a False that comes only from a 429, a rate-limit body
      or a refused gate becomes None with the reason; any other error answer keeps the False; a
      True is never touched; the shared http helper and the declared gate feed it;
  (b) the adapters that read the status themselves or keep their own client, one by one: a 429 is
      None (with Retry-After when given), an open breaker is None (with the reopening time);
  (c) bluesky and openreview probe the search path itself on every check, reusing the login: True
      on a parsed answer with at least one item; None on 429 or a busy gate; False on any other
      error, an unparseable answer or zero items; a refused kept login is re-made ONCE;
  (d) the curl_cffi sources go through omniseek.core.curl, so their responses are recorded and a 429
      they get reads as not verified, while a 5xx still reads as down.

The sweep over every registered source is tests/test_health_guard_sweep.py.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from omniseek.core import _probe, cache, diag, fetcher, http, upstreams  # noqa: E402
from omniseek.core._guard import GateBusy  # noqa: E402


def _resp(status: int, *, json=None, text: str = "", headers=None, url: str = "https://up.example/x"):
    req = httpx.Request("GET", url)
    if json is not None:
        return httpx.Response(status, json=json, headers=headers, request=req)
    return httpx.Response(status, text=text, headers=headers, request=req)


def _status_error(status: int, url: str = "https://up.example/x", headers=None):
    r = _resp(status, headers=headers, url=url)
    return httpx.HTTPStatusError(f"HTTP {status}", request=r.request, response=r)


class _Adapter:
    """A stand-in source whose check runs ``body`` inside the health funnel."""
    name = "_probe_test"

    def __init__(self, body):
        self._body = body

    def health_check(self):
        return self._body()


# ── (a) the ledger ───────────────────────────────────────────────────────────────────────────────
class LedgerRules(unittest.TestCase):
    def test_a_false_from_a_429_alone_reads_none_with_the_reason(self):
        with _probe.watching() as led:
            _probe.note_response(429, where="up.example", retry_after=7)
        ok, msg = _probe.reread(False, "request failed", led)
        self.assertIsNone(ok)
        self.assertIn("rate-limited (HTTP 429 from up.example, Retry-After 7s)", msg)
        self.assertIn("the check said: request failed", msg)

    def test_a_another_error_answer_keeps_the_false(self):
        with _probe.watching() as led:
            _probe.note_response(429, where="up.example")
            _probe.note_response(503, where="up.example")
        self.assertEqual(_probe.reread(False, "down", led), (False, "down"))

    def test_a_a_true_is_never_touched_and_none_stays_none(self):
        with _probe.watching() as led:
            _probe.note_response(429, where="up.example")
        self.assertEqual(_probe.reread(True, "OK", led), (True, "OK"))
        self.assertEqual(_probe.reread(None, "not probed", led), (None, "not probed"))

    def test_a_a_refused_gate_reads_none(self):
        with _probe.watching() as led:
            upstreams.UpstreamBusy("x: no permit within 0.1s (gate saturated); not sent")
            GateBusy("pool busy")
        ok, msg = _probe.reread(False, "failed", led)
        self.assertIsNone(ok)
        self.assertIn("an eye gate held the request back", msg)

    def test_a_an_error_body_naming_a_rate_limit_counts_as_one(self):
        with _probe.watching() as led:
            _probe.note_response(403, where="api.github.com")
            _probe.note_error_body(403, '{"message": "API rate limit exceeded"}', where="api.github.com")
        ok, msg = _probe.reread(False, "failed", led)
        self.assertIsNone(ok)
        self.assertIn("HTTP 403", msg)
        with _probe.watching() as led2:
            _probe.note_response(403, where="x")
            _probe.note_error_body(403, "Forbidden", where="x")
        self.assertIs(_probe.reread(False, "failed", led2)[0], False)

    def test_a_outside_a_check_nothing_is_recorded_and_nested_ledgers_merge(self):
        _probe.note_response(429, where="nobody")  # no ledger open: a no-op, never raises
        with _probe.watching() as outer:
            with _probe.watching() as inner:
                _probe.note_response(429, where="in")
            self.assertEqual(len(inner.held), 1)
        self.assertEqual(len(outer.held), 1)

    def test_a_the_shared_http_helper_and_observe_feed_the_ledger(self):
        def handler(request):
            if request.url.path == "/limited":
                return httpx.Response(429, headers={"Retry-After": "12"}, text="slow down")
            return httpx.Response(403, text='{"error_name": "throttle_violation"}')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch.object(http, "_get_client", lambda: client), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None):
            with _probe.watching() as led:
                self.assertIsNone(http.get_json("https://up.example/limited"))
            self.assertEqual(led.held[-1][0], "429")
            self.assertEqual(led.held[-1][2], 12.0)
            with _probe.watching() as led2:
                self.assertIsNone(http.get_json("https://up.example/throttled"))
            self.assertEqual([h[0] for h in led2.held], ["body"])
            self.assertEqual(led2.errors, [])

    def test_a_the_health_funnel_rereads_every_source(self):
        def limited():
            upstreams.observe("https://up.example/a", {"Retry-After": "5"}, 429)
            return False, "request failed (timeout / network / oversize)"

        def busy():
            raise upstreams.UpstreamBusy("x: no permit within 0.1s (gate saturated); not sent")

        def broken():
            upstreams.observe("https://up.example/a", {}, 500)
            return False, "HTTP 500"
        ok, msg = fetcher._safe_health(_Adapter(limited))
        self.assertIsNone(ok)
        self.assertIn("Retry-After 5s", msg)
        self.assertIsNone(fetcher._safe_health(_Adapter(busy))[0])
        self.assertEqual(fetcher._safe_health(_Adapter(broken)), (False, "HTTP 500"))
        ok, msg, done = fetcher.health_check_outcome(_Adapter(limited), timeout=5)
        self.assertIsNone(ok)
        self.assertTrue(done)


# ── (b) adapters that read the status themselves ─────────────────────────────────────────────────
class AdaptersReadRateLimitAndBreakerAsNotVerified(unittest.TestCase):
    def _assert_rate_limited(self, result, retry_after: str = ""):
        ok, msg = result
        self.assertIsNone(ok, msg)
        self.assertIn("rate-limited", msg)
        if retry_after:
            self.assertIn(f"Retry-After {retry_after}", msg)

    def _assert_breaker(self, result):
        ok, msg = result
        self.assertIsNone(ok, msg)
        self.assertIn("circuit breaker open", msg)

    def test_b_s2_authors_and_core_429(self):
        from omniseek.core.sources.api import core_source, s2_authors_source
        with mock.patch.object(s2_authors_source.http, "direct",
                               lambda *a, **k: _resp(429, headers={"Retry-After": "30"})):
            self._assert_rate_limited(s2_authors_source.S2AuthorsAdapter().health_check(), "30s")
        ad = core_source.CoreAdapter()
        with mock.patch.object(core_source.CoreAdapter, "_key", lambda self: "test-key"), \
                mock.patch.object(core_source, "_core_get", lambda *a, **k: _resp(429)):
            self._assert_rate_limited(ad.health_check())
        with mock.patch.object(core_source.CoreAdapter, "_key", lambda self: "test-key"), \
                mock.patch.object(core_source, "_core_get", lambda *a, **k: _resp(200, json={})):
            self.assertIs(ad.health_check()[0], True)

    def test_b_arxiv_breaker_open_and_429(self):
        from omniseek.core.sources.api import arxiv_source
        g = arxiv_source._guard
        with mock.patch.dict(g.state, {"open_until": time.time() + 90}), \
                mock.patch.object(arxiv_source.http, "direct", lambda *a, **k: self.fail("sent")):
            ok, msg = arxiv_source.ArxivAdapter().health_check()
        self._assert_breaker((ok, msg))
        self.assertIn("reopens in", msg)

        class _Hold:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return False
        with mock.patch.object(g, "is_open", lambda: False), \
                mock.patch.object(g, "hold", lambda *a, **k: _Hold()), \
                mock.patch.object(arxiv_source.http, "direct",
                                  lambda *a, **k: _resp(429, headers={"Retry-After": "60"})):
            self._assert_rate_limited(arxiv_source.ArxivAdapter().health_check(), "60s")

    def test_b_s2_shared_probe_429_and_breaker(self):
        from omniseek.core import _s2

        def _limited(*_a, **_k):
            raise ConnectionRefusedError("HTTP status 429 Too Many Requests.")
        with mock.patch.dict(_s2._health, {"result": None, "at": 0.0}), \
                mock.patch.object(_s2, "_call", _limited):
            self._assert_rate_limited(_s2.health())
        with mock.patch.dict(_s2._health, {"result": None, "at": 0.0}), \
                mock.patch.dict(_s2._state, {"open_until": time.time() + 60}):
            self._assert_breaker(_s2.health())

    def test_b_github_shared_probe_rate_limit_breaker_and_outage(self):
        from omniseek.core import _github

        def _throttled(*_a, **_k):
            _github._state["last_429"] = time.time()
            return None
        with mock.patch.dict(_github._health, {"result": None, "at": 0.0}), \
                mock.patch.dict(_github._state, {"open_until": 0.0, "last_429": 0.0}), \
                mock.patch.object(_github, "get_json", _throttled):
            self._assert_rate_limited(_github.health())
        with mock.patch.dict(_github._health, {"result": None, "at": 0.0}), \
                mock.patch.dict(_github._state, {"open_until": 0.0, "last_429": 0.0}), \
                mock.patch.object(_github, "get_json", lambda *a, **k: None):
            self.assertIs(_github.health()[0], False)
        with mock.patch.dict(_github._health, {"result": None, "at": 0.0}), \
                mock.patch.dict(_github._state, {"open_until": time.time() + 60}):
            self._assert_breaker(_github.health())

    def test_b_openalex_and_stackexchange_shared_probes(self):
        from omniseek.core import _openalex, _stackexchange

        def _oa_429(*_a, **_k):
            upstreams.observe("openalex", {"Retry-After": "3"}, 429, defer_on_429=False)
            raise _status_error(429, "https://api.openalex.org/works")
        with mock.patch.dict(_openalex._health, {"result": None, "at": 0.0}), \
                mock.patch.object(_openalex, "get_json", _oa_429):
            self._assert_rate_limited(_openalex.health())

        def _oa_open(*_a, **_k):
            raise _openalex.OpenAlexDown("circuit open 50s more")
        with mock.patch.dict(_openalex._health, {"result": None, "at": 0.0}), \
                mock.patch.dict(_openalex._state, {"open_until": time.time() + 50}), \
                mock.patch.object(_openalex, "get_json", _oa_open):
            self._assert_breaker(_openalex.health())

        def _se_429(*_a, **_k):
            upstreams.observe("https://api.stackexchange.com/2.3/questions", {}, 429)
            return None
        with mock.patch.dict(_stackexchange._health, {"result": None, "at": 0.0}), \
                mock.patch.object(_stackexchange, "_se_cooling", lambda: False), \
                mock.patch.object(_stackexchange, "_se_get", _se_429):
            self._assert_rate_limited(_stackexchange.health())
        with mock.patch.dict(_stackexchange._health, {"result": None, "at": 0.0}), \
                mock.patch.object(_stackexchange, "_se_cooling", lambda: True):
            self._assert_breaker(_stackexchange.health())

    def test_b_search_backend_rate_limit_signal_is_cached_as_none(self):
        from omniseek.core.sources.api import _search_backend as sb
        live = {"active": "ddg", "nominal": True, "ddg": {"disabled": "", "cooling_s": 0},
                "brave": {"keyed": False, "cooling_s": 0}}
        calls = []

        def _soft(*_a, **_k):
            calls.append(1)
            raise RuntimeError("ddg soft rate-limit; cooling 60s until 12:00:00")
        with mock.patch.object(sb, "backend_state", lambda: live), \
                mock.patch.object(sb, "_brave_key", lambda: None), \
                mock.patch.dict(sb._ping, {"t": 0.0, "ok": None, "msg": ""}), \
                mock.patch.object(sb, "search_web", _soft):
            self._assert_rate_limited(sb.backend_ping())
            self._assert_rate_limited(sb.backend_ping())   # the second venue reads the cached verdict
        self.assertEqual(len(calls), 1)

    def test_b_exa_shared_probe_429(self):
        from omniseek.core.sources.api import exa_source

        def _limited(*_a, **_k):
            upstreams.observe("https://api.exa.ai/search", {}, 429)
            return None
        with mock.patch.dict(exa_source._health_cache, {"at": 0.0, "result": None}), \
                mock.patch.object(exa_source.http, "post_json", _limited):
            self._assert_rate_limited(exa_source._health("test-key"))

    def test_b_reddit_arctic_refusal_and_cooldown(self):
        from omniseek.core.sources.api import reddit_source

        def _refused(*_a, **_k):
            diag.note("http.get", url="https://arctic-shift.photon-reddit.com/api/posts/search",
                      status=422, body='{"error": "Slow down"}')
            return None
        with mock.patch.object(reddit_source, "_arctic_cooling", lambda: False), \
                mock.patch.object(reddit_source, "_arctic_get", _refused):
            self._assert_rate_limited(reddit_source.RedditAdapter().health_check())
        with mock.patch.object(reddit_source, "_arctic_cooldown_until", time.time() + 100):
            self._assert_breaker(reddit_source.RedditAdapter().health_check())

    def test_b_xiaohongshu_cn_open_breaker(self):
        import tempfile
        from omniseek.core.sources.walled import xiaohongshu_cn_source as x
        # health_check only reads state; the incident log is redirected anyway (smoke's convention:
        # a test that imports the guard never writes the real black box)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.multiple(x, _BROWSER_OK=True, _SEALED=False, _last_signal="test signal",
                                    _tripped_until=time.time() + 120,
                                    _INCIDENT_PATH=Path(tmp) / "incidents.jsonl"):
            ok, msg = x.XiaohongshuCNAdapter().health_check()
        self._assert_breaker((ok, msg))
        self.assertIn("reopens in", msg)

    def test_b_own_clients_429(self):
        from omniseek.core.sources.api import mycareersfuture_source as mcf, sec_financials_source as sec
        from omniseek.core.sources.scrape import mlrc_source
        from omniseek.core.sources.walled import bytedance_seed_source as bd, feishu_jobs_source as fs
        try:  # a personal source: the public mirror's sync (sync_from_eye.sh step 1) removes it
            from omniseek.core.sources.walled import mokahr_ats_source as mk
        except ImportError:
            mk = None
        with mock.patch.object(mcf.httpx, "post", lambda *a, **k: _resp(429, headers={"Retry-After": "9"})):
            self._assert_rate_limited(mcf.MyCareersFutureAdapter().health_check(), "9s")

        def _gate(*_a, **_k):
            raise GateBusy("SEC gate busy")
        with mock.patch.object(sec, "_load_ticker_map", _gate):
            ok, msg = sec.SECFinancialsAdapter().health_check()
        self.assertIsNone(ok, msg)

        def _raise_429(*_a, **_k):
            raise _status_error(429, "https://app.mokahr.com/api/x")
        if mk is not None:
            with mock.patch.object(mk.MokahrATSAdapter, "_list_site_jobs", _raise_429):
                self._assert_rate_limited(mk.MokahrATSAdapter().health_check())
        with mock.patch.object(fs, "_feishu_post", lambda *a, **k: _resp(429)):
            self._assert_rate_limited(fs.FeishuJobsAdapter().health_check())
        with mock.patch.object(cache, "get", lambda *a, **k: None), \
                mock.patch.object(bd.httpx, "get", lambda *a, **k: _resp(429)):
            self._assert_rate_limited(bd.BytedanceSeedAdapter().health_check())
        with mock.patch.object(mlrc_source.auth, "load", lambda n: None), \
                mock.patch.object(mlrc_source.httpx, "get", lambda *a, **k: _resp(429)):
            self._assert_rate_limited(mlrc_source.MLRCAdapter().health_check())

    def test_b_overseas_ai_jobs_needs_the_board_to_answer(self):
        from omniseek.core.sources.scrape import overseas_ai_jobs_source as oj
        with mock.patch.object(oj, "_get", lambda url: None):
            self.assertIs(oj.OverseasAIJobsAdapter().health_check()[0], False)
        board = {"jobs": [{"title": "Research Engineer"}, {"title": "Recruiter"}]}
        with mock.patch.object(oj, "_get", lambda url: _resp(200, json=board)):
            ok, msg = oj.OverseasAIJobsAdapter().health_check()
        self.assertIs(ok, True)


# ── (c) bluesky and openreview probe the search path ─────────────────────────────────────────────
def _curl_resp(status: int, body: bytes = b"{}", headers=None, url: str = "https://up.example/x"):
    from curl_cffi import requests as creq
    r = creq.Response()
    r.status_code, r.ok, r.url = status, 200 <= status < 400, url
    r.reason = "Too Many Requests" if status == 429 else "x"
    r.headers = creq.Headers(headers or {})
    r.content = body
    return r


class CurlSourcesAreRecorded(unittest.TestCase):
    """omniseek.core.curl: the curl_cffi calls the anti-bot sources make are recorded like any other
    response, so a 429 they get reads as not verified (2026-10-04, second follow-up)."""

    def _answer(self, status, body=b"{}", headers=None):
        def request(self_, method, url, *a, **k):
            return _curl_resp(status, body, headers, url=url)
        from curl_cffi import requests as creq
        return mock.patch.object(creq.Session, "request", request)

    def test_d_module_calls_and_sessions_reach_the_ledger(self):
        from omniseek.core import curl
        with self._answer(429, headers={"Retry-After": "15"}):
            with _probe.watching() as led:
                r = curl.post("https://api.juejin.cn/search_api/v1/search", json={})
            self.assertEqual(r.status_code, 429)
            self.assertEqual(led.held[-1][:3], ("429", "api.juejin.cn", 15.0))
            with _probe.watching() as led2:
                s = curl.Session(impersonate="chrome")
                s.get("https://weixin.sogou.com/weixin?type=2&query=x")
            self.assertEqual(led2.held[-1][0], "429")
        with self._answer(403, body=b'{"message": "API rate limit exceeded"}'):
            with _probe.watching() as led3:
                curl.get("https://up.example/x")
        self.assertEqual([h[0] for h in led3.held], ["body"])
        with self._answer(503):
            with _probe.watching() as led4:
                curl.get("https://up.example/x")
        self.assertEqual((led4.held, led4.errors), ([], [(503, "up.example")]))

    def test_d_the_curl_sources_read_429_as_not_verified_and_5xx_as_down(self):
        from omniseek.core.sources.scrape import (cninfo_source, eastmoney_source, gov_policy_source,
                                                juejin_source, sogou_weixin_source)
        adapters = [cninfo_source.CninfoAdapter(), eastmoney_source.EastMoneyAdapter(),
                    gov_policy_source.GovPolicyAdapter(), juejin_source.JuejinAdapter(),
                    sogou_weixin_source.SogouWeixinAdapter()]
        for ad in adapters:
            with self.subTest(source=ad.name):
                # a JSON error body must not read as an empty answer (that used to be True)
                with self._answer(429, body=b'{"error": "Too Many Requests"}', headers={"Retry-After": "5"}):
                    ok, msg = fetcher._safe_health(ad)
                self.assertIsNone(ok, msg)
                self.assertIn("Retry-After 5s", msg)
                with self._answer(503, body=b'{"error": "unavailable"}'):
                    ok, msg = fetcher._safe_health(ad)
                self.assertIs(ok, False, msg)

    def test_d_an_error_answer_is_a_failed_fetch_not_an_empty_result(self):
        from omniseek.core.sources.scrape import cninfo_source, gov_policy_source, juejin_source
        for ad in (cninfo_source.CninfoAdapter(), gov_policy_source.GovPolicyAdapter(),
                   juejin_source.JuejinAdapter()):
            with self.subTest(source=ad.name), self._answer(429, body=b'{"error": "x"}'):
                self.assertIsNone(ad._raw_fetch("x", 1))


class _FakeBlueskyClient:
    """atproto.Client stand-in: ``login`` and ``app.bsky.feed.search_posts`` follow a script."""
    logins = 0
    login_raises = None
    search_script: list = []
    searches: list = []

    def __init__(self):
        cls = type(self)
        self.app = SimpleNamespace(bsky=SimpleNamespace(feed=SimpleNamespace(search_posts=self._search)))
        self._cls = cls

    def login(self, handle, password):
        self._cls.logins += 1
        exc = self._cls.login_raises
        if isinstance(exc, list):
            exc = exc.pop(0) if exc else None
        if exc is not None:
            raise exc

    def _search(self, params):
        self._cls.searches.append(params)
        step = self._cls.search_script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _xrpc(cls, status: int, headers=None, error=None):
    from atproto_client.request import Response
    content = {"error": error} if error else b""
    return cls(Response(success=False, status_code=status, content=content, headers=headers or {}))


class BlueskyProbesSearch(unittest.TestCase):
    def setUp(self):
        from omniseek.core.sources.api import bluesky_source as bs
        self.bs = bs
        _FakeBlueskyClient.logins = 0
        _FakeBlueskyClient.login_raises = None
        _FakeBlueskyClient.search_script = []
        _FakeBlueskyClient.searches = []
        self.patches = [
            mock.patch.object(bs, "Client", _FakeBlueskyClient),
            mock.patch.object(bs.auth, "is_configured", lambda n: True),
            mock.patch.object(bs.auth, "load", lambda n: {"handle": "t.bsky.social", "app_password": "x"}),
        ]
        for p in self.patches:
            p.start()
        self.ad = bs.BlueskyAdapter()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _posts(self, n):
        return SimpleNamespace(posts=[object()] * n)

    def test_c_true_on_a_post_and_the_session_is_reused_not_remade(self):
        _FakeBlueskyClient.search_script = [self._posts(1), self._posts(1)]
        self.assertIs(self.ad.health_check()[0], True)
        ok, msg = self.ad.health_check()
        self.assertIs(ok, True, msg)
        self.assertEqual(_FakeBlueskyClient.logins, 1)
        self.assertEqual(len(_FakeBlueskyClient.searches), 2)
        self.assertEqual(_FakeBlueskyClient.searches[0], {"q": self.bs._PROBE_QUERY, "limit": 1})

    def test_c_429_and_a_busy_gate_are_none(self):
        from atproto_client.exceptions import RequestException
        _FakeBlueskyClient.search_script = [self._posts(1),
                                            _xrpc(RequestException, 429, {"retry-after": "40"})]
        self.ad.health_check()
        ok, msg = self.ad.health_check()
        self.assertIsNone(ok, msg)
        self.assertIn("Retry-After 40s", msg)

        def _busy(*_a, **_k):
            raise upstreams.UpstreamBusy("bluesky: no permit within 1.0s (gate saturated); not sent")
        with mock.patch.object(self.bs.upstreams, "hold", _busy):
            ok, msg = self.ad.health_check()
        self.assertIsNone(ok, msg)
        self.assertIn("gate", msg)

    def test_c_false_on_an_error_a_bad_shape_or_zero_posts(self):
        from atproto_client.exceptions import ModelError, RequestException
        for step, why in ((_xrpc(RequestException, 500), "HTTP 500"), (ModelError("bad"), "parse"),
                          (SimpleNamespace(posts=None), "posts list"), (self._posts(0), "0 posts")):
            with self.subTest(why=why):
                _FakeBlueskyClient.search_script = [step]
                ok, msg = self.ad.health_check()
                self.assertIs(ok, False, msg)

    def test_c_a_refused_kept_session_is_remade_once(self):
        from atproto_client.exceptions import BadRequestError, UnauthorizedError
        _FakeBlueskyClient.search_script = [self._posts(1), _xrpc(UnauthorizedError, 401), self._posts(1)]
        self.ad.health_check()
        ok, msg = self.ad.health_check()
        self.assertIs(ok, True, msg)
        self.assertEqual(_FakeBlueskyClient.logins, 2)
        # an expired token is the same case; a failed re-login is False
        _FakeBlueskyClient.search_script = [_xrpc(BadRequestError, 400, error="ExpiredToken")]
        _FakeBlueskyClient.login_raises = [_xrpc(UnauthorizedError, 401)]
        ok, msg = self.ad.health_check()
        self.assertIs(ok, False, msg)
        self.assertEqual(_FakeBlueskyClient.logins, 3)

    def test_c_a_fresh_login_that_search_refuses_is_not_remade(self):
        from atproto_client.exceptions import UnauthorizedError
        _FakeBlueskyClient.search_script = [_xrpc(UnauthorizedError, 403)]
        ok, msg = self.ad.health_check()
        self.assertIs(ok, False, msg)
        self.assertEqual(_FakeBlueskyClient.logins, 1)

    def test_c_login_429_is_none_other_login_failures_false_no_creds_false(self):
        from atproto_client.exceptions import RequestException, UnauthorizedError
        _FakeBlueskyClient.login_raises = [_xrpc(RequestException, 429)]
        self.assertIsNone(self.ad.health_check()[0])
        _FakeBlueskyClient.login_raises = [_xrpc(UnauthorizedError, 401)]
        self.assertIs(self.ad.health_check()[0], False)
        with mock.patch.object(self.bs.auth, "is_configured", lambda n: False):
            ok, msg = self.ad.health_check()
        self.assertIs(ok, False)
        self.assertIn("credentials not configured", msg)


class OpenReviewProbesSearch(unittest.TestCase):
    def setUp(self):
        from omniseek.core.sources.api import openreview_source as ors
        self.ors = ors
        self.logins: list = []
        self.searches: list = []
        self.login_script: list = []
        self.search_script: list = []

        def _post(url, **kw):
            self.logins.append(url)
            step = self.login_script.pop(0) if self.login_script else _resp(200, json={"token": "tok"})
            return step

        def _get(url, **kw):
            self.searches.append((url, kw))
            return self.search_script.pop(0)
        self.patches = [
            mock.patch.object(ors.httpx, "post", _post),
            mock.patch.object(ors.httpx, "get", _get),
            mock.patch.object(ors.auth, "is_configured", lambda n: True),
            mock.patch.object(ors.auth, "load", lambda n: {"username": "u@example.com", "password": "p"}),
        ]
        for p in self.patches:
            p.start()
        self.ad = ors.OpenReviewAdapter()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _notes(self, n):
        return _resp(200, json={"notes": [{"id": str(i)} for i in range(n)]})

    def test_c_true_on_a_paper_and_the_token_is_reused_not_remade(self):
        self.search_script = [self._notes(1), self._notes(1)]
        self.assertIs(self.ad.health_check()[0], True)
        ok, msg = self.ad.health_check()
        self.assertIs(ok, True, msg)
        self.assertEqual(len(self.logins), 1)
        url, kw = self.searches[0]
        self.assertTrue(url.endswith("/notes/search"))
        self.assertEqual(kw["params"], {"term": self.ors._PROBE_TERM, "limit": 1, "source": "forum"})
        self.assertEqual(kw["headers"], {"Authorization": "Bearer tok"})

    def test_c_429_from_search_or_login_is_none(self):
        self.search_script = [_resp(429, headers={"Retry-After": "20"})]
        ok, msg = self.ad.health_check()
        self.assertIsNone(ok, msg)
        self.assertIn("Retry-After 20s", msg)
        ad = self.ors.OpenReviewAdapter()
        self.login_script = [_resp(429, url="https://api2.openreview.net/login")]
        ok, msg = ad.health_check()
        self.assertIsNone(ok, msg)
        self.assertIn("rate-limited", msg)

    def test_c_false_on_an_error_a_bad_shape_or_zero_papers(self):
        for step, why in ((_resp(500), "HTTP 500"), (_resp(200, text="<html>"), "not JSON"),
                          (_resp(200, json={"items": []}), "notes list"), (self._notes(0), "0 papers")):
            with self.subTest(why=why):
                self.search_script = [step]
                ok, msg = self.ad.health_check()
                self.assertIs(ok, False, msg)

    def test_c_a_refused_kept_token_is_remade_once(self):
        self.search_script = [self._notes(1), _resp(401), self._notes(1)]
        self.ad.health_check()
        ok, msg = self.ad.health_check()
        self.assertIs(ok, True, msg)
        self.assertEqual(len(self.logins), 2)
        # a failed re-login is False
        self.search_script = [_resp(403)]
        self.login_script = [_resp(400, url="https://api2.openreview.net/login")]
        ok, msg = self.ad.health_check()
        self.assertIs(ok, False, msg)
        self.assertEqual(len(self.logins), 3)

    def test_c_a_fresh_token_that_search_refuses_is_not_remade(self):
        self.search_script = [_resp(401)]
        ok, msg = self.ad.health_check()
        self.assertIs(ok, False, msg)
        self.assertEqual(len(self.logins), 1)

    def test_c_mfa_login_and_missing_credentials_are_false(self):
        self.login_script = [_resp(200, json={"mfaPending": True, "mfaMethods": ["emailOtp"]})]
        ok, msg = self.ad.health_check()
        self.assertIs(ok, False, msg)
        self.assertIn("multi-factor", msg)
        self.assertEqual(self.searches, [])
        with mock.patch.object(self.ors.auth, "is_configured", lambda n: False):
            ok, msg = self.ors.OpenReviewAdapter().health_check()
        self.assertIs(ok, False)
        self.assertIn("credentials not configured", msg)


if __name__ == "__main__":
    unittest.main()
