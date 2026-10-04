"""Guard: EVERY registered source reads a rate limit and an open breaker as "not verified" (None).

The contract (fetcher.SourceAdapter.health_check, 2026-10-04): a health check that got HTTP 429, or
that a circuit breaker / back-off of OmniSeek kept from sending, has not verified anything, so it
returns None, never False ("down") and never True ("works"). Fixing the sources one by one left the
next new source free to get it wrong again; this sweep makes a new source that does fail here.

How: ONE child process (this file run with ``--sweep``), so nothing here can leak into the other
suites and nothing real is reachable:

- HOME / USERPROFILE point at a fresh temp dir before omniseek is imported: no real credential,
  cache or state file is ever read; credentials are fake values handed out by a stubbed ``auth``;
- every socket connect except to loopback (asyncio's event loop on Windows talks to itself through
  a loopback socket pair) and every DNS lookup raise, no subprocess can start, the CDP browser
  helpers report "no browser";
- every httpx transport, and every curl_cffi request (sessions, module-level calls, the shared curl
  tier), answers HTTP 429 (Retry-After: 1) to every request.

Then, with every shared probe cache cleared, it runs every registered source's health check
through the one funnel all readers use (``fetcher.health_check_outcome``): first as is (every
answer a 429), then again with every breaker of OmniSeek forced open. The parent asserts:

- 429 sweep: every source not exempt returns None, and its check completed (a None from our own
  timeout would hide the verdict). A source whose check sends nothing at all (LOCAL_ONLY: pdf, a
  local parser) must not return False;
- breaker sweep: every source not exempt returns None (so neither False nor a True made without a
  request), and every source of a module behind a breaker (BREAKER_MODULES) says the breaker is open.

EXEMPT lists the sources this harness cannot drive, each with its reason; the test also fails when an
exempt name is no longer registered, so the list cannot rot.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_MARK = "@@HEALTH-GUARD-SWEEP@@ "
_SWEEP_TIMEOUT_S = 600     # the whole child; measured well under a minute on 2026-10-04 (Windows)
_PER_SOURCE_S = 40.0       # per health check inside the child

_BROWSER = "drives the CDP browser (no browser in the sandbox; the stub makes it report CDP down)"
_YTDLP = "sends through yt-dlp's own HTTP stack, not httpx (the sandbox refuses its sockets)"

# name -> why the harness cannot hold it to the contract. Keep each reason true. The curl_cffi
# sources (cninfo, eastmoney, gov_policy, juejin, sogou_weixin, higheredjobs_cs) left this list on
# 2026-10-04: libcurl answers 429 here too, and their responses are recorded (omniseek.core.curl, the
# shared curl tier's _curl_hops).
EXEMPT: dict[str, str] = {
    "cdp_fulltext": _BROWSER, "douban_groups": _BROWSER, "douyin": _BROWSER,
    "ircc_ee_rounds": _BROWSER, "ircc_processing_times": _BROWSER, "polyu": _BROWSER,
    "xiaohongshu": _BROWSER, "xiaomuchong": _BROWSER, "yipinsanfendi": _BROWSER, "zhihu": _BROWSER,
    "zhihu_users": _BROWSER,
    "gter": _BROWSER + "; every site of the row is rendered",
    "scrape_canada": _BROWSER + "; every site of the row is rendered",
    "scrape_hongkong": _BROWSER + "; every site of the row is rendered",
    "scrape_js_sites": _BROWSER + "; every site of the row is rendered",
    "ml_conferences": _BROWSER + "; the conference pages are rendered",
    "mpnp_draws": _BROWSER + "; the draws page is rendered",
    "youtube": _YTDLP, "youtube_channels": _YTDLP, "slideslive_talks": _YTDLP,
}

# Sources whose check sends nothing at all (a local dependency is the whole of what it can verify):
# the contract only rules out False for them.
LOCAL_ONLY: dict[str, str] = {
    "pdf": "no fixed upstream; the check only imports the local PDF parser (driver ruling 2026-10-04)",
}
# The module each LOCAL_ONLY check imports. Where it is not installed (a base install has no [pdf]
# extra, as in the public mirror's CI) False is the true answer, so the sweep holds it to nothing.
LOCAL_ONLY_NEEDS: dict[str, str] = {"pdf": "fitz"}

# Modules whose sources sit behind a breaker / back-off of OmniSeek: in the breaker sweep every source
# they register must say the breaker is open (so its None comes from the breaker branch, not from a
# 429 that slipped past an open breaker). By module, so adding or retiring a row needs no edit here.
BREAKER_MODULES: dict[str, str] = {
    "omniseek.core.sources.api.github_source": "_github breaker",
    "omniseek.core.sources.api.github_trending_source": "_github breaker",
    "omniseek.core.sources.api.openalex_source": "_openalex breaker",
    "omniseek.core.sources.api.org_watch_source": "_openalex breaker",
    "omniseek.core.sources.api.researcher_watch_source": "_openalex breaker",
    "omniseek.core.sources.api.semantic_scholar_source": "_s2 breaker",
    "omniseek.core.sources.api.s2_snippet_source": "_s2 breaker",
    "omniseek.core.sources.api.arxiv_source": "arXiv breaker",
    "omniseek.core.sources.api.reddit_source": "Arctic Shift cooldown",
    "omniseek.core.sources.api.search_index_source": "web-search backend cooldown",
    "omniseek.core.sources.api.nowcoder_source": "web-search backend cooldown (CDP is down in the sandbox)",
    "omniseek.core.sources.scrape.stackoverflow_source": "Stack Exchange quota cooldown",
    "omniseek.core.sources.scrape.academia_se_source": "Stack Exchange quota cooldown",
    "omniseek.core.sources.scrape.ai_se_source": "Stack Exchange quota cooldown",
    "omniseek.core.sources.scrape.cs_se_source": "Stack Exchange quota cooldown",
    "omniseek.core.sources.scrape.crossvalidated_source": "Stack Exchange quota cooldown",
    "omniseek.core.sources.scrape.datascience_se_source": "Stack Exchange quota cooldown",
    "omniseek.core.sources.walled.xiaohongshu_cn_source": "风控 breaker",
}


# ── the child ──────────────────────────────────────────────────────────────────────────────────
def _sandbox_network() -> None:
    import ipaddress
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _loopback(address) -> bool:
        # asyncio's event loop on Windows talks to itself through a loopback socket pair; nothing
        # else here may connect anywhere (every CDP helper is stubbed in _sandbox_after_import).
        try:
            return ipaddress.ip_address(address[0]).is_loopback
        except (TypeError, ValueError, IndexError):
            return False

    def _connect(self, address):
        if _loopback(address):
            return real_connect(self, address)
        raise OSError("network disabled by the health guard sweep")

    def _connect_ex(self, address):
        if _loopback(address):
            return real_connect_ex(self, address)
        raise OSError("network disabled by the health guard sweep")

    def _refuse(*_a, **_k):
        raise OSError("network disabled by the health guard sweep")

    def _refuse_dns(*_a, **_k):
        raise socket.gaierror("DNS disabled by the health guard sweep")

    socket.socket.connect = _connect           # type: ignore[assignment]
    socket.socket.connect_ex = _connect_ex     # type: ignore[assignment]
    socket.create_connection = _refuse         # type: ignore[assignment]
    socket.getaddrinfo = _refuse_dns           # type: ignore[assignment]

    import httpx

    def _answer_429(request):
        return httpx.Response(429, headers={"Retry-After": "1", "Content-Type": "application/json"},
                              content=b'{"error": "Too Many Requests"}', request=request)

    async def _aanswer_429(self, request):
        return _answer_429(request)

    httpx.HTTPTransport.handle_request = lambda self, request: _answer_429(request)  # type: ignore[assignment]
    httpx.AsyncHTTPTransport.handle_async_request = _aanswer_429  # type: ignore[assignment]

    try:
        import curl_cffi
        from curl_cffi import requests as creq

        def _curl_429(self, method, url, *_a, **_k):
            # A real curl_cffi Response, as libcurl would hand it back: every request through
            # curl_cffi (module-level calls, sessions, the shared curl tier) goes through here.
            r = creq.Response()
            r.status_code, r.ok, r.reason, r.url = 429, False, "Too Many Requests", str(url)
            r.headers = creq.Headers({"Retry-After": "1", "Content-Type": "application/json"})
            r.content = b'{"error": "Too Many Requests"}'
            return r

        async def _acurl_429(self, method, url, *a, **k):
            return _curl_429(self, method, url, *a, **k)

        creq.Session.request = _curl_429           # type: ignore[assignment]
        creq.AsyncSession.request = _acurl_429     # type: ignore[assignment]
        curl_cffi.Curl.perform = _refuse           # type: ignore[assignment]  (anything below that)
    except Exception:  # noqa: BLE001 (not installed: nothing to answer)
        pass


def _sandbox_after_import() -> None:
    import subprocess as sp

    def _no_process(*_a, **_k):
        raise OSError("subprocesses disabled by the health guard sweep")

    sp.Popen.__init__ = _no_process            # type: ignore[assignment]

    def _no_cdp(*_a, **_k):
        return False, "no CDP browser in the health guard sweep"

    def _no_cdp_call(*_a, **_k):
        raise RuntimeError("no CDP browser in the health guard sweep")

    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("omniseek"):
            continue
        if callable(getattr(mod, "cdp_health", None)):
            mod.cdp_health = _no_cdp
        if callable(getattr(mod, "cdp_call", None)):
            mod.cdp_call = _no_cdp_call

    from omniseek.core import auth
    fake = {"api_key": "test-key", "key": "test-key", "app_id": "test-id", "app_key": "test-key",
            "token": "test-token", "bot_token": "test-token", "auth_token": "test-token",
            "handle": "test.bsky.social", "app_password": "test-pass", "username": "test@example.com",
            "password": "test-pass", "secret": "test-secret", "api_secret": "test-secret",
            "client_id": "test-id", "client_secret": "test-secret", "email": "test@example.com"}
    auth.load = lambda source: dict(fake)      # type: ignore[assignment]
    auth.is_configured = lambda source: True   # type: ignore[assignment]

    # xiaohongshu_cn's risk incident log: already under the temp HOME, redirected by name anyway
    # (smoke's convention for every test that imports that guard).
    from omniseek.core.sources.walled import xiaohongshu_cn_source
    xiaohongshu_cn_source._INCIDENT_PATH = Path(os.environ["HOME"]) / "xhs_cn_incidents.jsonl"


def _clear_probe_caches() -> None:
    from omniseek.core import _github, _openalex, _s2, _stackexchange
    for mod in (_github, _openalex, _s2, _stackexchange):
        mod._health["result"] = None
        mod._health["at"] = 0.0
    from omniseek.core.sources.api import _search_backend, exa_source
    _search_backend._ping.update(t=0.0, ok=None, msg="")
    exa_source._health_cache.update(at=0.0, result=None)


def _close_breakers() -> None:
    from omniseek.core import _stackexchange, upstreams
    for g in list(upstreams._guards.values()):
        with g.lock:
            g.state["open_until"] = 0.0
            g.state["fails"] = 0
    _stackexchange._se_cooldown_until = 0.0


def _open_breakers() -> None:
    import time
    from omniseek.core import _stackexchange, upstreams
    from omniseek.core.sources.api import _search_backend, reddit_source
    from omniseek.core.sources.walled import xiaohongshu_cn_source, xiaohongshu_source
    until = time.time() + 600
    for g in list(upstreams._guards.values()):
        with g.lock:
            g.state["open_until"] = until
    reddit_source._arctic_cooldown_until = until
    _search_backend._brave_cooldown_until = until
    _search_backend._ddg_cooldown_until = until
    _stackexchange._se_cooldown_until = time.monotonic() + 600
    xiaohongshu_source._backoff_until = until
    xiaohongshu_cn_source._tripped_until = until


def _sweep() -> dict:
    from concurrent.futures import ThreadPoolExecutor
    from omniseek.core import fetcher
    adapters = [fetcher.get_adapter(n) for n in fetcher.all_adapter_names()]
    adapters = [a for a in adapters if a is not None]

    def one(a):
        ok, msg, done = fetcher.health_check_outcome(a, timeout=_PER_SOURCE_S)
        return a.name, [ok, str(msg)[:400], done, type(a).__module__]

    with ThreadPoolExecutor(max_workers=24) as ex:
        return dict(ex.map(one, adapters))


def _child_main() -> None:
    import logging
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(ROOT / "src"))
    _sandbox_network()
    import omniseek.server  # noqa: F401 (registers every source)
    _sandbox_after_import()
    _clear_probe_caches()
    _close_breakers()
    rate = _sweep()
    _clear_probe_caches()
    _open_breakers()
    breaker = _sweep()
    sys.stdout.write(_MARK + json.dumps({"rate": rate, "breaker": breaker}) + "\n")
    sys.stdout.flush()
    os._exit(0)   # do not wait on a probe thread the sandbox left hanging


# ── the parent ─────────────────────────────────────────────────────────────────────────────────
class EverySourceReadsHoldBackAsNotVerified(unittest.TestCase):
    results: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory(prefix="eye-health-guard-") as home:
            env = {**os.environ, "HOME": home, "USERPROFILE": home,
                   "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--sweep"],
                                 cwd=str(ROOT), env=env, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=_SWEEP_TIMEOUT_S)
        line = next((ln for ln in run.stdout.splitlines() if ln.startswith(_MARK)), None)
        if line is None:
            raise AssertionError(f"the sweep child produced no result (rc={run.returncode}); "
                                 f"stderr tail: {run.stderr.strip().splitlines()[-15:]}")
        cls.results = json.loads(line[len(_MARK):])

    def _violations(self, sweep: str) -> list[str]:
        bad = []
        for name, (ok, msg, done, _module) in sorted(self.results[sweep].items()):
            if name in EXEMPT:
                continue
            if not done:
                bad.append(f"{name}: did not complete ({msg})")
            elif name in LOCAL_ONLY:
                if ok is False and importlib.util.find_spec(LOCAL_ONLY_NEEDS[name]) is not None:
                    bad.append(f"{name}: False although its check sends nothing ({msg})")
            elif ok is not None:
                bad.append(f"{name}: {ok!r} ({msg})")
        return bad

    def test_a_429_everywhere_reads_not_verified(self):
        bad = self._violations("rate")
        self.assertEqual(bad, [], "these sources did not read HTTP 429 as None:\n" + "\n".join(bad))

    def test_b_open_breakers_read_not_verified(self):
        bad = self._violations("breaker")
        self.assertEqual(bad, [], "these sources did not read an open breaker (or a 429) as None:\n"
                         + "\n".join(bad))

    def test_c_breaker_sources_say_the_breaker_is_open(self):
        sweep = self.results["breaker"]
        covered = {n: row for n, row in sweep.items() if row[3] in BREAKER_MODULES}
        missing = sorted(set(BREAKER_MODULES) - {row[3] for row in covered.values()})
        self.assertEqual(missing, [], f"breaker modules that registered no source: {missing}")
        bad = [f"{n} ({BREAKER_MODULES[row[3]]}): {row[1]}" for n, row in sorted(covered.items())
               if "circuit breaker open" not in row[1]]
        self.assertEqual(bad, [], "these breaker sources did not report the open breaker:\n" + "\n".join(bad))

    def test_d_the_exempt_lists_name_registered_sources(self):
        # polyu and mokahr_ats are personal sources the public mirror's sync removes
        # (sync_from_eye.sh step 1), so there they are absent by design, not stale.
        stale = sorted(n for n in (*EXEMPT, *LOCAL_ONLY)
                       if n not in self.results["rate"] and n not in ("polyu", "mokahr_ats"))
        self.assertEqual(stale, [], f"exempt names that are no longer registered: {stale}")
        self.assertTrue(all(isinstance(r, str) and r for r in (*EXEMPT.values(), *LOCAL_ONLY.values())))
        self.assertEqual(set(LOCAL_ONLY_NEEDS), set(LOCAL_ONLY), "every LOCAL_ONLY row names the module its check imports")


if __name__ == "__main__" and sys.argv[1:2] == ["--sweep"]:
    _child_main()
