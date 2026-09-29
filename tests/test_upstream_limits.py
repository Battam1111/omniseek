"""Upstream declarations: declared, enforced by the shared limiter, and checked against readings.

Offline: every network edge is a stub (DNS included), except one curl-tier case that talks to a server
the test itself runs on 127.0.0.1. The arXiv case measures real time (two 3 s gaps), so this suite adds
about 7 s to the battery; that is the published limit, not a tunable. The gate-fix cases (task R2, review
X of 2026-09-29) add a few seconds more: T1 to T15 are named in their test names. The progress cases (task
R3b) and the first-byte cases (review Q1) add about 60 s: each races a second caller against a request
that takes a second or more, once for every way OmniSeek reads a response.
"""
import asyncio
import bisect
import contextlib
import contextvars
import email.utils
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from unittest import mock

import anyio
import httpx

import omniseek.server
from omniseek.core import _guard as guard_mod
from omniseek.core import _openalex, http, upstreams
from omniseek.core._guard import BackendGuard, bounded_async_slot

EYE = Path(__file__).parents[1] / "src" / "omniseek" / "core"


def _reset_guard(g: BackendGuard) -> None:
    """Forget pacing / window / breaker history so one test's reservations never delay another."""
    with g.pace_lock:
        g.pace_state["next_at"] = 0.0
        g.pace_state["floor"] = 0.0
        for log in g._win_logs:
            log.clear()
    with g.lock:
        g.state["fails"] = 0
        g.state["open_until"] = 0.0


def _in_service_sources():
    """(the in-service sources, every source the code has, backend_of)."""
    omniseek.server.load_sources()
    from omniseek.core import fetcher
    catalog = fetcher.all_adapter_names()
    names = [n for n in catalog if not fetcher.retired_reason(fetcher.get_adapter(n))]
    return names, catalog, fetcher.backend_of


class DeclarationTests(unittest.TestCase):
    def test_declarations_load_and_every_entry_passes_the_schema(self):
        d = upstreams.declarations()
        self.assertIsNone(upstreams.load_error())
        self.assertGreater(len(d), 50)
        problems = [p for uid, e in d.items() for p in upstreams.schema_problems(uid, e)]
        self.assertEqual([], problems)

    def test_every_in_service_source_is_covered_by_a_declaration(self):
        names, catalog, backend_of = _in_service_sources()
        cov = upstreams.coverage(names, backend_of, catalog)
        self.assertEqual([], cov["undeclared"],
                         "each source must be listed under an upstream in upstreams.json")
        self.assertEqual([], cov["unknown_sources"], "declarations name sources that do not exist")

    def test_a_source_parked_at_run_time_leaves_both_checks_as_they_were(self):
        """Driver ruling of 2026-09-29 (the mini pre-deploy check): a source parked by runtime state or an
        online override (here: a retire overlay row, as a curator verdict writes one) is out of service,
        but the code still has it. "Every in-service source is declared" is judged on the in-service
        sources, "every declared name exists" on every source, so parking one changes neither."""
        from omniseek.core import fetcher
        names, catalog, backend_of = _in_service_sources()
        declared = sorted(s for e in upstreams.declarations().values() for s in (e.get("sources") or ())
                          if s in names)
        parked = declared[0]
        with mock.patch.object(fetcher, "_explicit_only_overrides",
                               lambda: {parked: "retired: parked by a test 2026-09-29"}):
            live, catalog2, _ = _in_service_sources()
            self.assertNotIn(parked, live)          # the overlay took the source out of service
            self.assertIn(parked, catalog2)         # and the code still has it
            cov = upstreams.coverage(live, backend_of, catalog2)
            before = upstreams.coverage(names, backend_of, catalog)
            self.assertEqual((before["undeclared"], before["unknown_sources"]),
                             (cov["undeclared"], cov["unknown_sources"]))
            self.assertEqual(([], []), (cov["undeclared"], cov["unknown_sources"]))
            # the health view answers the same way
            hb = upstreams.health_block(live, backend_of, catalog=catalog2)
            self.assertEqual([], hb["declared_but_unknown_sources"])
            # what the check did before this ruling: the in-service list stood in for every source
            self.assertIn(parked, [s for _, s in upstreams.coverage(live, backend_of)["unknown_sources"]])

    def test_arxiv_is_declared_as_published_and_enforced_as_declared(self):
        e = upstreams.entry("arxiv")
        self.assertEqual(1, e["terms"]["max_concurrency"])
        self.assertEqual(3.0, e["terms"]["min_interval_s"])
        self.assertIn("info.arxiv.org/help/api/tou.html", e["terms"]["url"])
        from omniseek.core.sources.api import arxiv_source
        g = upstreams.guard("arxiv")
        self.assertIs(g, arxiv_source._guard)
        self.assertEqual((1, 3.0), (g.max_inflight, g.min_interval_s))

    def test_a_gated_host_belongs_to_exactly_one_gated_upstream(self):
        owners = {}
        for uid, e in upstreams.declarations().items():
            if e.get("gate"):
                for h in e.get("hosts") or ():
                    owners.setdefault(h, []).append(uid)
        self.assertEqual({}, {h: u for h, u in owners.items() if len(u) > 1})
        # a domain suffix of one gated upstream may not cover a host or a suffix of another
        suffixes = {h: u[0] for h, u in owners.items() if h.startswith(".")}
        overlaps = [(h, uid, sfx, suffixes[sfx]) for h, (uid,) in owners.items() for sfx in suffixes
                    if suffixes[sfx] != uid and ("." + h.lstrip(".")).endswith(sfx)]
        self.assertEqual([], overlaps)
        for h, (uid,) in owners.items():
            if upstreams.entry(uid)["gate"].get("http_gate", True):
                probe = "probe" + h if h.startswith(".") else h   # a suffix is probed by a subdomain
                self.assertEqual(uid, upstreams.gated_uid_for_url(f"https://{probe}/x"))

    def test_modules_hold_the_registry_guard_not_a_private_one(self):
        from omniseek.core import _github, _s2, _stackexchange
        from omniseek.core.sources.api import arxiv_source, core_source
        self.assertIs(upstreams.guard("openalex"), _openalex._guard)
        self.assertIs(upstreams.guard("s2"), _s2._guard)
        self.assertIs(upstreams.guard("github"), _github._guard)
        self.assertIs(upstreams.guard("github_code_search"), _github._code_guard)
        self.assertIs(upstreams.guard("stackexchange"), _stackexchange._se_guard)
        self.assertIs(_stackexchange._se_guard.sema, _stackexchange._se_sema)
        self.assertIs(upstreams.guard("arxiv"), arxiv_source._guard)
        self.assertIs(upstreams.guard("core"), core_source._core_guard)
        self.assertEqual([(10, 60.0)], core_source._core_guard.windows)

    def test_direct_httpx_to_a_gated_host_goes_through_the_gate(self):
        """A module that names a gated host and sends to it with its own httpx (not the shared
        client, not http.direct) must hold that host's declared gate WHILE the request is on the
        wire. Found by a scan (a new module appears here the day it is written), proved by running
        it: each found module has a row in _DIRECT_GATE_ROWS that sends one request with only the
        transport stubbed, and the gate must be seen held at that moment. (Gates marked http_gate
        false are enforced by their own module's pacer, the web-search backend; a host that only
        appears as a link to show the user is listed with its reason.)"""
        gated_hosts = {h for e in upstreams.declarations().values()
                       if e.get("gate") and e["gate"].get("http_gate", True)
                       for h in e.get("hosts") or ()}
        gated_hosts |= {h for e in upstreams.declarations().values()
                        for h in ((e.get("terms") or {}).get("robots_crawl_delay_s") or {})}
        suffixes = {h for h in gated_hosts if h.startswith(".")}   # ".wikipedia.org": any subdomain
        gated_hosts -= suffixes
        direct = re.compile(r"^\s*(?:[\w.\[\]]+\s*=\s*)?(?:return\s+|await\s+)*"
                            r"(?:httpx\.(?:get|post|put|stream|request)|\w*_client\(\)\.(?:get|post|stream))\(",
                            re.M)
        exempt = {"upstreams.py", "_guard.py", "http.py"}
        link_only = {"csrankings_source.py": "dblp.org is a search link handed to the user; the only "
                                             "egress is the CSrankings CSV on raw.githubusercontent.com",
                     "alphaxiv_source.py": "arxiv.org/abs links are built for the user; its only direct "
                                           "egress is the health probe to api.alphaxiv.org"}
        found = set()
        for path in EYE.rglob("*.py"):
            if path.name in exempt or path.name in link_only:
                continue
            text = path.read_text(encoding="utf-8")
            if not direct.search(text):
                continue
            named = [h for h in gated_hosts if f"//{h}" in text]
            named += [x for x in suffixes
                      if re.search(r"//[a-z0-9-]+(?:\.[a-z0-9-]+)*" + re.escape(x), text)]
            if named:
                found.add(str(path.relative_to(EYE)).replace("\\", "/"))
        rows = _direct_gate_rows()
        # every discovered module needs a row; a module that has since moved to http.direct keeps its
        # row, so it is still proved to hold its gate on the wire (task R3b)
        self.assertEqual([], sorted(found - set(rows)),
                         "every direct-httpx module that names a gated host needs a row that runs it")
        for mod, (uid, call) in sorted(rows.items()):
            with self.subTest(module=mod):
                g = upstreams.guard(uid)
                _reset_guard(g)
                seen = []

                def handle(self, request, g=g, seen=seen):
                    seen.append(g.sema._value < g.max_inflight)   # a permit is out: the gate is held
                    return httpx.Response(200, headers={"content-type": "application/json"},
                                          stream=httpx.ByteStream(b"{}"), request=request)
                with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                          _async_of(handle)), \
                        mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                        mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                    call()
                _reset_guard(g)
                self.assertTrue(seen, f"{mod}: no request reached the wire")
                self.assertTrue(all(seen), f"{mod}: a request went out without its gate: {seen}")


def _async_of(handle):
    async def ahandle(self, request):
        return handle(self, request)
    return ahandle


def _direct_gate_rows() -> dict:
    """module path -> (upstream id, a call that sends ONE request through that module's own httpx)."""
    from omniseek.core import _github, cache
    from omniseek.core.sources.api import (arxiv_source, crossref_source, dblp_source, hackernews_source,
                                         hf_daily_papers_source, huggingface_hub_source,
                                         llm_leaderboard_source)
    from omniseek.core.sources.walled import discord_communities_source as disc

    def no_cache(fn):
        def run():
            with mock.patch.object(cache, "get", lambda *a, **k: None), \
                    mock.patch.object(cache, "set", lambda *a, **k: None):
                return fn()
        return run

    def leaderboard():
        with mock.patch.object(llm_leaderboard_source.auth, "load", lambda name: {"api_key": "k"}):
            llm_leaderboard_source.LLMLeaderboardAdapter()._models()

    def openalex():
        with mock.patch.object(_openalex, "_api_key", None):
            _openalex.get_json("/works", {"search": "x", "per-page": 1})
    return {
        "_github.py": ("github", no_cache(lambda: _github.get_json("/rate_limit"))),
        "_openalex.py": ("openalex", no_cache(openalex)),
        "sources/api/arxiv_source.py": ("arxiv", lambda: arxiv_source.ArxivAdapter().health_check()),
        "sources/api/crossref_source.py": ("crossref",
                                           lambda: crossref_source.CrossrefAdapter().health_check()),
        "sources/api/dblp_source.py": ("dblp", lambda: dblp_source.DBLPAdapter().health_check()),
        "sources/api/hackernews_source.py": ("hn_algolia",
                                             lambda: hackernews_source.HackerNewsAdapter().health_check()),
        "sources/api/hf_daily_papers_source.py": (
            "huggingface", lambda: hf_daily_papers_source.HFDailyPapersAdapter().health_check()),
        "sources/api/huggingface_hub_source.py": (
            "huggingface", lambda: huggingface_hub_source.HuggingFaceHubAdapter().health_check()),
        "sources/api/llm_leaderboard_source.py": ("artificial_analysis", no_cache(leaderboard)),
        "sources/walled/discord_communities_source.py": (
            "discord", no_cache(lambda: disc.DiscordCommunitiesAdapter()._discover_channels("t"))),
    }


class GuardMechanicsTests(unittest.TestCase):
    def test_window_admits_the_budget_then_waits_for_the_oldest_to_age_out(self):
        g = BackendGuard("t-window", 5, windows=[(2, 0.3)])
        waits = [g.reserve_pace_slot() for _ in range(3)]
        self.assertLess(waits[0], 0.01)
        self.assertLess(waits[1], 0.01)
        self.assertGreater(waits[2], 0.25)

    def test_defer_pushes_the_next_start_for_every_caller(self):
        g = BackendGuard("t-defer", 2)
        g.defer(0.2)
        self.assertGreater(g.pace_backlog_s(), 0.15)

    def test_hold_is_reentrant_in_one_context_and_releases_on_exit(self):
        g = BackendGuard("t-reentrant", 1, min_interval_s=0.0)
        with g.hold(0.2, lambda w: RuntimeError("busy")):
            self.assertTrue(g.held())
            with g.hold(0.2, lambda w: RuntimeError("busy")):  # must not wait on itself
                pass
        self.assertFalse(g.held())
        self.assertTrue(g.sema.acquire(timeout=0))
        g.sema.release()

    def test_a_shed_start_slot_releases_the_permit(self):
        g = BackendGuard("t-shed", 1, min_interval_s=5.0)
        g.reserve_pace_slot()  # the next start is ~5 s away
        with self.assertRaises(RuntimeError):
            with g.hold(0.2, lambda w: RuntimeError("busy"),
                        lambda w: RuntimeError("shed") if w > 1.0 else None):
                self.fail("a shed request must not run")
        self.assertTrue(g.sema.acquire(timeout=0))
        g.sema.release()
        self.assertFalse(g.held())


class _Recorder:
    """A fake transport: records each request's start/end and the peak number in flight."""

    def __init__(self, latency: float, status: int = 200, headers=None, body=b"<feed/>"):
        self.latency, self.status, self.headers, self.body = latency, status, headers or {}, body
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.spans = []

    def _enter(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        return time.monotonic()

    def _leave(self, t0):
        with self.lock:
            self.now -= 1
            self.spans.append((t0, time.monotonic()))

    def _response(self, method, url):
        # stream=, not content=: a content= Response is read at construction, and OmniSeek's http
        # helpers stream the body (iter_raw) exactly as they do from a real transport.
        return httpx.Response(self.status, headers=self.headers, stream=httpx.ByteStream(self.body),
                              request=httpx.Request(method, url))

    def sync_client(self):
        rec = self

        class _Client:
            @contextlib.contextmanager
            def stream(self, method, url, **kw):
                t0 = rec._enter()
                try:
                    time.sleep(rec.latency)
                    yield rec._response(method, url)
                finally:
                    rec._leave(t0)
        return _Client()

    def async_client(self):
        rec = self

        class _AClient:
            @contextlib.asynccontextmanager
            async def stream(self, method, url, **kw):
                t0 = rec._enter()
                try:
                    await anyio.sleep(rec.latency)
                    yield rec._response(method, url)
                finally:
                    rec._leave(t0)
        return _AClient()


@contextlib.contextmanager
def _stub_http(rec: _Recorder):
    saved = (http._get_client, http._aget_client, http._netguard.security_block_reason)
    http._get_client = rec.sync_client
    http._aget_client = rec.async_client
    http._netguard.security_block_reason = lambda url: None
    try:
        yield
    finally:
        http._get_client, http._aget_client, http._netguard.security_block_reason = saved


class ArxivLimiterTests(unittest.TestCase):
    """The declared arXiv terms, measured: requests fired at the same moment from the sync source
    path, the async source path and a plain shared-http call (enrich's path) never overlap and start
    at least 3 s apart."""

    def test_concurrent_requests_one_in_flight_and_three_seconds_apart(self):
        from omniseek.core.sources.api import arxiv_source
        g = arxiv_source._guard
        _reset_guard(g)
        rec = _Recorder(latency=0.4)  # slower than nothing: overlap would show if the cap were > 1
        results = {}

        def sync_source():
            results["sync"] = arxiv_source._arxiv_get_text(arxiv_source._API,
                                                           params={"search_query": "a"})

        def async_source():
            results["async"] = anyio.run(lambda: arxiv_source._arxiv_aget_text(
                arxiv_source._API, params={"search_query": "b"}))

        def plain_http():  # enrich._arxiv_integrity's route: only the shared host gate protects it
            results["http"] = http.get_text("https://export.arxiv.org/api/query?id_list=2401.00001")

        with _stub_http(rec):
            threads = [threading.Thread(target=f) for f in (sync_source, async_source, plain_http)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        _reset_guard(g)
        self.assertEqual({"sync", "async", "http"}, {k for k, v in results.items() if v})
        self.assertEqual(1, rec.peak, "arXiv allows one connection at a time")
        starts = sorted(s for s, _ in rec.spans)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(2, len(gaps))
        self.assertGreaterEqual(min(gaps), 3.0 - 0.05, f"start gaps {gaps}")


@contextlib.contextmanager
def _temp_upstream(uid, host, gate):
    """Declare a throwaway gated upstream for one test (restores the registry afterwards)."""
    d = upstreams.declarations()
    d[uid] = {"name": uid, "hosts": [host], "sources": [], "tools": ["test"],
              "terms": {"max_concurrency": upstreams.UNPUBLISHED}, "gate": gate}
    saved_index = dict(upstreams._host_index)
    upstreams._host_index[host] = uid
    try:
        yield upstreams.guard(uid)
    finally:
        d.pop(uid, None)
        upstreams._guards.pop(uid, None)
        upstreams._readings.pop(uid, None)
        upstreams._host_index.clear()
        upstreams._host_index.update(saved_index)


class HttpHostGateTests(unittest.TestCase):
    def test_shared_http_client_paces_a_gated_host_across_threads(self):
        with _temp_upstream("t-http", "gated.test.invalid",
                            {"max_inflight": 1, "min_interval_s": 0.25, "max_wait_s": 5.0}):
            rec = _Recorder(latency=0.05)
            with _stub_http(rec):
                ts = [threading.Thread(target=lambda: http.get_text("https://gated.test.invalid/a"))
                      for _ in range(3)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(10)
            starts = sorted(s for s, _ in rec.spans)
            self.assertEqual(1, rec.peak)
            self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.25 - 0.02)

    def test_a_429_with_retry_after_defers_every_caller_and_is_recorded(self):
        with _temp_upstream("t-429", "limited.test.invalid",
                            {"max_inflight": 2, "min_interval_s": 0.0, "max_wait_s": 5.0}) as g:
            rec = _Recorder(latency=0.0, status=429,
                            headers={"Retry-After": "2", "X-RateLimit-Remaining": "0"})
            with _stub_http(rec):
                self.assertIsNone(http.get("https://limited.test.invalid/x"))
            self.assertGreater(g.pace_backlog_s(), 1.5)
            r = upstreams.readings("t-429")
            self.assertIsNotNone(r.get("last_429_at"))
            self.assertEqual("0", r["lanes"]["_"]["fields"]["x-ratelimit-remaining"])

    def test_a_gate_that_cannot_admit_in_time_sends_nothing(self):
        with _temp_upstream("t-busy", "busy.test.invalid",
                            {"max_inflight": 1, "min_interval_s": 0.0, "max_wait_s": 0.1}) as g:
            rec = _Recorder(latency=0.0)
            self.assertTrue(g.sema.acquire(timeout=0))
            try:
                with _stub_http(rec):
                    self.assertIsNone(http.get("https://busy.test.invalid/x"))
            finally:
                g.sema.release()
            self.assertEqual([], rec.spans)


class _OAResp:
    def __init__(self, status, headers, payload=None):
        self.status_code, self.headers, self._p = status, headers, payload or {"results": []}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://api.openalex.org"),
                                        response=httpx.Response(self.status_code))

    def json(self):
        return self._p


@contextlib.contextmanager
def _oa_state(answers):
    """Stub the OpenAlex client with a scripted list of (lane -> response) answers; restore all state."""
    saved = (_openalex._api_key, _openalex._get_client, json.loads(json.dumps(_openalex._usage)),
             dict(_openalex._state["dry_until"]))
    calls = []

    class _C:
        def get(self, url, params=None, timeout=None):
            lane = "keyed" if (params or {}).get("api_key") else "anon"
            calls.append(lane)
            return answers[lane]
    _openalex._api_key = "TEST_KEY"
    _openalex._get_client = lambda: _C()
    _openalex._usage = {"since": None, "by_caller": {}, "spilled_to_anon": 0,
                        "remaining": {"keyed": None, "anon": None}, "lanes": {}}
    _openalex._state["dry_until"].update({"keyed": 0.0, "anon": 0.0})
    _reset_guard(_openalex._guard)
    try:
        yield calls
    finally:
        _openalex._api_key, _openalex._get_client = saved[0], saved[1]
        _openalex._usage = saved[2]
        _openalex._state["dry_until"].clear()
        _openalex._state["dry_until"].update(saved[3])
        _reset_guard(_openalex._guard)
        upstreams._readings.pop("openalex", None)


class OpenAlexReadingTests(unittest.TestCase):
    KEYED_OK = {"x-ratelimit-limit": "10000", "x-ratelimit-limit-usd": "1",
                "x-ratelimit-remaining": "7331", "x-ratelimit-remaining-usd": "0.7331",
                "x-ratelimit-reset": "3600", "x-ratelimit-cost-usd": "0.0001"}
    ANON_OK = {"x-ratelimit-limit": "1000", "x-ratelimit-limit-usd": "0.1",
               "x-ratelimit-remaining": "999", "x-ratelimit-remaining-usd": "0.0999",
               "x-ratelimit-reset": "3600"}

    def test_each_lane_records_what_openalex_reports(self):
        with _oa_state({"keyed": _OAResp(200, self.KEYED_OK), "anon": _OAResp(200, self.ANON_OK)}):
            _openalex.get_json("/works", {"filter": "x"})
            _openalex._api_key = None  # force the keyless lane once
            _openalex.get_json("/works", {"filter": "x"})
            st = _openalex.usage_stats()
            self.assertEqual(1.0, st["lanes"]["keyed"]["limit-usd"])
            self.assertEqual(0.1, st["lanes"]["anon"]["limit-usd"])
            self.assertEqual(0.1, st["keyless_share_of_keyed"])
            self.assertEqual(7331, st["remaining"]["keyed"])

    def test_a_budget_429_dries_the_lane_until_the_reported_reset_then_spills(self):
        spent = {"x-ratelimit-remaining": "0", "x-ratelimit-remaining-usd": "0",
                 "x-ratelimit-reset": "120", "x-ratelimit-limit-usd": "1"}
        with _oa_state({"keyed": _OAResp(429, spent), "anon": _OAResp(200, self.ANON_OK)}) as calls:
            _openalex.get_json("/works", {"filter": "x"})
            self.assertEqual(["keyed", "anon"], calls)
            dry = _openalex._state["dry_until"]["keyed"] - time.monotonic()
            self.assertTrue(100 < dry <= 120, f"dry for {dry:.0f}s, expected the reported 120s")

    def test_an_empty_lane_is_skipped_for_billable_calls_but_not_for_free_lookups(self):
        with _oa_state({"keyed": _OAResp(200, self.KEYED_OK), "anon": _OAResp(200, self.ANON_OK)}):
            _openalex._usage["lanes"]["keyed"] = {"remaining": 0, "resets_at": time.time() + 600}
            billable = [n for n, _ in _openalex._open_lanes({}, billable=True)]
            free = [n for n, _ in _openalex._open_lanes({}, billable=False)]
            self.assertEqual(["anon"], billable)
            self.assertEqual(["keyed", "anon"], free)
            self.assertTrue(_openalex._billable("/works", {"filter": "doi:1"}))  # a list call
            self.assertFalse(_openalex._billable("/works/W123", {}))              # get-by-id: free

    def test_health_block_flags_a_reading_that_contradicts_the_declaration(self):
        with _oa_state({}):
            upstreams.observe("openalex", {"x-ratelimit-limit-usd": "0.5"}, 200, lane="keyed")
            hb = upstreams.health_block(["openalex"], lambda s: "openalex")
            row = next(r for r in hb["rows"] if r["id"] == "openalex")
            self.assertTrue(any("x-ratelimit-limit-usd" in f and "differs" in f for f in row["flags"]),
                            row["flags"])


class HealthBlockTests(unittest.TestCase):
    def test_undeclared_sources_and_stale_checks_are_flagged(self):
        hb = upstreams.health_block(["arxiv", "zz_not_declared_anywhere"], lambda s: s,
                                    today="2027-06-01")
        self.assertEqual(["zz_not_declared_anywhere"], hb["undeclared_sources"])
        arxiv = next(r for r in hb["rows"] if r["id"] == "arxiv")
        self.assertTrue(any("older than" in f for f in arxiv["flags"]))
        self.assertEqual(1, arxiv["terms"]["max_concurrency"])

    def test_a_pending_limit_is_flagged_not_hidden(self):
        d = upstreams.declarations()
        d["t-pending"] = {"name": "t", "hosts": ["pending.test.invalid"], "tools": ["test"],
                          "terms": {"max_concurrency": 2, "min_interval_s": upstreams.UNPUBLISHED,
                                    "windows": upstreams.UNPUBLISHED,
                                    "daily_quota": upstreams.UNPUBLISHED, "url": "https://x.invalid",
                                    "checked": "2026-09-29", "where_checked": "test"},
                          "pending": "a published value the gate does not enforce yet"}
        try:
            hb = upstreams.health_block([], lambda s: s)
        finally:
            d.pop("t-pending", None)
        row = next(r for r in hb["rows"] if r["id"] == "t-pending")
        self.assertTrue(any("not enforced yet" in f for f in row["flags"]), row["flags"])

    def test_no_declared_limit_is_left_pending(self):
        self.assertEqual([], sorted(k for k, e in upstreams.declarations().items() if e.get("pending")))

    def test_crawl_delay_hosts_show_their_gate_in_the_health_block(self):
        hb = upstreams.health_block([], lambda s: s)
        row = next(r for r in hb["rows"] if r["id"] == "web:substack_matrix")
        self.assertEqual(3600.0, row["host_gates"]["statmodeling.stat.columbia.edu"]["min_interval_s"])
        self.assertEqual(1, row["host_gates"]["lemire.me"]["max_inflight"])


@contextlib.contextmanager
def _temp_host_delay(host, seconds):
    """Declare a robots.txt Crawl-delay for a throwaway host for one test."""
    d = upstreams.declarations()
    d["t-robots-" + host.split(".")[0]] = {
        "name": host, "hosts": [host], "tools": ["test"],
        "terms": {"robots_crawl_delay_s": {host: seconds}}}
    upstreams._host_delay[host] = float(seconds)
    try:
        yield upstreams.host_guard(host)
    finally:
        d.pop("t-robots-" + host.split(".")[0], None)
        upstreams._host_delay.pop(host, None)
        upstreams._host_guards.pop(host, None)


def _spacing(rec):
    starts = sorted(s for s, _ in rec.spans)
    return [b - a for a, b in zip(starts, starts[1:])]


class CrawlDelayGateTests(unittest.TestCase):
    """robots.txt Crawl-delay is enforced as the host's gate: one request at a time, starts at least
    the delay apart; with an upstream gate on the same host the stricter holds (2026-09-29)."""

    def _burst(self, url, n=3):
        rec = _Recorder(latency=0.02)
        with _stub_http(rec):
            ts = [threading.Thread(target=lambda: http.get_text(url)) for _ in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(15)
        return rec

    def test_a_crawl_delay_host_gets_one_request_at_a_time_delay_apart(self):
        with _temp_host_delay("delayed.test.invalid", 0.3):
            rec = self._burst("https://delayed.test.invalid/feed")
        self.assertEqual(3, len(rec.spans))
        self.assertEqual(1, rec.peak)
        self.assertGreaterEqual(min(_spacing(rec)), 0.3 - 0.02, _spacing(rec))

    def test_the_stricter_of_crawl_delay_and_upstream_gate_holds(self):
        # the Crawl-delay is the stricter: upstream allows 0.05 s, robots.txt says 0.3 s
        with _temp_upstream("t-both-a", "both-a.test.invalid",
                            {"max_inflight": 4, "min_interval_s": 0.05, "max_wait_s": 5.0}), \
                _temp_host_delay("both-a.test.invalid", 0.3):
            rec = self._burst("https://both-a.test.invalid/x")
        self.assertEqual(1, rec.peak)
        self.assertGreaterEqual(min(_spacing(rec)), 0.3 - 0.02, _spacing(rec))
        # the upstream gate is the stricter: it says 0.4 s, robots.txt only 0.1 s
        with _temp_upstream("t-both-b", "both-b.test.invalid",
                            {"max_inflight": 1, "min_interval_s": 0.4, "max_wait_s": 5.0}), \
                _temp_host_delay("both-b.test.invalid", 0.1):
            rec = self._burst("https://both-b.test.invalid/x")
        self.assertGreaterEqual(min(_spacing(rec)), 0.4 - 0.02, _spacing(rec))

    def test_every_declared_crawl_delay_is_a_host_gate(self):
        declared = {}
        for e in upstreams.declarations().values():
            for h, secs in ((e.get("terms") or {}).get("robots_crawl_delay_s") or {}).items():
                declared[h] = max(secs, declared.get(h, 0))
        self.assertGreaterEqual(len(declared), 20)
        for h, secs in declared.items():
            gates = upstreams._gates_for_url(f"https://{h}/any")
            g = gates[-1][1]
            self.assertEqual((h, 1, float(secs)), (g.name, g.max_inflight, g.min_interval_s))
        # one host listed by two declarations shares ONE gate
        self.assertIs(upstreams.host_guard("www.alignmentforum.org"),
                      upstreams._gates_for_url("https://www.alignmentforum.org/feed.xml")[-1][1])

    def _assert_fetch_takes_host_gate(self, host, call, patch_target, patch_attr, fake_factory):
        """Run ``call`` with the network stubbed; every request it makes must hold the Crawl-delay
        gate of the host it requests, and the first one must be ``host``."""
        seen = []

        def record(url):
            h = upstreams._host(str(url))
            seen.append((h, upstreams.crawl_delay_for(h) is not None
                         and upstreams.host_guard(h).held()))
        for g in list(upstreams._host_guards.values()):
            _reset_guard(g)
        with mock.patch.object(patch_target, patch_attr, fake_factory(record)):
            call()
        for g in list(upstreams._host_guards.values()):
            _reset_guard(g)
        self.assertTrue(seen, f"{host}: no request was made")
        self.assertEqual(host, seen[0][0])
        self.assertTrue(all(held for _, held in seen), f"not held: {seen}")

    @staticmethod
    def _fake_transport(record):
        """A transport stub for the direct paths (http.direct builds a real client): records the host
        while the request is on the wire."""
        def handle(self, request):
            record(str(request.url))
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  stream=httpx.ByteStream(b"<html></html>"), request=request)
        return handle

    def test_direct_fetch_paths_take_the_host_gate(self):
        from omniseek.core.sources.scrape import (ai_residencies_source, ajo_source, news_scraper_source,
                                                overseas_ai_jobs_source, page_watch_source)
        cases = [
            ("f.gter.net", lambda: news_scraper_source._get("https://f.gter.net/forum.php")),
            ("brightdata.com", lambda: page_watch_source._page_text("https://brightdata.com/x")),
            ("academicjobsonline.org", lambda: ajo_source.AJOAdapter()._positions()),
            ("api.lever.co", lambda: ai_residencies_source._http_get("https://api.lever.co/v0/postings/x")),
            ("api.lever.co", lambda: overseas_ai_jobs_source._get("https://api.lever.co/v0/postings/y")),
        ]
        from omniseek.core import cache
        with mock.patch.object(cache, "get", lambda *a, **k: None), \
                mock.patch.object(cache, "set", lambda *a, **k: None):
            for host, call in cases:
                with self.subTest(host=host):
                    self._assert_fetch_takes_host_gate(host, call, httpx.HTTPTransport,
                                                       "handle_request", self._fake_transport)

    def test_cdp_render_and_document_download_take_the_host_gate(self):
        from omniseek.core import docreader
        from omniseek.core.sources.scrape import ml_conferences_source
        from omniseek.core.sources.walled import _cdp

        def fake_cdp(record):
            def fake(fn, initial_url=None, on_turn=None, **k):   # the real cdp_call's order:
                with (on_turn() if on_turn else contextlib.nullcontext()):   # turn, then the gate
                    record(initial_url)
                return "<html></html>"
            return fake
        self._assert_fetch_takes_host_gate(
            "blog.neurips.cc", lambda: ml_conferences_source.MlConferencesAdapter()._raw_fetch("x", 3),
            _cdp, "cdp_call", fake_cdp)

        def fake_wire(record):   # the real walker and client; only the transport is stubbed
            def handle(self, request):
                record(str(request.url))
                return httpx.Response(200, headers={"content-type": "application/pdf"},
                                      stream=httpx.ByteStream(b"%PDF-1.4 x"), request=request)
            return handle

        def download():
            path, _ct, _cd = docreader._download("https://arxiv.org/pdf/2401.00001.pdf", "pdf")
            path.unlink()
        with mock.patch.object(docreader._netguard, "security_block_reason", lambda url: None):
            self._assert_fetch_takes_host_gate("arxiv.org", download, httpx.HTTPTransport,
                                               "handle_request", fake_wire)


class SingleConnectionTests(unittest.TestCase):
    """arXiv: "limit requests to a single connection at a time". Beyond one request in flight, no
    connection may outlive its request: Connection: close on every request, and a no-keep-alive
    HTTP/1.1 transport mounted for the host on both shared clients (2026-09-29)."""

    def test_arxiv_is_the_declared_single_connection_host_and_gets_connection_close(self):
        self.assertEqual(["export.arxiv.org"], upstreams.single_connection_hosts())
        self.assertEqual("close", http._with_connection_close(
            "https://export.arxiv.org/api/query", {}).get("Connection"))
        self.assertNotIn("Connection", http._with_connection_close("https://api.crossref.org/x", {}))

    def test_both_shared_clients_mount_the_one_connection_transport_for_arxiv(self):
        from omniseek.core import safeurl
        saved = (http._client, http._aclient, http._aclient_loop)
        http._client = http._aclient = http._aclient_loop = None
        try:
            url = httpx.URL("https://export.arxiv.org/api/query")
            t = http._get_client()._transport_for_url(url)
            self.assertIsInstance(t, safeurl.SSRFGuardTransport)
            self.assertIsInstance(t._wrapped, httpx.HTTPTransport)
            self.assertIsNot(t, http._get_client()._transport_for_url(httpx.URL("https://x.org/")))
            at = anyio.run(lambda: _async_transport(url))
            self.assertIsInstance(at, safeurl.AsyncSSRFGuardTransport)
            self.assertIsInstance(at._wrapped, httpx.AsyncHTTPTransport)
        finally:
            for c in (http._client,):
                if c is not None:
                    c.close()
            http._client, http._aclient, http._aclient_loop = saved

    def test_arxiv_call_sites_send_connection_close(self):
        from omniseek.core.sources.api import arxiv_source
        g = arxiv_source._guard
        _reset_guard(g)
        seen = []
        with mock.patch.object(arxiv_source.http, "get_text",
                               lambda url, **k: seen.append(k.get("headers")) or "<feed/>"):
            arxiv_source._arxiv_get_text(arxiv_source._API, params={"search_query": "x"})
        _reset_guard(g)
        with mock.patch.object(arxiv_source.http, "direct",
                               lambda method, url, **k: seen.append(k.get("headers")) or httpx.Response(
                                   200, request=httpx.Request(method, url))):
            arxiv_source.ArxivAdapter().health_check()
        _reset_guard(g)
        self.assertEqual(2, len(seen))
        self.assertTrue(all((h or {}).get("Connection") == "close" for h in seen), seen)

    def test_the_transport_closes_each_connection_after_its_response(self):
        """Behaviour, on a loopback server that would KEEP connections alive: with the one-connection
        transport every request gets its own connection and the CLIENT closes it before the next
        request starts; the default pooled transport keeps it open (negative control)."""
        import http.server as std_http_server
        import socketserver

        events = {"accepted": 0, "closed": threading.Event(), "conns": []}

        class Handler(std_http_server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                events["accepted"] += 1

            def parse_request(self):
                ok = super().parse_request()
                events["conns"].append(self.headers.get("Connection"))
                self.close_connection = False   # the server itself never closes
                return ok

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(b"ok")

            def finish(self):
                super().finish()
                events["closed"].set()

            def log_message(self, *a):
                pass

        class Server(socketserver.ThreadingMixIn, std_http_server.HTTPServer):
            daemon_threads = True

        srv = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/"
        try:
            with httpx.Client(transport=http._one_connection_transport()) as client:
                for i in range(3):
                    events["closed"].clear()
                    client.get(base, headers={"Connection": "close"})
                    self.assertTrue(events["closed"].wait(3.0),
                                    f"request {i + 1}: the connection outlived its response")
            self.assertEqual(3, events["accepted"])
            self.assertEqual(["close"] * 3, events["conns"])
            events["closed"].clear()
            with httpx.Client() as keep:   # negative control: the default transport keeps it open
                keep.get(base)
                self.assertFalse(events["closed"].wait(0.5), "control: default pool closed early")
        finally:
            srv.shutdown()
            srv.server_close()


async def _async_transport(url):
    return http._aget_client()._transport_for_url(url)


def _wm_ua() -> str:
    return upstreams.entry("wikimedia")["user_agent"]


class WikimediaUserAgentTests(unittest.TestCase):
    """Wikimedia requests carry a policy-compliant User-Agent (name/version, the public OmniSeek repo
    as contact, 'bot', library; no email, not a browser's), and the 'User-Agent only' tier is the
    gate: at most 3 concurrent, 200 per minute (2026-09-29). The string is declared once, in
    upstreams.json (entry "wikimedia", field "user_agent")."""

    def test_the_user_agent_follows_the_policy_format(self):
        ua = _wm_ua()
        self.assertRegex(ua, r"^[\w.-]+/[\w.]+ \([^)]*https://github\.com/Battam1111/omniseek[^)]*\) "
                             r"python-httpx/[\w.]+$")
        self.assertIn("bot", ua.lower())
        self.assertNotIn("@", ua)
        self.assertFalse(ua.startswith("Mozilla"))

    def test_it_is_written_in_one_place(self):
        src = EYE.parent   # src/omniseek
        holders = sorted(str(f.relative_to(src)) for f in src.rglob("*")
                         if f.suffix in (".py", ".json") and "omniseek; retrieval bot"
                         in f.read_text(encoding="utf-8", errors="replace"))
        self.assertEqual([str(Path("core") / "upstreams.json")], holders)

    def test_every_wikimedia_call_sends_it(self):
        from omniseek.core.sources.scrape import wikidata_wikipedia_source as wiki
        src = Path(wiki.__file__).read_text(encoding="utf-8")
        body = src.split("\nclass WikidataWikipediaAdapter", 1)[1]   # everything after the helpers
        self.assertNotRegex(body, r"(=|await)\s*http\.a?get_json\(",
                            "a Wikimedia call bypasses the declared User-Agent")
        seen = []

        def fake(url, **k):
            seen.append((url, (k.get("headers") or {}).get("User-Agent")))
            return None
        from omniseek.core import cache
        with mock.patch.object(wiki.http, "get_json", fake), \
                mock.patch.object(cache, "get", lambda *a, **k: None):
            wiki.WikidataWikipediaAdapter()._raw_fetch("Yoshua Bengio", 3)
        self.assertTrue(seen)
        self.assertTrue(all(ua == _wm_ua() for _, ua in seen), seen)

    def test_the_gate_is_the_user_agent_only_tier(self):
        self.assertEqual("wikimedia", upstreams.gated_uid_for_url("https://en.wikipedia.org/w/api.php"))
        self.assertEqual("wikimedia", upstreams.gated_uid_for_url("https://www.wikidata.org/w/api.php"))
        g = upstreams.guard("wikimedia")
        self.assertEqual((3, [(200, 60.0)]), (g.max_inflight, g.windows))


_WIKI = "https://en.wikipedia.org/wiki/Alan_Turing"
_PAGE = ("<html><head><title>A page</title></head><body><main>"
         + "<p>" + " ".join(["plain server-rendered article text"] * 60) + "</p>"
         + "</main></body></html>").encode()


def _host_of(request) -> str:
    return (request.headers.get("host") or request.url.host).split(":")[0]


def _redirecting(chain: dict):
    """A transport body: 302 to chain[host] for a host in ``chain``, else 200 with a page."""
    def respond(request):
        nxt = chain.get(_host_of(request))
        if nxt:
            return httpx.Response(302, headers={"location": nxt}, stream=httpx.ByteStream(b""),
                                  request=request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              stream=httpx.ByteStream(_PAGE), request=request)
    return respond


@contextlib.contextmanager
def _sync_wire(respond, gate=None):
    """Every sync httpx transport in the process answers with ``respond``; yields [(host, UA)] as
    the request left the client (after its hooks), plus whether ``gate`` was held at that moment
    when one is given. No DNS, no socket."""
    seen = []

    def handle(self, request):
        row = (_host_of(request), request.headers.get("user-agent"))
        seen.append(row + (gate.held(),) if gate is not None else row)
        return respond(request)
    with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
            mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
            mock.patch.object(http._netguard, "resolve_pin", _no_pin):
        yield seen


def _no_pin(url):
    """The SSRF transport's resolver, stubbed: never resolve, never pin (pass through by name)."""
    return None, urlsplit(url).hostname, "dns"


def _eye_read_seen(tc, url, respond, gate=None):
    """Run the real omniseek_read body on ``url`` with the wire stubbed (no DNS, no socket, no adapter
    claims it, so the generic web read answers); returns what left the client, per request."""
    from omniseek.core import cache, fetcher, safeurl, web_fallback
    jina = []
    with _sync_wire(respond, gate) as seen, \
            mock.patch.dict(fetcher._adapters, clear=True), \
            mock.patch.object(safeurl, "_resolve_safe_ip",
                              lambda host: ("93.184.216.34", socket.AF_INET, None)), \
            mock.patch.object(cache, "get", lambda *a, **k: None), \
            mock.patch.object(cache, "set", lambda *a, **k: None), \
            mock.patch.object(web_fallback, "_jina_markdown", lambda u: jina.append(u)):
        out = omniseek.server.omniseek_read.__wrapped__(target=url)
    tc.assertEqual([], jina, "the plain read was enough; nothing may go through Jina")
    tc.assertTrue(out.get("matched"), out)
    return seen


class DeclaredUserAgentTests(unittest.TestCase):
    """A host whose upstream declares a User-Agent gets exactly that string from every shared egress
    layer (both shared clients, safe_fetch and so web_fallback / omniseek_read, the redirect walker used by
    docreader, the curl tier, the direct fetch paths), redirect hops included; every other host keeps
    what it had; the shared browser refuses such a host (driver decision, 2026-09-29)."""

    def setUp(self):
        _reset_guard(upstreams.guard("wikimedia"))
        for g in list(upstreams._host_guards.values()):
            _reset_guard(g)

    tearDown = setUp

    def _eye_read(self, url, respond):
        return _eye_read_seen(self, url, respond)

    def test_eye_read_of_a_wikipedia_link_sends_the_declared_user_agent(self):
        seen = self._eye_read(_WIKI, _redirecting({}))
        self.assertEqual([("en.wikipedia.org", _wm_ua())], seen)

    def test_eye_read_of_another_host_is_unaffected(self):
        seen = self._eye_read("https://example.org/page", _redirecting({}))
        self.assertEqual([("example.org", http.USER_AGENT)], seen)

    def test_safe_fetch_sets_it_per_redirect_hop(self):
        seen = self._eye_read("https://example.org/start", _redirecting({
            "example.org": _WIKI, "en.wikipedia.org": "https://example.com/end"}))
        self.assertEqual([("example.org", http.USER_AGENT), ("en.wikipedia.org", _wm_ua()),
                          ("example.com", http.USER_AGENT)], seen)

    def test_the_shared_sync_client_sets_it_per_redirect_hop(self):
        saved = http._client
        http._client = None
        try:
            with _sync_wire(_redirecting({"example.org": _WIKI,
                                          "en.wikipedia.org": "https://example.com/end"})) as seen:
                self.assertIsNotNone(http.get_text("https://example.org/start"))
                self.assertIsNotNone(http.get_text(_WIKI, headers={"User-Agent": "caller/1"}))
            self.assertEqual([("example.org", http.USER_AGENT), ("en.wikipedia.org", _wm_ua()),
                              ("example.com", http.USER_AGENT), ("en.wikipedia.org", _wm_ua()),
                              ("example.com", http.USER_AGENT)], seen)
        finally:
            if http._client is not None:
                http._client.close()
            http._client = saved

    def test_the_shared_async_client_sets_it_per_redirect_hop(self):
        respond = _redirecting({"example.org": _WIKI, "en.wikipedia.org": "https://example.com/end"})
        seen = []

        async def handle(self, request):
            seen.append((_host_of(request), request.headers.get("user-agent")))
            return respond(request)

        async def run():
            try:
                return await http.aget_text("https://example.org/start")
            finally:
                await http.aclose_client()
        saved = (http._aclient, http._aclient_loop)
        http._aclient = http._aclient_loop = None
        try:
            with mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None):
                self.assertIsNotNone(anyio.run(run))
        finally:
            http._aclient, http._aclient_loop = saved
        self.assertEqual([("example.org", http.USER_AGENT), ("en.wikipedia.org", _wm_ua()),
                          ("example.com", http.USER_AGENT)], seen)

    def test_the_redirect_walker_and_document_download_set_it_per_hop(self):
        from omniseek.core import docreader, safeurl
        with _sync_wire(_redirecting({"example.org": _WIKI})) as seen:
            with httpx.Client(headers={"User-Agent": "doc-reader/1"}) as client:
                r = safeurl.walk_redirects_revalidated(client, "GET", "https://example.org/doc")
                r.close()
            path, _ctype, _cd = docreader._download("https://www.wikidata.org/file.bin", "bin")
            path.unlink()
        self.assertEqual([("example.org", "doc-reader/1"), ("en.wikipedia.org", _wm_ua()),
                          ("www.wikidata.org", _wm_ua())], seen)

    def test_the_curl_tier_sends_it_and_leaves_other_hosts_alone(self):
        import sys
        import tempfile
        import types
        sent = []

        class _Resp:
            status_code, headers, content = 200, {}, b"body"

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=1):
                yield b"body"

            def close(self):
                return None

        def request(method, url, **k):
            sent.append((upstreams._host(url), (k.get("headers") or {}).get("User-Agent")))
            return _Resp()
        fake = types.ModuleType("curl_cffi.requests")
        fake.request = request
        fake.get = lambda url, **k: request("GET", url, **k)
        pkg = types.ModuleType("curl_cffi")
        pkg.requests = fake
        with mock.patch.dict(sys.modules, {"curl_cffi": pkg, "curl_cffi.requests": fake}), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                tempfile.TemporaryDirectory() as tmp:
            http._impersonated_request("GET", _WIKI, headers={"User-Agent": http.USER_AGENT})
            http._impersonated_request("GET", "https://example.org/feed")
            http.download_to_file("https://www.wikidata.org/big.bin", str(Path(tmp) / "f"),
                                  max_bytes=100)
        self.assertEqual([("en.wikipedia.org", _wm_ua()), ("example.org", None),
                          ("www.wikidata.org", _wm_ua())], sent)

    def test_direct_fetch_paths_send_it(self):
        from omniseek.core import cache
        from omniseek.core.sources.scrape import (ai_residencies_source, ajo_source, news_scraper_source,
                                                overseas_ai_jobs_source, page_watch_source)
        sent = []

        def handle(self, request):
            sent.append((request.url.host, request.headers.get("user-agent")))
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  stream=httpx.ByteStream(b"<html></html>"), request=request)
        with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(cache, "get", lambda *a, **k: None), \
                mock.patch.object(cache, "set", lambda *a, **k: None):
            news_scraper_source._get(_WIKI)
            page_watch_source._page_text(_WIKI)
            ai_residencies_source._http_get(_WIKI)
            overseas_ai_jobs_source._get(_WIKI)
            news_scraper_source._get("https://example.org/news")
            # ajo reads one fixed host: give it a declared User-Agent for this test only
            with mock.patch.dict(upstreams._host_ua, {"academicjobsonline.org": "declared-test/1"}):
                ajo_source.AJOAdapter()._positions()
        self.assertEqual([("en.wikipedia.org", _wm_ua())] * 4
                         + [("example.org", news_scraper_source.UA),
                            ("academicjobsonline.org", "declared-test/1")], sent)

    def test_the_shared_browser_refuses_a_host_with_a_declared_user_agent(self):
        from omniseek.core.sources.scrape import ml_conferences_source, news_scraper_source, page_watch_source
        from omniseek.core.sources.walled import _cdp
        with self.assertRaises(upstreams.DeclaredUserAgentRefused):
            with upstreams.egress(_WIKI, browser=True):
                pass
        with upstreams.egress("https://example.org/", browser=True):
            pass   # no declared User-Agent: the browser may load it
        rendered = []

        def fake_cdp(fn, initial_url=None, **k):
            rendered.append(upstreams._host(initial_url))
            return "<html></html>"
        with mock.patch.object(_cdp, "cdp_call", fake_cdp):
            self.assertIsNone(news_scraper_source._render(_WIKI))
            self.assertEqual("", page_watch_source._render_html(_WIKI))
            with mock.patch.dict(upstreams._host_ua, {"blog.neurips.cc": "declared-test/1"}):
                ml_conferences_source.MlConferencesAdapter()._raw_fetch("x", 3)
        self.assertNotIn("en.wikipedia.org", rendered)
        self.assertNotIn("blog.neurips.cc", rendered)
        self.assertIn("blog.iclr.cc", rendered)   # the other venues still render

    def test_matching_is_per_host_case_and_trailing_dot_insensitive(self):
        for target in ("https://EN.Wikipedia.org./wiki/X", "en.wikipedia.org", "www.wikidata.org"):
            self.assertEqual(_wm_ua(), upstreams.user_agent_for(target), target)
        self.assertEqual("wikimedia", upstreams.gated_uid_for_url("https://en.wikipedia.org./w/api.php"))
        for target in ("https://example.org/", "https://wikipedia.org.example.com/"):
            self.assertIsNone(upstreams.user_agent_for(target), target)
        self.assertEqual({"user-agent": "x"},
                         upstreams.with_declared_user_agent("https://example.org/", {"user-agent": "x"}))
        self.assertEqual({"Accept": "y", "User-Agent": _wm_ua()},
                         upstreams.with_declared_user_agent(_WIKI, {"user-agent": "x", "Accept": "y"}))

    def test_a_host_has_at_most_one_declared_user_agent_and_the_schema_checks_it(self):
        by_host = {}
        for e in upstreams.declarations().values():
            if e.get("user_agent"):
                for h in e.get("hosts") or ():
                    by_host.setdefault(h.lower(), set()).add(e["user_agent"])
        self.assertTrue(by_host)
        self.assertTrue(all(len(v) == 1 for v in by_host.values()), by_host)
        # a suffix and a host (or a longer suffix) it covers may not declare different strings
        clash = [(h, sfx) for h in by_host for sfx in by_host if sfx.startswith(".") and h != sfx
                 and ("." + h.lstrip(".")).endswith(sfx) and by_host[h] != by_host[sfx]]
        self.assertEqual([], clash)
        for bad in ("", "   ", 5):
            problems = upstreams.schema_problems("t", dict(upstreams.entry("wikimedia"), user_agent=bad))
            self.assertTrue(any("user_agent" in p for p in problems), (bad, problems))


_FAMILY = ("wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org", "wikiquote.org",
           "wikibooks.org", "wikisource.org", "wikinews.org", "wikiversity.org", "wikivoyage.org",
           "mediawiki.org")


class WikimediaFamilyTests(unittest.TestCase):
    """The whole Wikimedia family is one declaration (driver decision, 2026-09-29): every listed domain
    and every subdomain of it share the one Wikimedia gate and the declared user_agent. A suffix is
    written with a leading dot and matches at a label boundary only, so a look-alike never does."""

    def setUp(self):
        _reset_guard(upstreams.guard("wikimedia"))

    tearDown = setUp

    def test_the_family_is_declared_as_domains_and_leading_dot_suffixes(self):
        hosts = set(upstreams.entry("wikimedia")["hosts"])
        missing = [h for d in _FAMILY for h in (d, "." + d) if h not in hosts]
        self.assertEqual([], missing)

    def test_family_hosts_share_the_one_gate_and_the_user_agent(self):
        wm = upstreams.guard("wikimedia")
        for host in ("zh.wikipedia.org", "commons.wikimedia.org", "upload.wikimedia.org", "wikipedia.org",
                     "query.wikidata.org", "de.wiktionary.org", "www.mediawiki.org", "a.b.wikivoyage.org",
                     "en.wikipedia.org", "www.wikidata.org"):
            with self.subTest(host=host):
                gates = upstreams._gates_for_url(f"https://{host}/x")
                self.assertEqual(["wikimedia"], [k for k, _, _ in gates])
                self.assertIs(wm, gates[0][1])
                self.assertEqual(_wm_ua(), upstreams.user_agent_for(host))

    def test_look_alike_hosts_do_not_match(self):
        for host in ("evilwikipedia.org", "notwikipedia.org", "evil-wikipedia.org", "wikipedia.org.evil.com",
                     "wikipedia.com", "xwikimedia.org", "mediawiki.org.example.com"):
            with self.subTest(host=host):
                self.assertIsNone(upstreams.uid_for_host(host))
                self.assertEqual([], upstreams._gates_for_url(f"https://{host}/x"))
                self.assertIsNone(upstreams.user_agent_for(host))

    def test_eye_read_of_zh_wikipedia_and_commons_sends_it_inside_the_wikimedia_gate(self):
        wm = upstreams.guard("wikimedia")
        for url, host in (("https://zh.wikipedia.org/wiki/Alan_Turing", "zh.wikipedia.org"),
                          ("https://commons.wikimedia.org/wiki/Main_Page", "commons.wikimedia.org")):
            with self.subTest(host=host):
                self.assertEqual([(host, _wm_ua(), True)], _eye_read_seen(self, url, _redirecting({}), wm))
        for url, host in (("https://evilwikipedia.org/wiki/X", "evilwikipedia.org"),
                          ("https://example.org/page", "example.org")):
            with self.subTest(host=host):
                self.assertEqual([(host, http.USER_AGENT, False)],
                                 _eye_read_seen(self, url, _redirecting({}), wm))

    def test_family_hosts_count_against_one_gate(self):
        """Six family hosts fired at once: at most 3 in flight in total, the Wikimedia limit. Six
        separate gates would have let all six run together."""
        rec = _Recorder(latency=0.3)
        urls = [f"https://{h}/w/api.php" for h in ("en.wikipedia.org", "zh.wikipedia.org", "wikipedia.org",
                                                   "commons.wikimedia.org", "www.wikidata.org",
                                                   "fr.wiktionary.org")]
        results = {}
        with _stub_http(rec):
            ts = [threading.Thread(target=lambda u=u: results.__setitem__(u, http.get_text(u))) for u in urls]
            for t in ts:
                t.start()
            for t in ts:
                t.join(15)
        self.assertEqual(6, len(rec.spans))
        self.assertTrue(all(results.get(u) for u in urls), results)
        self.assertEqual(3, rec.peak)

    def test_suffix_precedence_and_the_schema(self):
        # an ungated declaration listing a family host exactly does not take it from the gate
        d = upstreams.declarations()
        d["t-ungated"] = {"name": "t", "hosts": ["zh.wikipedia.org"], "sources": [], "tools": ["test"],
                          "terms": {"max_concurrency": upstreams.UNPUBLISHED}}
        saved = dict(upstreams._host_index)
        upstreams._host_index["zh.wikipedia.org"] = "t-ungated"
        try:
            self.assertEqual("wikimedia", upstreams.uid_for_host("zh.wikipedia.org"))
        finally:
            d.pop("t-ungated", None)
            upstreams._host_index.clear()
            upstreams._host_index.update(saved)
        e = upstreams.entry("wikimedia")
        for bad in (".org", "*.wikipedia.org", "Wikipedia.org", "wikipedia.org/", "wiki pedia.org"):
            with self.subTest(host=bad):
                problems = upstreams.schema_problems("t", dict(e, hosts=[bad]))
                self.assertTrue(any("host" in p for p in problems), problems)
        self.assertEqual([], upstreams.schema_problems("t", dict(e, hosts=[".wikipedia.org"])))
        one = upstreams.entry("arxiv")
        self.assertTrue(any("one-connection" in p for p in upstreams.schema_problems(
            "t", dict(one, hosts=["export.arxiv.org", ".arxiv.org"]))))
        self.assertEqual(["export.arxiv.org"], upstreams.single_connection_hosts())


class _WireClient:
    """Stand-in for the web-search backend's pooled clients: records every URL asked for and answers
    Brave with the scripted status; it would answer DuckDuckGo too, so a request there shows up here
    instead of being refused by the stub."""

    def __init__(self, brave_status=429):
        self.urls = []
        self.brave_status = brave_status

    def _answer(self, method, url):
        self.urls.append(url)
        req = httpx.Request(method, url)
        if "duckduckgo" in url:
            return httpx.Response(200, text="<html></html>", request=req)
        if self.brave_status == 200:
            return httpx.Response(200, json={"web": {"results": [
                {"title": "t", "url": "https://x.com/item/1", "description": "d"}]}}, request=req)
        return httpx.Response(self.brave_status, headers={"Retry-After": "60"}, request=req)

    def get(self, url, **kw):
        return self._answer("GET", url)

    def post(self, url, **kw):
        return self._answer("POST", url)


class _AWireClient(_WireClient):
    async def get(self, url, **kw):
        return self._answer("GET", url)

    async def post(self, url, **kw):
        return self._answer("POST", url)


@contextlib.contextmanager
def _temp_disabled_upstream(uid="t-off", reason="test: this upstream is out of service"):
    """Declare a throwaway upstream OmniSeek must not send to (restores the registry afterwards)."""
    d = upstreams.declarations()
    d[uid] = {"name": uid, "kind": "api", "hosts": ["off.test.invalid"], "sources": [], "tools": ["test"],
              "disabled": {"reason": reason, "source": "https://off.test.invalid/robots.txt",
                           "checked": "2026-09-29"},
              "terms": {"max_concurrency": upstreams.UNPUBLISHED, "min_interval_s": upstreams.UNPUBLISHED,
                        "windows": upstreams.UNPUBLISHED, "daily_quota": upstreams.UNPUBLISHED,
                        "url": "https://off.test.invalid/terms", "checked": "2026-09-29",
                        "where_checked": "test declaration"}}
    try:
        yield uid, reason
    finally:
        d.pop(uid, None)


class DisabledUpstreamTests(unittest.TestCase):
    """The generic ``disabled`` declaration: an upstream OmniSeek must not send to, with the reason and
    its source (kept as a mechanism when the DuckDuckGo decision was withdrawn, 2026-09-29). Tested on
    a throwaway upstream; the web-search backend's keyless fallback is the one place wired to read it,
    so those tests point that wiring at the throwaway upstream, never at DuckDuckGo's declaration."""

    def setUp(self):
        from omniseek.core.sources.api import _search_backend as sb
        self.sb = sb
        self._reset()

    def tearDown(self):
        self._reset()

    def _reset(self):
        sb = self.sb
        sb._brave_cooldown_until = sb._brave_last_call = 0.0
        sb._ddg_cooldown_until = sb._ddg_last_call = 0.0
        sb._ddg_consecutive_trips = 0
        sb._last_error = None
        sb._ping.update(t=0.0, ok=None, msg="")

    @contextlib.contextmanager
    def _backend(self, key, brave_status=429, fallback_uid=None):
        client, aclient = _WireClient(brave_status), _AWireClient(brave_status)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.sb, "_get_client", lambda: client))
            stack.enter_context(mock.patch.object(self.sb, "_aget_client", lambda: aclient))
            stack.enter_context(mock.patch.object(self.sb, "_brave_key", lambda: key))
            if fallback_uid:
                stack.enter_context(mock.patch.object(self.sb, "_FALLBACK_UID", fallback_uid))
            yield client, aclient

    def test_the_declaration_carries_the_reason_and_the_health_view_shows_it(self):
        with _temp_disabled_upstream() as (uid, reason):
            self.assertEqual(reason, upstreams.disabled_reason(uid))
            self.assertEqual([], upstreams.schema_problems(uid, upstreams.entry(uid)))
            rows = {r["id"]: r for r in upstreams.health_block([], lambda s: s)["rows"]}
            self.assertEqual(reason, rows[uid].get("disabled"))
            for bad in ({"reason": reason}, {"reason": "", "source": "https://x", "checked": "2026-09-29"},
                        "off"):
                problems = upstreams.schema_problems(uid, dict(upstreams.entry(uid), disabled=bad))
                self.assertTrue(any("disabled" in p for p in problems), (bad, problems))
        self.assertIsNone(upstreams.disabled_reason(uid))
        self.assertEqual([], [u for u, e in upstreams.declarations().items() if e.get("disabled")],
                         "no shipped upstream is disabled")

    def test_a_disabled_fallback_gets_no_request_and_the_caller_is_told_why(self):
        with _temp_disabled_upstream() as (uid, reason):
            with self._backend("k", 429, fallback_uid=uid) as (client, aclient):
                with self.assertRaises(self.sb.WebSearchUnavailable) as failed:
                    self.sb.search_web("site:x.com q", n=3)          # Brave answers 429
                with self.assertRaises(self.sb.WebSearchUnavailable) as cooling:
                    anyio.run(lambda: self.sb.asearch_web("site:x.com q", 3))   # Brave now cooling
                with self.assertRaises(self.sb.WebSearchUnavailable):
                    self.sb._ddg("q", 1)
                with self.assertRaises(self.sb.WebSearchUnavailable):
                    anyio.run(lambda: self.sb._addg("q", 1))
            self._reset()
            with self._backend(None, fallback_uid=uid) as (unkeyed, _):
                with self.assertRaises(self.sb.WebSearchUnavailable) as no_key:
                    self.sb.search_web("site:x.com q")
        self.assertEqual(1, len(client.urls), client.urls)          # the one Brave call, nothing else
        self.assertIn("api.search.brave.com", client.urls[0])
        self.assertEqual([], aclient.urls + unkeyed.urls)
        self.assertIn(reason, str(failed.exception))
        self.assertIn("brave failed (brave rate-limited (429))", str(failed.exception))
        self.assertIn("brave cooling", str(cooling.exception))
        self.assertIn("brave unkeyed", str(no_key.exception))
        self.assertIn("no request sent", str(no_key.exception))

    def test_the_callers_return_empty_with_the_reason_as_their_diagnostic(self):
        from omniseek.core import cache, diag, fetcher
        from omniseek.core.normalize import Document
        from omniseek.core.sources.api import nowcoder_source as nc
        from omniseek.core.sources.api import search_index_source as si
        venue = si._SearchVenue(name="t", description="", site="x.com", url_filter=r"/item/")
        ad = nc.NowcoderAdapter()
        feed = {"docs": []}
        writes = []
        with _temp_disabled_upstream() as (uid, reason), \
                self._backend(None, fallback_uid=uid) as (client, _), \
                mock.patch.object(cache, "get_docs", lambda k: None), \
                mock.patch.object(cache, "set_docs", lambda *a, **k: writes.append(a)), \
                mock.patch.object(nc.NowcoderAdapter, "_job_ids", lambda self: [645]), \
                mock.patch.object(nc.NowcoderAdapter, "_fetch_job",
                                  lambda self, job_id, order=3, pages=2: list(feed["docs"])):
            runs = {}
            for name, call in (("venue", lambda: venue.search("q")),
                               ("avenue", lambda: anyio.run(lambda: venue.asearch("q"))),
                               ("nc_feed", lambda: ad.search("", 5)),
                               ("nc_query", lambda: ad.search("微软 实习", 5))):
                diag.enable()
                runs[name] = (call(), diag.drain())
            feed["docs"] = [Document(source="nowcoder", source_id="n1", title="微软 实习 面经",
                                            content="微软 实习 面经",
                                            url="https://www.nowcoder.com/feed/main/detail/n1", metadata={})]
            diag.enable()
            runs["nc_partial"] = (ad.search("微软 实习", 5), diag.drain())
        self.assertEqual([], client.urls)
        self.assertEqual([], writes, "an empty that says why is never cached into a silent one")
        helpers = {"venue": "t.backend", "avenue": "t.backend", "nc_feed": "nowcoder.backend",
                   "nc_query": "nowcoder.backend", "nc_partial": "nowcoder.backend"}
        for name, (docs, caps) in runs.items():
            with self.subTest(run=name):
                self.assertEqual(["n1"] if name == "nc_partial" else [], [d.source_id for d in docs])
                self.assertEqual([helpers[name]], [c.get("helper") for c in caps])
                self.assertIn(reason, caps[0].get("exc") or "")
        note = fetcher._build_diagnostic(venue, docs=[], captures=runs["venue"][1], timed_out=False,
                                         raised=None, deadline_s=None)["note"]
        self.assertIn("the adapter said WHY", note)

    def test_backend_state_ping_and_the_eye_search_stamp_report_it(self):
        with _temp_disabled_upstream() as (uid, reason), \
                self._backend(None, fallback_uid=uid) as (client, _):
            st = self.sb.backend_state()
            ok, msg = self.sb.backend_ping()
            out = {}
            omniseek.server._stamp_backend_state(out, ["nowcoder"])   # named: no registry needed
        self.assertEqual(("none", False, reason), (st["active"], st["nominal"], st["ddg"]["disabled"]))
        self.assertFalse(ok)
        self.assertIn(reason, msg)
        self.assertEqual([], client.urls, "the probe spends nothing it knows cannot be served")
        self.assertEqual(reason, out["_meta"]["web_search_backend"]["ddg"]["disabled"])

    def test_with_nothing_disabled_the_duckduckgo_fallback_serves_as_before(self):
        """DuckDuckGo's /html fallback is in service (the disable was withdrawn, 2026-09-29): a failing
        Brave falls back to it, and an unkeyed Brave goes straight to it."""
        self.assertIsNone(upstreams.disabled_reason("duckduckgo_html"))
        with self._backend("k", 429) as (client, _):
            self.assertEqual([], self.sb.search_web("site:x.com q", n=3))
        self.assertEqual(2, len(client.urls), client.urls)
        self.assertIn("api.search.brave.com", client.urls[0])
        self.assertIn("html.duckduckgo.com", client.urls[1])
        self._reset()
        with self._backend(None) as (client, _):
            self.assertEqual([], self.sb.search_web("site:x.com q", n=3))
            st = self.sb.backend_state()
        self.assertEqual(1, len(client.urls))
        self.assertIn("html.duckduckgo.com", client.urls[0])
        self.assertEqual(("ddg", True, None), (st["active"], st["nominal"], st["ddg"]["disabled"]))


# ── task R2: the fixes to review X's findings (2026-09-29) ───────────────────────────────────────
def _deadline(seconds):
    """The caller's deadline (ruling 2), or nothing on code that predates it (to show the old failure)."""
    make = getattr(guard_mod, "deadline_after", None)
    return make(seconds) if make else contextlib.nullcontext()


class AsyncAdmissionTests(unittest.TestCase):
    """F1 and F4: an async wait for a permit is polled inside the coroutine, so asyncio's own cancel
    (asyncio.run tearing down an omniseek_gather child) can never strand a permit, and a waiter holds no
    worker thread."""

    def _native_cancel(self, enter) -> int:
        g = BackendGuard("t-native-cancel", 1)
        self.assertTrue(g.sema.acquire(timeout=0))           # another caller holds the only permit

        async def straggler():
            async with enter(g):
                await anyio.sleep(10)

        async def main():
            asyncio.create_task(straggler())                  # detached, like a timed-out source task
            await asyncio.sleep(0.2)                          # now parked in the permit wait
            # returning makes asyncio.run cancel the straggler natively (task.cancel)

        timer = threading.Timer(0.5, g.sema.release)          # the other caller finishes at 0.5 s
        timer.start()
        asyncio.run(main())
        timer.join()
        time.sleep(0.5)            # a worker thread left behind would have taken the permit by now
        return g.sema._value

    def test_T1_asyncio_cancel_leaves_no_permit_taken(self):
        cases = {
            "ahold": lambda g: g.ahold(3.0, lambda w: RuntimeError("busy")),
            "aslot": lambda g: g.aslot(3.0, lambda w: RuntimeError("busy")),
            "bounded_async_slot": lambda g: bounded_async_slot(g.sema, 3.0, lambda w: RuntimeError("busy")),
        }
        for name, enter in cases.items():
            with self.subTest(wait=name):
                self.assertEqual(1, self._native_cancel(enter), "1 = released, 0 = leaked")

    def test_T1_eye_gather_shape_gives_the_arxiv_permit_back(self):
        """probe_b: two pool threads, each its own asyncio.run (omniseek_gather's children). A holds the one
        arXiv permit for 1.2 s; B's deadline passes at 0.6 s while it waits, B's loop is torn down."""
        from omniseek.core.sources.api import arxiv_source
        g = arxiv_source._guard
        _reset_guard(g)

        class _AClient:
            @contextlib.asynccontextmanager
            async def stream(self, method, url, **kw):
                await anyio.sleep(1.2)
                yield httpx.Response(200, stream=httpx.ByteStream(b"<feed/>"),
                                     request=httpx.Request(method, url))

        out = {}

        def child(name, deadline):
            async def body():
                t = asyncio.create_task(arxiv_source._arxiv_aget_text(
                    arxiv_source._API, params={"search_query": name}))
                done, _ = await asyncio.wait({t}, timeout=deadline)
                out[name] = "done" if t in done else "detached"
            asyncio.run(body())
        with mock.patch.object(http, "_aget_client", lambda: _AClient()), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None):
            ta = threading.Thread(target=child, args=("A", 10.0))
            tb = threading.Thread(target=child, args=("B", 0.6))
            ta.start()
            time.sleep(0.1)
            tb.start()
            ta.join(15)
            tb.join(15)
            time.sleep(0.5)
        free = g.sema._value
        _reset_guard(g)
        self.assertEqual({"A": "done", "B": "detached"}, out)
        self.assertEqual(1, free, "the one arXiv permit must be free again")

    def test_T2_anyio_cancel_leaves_no_permit_taken(self):
        g = BackendGuard("t-anyio-cancel", 1)
        self.assertTrue(g.sema.acquire(timeout=0))

        async def main():
            with anyio.move_on_after(0.2):
                async with g.ahold(3.0, lambda w: RuntimeError("busy")):
                    await anyio.sleep(10)
        timer = threading.Timer(0.5, g.sema.release)
        timer.start()
        anyio.run(main)
        timer.join()
        time.sleep(0.5)
        self.assertEqual(1, g.sema._value)

    def test_T3_waiting_on_a_gate_holds_no_worker_thread(self):
        """probe_h: four coroutines wait on a full gate while the loop has four worker tokens; any other
        to_thread call (a DNS lookup, a sync tool body) must still run at once."""
        async def scenario():
            anyio.to_thread.current_default_thread_limiter().total_tokens = 4
            g = BackendGuard("t-threads", 1)
            self.assertTrue(g.sema.acquire(timeout=0))

            async def waiter():
                with contextlib.suppress(RuntimeError):
                    async with g.ahold(2.0, lambda w: RuntimeError("busy")):
                        pass
            async with anyio.create_task_group() as tg:
                for _ in range(4):
                    tg.start_soon(waiter)
                await anyio.sleep(0.2)
                t0 = time.monotonic()
                await anyio.to_thread.run_sync(lambda: None)
                took = time.monotonic() - t0
                g.sema.release()
            return took
        self.assertLess(anyio.run(scenario), 0.2)

    def test_T5_a_gate_wait_never_blocks_the_event_loop(self):
        async def scenario():
            g = BackendGuard("t-loop", 1)
            self.assertTrue(g.sema.acquire(timeout=0))
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    ticks += 1
                    await anyio.sleep(0.01)
            async with anyio.create_task_group() as tg:
                tg.start_soon(ticker)
                with contextlib.suppress(upstreams.UpstreamBusy):
                    with _temp_upstream("t-loop-up", "loop.test.invalid",
                                        {"max_inflight": 1, "max_wait_s": 0.5}) as up:
                        self.assertTrue(up.sema.acquire(timeout=0))
                        try:
                            async with upstreams.aegress("https://loop.test.invalid/x"):
                                pass
                        finally:
                            up.sema.release()
                tg.cancel_scope.cancel()
            g.sema.release()
            return ticks
        self.assertGreater(anyio.run(scenario), 20)

    def test_a_child_task_or_thread_is_a_new_request(self):
        """Pending 2: the held-marker names its holder; the same task or thread re-enters freely, a
        child that inherited the context takes the gate like any other request."""
        g = BackendGuard("t-holder", 1)
        waits = []

        def busy(w):
            waits.append(w)          # the wait a refusal reports: the budget it waited, sync and async
            return RuntimeError("busy")

        def try_hold():
            try:
                with g.hold(0.2, busy):
                    return "entered"
            except RuntimeError:
                return "busy"
        with g.hold(1.0, busy):
            with g.hold(1.0, busy):          # the same thread: re-entrant, no second permit
                pass
            ctx = contextvars.copy_context()
            got = []
            t = threading.Thread(target=lambda: got.append(ctx.run(try_hold)))
            t.start()
            t.join(5)
            self.assertEqual(["busy"], got)
        self.assertEqual(1, g.sema._value)

        async def scenario():
            async with g.ahold(1.0, busy):
                async with g.ahold(1.0, busy):   # the same task: re-entrant
                    pass

                async def child():
                    try:
                        async with g.ahold(0.2, busy):
                            return "entered"
                    except RuntimeError:
                        return "busy"
                return await asyncio.create_task(child())
        self.assertEqual("busy", anyio.run(scenario))
        self.assertEqual(1, g.sema._value)
        self.assertEqual(2, len(waits))
        self.assertTrue(all(0.15 < w <= 0.2 for w in waits), waits)

    def test_P2_a_hold_left_from_another_thread_or_task_leaves_no_holder_record(self):
        """Review P2: the holder's record lives in the gate, so a hold entered in one thread (task) and
        left from another (a streamed response closed elsewhere, a collector finishing it) leaves no
        record: the first thread's next request takes the gate like any other."""
        g = BackendGuard("t-p2", 1)

        def busy(w):
            return RuntimeError("busy")
        cm = g.hold(1.0, busy)
        cm.__enter__()
        t = threading.Thread(target=lambda: cm.__exit__(None, None, None))
        t.start()
        t.join(5)
        self.assertEqual((False, 1), (g.held(), g.sema._value))
        with g.hold(0.3, busy):
            inside = g.sema._value                    # 0: this hold took the permit itself
        self.assertEqual((0, 1), (inside, g.sema._value))

        async def scenario():
            acm = g.ahold(1.0, busy)
            await acm.__aenter__()

            async def leave():
                await acm.__aexit__(None, None, None)
            await asyncio.create_task(leave())
            held = g.held()
            async with g.ahold(0.3, busy):
                return held, g.sema._value
        self.assertEqual((False, 0), anyio.run(scenario))
        self.assertEqual(1, g.sema._value)


class GateCoreTests(unittest.TestCase):
    """Task R3 (driver rulings of 2026-09-29 on review N1 to N5, P1, P3, P6): one first-come-first-served
    line for sync and async callers, leased permits, one try at a host gate while holding the browser's
    turn, and the smaller fixes."""

    @staticmethod
    def _busy(w):
        return RuntimeError(f"busy {w:.2f}")

    # ── one line (N1) ─────────────────────────────────────────────────────────────────────────
    def _n1_trial(self, with_sync: bool) -> bool:
        """probe_o2 (scaled down): 1 permit, starts at least 0.2 s apart, a request takes 0.05 s; one
        sync caller sends one request after another; a coroutine asks with a 1.5 s budget."""
        g = BackendGuard("t-n1", 1, min_interval_s=0.2)
        stop = threading.Event()

        def sync_caller():
            while not stop.is_set():
                try:
                    with g.hold(5.0, self._busy):
                        time.sleep(0.05)
                except RuntimeError:
                    pass
        t = threading.Thread(target=sync_caller, daemon=True) if with_sync else None
        if t:
            t.start()
        time.sleep(0.15)
        got = {"ok": False}

        async def coroutine_caller():
            try:
                async with g.ahold(1.5, self._busy):
                    got["ok"] = True
            except RuntimeError:
                pass
        anyio.run(coroutine_caller)
        stop.set()
        if t:
            t.join(3)
        return got["ok"]

    def test_N1_a_sequential_sync_caller_cannot_keep_a_coroutine_out(self):
        wins = sum(self._n1_trial(True) for _ in range(10))
        self.assertGreaterEqual(wins, 9, f"the coroutine got the gate in {wins}/10 trials")

    def test_the_line_is_first_come_first_served_for_threads_and_coroutines(self):
        g = BackendGuard("t-fifo", 1)
        self.assertTrue(g.sema.acquire(timeout=0))              # the current holder
        order = []

        def thread_waiter(name):
            with g.hold(3.0, self._busy):
                order.append(name)
                time.sleep(0.02)

        async def main():
            async def coroutine_waiter(name):
                async with g.ahold(3.0, self._busy):
                    order.append(name)
                    await anyio.sleep(0.02)
            a = threading.Thread(target=thread_waiter, args=("thread A",))
            a.start()
            await anyio.sleep(0.1)
            b = asyncio.create_task(coroutine_waiter("coroutine B"))
            await anyio.sleep(0.1)
            c = threading.Thread(target=thread_waiter, args=("thread C",))
            c.start()
            await anyio.sleep(0.1)
            barged = g.sema.acquire(blocking=False)             # a newcomer while three wait
            g.sema.release()                                    # the holder finishes
            await b
            await anyio.to_thread.run_sync(a.join, 5)
            await anyio.to_thread.run_sync(c.join, 5)
            return barged
        barged = anyio.run(main)
        self.assertFalse(barged, "a newcomer took a permit while others were in line")
        self.assertEqual(["thread A", "coroutine B", "thread C"], order)
        self.assertEqual(1, g.sema._value)

    def test_a_coroutine_cancelled_after_the_hand_over_passes_the_permit_on(self):
        g = BackendGuard("t-pass-on", 1)
        self.assertTrue(g.sema.acquire(timeout=0))

        async def main():
            got = []

            async def waiter(name):
                async with g.ahold(3.0, self._busy):
                    got.append(name)
            w1 = asyncio.create_task(waiter("first"))
            await anyio.sleep(0.05)
            w2 = asyncio.create_task(waiter("second"))
            await anyio.sleep(0.05)
            g.sema.release()          # handed to the first in line, whose task has not run yet...
            w1.cancel()               # ...and is cancelled before it does
            await asyncio.gather(w1, w2, return_exceptions=True)
            return got
        self.assertEqual(["second"], anyio.run(main))
        self.assertEqual(1, g.sema._value)

    # ── leases (P1) ────────────────────────────────────────────────────────────────────────────
    def test_P1_a_permit_whose_lease_ran_out_is_taken_back_once(self):
        """A holder that never returns (stuck) keeps a permit only for its lease: the waiting budget
        (0.3 s) plus the request's own timeout, here unknown, so the declared max_wait (0.3 s) stands
        in. Then the gate takes it back, logs it and counts it; the late holder's give-back is a no-op,
        so the permit is not returned twice."""
        g = BackendGuard("t-lease", 1)
        entered, release = threading.Event(), threading.Event()

        def stuck_holder():
            with g.hold(0.3, self._busy):
                entered.set()
                release.wait(10)                                # stuck, far past its lease
        t = threading.Thread(target=stuck_holder, daemon=True)
        t.start()
        self.assertTrue(entered.wait(5))
        t0 = time.monotonic()
        with self.assertLogs("omniseek.core._guard", level="WARNING") as logs:
            with g.hold(2.0, self._busy):                       # the next caller
                waited = time.monotonic() - t0
                inside = g.sema._value
        release.set()                                           # the stuck holder returns at last
        t.join(5)
        self.assertTrue(0.4 < waited < 1.2, waited)
        self.assertEqual((0, 1, 1), (inside, g.sema._value, g.snapshot()["leases_reclaimed"]))
        self.assertIn("outlived its lease", "\n".join(logs.output))
        self.assertTrue(g.sema.acquire(timeout=0))
        self.assertFalse(g.sema.acquire(timeout=0), "one permit only: the late return added none")
        g.sema.release()

    def _pooled_cdp(self, stuck_until: threading.Event, loads: list):
        """A pooled CDP (OMNISEEK_CDP_POOL=1) on a fake browser: a page load waits for ``stuck_until``
        (a load that hangs), or goes at once when it is set."""
        class _Page:
            def goto(self, url, **kw):
                loads.append(url)
                stuck_until.wait(10)

            def content(self):
                return "<html></html>"

            def close(self):
                pass

        class _Ctx:
            pages: list = []

            def new_page(self):
                return _Page()

        class _Browser:
            contexts = [_Ctx()]

            def is_connected(self):
                return True

        class _PW:
            class chromium:
                @staticmethod
                def connect_over_cdp(url, timeout=None):
                    return _Browser()

            def start(self):
                return self

            def stop(self):
                pass
        return _PW

    def test_P1_a_stuck_pooled_render_gives_the_host_back_when_its_lease_runs_out(self):
        """Pooled CDP (as on mini): a render whose page load hangs holds the host's gate inside the pool
        worker. With a 0.6 s render timeout the caller gives up at 0.6 s and the pool retires the
        worker, which stays stuck; the host's gate comes back when the lease (the render's own 0.6 s,
        no waiting) runs out, instead of never."""
        from omniseek.core.sources.walled import _cdp
        host = "stuck-render.test.invalid"
        url, cdp_url = f"https://{host}/", "http://127.0.0.1:59981"
        stuck, loads = threading.Event(), []
        fake_pw = self._pooled_cdp(stuck, loads)
        turn = getattr(upstreams, "browser_turn", None)          # (the code before R3 had none)
        on_turn = (lambda: turn(url, 0.6)) if turn else (lambda: upstreams.egress(url, browser=True))
        try:
            with _temp_host_delay(host, 0.05) as hg, \
                    mock.patch.dict(os.environ, {_cdp._POOL_ENV: "1"}), \
                    mock.patch.dict(_cdp._pools, clear=True), \
                    mock.patch.object(_cdp, "ensure_browser", lambda *a, **k: None), \
                    mock.patch.object(_cdp, "_browser_instance", lambda u: "fake"), \
                    mock.patch.object(_cdp, "sync_playwright", lambda: fake_pw()):
                _reset_guard(hg)
                t0 = time.monotonic()
                with self.assertRaises(TimeoutError):
                    _cdp.cdp_call(lambda page: page.content(), initial_url=url, timeout=0.6,
                                  cdp_url=cdp_url, on_turn=on_turn)
                self.assertEqual([url], loads)
                held_after_timeout = hg.sema._value           # 0: the stuck worker still holds it
                with upstreams.egress(url, until=time.monotonic() + 3.0):
                    back_after = time.monotonic() - t0
                reclaimed = hg.snapshot()["leases_reclaimed"]
                _reset_guard(hg)
        finally:
            stuck.set()                                       # let the stuck worker go at last
        time.sleep(0.2)
        self.assertEqual(0, held_after_timeout)
        self.assertTrue(0.5 < back_after < 2.0, back_after)
        self.assertEqual(1, reclaimed)

    # ── one try at the host while holding the browser's turn (P6) ────────────────────────────────
    def test_P6_a_render_with_the_browser_turn_gives_it_up_when_the_host_is_busy(self):
        """news_scraper's render: the Chrome turn is free, the host's one permit is taken by another
        caller. The render must not wait for the host while it holds the turn: it gives the turn up at
        once (another browser call can have it right away) and nothing is loaded."""
        from omniseek.core.sources.scrape import news_scraper_source
        from omniseek.core.sources.walled import _cdp
        host = "busy-host.test.invalid"
        url = f"https://{host}/"
        with _temp_host_delay(host, 0.05) as hg, \
                mock.patch.object(_cdp, "ensure_browser", lambda *a, **k: None), \
                mock.patch.dict(os.environ, {_cdp._POOL_ENV: "0"}):
            _reset_guard(hg)
            self.assertTrue(hg.sema.acquire(timeout=0))       # another caller has the host
            chrome = _cdp._gate_for(_cdp.DEFAULT_CDP_URL)
            turn_free = []
            probe = threading.Timer(0.15, lambda: turn_free.append(chrome.acquire(timeout=0)))
            try:
                probe.start()
                t0 = time.monotonic()
                with _deadline(1.0):
                    out = news_scraper_source._render(url)
                took = time.monotonic() - t0
                probe.join()
            finally:
                if turn_free and turn_free[0]:
                    chrome.release()
                hg.sema.release()
                _reset_guard(hg)
        self.assertIsNone(out)
        self.assertLess(took, 0.15, f"held the browser turn {took:.2f}s waiting for the host")
        self.assertEqual([True], turn_free)

    # ── an outer hold of the other kind (P3) ─────────────────────────────────────────────────────
    def test_P3_a_module_client_takes_its_own_gate_beside_a_hold_of_the_other_kind(self):
        host = "p3.test.invalid"
        seen = []

        def handle(self_, request):
            seen.append(upstreams.guard("t-p3").sema._value)   # 0: the gate is held on the wire
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)

        async def async_client_beside_a_sync_hold():
            token = http._hops_var.set(upstreams.HopGates())
            try:
                async with http.AsyncHopClient(timeout=5) as c:
                    await c.get(f"https://{host}/a")
            finally:
                http._hops_var.reset(token)
        with _temp_upstream("t-p3", host, {"max_inflight": 1, "max_wait_s": 1.0}) as g, \
                mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _async_of(handle)):
            _reset_guard(g)
            token = http._hops_var.set(upstreams.AsyncHopGates())   # an async request's gates
            try:
                with http.HopClient(timeout=5) as c:
                    c.get(f"https://{host}/s")
            finally:
                http._hops_var.reset(token)
            anyio.run(async_client_beside_a_sync_hold)
            free = g.sema._value
            _reset_guard(g)
        self.assertEqual(([0, 0], 1), (seen, free))

    # ── the smaller fixes (N2 to N5) ────────────────────────────────────────────────────────────
    def test_N2_a_same_host_hop_that_cannot_connect_keeps_the_visits_start_slot(self):
        """probe_n: /start answers 301 to /start/, which cannot connect. /start reached the host, so
        the next request to it waits the Crawl-delay (0.5 s here) instead of going at once."""
        host = "n2.test.invalid"
        wire = []

        def handle(self_, request):
            if request.url.path == "/start/":
                raise httpx.ConnectError("connect failed", request=request)
            wire.append((time.monotonic(), request.url.path))
            if request.url.path == "/start":
                return httpx.Response(301, headers={"location": f"https://{host}/start/"},
                                      stream=httpx.ByteStream(b""), request=request)
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)
        saved = http._client
        http._client = None
        try:
            with _temp_host_delay(host, 0.5) as hg, \
                    mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                _reset_guard(hg)
                http._get_client()   # built beforehand: the spacing counts from the reserved start
                self.assertIsNone(http.get_text(f"https://{host}/start", retry_transient=False))
                self.assertEqual("ok", http.get_text(f"https://{host}/other"))
                _reset_guard(hg)
        finally:
            if http._client is not None:
                http._client.close()
            http._client = saved
        self.assertEqual(["/start", "/other"], [p for _, p in wire])
        self.assertGreaterEqual(wire[1][0] - wire[0][0], 0.5 - 0.02)

    def test_N2_an_outer_hold_keeps_its_slot_when_a_later_hop_is_refused(self):
        """probe_t: CORE answers 302 to export.arxiv.org, whose one permit another caller holds. The
        CORE request was sent, so CORE's window counts it."""
        from omniseek.core.sources.api import core_source
        wire = []

        def handle(self_, request):
            wire.append(request.url.host)
            if request.url.host == "api.core.ac.uk":
                return httpx.Response(302, headers={"location": "https://export.arxiv.org/api/query?x=1"},
                                      stream=httpx.ByteStream(b""), request=request)
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"), request=request)
        arxiv, core = upstreams.guard("arxiv"), core_source._core_guard
        _reset_guard(arxiv)
        _reset_guard(core)
        self.assertTrue(arxiv.sema.acquire(timeout=0))
        try:
            with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None):
                with _deadline(1.0), self.assertRaises(upstreams.UpstreamBusy):
                    core_source._core_get("https://api.core.ac.uk/v3/search/works?q=x",
                                          follow_redirects=True)
            used = core.snapshot()["windows"][0]["used"]
        finally:
            arxiv.sema.release()
            _reset_guard(arxiv)
            _reset_guard(core)
        self.assertEqual((["api.core.ac.uk"], 1), (wire, used))

    def test_N3_a_refund_never_undoes_a_retry_after(self):
        """probe_m on a fake clock: R1 reserved at 0.00 (4 permits, starts at least 1 s apart); at
        0.10 a Retry-After asks for nothing before 0.60; at 0.20 R1 is refunded; R2 at 0.25 must not
        start before 0.60."""
        clock = WindowPropertyTests._Clock()
        clock.now = 0.0
        with mock.patch.object(guard_mod, "time", clock):
            g = BackendGuard("t-n3", 4, min_interval_s=1.0)
            r1 = g.reserve()
            clock.now = 0.10
            g.defer(0.5)
            clock.now = 0.20
            g.refund(r1)
            clock.now = 0.25
            r2 = g.reserve()
        self.assertEqual(0.0, r1.start)
        self.assertGreaterEqual(r2.start, 0.60 - 1e-9)

    def test_N4_every_primitive_refuses_past_the_deadline(self):
        """probe_q: with the caller's deadline already past and permits free, nothing enters."""
        g = BackendGuard("t-n4", 2)
        sema = threading.BoundedSemaphore(2)

        def sync_case(cm_factory):
            with guard_mod.deadline_until(time.monotonic() - 1.0):
                try:
                    with cm_factory():
                        return "entered"
                except RuntimeError:
                    return "refused"

        async def async_case(cm_factory):
            with guard_mod.deadline_until(time.monotonic() - 1.0):
                try:
                    async with cm_factory():
                        return "entered"
                except RuntimeError:
                    return "refused"
        got = {
            "hold": sync_case(lambda: g.hold(5.0, self._busy, self._busy)),
            "slot": sync_case(lambda: g.slot(5.0, self._busy)),
            "bounded_slot": sync_case(lambda: guard_mod.bounded_slot(sema, 5.0, self._busy)),
            "ahold": anyio.run(async_case, lambda: g.ahold(5.0, self._busy, self._busy)),
            "aslot": anyio.run(async_case, lambda: g.aslot(5.0, self._busy)),
            "bounded_async_slot": anyio.run(async_case,
                                            lambda: guard_mod.bounded_async_slot(sema, 5.0, self._busy)),
        }
        self.assertEqual({k: "refused" for k in got}, got)

    def test_N5_a_refused_code_search_keeps_no_code_search_slot(self):
        """probe_s: the GitHub-wide gate is full; a code search under a 0.5 s deadline is turned away
        by it, and the code-search gate (starts 6 s apart) keeps no start slot for it."""
        from omniseek.core import _github
        g, code = _github._guard, _github._code_guard
        _reset_guard(g)
        _reset_guard(code)
        taken = 0
        while g.sema.acquire(timeout=0):
            taken += 1
        try:
            with mock.patch.object(_github.cache, "get", lambda *a, **k: None), _deadline(0.5):
                out = _github.get_json("/search/code", params={"q": "x"})
            backlog = code.pace_backlog_s()
        finally:
            for _ in range(taken):
                g.sema.release()
            _reset_guard(g)
            _reset_guard(code)
        self.assertIsNone(out)
        self.assertEqual(0.0, backlog, "the refused code search must not keep a code-search start slot")


class GateBudgetTests(unittest.TestCase):
    """F2 and ruling 2: the permit wait plus the start wait, over every gate one request passes, stay
    within the declared max_wait_s AND the caller's deadline; past that the request is not sent."""

    def test_T4_the_caller_deadline_bounds_the_arxiv_wait(self):
        from omniseek.core.sources.api import arxiv_source
        g = arxiv_source._guard
        _reset_guard(g)
        self.assertTrue(g.sema.acquire(timeout=0))     # another caller holds the one permit
        try:
            t0 = time.monotonic()
            with _deadline(1.5):
                r = anyio.run(lambda: arxiv_source._arxiv_aget_text(arxiv_source._API,
                                                                    params={"search_query": "x"}))
            took = time.monotonic() - t0
        finally:
            g.sema.release()
            _reset_guard(g)
        self.assertIsNone(r)
        self.assertLess(took, 2.5, f"waited {took:.1f}s past a 1.5 s deadline")

    def _paths(self):
        """(name, guard, call, declared_uid or module alias): one request through each gate path."""
        from omniseek.core import _github, _s2, _stackexchange
        from omniseek.core.sources.api import arxiv_source, core_source

        def swallow(fn):
            def run():
                with contextlib.suppress(Exception):
                    fn()
            return run

        def s2_call():
            _s2._call("t", lambda: None)

        def oa_call():
            with mock.patch.object(_openalex, "_api_key", None):
                _openalex.get_json("/works", {"search": "x"})

        def bluesky():
            with upstreams.hold("bluesky"):
                pass
        return [
            ("arxiv (sync)", arxiv_source._guard,
             lambda: arxiv_source._arxiv_get_text(arxiv_source._API, params={"q": "x"}), "arxiv"),
            ("arxiv (async)", arxiv_source._guard,
             lambda: anyio.run(lambda: arxiv_source._arxiv_aget_text(arxiv_source._API)), "arxiv"),
            ("stackexchange", _stackexchange._se_guard,
             lambda: _stackexchange._se_get("https://api.stackexchange.com/2.3/search", {}),
             "stackexchange"),
            ("core", core_source._core_guard,
             swallow(lambda: core_source._core_get("https://api.core.ac.uk/v3/search/works")), "core"),
            ("github", _github._guard, lambda: _github.get_json("/rate_limit"), "github"),
            ("s2", _s2._guard, swallow(s2_call), (_s2, "_PACE_MAX_WAIT_S")),
            ("openalex", _openalex._guard, swallow(oa_call), (_openalex, "_PACE_MAX_WAIT_S")),
            ("shared client (crossref)", upstreams.guard("crossref"),
             lambda: http.get_text("https://api.crossref.org/works"), "crossref"),
            ("upstreams.hold (bluesky)", upstreams.guard("bluesky"), swallow(bluesky), "bluesky"),
        ]

    @staticmethod
    def _declared(which, seconds):
        if isinstance(which, str):
            return mock.patch.dict(upstreams.entry(which)["gate"], {"max_wait_s": seconds})
        mod, attr = which
        return mock.patch.object(mod, attr, seconds)

    def test_every_gate_path_waits_at_most_the_declared_or_the_deadline(self):
        from omniseek.core import cache
        # the shared client is built before any gate is entered (task R3b), and building it the first
        # time takes about a second: built beforehand, since this measures waiting at gates
        http._get_client()
        wire = mock.patch.object(http._netguard, "security_block_reason", lambda url: None)
        with wire, mock.patch.object(cache, "get", lambda *a, **k: None), \
                mock.patch.object(cache, "set", lambda *a, **k: None):
            for name, g, call, declared in self._paths():
                for case in ("deadline", "declared", "start slot"):
                    with self.subTest(path=name, case=case):
                        _reset_guard(g)
                        held = 0
                        if case != "start slot":      # every permit taken by other callers
                            while g.sema.acquire(timeout=0):
                                held += 1
                        else:                         # permits free, the next start 100 s away
                            g.pace_state["next_at"] = time.monotonic() + 100
                        try:
                            t0 = time.monotonic()
                            if case == "deadline":
                                with _deadline(0.4):
                                    call()
                            elif case == "declared":
                                with self._declared(declared, 0.4):
                                    call()
                            else:
                                call()
                            took = time.monotonic() - t0
                        finally:
                            for _ in range(held):
                                g.sema.release()
                            _reset_guard(g)
                        bound = 0.3 if case == "start slot" else 0.4 + 0.35
                        self.assertLess(took, bound, f"{name}: {case}: waited {took:.2f}s")

    def test_a_request_that_cannot_start_in_time_is_turned_away_at_once(self):
        with _temp_upstream("t-late", "late.test.invalid",
                            {"max_inflight": 1, "min_interval_s": 5.0, "max_wait_s": 2.0}) as g:
            _reset_guard(g)
            with upstreams.egress("https://late.test.invalid/a"):
                pass
            t0 = time.monotonic()
            with self.assertRaises(upstreams.UpstreamBusy) as cm:
                with upstreams.egress("https://late.test.invalid/b"):
                    pass
            self.assertLess(time.monotonic() - t0, 0.1)
            self.assertIn("not sent", str(cm.exception))
            _reset_guard(g)


class RedirectRuleTests(unittest.TestCase):
    """F3, F5 and ruling 1: a redirect to the same host continues the same visit; a redirect to another
    host lets go of the first host's gates and takes its own within the time left; never two at once."""

    def setUp(self):
        for g in list(upstreams._host_guards.values()):
            _reset_guard(g)

    tearDown = setUp

    def test_T6_a_same_host_redirect_on_a_crawl_delay_host_is_one_visit(self):
        from omniseek.core import safeurl

        def respond(request):
            path = request.url.path
            if not path.endswith("/"):
                return httpx.Response(301, headers={"location": f"https://{_host_of(request)}{path}/"},
                                      stream=httpx.ByteStream(b""), request=request)
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  stream=httpx.ByteStream(b"<html><body>ok</body></html>"),
                                  request=request)
        with _sync_wire(respond) as seen, \
                mock.patch.object(safeurl, "_resolve_safe_ip",
                                  lambda host: ("93.184.216.34", socket.AF_INET, None)):
            t0 = time.monotonic()
            res = safeurl.safe_fetch("https://blog.iclr.cc/2024/05/01/a-post")   # Crawl-delay 20 s
            took = time.monotonic() - t0
        self.assertEqual((True, None), (res["ok"], res["blocked_reason"]))
        self.assertEqual(["blog.iclr.cc", "blog.iclr.cc"], [h for h, _ in seen])
        self.assertLess(took, 5.0)
        backlog = upstreams.host_guard("blog.iclr.cc").pace_backlog_s()
        self.assertTrue(18.0 < backlog <= 20.0, f"one start slot for the visit, got backlog {backlog}")

    def test_T7_a_redirect_landing_on_a_gated_host_takes_its_gate(self):
        """probe_e: example.org 302s to a host with a 0.3 s Crawl-delay, twice; then that host is read
        twice directly. Every request that reaches it is at least 0.3 s after the previous one."""
        landing = "landing.test.invalid"
        stamps = []

        def handle(self, request):
            host = _host_of(request)
            if host == "example.org":
                return httpx.Response(302, headers={"location": f"https://{landing}/y"},
                                      stream=httpx.ByteStream(b""), request=request)
            stamps.append(time.monotonic())
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)
        saved = http._client
        http._client = None
        try:
            with _temp_host_delay(landing, 0.3), \
                    mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                for url in ("https://example.org/a", "https://example.org/b",
                            f"https://{landing}/c", f"https://{landing}/d"):
                    self.assertIsNotNone(http.get_text(url))
        finally:
            if http._client is not None:
                http._client.close()
            http._client = saved
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(4, len(stamps))
        self.assertGreaterEqual(min(gaps), 0.3 - 0.02, gaps)

    def test_a_cross_host_redirect_never_holds_two_host_gates(self):
        a, b = "a-host.test.invalid", "b-host.test.invalid"
        states = []

        def handle(self, request):
            ga, gb = upstreams.host_guard(a), upstreams.host_guard(b)
            states.append((_host_of(request), ga.sema._value, gb.sema._value))
            if _host_of(request) == a:
                return httpx.Response(302, headers={"location": f"https://{b}/z"},
                                      stream=httpx.ByteStream(b""), request=request)
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)
        saved = http._client
        http._client = None
        try:
            with _temp_host_delay(a, 0.05), _temp_host_delay(b, 0.05), \
                    mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                self.assertIsNotNone(http.get_text(f"https://{a}/x"))
                hops = upstreams.HopGates()
                hops.enter(f"https://{a}/1")
                held_a = (upstreams.host_guard(a).sema._value, upstreams.host_guard(b).sema._value)
                time.sleep(0.06)
                hops.enter(f"https://{b}/2")
                held_b = (upstreams.host_guard(a).sema._value, upstreams.host_guard(b).sema._value)
                hops.close()
        finally:
            if http._client is not None:
                http._client.close()
            http._client = saved
        # (host on the wire, free permits of a's gate, free permits of b's gate)
        self.assertEqual([(a, 0, 1), (b, 1, 0)], states)
        self.assertEqual(((0, 1), (1, 0)), (held_a, held_b))

    def _chain(self, make_gates, first_wait, network, second_wait, deadline=None):
        """Two gated hosts, max_wait_s 0.5 each. The first hop waits ``first_wait`` at its gate (its
        next start is that far away), then ``network`` seconds pass with no waiting (the first
        response), then the second hop has to wait ``second_wait`` at its gate. Returns (admitted,
        seconds the second hop took or the refusal)."""
        a, b = "chain-a.test.invalid", "chain-b.test.invalid"
        with _temp_upstream("t-chain-a", a, {"max_inflight": 1, "max_wait_s": 0.5}) as ga, \
                _temp_upstream("t-chain-b", b, {"max_inflight": 1, "max_wait_s": 0.5}) as gb:
            _reset_guard(ga)
            _reset_guard(gb)
            try:
                with _deadline(deadline) if deadline is not None else contextlib.nullcontext():
                    return make_gates(a, b, ga, gb, first_wait, network, second_wait)
            finally:
                _reset_guard(ga)
                _reset_guard(gb)

    @staticmethod
    def _sync_chain(a, b, ga, gb, first_wait, network, second_wait):
        hops = upstreams.HopGates()
        try:
            if first_wait:
                ga.defer(first_wait)
            hops.enter(f"https://{a}/1")
            time.sleep(network)
            if second_wait:
                gb.defer(second_wait)
            t0 = time.monotonic()
            try:
                hops.enter(f"https://{b}/2")
                return True, time.monotonic() - t0
            except upstreams.UpstreamBusy as exc:
                return False, str(exc)
        finally:
            hops.close()

    @staticmethod
    def _async_chain(a, b, ga, gb, first_wait, network, second_wait):
        async def run():
            hops = upstreams.AsyncHopGates()
            try:
                if first_wait:
                    ga.defer(first_wait)
                await hops.enter(f"https://{a}/1")
                await anyio.sleep(network)
                if second_wait:
                    gb.defer(second_wait)
                t0 = time.monotonic()
                try:
                    await hops.enter(f"https://{b}/2")
                    return True, time.monotonic() - t0
                except upstreams.UpstreamBusy as exc:
                    return False, str(exc)
            finally:
                await hops.close()
        return anyio.run(run)

    def test_a_redirect_chain_has_one_budget(self):
        """Driver rulings of 2026-09-29 (section 16.5 item 1, and the objection in 16.8). Without a
        caller's deadline the chain's budget is the first gated hop's max_wait_s (0.5 s here) and only
        waiting at gates uses it up: after the first hop waited 0.3 s, a second hop that must wait
        0.4 s is refused (its own 0.5 s would allow it), one that must wait 0.1 s goes after about
        0.1 s. With a deadline the deadline is the chain's absolute end instead: the same 0.4 s wait
        is admitted under a 2 s deadline, a 0.6 s wait is still refused (each gate waits at most its
        own max_wait_s), and a 0.5 s deadline that passes during the first response refuses the
        second hop as past this call's budget."""
        for name, run in (("sync", self._sync_chain), ("async", self._async_chain)):
            with self.subTest(client=name):
                ok, why = self._chain(run, 0.3, 0.0, 0.4)
                self.assertFalse(ok, "waited 0.3 of 0.5: a 0.4 s wait is past the chain's budget")
                ok, took = self._chain(run, 0.3, 0.0, 0.1)
                self.assertTrue(ok and 0.05 < took < 0.3, took)
                ok, took = self._chain(run, 0.3, 0.0, 0.4, deadline=2.0)
                self.assertTrue(ok and 0.3 < took < 0.6, took)
                ok, why = self._chain(run, 0.0, 0.0, 0.6, deadline=2.0)
                self.assertFalse(ok, "a gate never waits past its own max_wait_s")
                ok, why = self._chain(run, 0.3, 0.3, 0.0, deadline=0.5)
                self.assertFalse(ok)
                self.assertIn("past this call's budget", why)
                self.assertNotIn("saturated", why)

    def test_a_slow_first_response_leaves_the_budget_to_later_hops(self):
        """repro_chain_end of the objection in 16.8: without a deadline, the first hop's network time
        is not waiting. After 0.6 s of it (more than the 0.5 s budget), a second hop to a free gate
        goes at once, and one that must wait 0.4 s still has the whole budget for it."""
        for name, run in (("sync", self._sync_chain), ("async", self._async_chain)):
            with self.subTest(client=name):
                ok, took = self._chain(run, 0.0, 0.6, 0.0)
                self.assertTrue(ok and took < 0.1, took)
                ok, took = self._chain(run, 0.0, 0.6, 0.4)
                self.assertTrue(ok and 0.3 < took < 0.6, took)

    def test_a_chain_through_the_shared_client_keeps_one_waiting_budget(self):
        """The same through a real read, a -> 302 -> b on the shared client. When a's gate made the
        read wait 0.3 s and b's would make it wait 0.4 s more, nothing is sent to b and the read
        returns None. When a instead takes 0.6 s to answer (no waiting) and b's gate is free, b is
        read."""
        a, b = "chain-a.test.invalid", "chain-b.test.invalid"
        seen, slow = [], {"a": 0.0}

        def handle(self_, request):
            seen.append(_host_of(request))
            if _host_of(request) == a:
                time.sleep(slow["a"])
                return httpx.Response(302, headers={"location": f"https://{b}/z"},
                                      stream=httpx.ByteStream(b""), request=request)
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)
        saved = http._client
        http._client = None
        out = {}
        try:
            with _temp_upstream("t-chain-a", a, {"max_inflight": 1, "max_wait_s": 0.5}) as ga, \
                    _temp_upstream("t-chain-b", b, {"max_inflight": 1, "max_wait_s": 0.5}) as gb, \
                    mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                http._get_client()                  # built beforehand: this test is about waiting
                for case, a_wait, a_net, b_start in (("waiting", 0.3, 0.0, 0.7),
                                                      ("slow answer", 0.0, 0.6, 0.0)):
                    _reset_guard(ga)
                    _reset_guard(gb)
                    seen.clear()
                    slow["a"] = a_net
                    if a_wait:
                        ga.defer(a_wait)
                    if b_start:
                        gb.defer(b_start)           # b's next start: 0.4 s after the hop to b begins
                    out[case] = (http.get_text(f"https://{a}/x"), list(seen),
                                 (ga.sema._value, gb.sema._value))
                _reset_guard(ga)
                _reset_guard(gb)
        finally:
            if http._client is not None:
                http._client.close()
            http._client = saved
        self.assertEqual((None, [a], (1, 1)), out["waiting"])
        self.assertEqual(("ok", [a, b], (1, 1)), out["slow answer"])

    def test_a_budget_used_up_before_the_gate_is_tried_says_so(self):
        """Driver ruling of 2026-09-29: when the budget is gone before a gate is even tried, the
        refusal says past this call's budget, not that the gate is saturated: whether the gate is
        free or another caller holds it, sync and async."""
        with _temp_upstream("t-spent", "spent.test.invalid", {"max_inflight": 1, "max_wait_s": 2.0}) as g:
            _reset_guard(g)
            url = "https://spent.test.invalid/x"
            msgs = []

            async def aegress_late():
                async with upstreams.aegress(url, until=time.monotonic() - 0.01):
                    pass
            cases = (("sync, until passed", lambda: _egress_until(url, time.monotonic() - 0.01)),
                     ("async, until passed", lambda: anyio.run(aegress_late)),
                     ("the caller's deadline passed", lambda: _passed_deadline_egress(url)))
            for held in (False, True):
                if held:
                    self.assertTrue(g.sema.acquire(timeout=0))     # another caller holds the gate
                try:
                    for how, call in cases:
                        with self.subTest(how=how, gate_held=held):
                            with self.assertRaises(upstreams.UpstreamBusy) as cm:
                                call()
                            msgs.append(str(cm.exception))
                finally:
                    if held:
                        g.sema.release()
            free = g.sema._value
            _reset_guard(g)
        self.assertEqual((1, 6), (free, len(msgs)))
        for m in msgs:
            self.assertIn("past this call's budget", m)
            self.assertNotIn("saturated", m)


def _egress_until(url, until):
    with upstreams.egress(url, until=until):
        pass


def _passed_deadline_egress(url):
    with guard_mod.deadline_until(time.monotonic() - 0.01):
        with upstreams.egress(url):
            pass


class ModuleClientRedirectTests(unittest.TestCase):
    """Ruling 1 for the modules that keep their own httpx: http.direct, HopClient / AsyncHopClient and
    direct_stream follow the one redirect rule, and no module follows redirects with a raw httpx call
    or client. Egress whose redirects another library follows is listed with its reason."""

    def setUp(self):
        for g in list(upstreams._host_guards.values()):
            _reset_guard(g)

    tearDown = setUp

    @staticmethod
    def _via(landing, stamps, held):
        """example.org 302s to ``landing``; each request that reaches ``landing`` records its time and
        whether ``landing``'s gate was held at that moment."""
        def handle(self_, request):
            if _host_of(request) == "example.org":
                return httpx.Response(302, headers={"location": f"https://{landing}/y"},
                                      stream=httpx.ByteStream(b""), request=request)
            held.append(upstreams.host_guard(landing).sema._value == 0)
            stamps.append(time.monotonic())
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)
        return handle

    def test_a_module_client_and_direct_take_the_landing_hosts_gate(self):
        landing, stamps, held = "landing-sync.test.invalid", [], []
        with _temp_host_delay(landing, 0.3) as g, \
                mock.patch.object(httpx.HTTPTransport, "handle_request", self._via(landing, stamps, held)):
            with http.HopClient(follow_redirects=True, timeout=5) as c:
                for _ in range(2):
                    self.assertEqual(200, c.get("https://example.org/a").status_code)
            for _ in range(2):
                self.assertEqual(200, http.direct("GET", "https://example.org/b",
                                                  follow_redirects=True).status_code)
            free = g.sema._value
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(([True] * 4, 1), (held, free))
        self.assertGreaterEqual(min(gaps), 0.3 - 0.02, gaps)

    def test_the_async_module_client_takes_it_too(self):
        landing, stamps, held = "landing-async.test.invalid", [], []

        async def run():
            async with http.AsyncHopClient(follow_redirects=True, timeout=5) as c:
                for _ in range(2):
                    self.assertEqual(200, (await c.get("https://example.org/a")).status_code)
            self.assertEqual(200, (await http.adirect("GET", "https://example.org/b",
                                                      follow_redirects=True)).status_code)
        with _temp_host_delay(landing, 0.3) as g, \
                mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                  _async_of(self._via(landing, stamps, held))):
            anyio.run(run)
            free = g.sema._value
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(([True] * 3, 1), (held, free))
        self.assertGreaterEqual(min(gaps), 0.3 - 0.02, gaps)

    def test_a_streamed_body_keeps_its_gate_until_it_is_closed(self):
        host = "stream.test.invalid"

        def handle(self_, request):
            return httpx.Response(200, stream=httpx.ByteStream(b"x" * 10), request=request)

        async def astream(g):
            async with http.AsyncHopClient(timeout=5) as c:
                async with c.stream("GET", f"https://{host}/f") as resp:
                    during = g.sema._value
                    body = b"".join([chunk async for chunk in resp.aiter_bytes()])
            return during, body
        with _temp_host_delay(host, 0.01) as g, \
                mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _async_of(handle)):
            other = []

            def another_caller():                                 # one at a time: it cannot get in
                try:
                    with upstreams.egress(f"https://{host}/g", until=time.monotonic() + 0.1):
                        other.append("sent")
                except upstreams.UpstreamBusy:
                    other.append("not sent")
            with http.direct_stream("GET", f"https://{host}/f", follow_redirects=True, timeout=5) as resp:
                during = g.sema._value
                t = threading.Thread(target=another_caller)
                t.start()
                t.join(5)
                body = b"".join(resp.iter_bytes())
            after = g.sema._value
            a_during, a_body = anyio.run(astream, g)
            a_after = g.sema._value
        self.assertEqual((0, ["not sent"], 1, b"x" * 10), (during, other, after, body))
        self.assertEqual((0, 1, b"x" * 10), (a_during, a_after, a_body))

    def test_a_module_client_inside_direct_adds_no_second_gate(self):
        """Inside http.direct the client joins the request's own gates: one permit, and across a
        cross-host redirect the first host's gate is let go before the second host's is taken."""
        a, b = "a-nest.test.invalid", "b-nest.test.invalid"
        with _temp_upstream("t-nest", "nest.test.invalid", {"max_inflight": 2, "max_wait_s": 2.0}) as g, \
                _temp_host_delay(a, 0.05) as ga, _temp_host_delay(b, 0.05) as gb:
            _reset_guard(g)
            seen = []

            def handle(self_, request):
                host = _host_of(request)
                seen.append((host, g.sema._value, ga.sema._value, gb.sema._value))
                if host == a:
                    return httpx.Response(302, headers={"location": f"https://{b}/z"},
                                          stream=httpx.ByteStream(b""), request=request)
                return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)
            with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    http.HopClient(timeout=5, follow_redirects=True) as c:
                http.direct("GET", "https://nest.test.invalid/a", client=c)
                c.get("https://nest.test.invalid/b")
                http.direct("GET", f"https://{a}/x", client=c, follow_redirects=True)
                time.sleep(0.06)
                c.get(f"https://{a}/y")
            free = (g.sema._value, ga.sema._value, gb.sema._value)
            _reset_guard(g)
        # (host on the wire, free permits of t-nest, of a's gate, of b's gate)
        self.assertEqual([("nest.test.invalid", 1, 1, 1), ("nest.test.invalid", 1, 1, 1),
                          (a, 2, 0, 1), (b, 2, 1, 0), (a, 2, 0, 1), (b, 2, 1, 0)], seen)
        self.assertEqual((2, 1, 1), free)

    def test_no_module_follows_redirects_outside_the_rule(self):
        """Static: no module follows redirects with a raw httpx call or client. Every such egress
        goes through http.direct / adirect / direct_stream, a HopClient / AsyncHopClient, a client
        built with http.hop_hooks / ahop_hooks (used through http.direct), the shared clients or
        safe_fetch. A raw client in a module that follows redirects anywhere is flagged too, since
        a per-call follow_redirects=True on it would pass the rule by."""
        import ast
        raw = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream",
               "Client", "AsyncClient"}

        def hooked(call):
            return any(k.arg == "event_hooks" and isinstance(k.value, ast.Call)
                       and isinstance(k.value.func, ast.Attribute)
                       and k.value.func.attr in ("hop_hooks", "ahop_hooks") for k in call.keywords)
        offenders = []
        for path in sorted(EYE.rglob("*.py")):
            rel = path.relative_to(EYE).as_posix()
            if rel == "http.py":
                continue
            follows, clients = False, []
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                is_raw = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                          and f.value.id == "httpx" and f.attr in raw)
                follow = any(k.arg == "follow_redirects" and isinstance(k.value, ast.Constant)
                             and k.value.value is True for k in node.keywords)
                follows = follows or follow
                if is_raw and follow:
                    offenders.append(f"{rel}:{node.lineno} httpx.{f.attr}(follow_redirects=True)")
                elif is_raw and f.attr in ("Client", "AsyncClient") and not hooked(node):
                    clients.append(f"{rel}:{node.lineno} raw httpx.{f.attr} in a module that follows")
            if follows:
                offenders += clients
        self.assertEqual([], offenders)

    # Egress whose redirects are followed inside another library, so the rule cannot be applied hop by
    # hop from OmniSeek: listed with the reason, so that a new one is a decision, not an accident.
    OTHER_LIBRARIES = {
        "sources/scrape/cninfo_source.py": "curl_cffi TLS impersonation (anti-bot); host ungated",
        "sources/scrape/eastmoney_source.py": "curl_cffi session, TLS impersonation; hosts ungated",
        "sources/scrape/gov_policy_source.py": "curl_cffi TLS impersonation (anti-bot); host ungated",
        "sources/scrape/juejin_source.py": "curl_cffi TLS impersonation (anti-bot); host ungated",
        "sources/scrape/sogou_weixin_source.py": "curl_cffi session, TLS impersonation; host ungated",
        "sources/walled/xiaohongshu_cn_source.py": "curl_cffi with per-request signed headers; ungated",
        "infra_jobs.py": "urllib, the wechat2rss feed liveness probe (reads a bounded prefix); ungated",
    }

    def test_egress_through_other_http_libraries_is_listed(self):
        found = set()
        lib = re.compile(r"^\s*(?:from curl_cffi import|import curl_cffi|import urllib\.request"
                         r"|from urllib\.request import|from urllib import request)", re.M)
        for path in EYE.rglob("*.py"):
            rel = path.relative_to(EYE).as_posix()
            if rel != "http.py" and lib.search(path.read_text(encoding="utf-8")):
                found.add(rel)
        self.assertEqual(sorted(self.OTHER_LIBRARIES), sorted(found),
                         "a module sending through curl_cffi or urllib itself needs a row here with "
                         "its reason")

    def _premise_problems(self) -> list:
        """Hosts in the listed modules' source that are declared with a gate or a Crawl-delay: the
        hosts of every URL written there, and every declared gated / Crawl-delay host (or domain
        suffix) that appears anywhere in the text (a bare host in a header, a host joined into a URL
        at run time)."""
        decl = upstreams.declarations()
        watched = {h for e in decl.values() if e.get("gate") for h in e.get("hosts") or ()}
        watched |= set(upstreams._host_delay)
        problems = []
        for rel in sorted(self.OTHER_LIBRARIES):
            text = (EYE / rel).read_text(encoding="utf-8").lower()
            hosts = set(re.findall(r"https?://([a-z0-9.-]+)", text))
            for h in watched:
                name = h.lstrip(".")
                before = r"(?<![a-z0-9-])" if h.startswith(".") else r"(?<![a-z0-9.-])"
                if re.search(before + re.escape(name) + r"(?![a-z0-9-]|\.[a-z0-9])", text):
                    hosts.add(name)
            for h in sorted(hosts):
                uid = upstreams.uid_for_host(h)
                gated = uid is not None and bool((decl.get(uid) or {}).get("gate"))
                if gated or upstreams.crawl_delay_for(h):
                    problems.append(f"{rel}: {h} is declared in upstreams.json with a gate or a "
                                    "Crawl-delay: 该把这个模块接上跳转规则了")
        return problems

    def test_the_modules_off_the_rule_still_reach_no_gated_host(self):
        """Driver ruling of 2026-09-29 (section 16.5, item 7): the modules above stay off the redirect
        rule only while no host they reach is declared with a gate or a robots.txt Crawl-delay. The
        day one is, this fails and says so. Its own reverse control: a gate declared for a host one of
        them uses is caught."""
        self.assertEqual([], self._premise_problems())
        with _temp_upstream("t-premise", "api.juejin.cn", {"max_inflight": 1, "max_wait_s": 1.0}):
            caught = self._premise_problems()
        self.assertTrue(any("juejin_source.py: api.juejin.cn" in p and "该把这个模块接上跳转规则了" in p
                            for p in caught), caught)


class MCPTransportGateTests(unittest.TestCase):
    """Driver ruling of 2026-09-29: the MCP transport (sources/_mcp.py) takes the declared gates like
    every other egress, although no MCP row exists today; and no module sends through the shared
    clients itself without doing the same."""

    def test_an_mcp_request_holds_the_declared_gate_while_on_the_wire(self):
        """The endpoint's gate is held while the request is on the wire; a saturated gate sends
        nothing; and an endpoint that redirects (307, the POST is kept) to another gated host takes
        that host's gate too, after letting go of the first (the one redirect rule)."""
        from omniseek.core.sources import _mcp
        host, moved = "mcp.test.invalid", "mcp-moved.test.invalid"
        seen = []
        with _temp_upstream("t-mcp", host, {"max_inflight": 1, "max_wait_s": 0.3}) as g, \
                _temp_upstream("t-mcp-moved", moved, {"max_inflight": 1, "max_wait_s": 0.3}) as g2:
            _reset_guard(g)
            _reset_guard(g2)

            def handle(self_, request):
                seen.append((_host_of(request), g.sema._value, g2.sema._value))   # free permits
                if request.url.path == "/old":
                    return httpx.Response(307, headers={"location": f"https://{moved}/mcp"},
                                          stream=httpx.ByteStream(b""), request=request)
                return httpx.Response(200, headers={"content-type": "application/json"},
                                      stream=httpx.ByteStream(b'{"jsonrpc": "2.0", "id": 1, "result": {}}'),
                                      request=request)
            saved = http._client
            http._client = None
            try:
                with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                        mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                        mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                    client = _mcp.MCPClient(f"https://{host}/mcp", timeout_s=5)
                    parsed, _ = client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    free_after = (g.sema._value, g2.sema._value)
                    self.assertTrue(g.sema.acquire(timeout=0))   # another caller has the one permit
                    try:
                        t0 = time.monotonic()
                        with self.assertRaises(_mcp.MCPTransportError) as cm:
                            client._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                        took = time.monotonic() - t0
                    finally:
                        g.sema.release()
                    _reset_guard(g)
                    redirected, _ = _mcp.MCPClient(f"https://{host}/old", timeout_s=5)._post(
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    free_end = (g.sema._value, g2.sema._value)
            finally:
                if http._client is not None:
                    http._client.close()
                http._client = saved
            _reset_guard(g)
            _reset_guard(g2)
        self.assertEqual({}, parsed.get("result"))
        self.assertEqual({}, redirected.get("result"))
        # (host on the wire, free permits of the endpoint's gate, of the moved host's gate)
        self.assertEqual([(host, 0, 1), (host, 0, 1), (moved, 1, 0)], seen)
        self.assertEqual(((1, 1), (1, 1)), (free_after, free_end))
        self.assertIn("declared upstream gate, request not sent", str(cm.exception))
        self.assertLess(took, 1.0)

    def test_direct_users_of_the_shared_clients_take_the_gates(self):
        """A module that sends through the shared clients itself (not through http's helpers) must do
        what http._request_capped does: hold upstreams.hop_gates() (or ahop_gates) and set
        http._hops_var, or the clients' hooks take no gate at all. Today that is the MCP transport
        only; a new one fails here until it is checked."""
        users = {}
        direct = re.compile(r"\bhttp\._a?get_client\(\)|from omniseek\.eye\.http import[^\n]*\b_a?get_client\b")
        for path in EYE.rglob("*.py"):
            rel = path.relative_to(EYE).as_posix()
            if rel == "http.py":
                continue
            text = path.read_text(encoding="utf-8")
            if direct.search(text):
                users[rel] = (bool(re.search(r"upstreams\.a?hop_gates\(", text))
                              and "http._hops_var.set(" in text)
        self.assertEqual({"sources/_mcp.py": True}, users)


class ResponseRecordingTests(unittest.TestCase):
    """Driver ruling of 2026-09-29 (section 16.5, item 2): every response is recorded once, on its own
    host, whichever client or layer made the request."""

    A, B = "rec-a.test.invalid", "rec-b.test.invalid"

    def _handle(self, self_, request):
        if _host_of(request) == self.A:
            return httpx.Response(302, headers={"location": f"https://{self.B}/z",
                                                "X-RateLimit-Remaining": "3"},
                                  stream=httpx.ByteStream(b""), request=request)
        return httpx.Response(200, headers={"X-RateLimit-Remaining": "7"},
                              stream=httpx.ByteStream(b"ok"), request=request)

    def _paths(self):
        from omniseek.core import safeurl
        url = f"https://{self.A}/x"

        async def shared_async():
            try:
                return await http.aget_text(url)
            finally:
                await http.aclose_client()

        async def async_client():
            async with http.AsyncHopClient(follow_redirects=True, timeout=5) as c:
                return (await c.get(url)).text

        def walker():
            with httpx.Client(follow_redirects=False, timeout=5) as c:
                r = safeurl.walk_redirects_revalidated(c, "GET", url)
                r.read()
                r.close()

        class _CurlResp:
            def __init__(self, status, headers):
                self.status_code, self.headers = status, headers

            def close(self):
                pass

        class _Curl:
            def request(self_, method, u, **kw):
                if urlsplit(u).hostname == self.A:
                    return _CurlResp(302, {"location": f"https://{self.B}/z", "x-ratelimit-remaining": "3"})
                return _CurlResp(200, {"x-ratelimit-remaining": "7"})

        def curl():
            with upstreams.hop_gates() as gates:
                http._curl_hops(_Curl(), "GET", url, gates, headers=None)

        def hop_client():
            with http.HopClient(follow_redirects=True, timeout=5) as c:
                return c.get(url).text
        return [
            ("shared client", lambda: http.get_text(url), (1, 1)),
            ("shared async client", lambda: anyio.run(shared_async), (1, 1)),
            ("http.direct", lambda: http.direct("GET", url, follow_redirects=True), (1, 1)),
            ("http.adirect", lambda: anyio.run(lambda: http.adirect("GET", url, follow_redirects=True)),
             (1, 1)),
            ("HopClient", hop_client, (1, 1)),
            ("AsyncHopClient", lambda: anyio.run(async_client), (1, 1)),
            ("safe_fetch", lambda: safeurl.safe_fetch(url), (1, 1)),
            ("redirect walker (docreader)", walker, (1, 1)),
            ("curl tier", curl, (1, 1)),
            ("http.direct, not following", lambda: http.direct("GET", url), (1, 0)),
        ]

    def test_every_response_is_recorded_once_on_its_own_host(self):
        from omniseek.core import safeurl
        saved = (http._client, http._aclient, http._aclient_loop)
        http._client = http._aclient = None
        try:
            with _temp_upstream("t-rec-a", self.A, {"max_inflight": 2, "max_wait_s": 2.0}) as ga, \
                    _temp_upstream("t-rec-b", self.B, {"max_inflight": 2, "max_wait_s": 2.0}) as gb, \
                    mock.patch.object(httpx.HTTPTransport, "handle_request",
                                      lambda s, r: self._handle(s, r)), \
                    mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                      _async_of(lambda s, r: self._handle(s, r))), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                    mock.patch.object(safeurl, "_resolve_safe_ip",
                                      lambda host: ("93.184.216.34", socket.AF_INET, None)):
                for name, call, want in self._paths():
                    with self.subTest(path=name):
                        for uid, g in (("t-rec-a", ga), ("t-rec-b", gb)):
                            upstreams._readings.pop(uid, None)
                            _reset_guard(g)
                        call()
                        got = tuple(upstreams.readings(uid).get("n", 0) for uid in ("t-rec-a", "t-rec-b"))
                        self.assertEqual(want, got, f"{name}: (responses recorded for a, for b)")
                        lanes = upstreams.readings("t-rec-b").get("lanes", {})
                        if want[1]:
                            self.assertEqual("7", lanes["_"]["fields"]["x-ratelimit-remaining"])
        finally:
            if http._client is not None:
                http._client.close()
            http._client, http._aclient, http._aclient_loop = saved


class DeferralTests(unittest.TestCase):
    """F6, F7 and ruling 3: a response carrying Retry-After defers the whole upstream on every path;
    the header is read in both of its forms; only listed modules switch that off."""

    def _429(self, seconds="30", status=429):
        def handle(self_, request):
            return httpx.Response(status, headers={"Retry-After": seconds},
                                  stream=httpx.ByteStream(b"{}"), request=request)
        return handle

    def test_T8_retry_after_defers_the_whole_gate_on_every_path(self):
        from omniseek.core import cache
        from omniseek.core.sources.api import core_source, llm_leaderboard_source
        from omniseek.core.sources.scrape import v2ex_source
        from omniseek.core.sources.walled import discord_communities_source as disc

        def guild_then_429(self_, request):
            if request.url.path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "1", "name": "g"}], request=request)
            return httpx.Response(429, headers={"Retry-After": "30"},
                                  stream=httpx.ByteStream(b"{}"), request=request)

        def leaderboard():
            with mock.patch.object(llm_leaderboard_source.auth, "load", lambda name: {"api_key": "k"}):
                llm_leaderboard_source.LLMLeaderboardAdapter()._models()

        def discord_one():
            # _one (the per-guild channel list) runs inside _discover_channels
            disc.DiscordCommunitiesAdapter()._discover_channels("t")

        cases = [
            ("v2ex", lambda: v2ex_source.V2exAdapter._get_topics("python"), self._429()),
            ("core", lambda: core_source._core_get("https://api.core.ac.uk/v3/search/works"),
             self._429()),
            ("core", lambda: anyio.run(lambda: core_source._acore_get(
                "https://api.core.ac.uk/v3/search/works")), self._429()),
            ("artificial_analysis", leaderboard, self._429()),
            ("discord", lambda: disc.DiscordCommunitiesAdapter()._discover_channels("t"), self._429()),
            ("discord", discord_one, guild_then_429),
            ("github", lambda: http.post_json("https://api.github.com/graphql", json={"q": 1}),
             self._429(status=403)),
            ("crossref", lambda: http.get_text("https://api.crossref.org/works"),
             self._429(status=503)),
        ]
        saved = (http._client, getattr(core_source, "_acore_client_obj", None))
        for uid, call, handle in cases:
            with self.subTest(upstream=uid):
                g = upstreams.guard(uid)
                _reset_guard(g)
                http._client = None
                core_source._acore_client_obj = None
                with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                          _async_of(handle)), \
                        mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                        mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                        mock.patch.object(cache, "get", lambda *a, **k: None), \
                        mock.patch.object(cache, "set", lambda *a, **k: None):
                    with contextlib.suppress(Exception):
                        call()
                backlog = g.pace_backlog_s()
                _reset_guard(g)
                self.assertGreater(backlog, 25, f"{uid}: Retry-After 30 left a backlog of {backlog:.1f}s")
        if http._client is not None:
            http._client.close()
        http._client, core_source._acore_client_obj = saved

    def test_only_listed_modules_switch_the_deferral_off(self):
        """Any call that passes defer_on_429=False (to observe, observe_response, a HopClient, the
        hop hooks, ...) must sit in a module listed in upstreams.SELF_BACKOFF with its reason."""
        import ast
        import inspect
        for fn in (upstreams.observe, upstreams.observe_response, http.hop_hooks, http.ahop_hooks,
                   http.HopClient.__init__, http.AsyncHopClient.__init__):
            self.assertIs(True, inspect.signature(fn).parameters["defer_on_429"].default, fn)
        offenders = []
        for path in EYE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            off = any(isinstance(n, ast.keyword) and n.arg == "defer_on_429"
                      and isinstance(n.value, ast.Constant) and n.value.value is False
                      for n in ast.walk(tree))
            if not off:
                continue
            mod = "omniseek.core." + ".".join(path.relative_to(EYE).with_suffix("").parts)
            if mod not in upstreams.SELF_BACKOFF:
                offenders.append(mod)
        self.assertEqual([], offenders, "a module that switches the deferral off must be in "
                                        "upstreams.SELF_BACKOFF with its reason")
        self.assertTrue(all(isinstance(r, str) and len(r) > 40 for r in upstreams.SELF_BACKOFF.values()))

    def test_retry_after_also_defers_the_hosts_crawl_delay_gate(self):
        """Driver ruling of 2026-09-29 (section 16.5, item 5): a response carrying Retry-After also
        defers the robots.txt Crawl-delay gate of the host it came from. A host with that gate and no
        upstream gate used to defer nothing."""
        from omniseek.core import safeurl
        only, both, pinned, byid = ("only.test.invalid", "both.test.invalid", "pinned.test.invalid",
                                    "byid.test.invalid")
        when = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60),
                                           usegmt=True)

        def handle(self_, request):
            ra = when if _host_of(request) == pinned else "30"
            return httpx.Response(429, headers={"Retry-After": ra}, stream=httpx.ByteStream(b"{}"),
                                  request=request)
        saved = http._client
        http._client = None
        try:
            with _temp_host_delay(only, 0.05) as g_only, _temp_host_delay(pinned, 0.05) as g_pinned, \
                    _temp_host_delay(byid, 0.05) as g_byid, \
                    _temp_upstream("t-both", both, {"max_inflight": 1, "max_wait_s": 2.0}) as g_up, \
                    _temp_host_delay(both, 0.05) as g_both, \
                    mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                    mock.patch.object(safeurl, "_resolve_safe_ip",
                                      lambda host: ("93.184.216.34", socket.AF_INET, None)):
                for g in (g_only, g_pinned, g_byid, g_up, g_both):
                    _reset_guard(g)
                self.assertIsNone(http.get_text(f"https://{only}/x"))          # the shared client
                res = safeurl.safe_fetch(f"https://{pinned}/x")                # omniseek_read's read
                self.assertIsNone(http.get_text(f"https://{both}/x"))
                upstreams.observe_response("t-both", httpx.Response(               # an upstream id:
                    429, headers={"Retry-After": "30"},                             # the response's
                    request=httpx.Request("GET", f"https://{byid}/y")))            # own host counts
                backlog = {k: g.pace_backlog_s() for k, g in (("only", g_only), ("pinned", g_pinned),
                                                              ("byid", g_byid), ("upstream", g_up),
                                                              ("host", g_both))}
                for g in (g_only, g_pinned, g_byid, g_up, g_both):
                    _reset_guard(g)
        finally:
            if http._client is not None:
                http._client.close()
            http._client = saved
        self.assertEqual((True, 429), (res["ok"], res["status"]))
        self.assertTrue(25 < backlog["only"] <= 30.5, backlog)
        self.assertTrue(55 < backlog["pinned"] <= 61, backlog)                   # the HTTP-date form
        self.assertTrue(25 < backlog["upstream"] <= 30.5 and 25 < backlog["host"] <= 30.5, backlog)
        self.assertTrue(25 < backlog["byid"] <= 30.5, backlog)

    def test_an_exempt_module_still_defers_the_hosts_crawl_delay_gate(self):
        """Driver ruling of 2026-09-29: defer_on_429=False (a module in SELF_BACKOFF) exempts the
        upstream gate only; the host's Crawl-delay gate is deferred regardless, whether the response
        is recorded by hand or by a HopClient built with defer_on_429=False (the search backend's
        shape)."""
        host = "exempt.test.invalid"

        def handle(self_, request):
            return httpx.Response(429, headers={"Retry-After": "30"}, stream=httpx.ByteStream(b"{}"),
                                  request=request)
        with _temp_upstream("t-exempt", host, {"max_inflight": 2, "max_wait_s": 2.0}) as g_up, \
                _temp_host_delay(host, 0.05) as g_host, \
                mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None):
            got = []
            for how in ("by hand", "HopClient"):
                _reset_guard(g_up)
                _reset_guard(g_host)
                if how == "by hand":
                    upstreams.observe_response(f"https://{host}/x", httpx.Response(
                        429, headers={"Retry-After": "30"}), defer_on_429=False)
                else:
                    with http.HopClient(defer_on_429=False, timeout=5) as c:
                        self.assertEqual(429, c.get(f"https://{host}/y").status_code)
                got.append((how, g_up.pace_backlog_s(), g_host.pace_backlog_s()))
            _reset_guard(g_up)
            _reset_guard(g_host)
        for how, upstream, host_gate in got:
            with self.subTest(recorded=how):
                self.assertEqual(0.0, upstream, "the exempt upstream gate is not deferred")
                self.assertTrue(25 < host_gate <= 30.5, f"the host gate must be deferred: {host_gate}")

    def test_T9_retry_after_in_both_forms(self):
        with _temp_upstream("t-date", "date.test.invalid", {"max_inflight": 1, "max_wait_s": 5.0}) as g:
            _reset_guard(g)
            when = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60),
                                               usegmt=True)
            upstreams.observe("t-date", {"Retry-After": when}, 429)
            self.assertTrue(55 < g.pace_backlog_s() <= 61, g.pace_backlog_s())   # not the day, 21
            _reset_guard(g)
            upstreams.observe("t-date", {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 429)
            self.assertEqual(0.0, g.pace_backlog_s())                             # a past date: 0
            upstreams.observe("t-date", {"Retry-After": "30"}, 429)
            self.assertTrue(29 < g.pace_backlog_s() <= 30.5)
            _reset_guard(g)
            upstreams.observe("t-date", {"Retry-After": "3600000"}, 503)          # capped
            self.assertTrue(599 < g.pace_backlog_s() <= 600.5)
            _reset_guard(g)
            upstreams.observe("t-date", {"Retry-After": "soon"}, 429)             # unreadable: none
            self.assertEqual(0.0, g.pace_backlog_s())


class RefundTests(unittest.TestCase):
    """F8: a request that never left gives back its start slot and its place in every window."""

    def test_T10_refused_requests_leave_the_window_as_they_found_it(self):
        up, host = upstreams.guard("zenodo"), upstreams.host_guard("zenodo.org")
        _reset_guard(up)
        _reset_guard(host)
        host.defer(60)                  # the host's next start is a minute away
        sent = shed = 0
        for _ in range(40):
            try:
                with upstreams.egress("https://zenodo.org/api/records"):
                    sent += 1
            except upstreams.UpstreamBusy:
                shed += 1
        used = [w["used"] for w in up.snapshot()["windows"]]
        _reset_guard(up)
        _reset_guard(host)
        self.assertEqual((0, 40), (sent, shed))
        self.assertEqual([0] * len(used), used, "the upstream window must count only what was sent")

    def test_a_connection_never_made_gives_its_slot_back(self):
        with _temp_upstream("t-refund", "refund.test.invalid",
                            {"max_inflight": 1, "min_interval_s": 5.0, "max_wait_s": 1.0,
                             "windows": [[3, 60]]}) as g:
            _reset_guard(g)
            with contextlib.suppress(httpx.ConnectError):
                with upstreams.egress("https://refund.test.invalid/a"):
                    raise httpx.ConnectError("never connected")
            self.assertEqual(0.0, g.pace_backlog_s())
            self.assertEqual(0, g.snapshot()["windows"][0]["used"])
            with contextlib.suppress(httpx.ReadTimeout):   # sent, then the answer was slow: it counts
                with upstreams.egress("https://refund.test.invalid/b"):
                    raise httpx.ReadTimeout("slow")
            self.assertGreater(g.pace_backlog_s(), 4.0)
            self.assertEqual(1, g.snapshot()["windows"][0]["used"])
            _reset_guard(g)


class CdpQueueTests(unittest.TestCase):
    """F9: a render waits for its turn at the browser WITHOUT the host gate, within the time left."""

    def test_T11_a_render_queues_for_the_browser_without_the_host_gate(self):
        from omniseek.core.sources.scrape import news_scraper_source
        from omniseek.core.sources.walled import _cdp
        url = "https://blog.iclr.cc/"                         # a Crawl-delay host: one-permit gate
        host = upstreams.host_guard("blog.iclr.cc")
        _reset_guard(host)
        chrome = _cdp._gate_for(_cdp.DEFAULT_CDP_URL)
        self.assertTrue(chrome.acquire(timeout=1))           # another browser call has the turn
        during = []
        probe = threading.Timer(0.2, lambda: during.append(host.sema._value))
        try:
            with mock.patch.object(_cdp, "ensure_browser", lambda *a, **k: None), \
                    mock.patch.dict(os.environ, {_cdp._POOL_ENV: "0"}):
                probe.start()
                t0 = time.monotonic()
                with _deadline(0.6):
                    out = news_scraper_source._render(url)
                took = time.monotonic() - t0
                probe.join()
        finally:
            chrome.release()
            _reset_guard(host)
        self.assertIsNone(out)
        self.assertLess(took, 1.5, f"queued {took:.1f}s past a 0.6 s deadline")
        self.assertEqual([1], during, "the host gate must stay free while the render queues")
        self.assertEqual(1, host.sema._value)


class UnreadableDeclarationTests(unittest.TestCase):
    """F10 and ruling 4: without a readable upstreams.json OmniSeek refuses to start."""

    def test_T12_the_server_refuses_to_start_and_says_which_file_and_why(self):
        bad = Path(tempfile.mkdtemp()) / "upstreams.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        code = ("import pathlib, omniseek.core.upstreams as u\n"
                f"u.DECL_PATH = pathlib.Path({str(bad)!r})\n"
                "import omniseek.server\n")
        env = dict(os.environ, PYTHONPATH=str(EYE.parents[1]), PYTHONUTF8="1")
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                           timeout=300)
        self.assertNotEqual(0, r.returncode, "the server must not start")
        self.assertIn("DeclarationUnreadable", r.stderr)
        self.assertIn(str(bad), r.stderr)
        self.assertIn("JSONDecodeError", r.stderr)
        self.assertIsNone(upstreams.load_error())   # this process read the real file


class WindowPropertyTests(unittest.TestCase):
    """T13 (probe_f): random arrivals, one to three windows, a minimum interval and random deferrals,
    on a fake clock: starts never decrease, keep the interval, never overfill a window, never come
    before a deferral, and a refund never lets a window overfill."""

    class _Clock:
        def __init__(self):
            self.now = 1000.0

        def monotonic(self):
            return self.now

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += max(0.0, s)

    def _check(self, seed: int) -> list:
        rnd = random.Random(seed)
        clock = self._Clock()
        windows = [(rnd.randint(1, 6), rnd.choice([0.5, 1.0, 2.0, 5.0, 10.0]))
                   for _ in range(rnd.randint(1, 3))]
        mi = rnd.choice([0.0, 0.0, 0.1, 0.3, 1.0])
        with mock.patch.object(guard_mod, "time", clock):
            g = BackendGuard(f"p{seed}", 4, min_interval_s=mi, windows=windows)
            starts, defers, problems = [], [], []
            for _ in range(200):
                clock.now += rnd.expovariate(3.0)
                if rnd.random() < 0.03:
                    d = rnd.uniform(0.1, 3.0)
                    g.defer(d)
                    defers.append(clock.now + d)
                res = g.reserve()
                if rnd.random() < 0.1:          # a request that never left
                    g.refund(res)
                    continue
                if defers and res.start < max(defers) - 1e-9:
                    problems.append(("start before a deferral", res.start, max(defers)))
                starts.append(res.start)
        starts.sort()
        for a, b in zip(starts, starts[1:]):
            if b - a < mi - 1e-9:
                problems.append(("min interval", a, b))
        for limit, secs in windows:
            for i, t in enumerate(starts):
                j = bisect.bisect_left(starts, t + secs - 1e-9)
                if j - i > limit:
                    problems.append(("window", limit, secs, j - i))
                    break
        return problems

    def test_T13_window_reservation_property(self):
        bad = {seed: p for seed in range(2000) if (p := self._check(seed))}
        self.assertEqual({}, dict(list(bad.items())[:3]))


class AsyncSingleConnectionTests(unittest.TestCase):
    def test_T14_the_async_one_connection_transport_closes_every_connection(self):
        """probe_g part 2: on a loopback server that keeps connections alive, the async one-connection
        transport closes each connection after its response; the default transport keeps it."""
        import http.server as std_http_server
        import socketserver
        lock = threading.Lock()
        events = {"accepted": 0, "finished": 0}

        class Handler(std_http_server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                with lock:
                    events["accepted"] += 1

            def parse_request(self):
                ok = super().parse_request()
                self.close_connection = False
                return ok

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(b"ok")

            def finish(self):
                super().finish()
                with lock:
                    events["finished"] += 1

            def log_message(self, *a):
                pass

        class Server(socketserver.ThreadingMixIn, std_http_server.HTTPServer):
            daemon_threads = True

        srv = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/"

        async def run(transport, n=3, headers=None):
            seen = []
            async with httpx.AsyncClient(transport=transport) as client:
                for _ in range(n):
                    await client.get(base, headers=headers)
                    await anyio.sleep(0.3)
                    with lock:
                        seen.append((events["accepted"], events["finished"]))
            return seen
        try:   # the transport alone, without the "Connection: close" header arXiv requests also carry
            one = anyio.run(lambda: run(http._one_connection_transport(asynchronous=True), 3))
            with lock:
                events.update(accepted=0, finished=0)
            ctl = anyio.run(lambda: run(httpx.AsyncHTTPTransport(), 2))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual([(1, 1), (2, 2), (3, 3)], one)        # a new connection each time, closed
        self.assertEqual(1, ctl[-1][0])                         # control: one kept-alive connection


class OpenAlexHeaderTests(unittest.TestCase):
    def test_T15_an_unreadable_quota_header_does_not_fail_a_good_response(self):
        class _R:
            status_code = 200

            def __init__(self, value):
                self.headers = {"x-ratelimit-remaining": value, "x-ratelimit-limit": value}

            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{"id": "W1"}]}

        class _Client:
            def __init__(self, value):
                self.value = value

            def get(self, url, params=None, timeout=None):
                return _R(self.value)
        _reset_guard(_openalex._guard)
        for value in ("nan", "inf", "", "abc", "-inf"):
            with self.subTest(header=value), \
                    mock.patch.object(_openalex, "_api_key", None), \
                    mock.patch.object(_openalex, "_get_client", lambda v=value: _Client(v)):
                got = _openalex.get_json("/works/W1")
                self.assertEqual({"results": [{"id": "W1"}]}, got)
        _reset_guard(_openalex._guard)


class StackExchangeBackoffTests(unittest.TestCase):
    def test_backoff_defers_the_shared_gate_and_quota_is_recorded(self):
        from omniseek.core import _stackexchange as se
        g = se._se_guard
        _reset_guard(g)
        saved = (http.get_json, se._se_cooldown_until, se._se_fail_streak)
        http.get_json = lambda *a, **k: {"items": [], "backoff": 2, "quota_max": 10000,
                                         "quota_remaining": 9990}
        try:
            self.assertIsNotNone(se._se_get("https://api.stackexchange.com/2.3/questions", {}))
            self.assertGreater(g.pace_backlog_s(), 1.5)
            fields = upstreams.readings("stackexchange")["lanes"]["_"]["fields"]
            self.assertEqual("10000", fields["quota_max"])
        finally:
            http.get_json, se._se_cooldown_until, se._se_fail_streak = saved
            _reset_guard(g)
            upstreams._readings.pop("stackexchange", None)


class GitHubCodeSearchGateTests(unittest.TestCase):
    def test_code_search_takes_its_own_gate_before_the_github_gate(self):
        from omniseek.core import _github as gh
        for g in (gh._guard, gh._code_guard):
            _reset_guard(g)
        try:
            with gh._hold_for("/search/issues"):
                pass
            self.assertEqual(0.0, gh._code_guard.pace_state["next_at"])
            _reset_guard(gh._guard)  # skip the 2 s GitHub spacing; the code-search gate is under test
            with gh._hold_for("/search/code"):
                self.assertTrue(gh._code_guard.held() and gh._guard.held())
            self.assertGreater(gh._code_guard.pace_backlog_s(), 5.0)
        finally:
            for g in (gh._guard, gh._code_guard):
                _reset_guard(g)


class _SlowBody(httpx.SyncByteStream):
    """A response body that arrives in ``n`` blocks of 512 bytes, ``gap`` seconds apart (``stall`` seconds
    instead before block ``stall_at``); ``probe`` (optional) is called just before each block leaves."""

    def __init__(self, n=10, gap=0.1, stall_at=None, stall=0.0, probe=None):
        self.n, self.gap, self.stall_at, self.stall, self.probe = n, gap, stall_at, stall, probe

    def __iter__(self):
        for i in range(self.n):
            time.sleep(self.stall if i == self.stall_at else self.gap)
            if self.probe is not None:
                self.probe()
            yield b"x" * 512

    def close(self):
        pass


class _ASlowBody(httpx.AsyncByteStream):
    """Async twin of ``_SlowBody`` (``n`` blocks, ``gap`` seconds apart)."""

    def __init__(self, n=10, gap=0.1):
        self.n, self.gap = n, gap

    async def __aiter__(self):
        for _ in range(self.n):
            await asyncio.sleep(self.gap)
            yield b"x" * 512

    async def aclose(self):
        pass


class ProgressLeaseTests(unittest.TestCase):
    """Driver ruling of 2026-09-29 on section 17.5, item 2: a request that holds a gate either renews its
    lease by its progress or has a total deadline no later than the lease. Where OmniSeek reads the body
    itself, every block of it moves the lease to that moment plus the request's own timeout; a holder
    that makes no progress is still reclaimed when its lease runs out; a library that reads the body
    where OmniSeek cannot see it is cut at a total deadline within the lease."""

    HOST = "progress.test.invalid"
    SIZE = 10 * 512

    def _race(self, send):
        """``send(url)`` makes one request to HOST (its timeout 0.3 s; the body takes about 1 s) under a
        gate of one permit and a declared 0.3 s waiting budget, so its lease ends 0.6 s after it took the
        permit unless the body renews it. As soon as the permit is taken, another caller asks for the gate
        and may wait 3 s. Returns (how long the other caller waited, or what refused it; leases the gate
        took back)."""
        url = f"https://{self.HOST}/body"
        with _temp_upstream("t-progress", self.HOST, {"max_inflight": 1, "max_wait_s": 0.3}) as g:
            _reset_guard(g)
            got_in = []

            def other_caller():
                give_up = time.monotonic() + 3.0
                while g.sema._value > 0 and time.monotonic() < give_up:   # until the request holds it
                    time.sleep(0.005)
                t0 = time.monotonic()
                try:
                    with g.hold(3.0, lambda w: upstreams.UpstreamBusy("busy"),
                                lambda w: upstreams.UpstreamBusy("late")):
                        got_in.append(time.monotonic() - t0)
                except upstreams.UpstreamBusy as exc:
                    got_in.append(exc)
            t = threading.Thread(target=other_caller)
            t.start()
            try:
                send(url)
            finally:
                t.join(5)
            reclaimed = g.snapshot()["leases_reclaimed"]
            _reset_guard(g)
        return (got_in or ["the other caller never finished"])[0], reclaimed

    def _paths(self):
        """(name, send) for every way OmniSeek reads a body itself, each request with a 0.3 s timeout, and
        every kind of hold that can be around it (the modules named are the ones of that shape)."""
        import types
        from omniseek.core import safeurl
        size = self.SIZE

        def busy(waited):
            return upstreams.UpstreamBusy("busy")

        def gate():
            return upstreams.guard("t-progress")   # the race's gate (declared while it runs)

        def shared(url):
            self.assertEqual(size, len(http.get_text(url, timeout=0.3) or ""))

        def shared_async(url):
            http._aget_client()   # built before the gate is taken (outside a loop: no loop binding)

            async def run():
                try:
                    return await http.aget_text(url, timeout=0.3)
                finally:
                    await http.aclose_client()
            self.assertEqual(size, len(anyio.run(run) or ""))

        def bounded(url):
            with guard_mod.bounded_slot(gate().sema, 0.3, busy, request_s=0.3):
                self.assertEqual(size, len(http.get_text(url, timeout=0.3) or ""))

        def abounded(url):
            http._aget_client()

            async def run():
                try:
                    async with bounded_async_slot(gate().sema, 0.3, busy, request_s=0.3):
                        return await http.aget_text(url, timeout=0.3)
                finally:
                    await http.aclose_client()
            self.assertEqual(size, len(anyio.run(run) or ""))

        def direct(url):
            self.assertEqual(size, len(http.direct("GET", url, timeout=0.3).content))

        def adirect(url):
            async def run():
                return len((await http.adirect("GET", url, timeout=0.3)).content)
            self.assertEqual(size, anyio.run(run))

        def direct_stream(url):
            with http.direct_stream("GET", url, timeout=0.3) as r:
                self.assertEqual(size, sum(len(b) for b in r.iter_bytes()))

        def module_client(url):
            with httpx.Client(timeout=0.3, event_hooks=http.progress_hooks()) as c, \
                    upstreams.egress(url, request_s=0.3):
                self.assertEqual(size, len(c.get(url).content))

        def module_async_client(url):
            async def run():
                async with httpx.AsyncClient(timeout=0.3, event_hooks=http.aprogress_hooks()) as c, \
                        upstreams.aegress(url, request_s=0.3):
                    return len((await c.get(url)).content)
            self.assertEqual(size, anyio.run(run))

        def slot_client(url):
            with httpx.Client(timeout=0.3, event_hooks=http.progress_hooks()) as c, \
                    gate().slot(0.3, busy, request_s=0.3):
                self.assertEqual(size, len(c.get(url).content))

        def aslot_client(url):
            async def run():
                async with httpx.AsyncClient(timeout=0.3, event_hooks=http.aprogress_hooks()) as c, \
                        gate().aslot(0.3, busy, request_s=0.3):
                    return len((await c.get(url)).content)
            self.assertEqual(size, anyio.run(run))

        def safe_fetch(url):
            out = safeurl.safe_fetch(url, timeout_total=0.3)
            self.assertEqual(size, out.get("bytes"), out)

        def curl_download(url):
            class _Resp:
                def raise_for_status(self):
                    return None

                def iter_content(self, chunk_size=None):
                    yield from _SlowBody()

                def close(self):
                    return None

            def hops(creq, method, u, gates, **kw):
                gates.enter(u)
                return _Resp()
            pkg = types.ModuleType("curl_cffi")     # the import inside, without the library's import time
            pkg.requests = types.ModuleType("curl_cffi.requests")
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(sys.modules, {"curl_cffi": pkg, "curl_cffi.requests": pkg.requests}), \
                    mock.patch.object(http, "_curl_hops", hops):
                self.assertEqual(size, http.download_to_file(url, str(Path(tmp) / "f"), max_bytes=10 ** 6,
                                                             timeout=0.3))
        return [("shared client (http.get and its kin; MCP)", shared),
                ("shared async client (http.aget and its kin)", shared_async),
                ("shared client under bounded_slot on a gate's permits", bounded),
                ("shared async client under bounded_async_slot on a gate's permits", abounded),
                ("http.direct (a module's raw calls, CORE)", direct),
                ("http.adirect (CORE async)", adirect),
                ("http.direct_stream (asr bilibili download)", direct_stream),
                ("module client with progress_hooks under egress/hold (GitHub, Bluesky)", module_client),
                ("module async client with aprogress_hooks under aegress/ahold", module_async_client),
                ("module client under BackendGuard.slot (OpenAlex)", slot_client),
                ("module async client under BackendGuard.aslot (OpenAlex async)", aslot_client),
                ("safe_fetch (its own client with progress_hooks)", safe_fetch),
                ("curl tier download (asr audio download)", curl_download)]

    @contextlib.contextmanager
    def _wire(self, body=_SlowBody):
        """Every httpx transport answers 200 with a slow body (sync: ``body()``; async: _ASlowBody). No DNS,
        no socket."""
        from omniseek.core import safeurl

        def handle(self_, request):
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=body(),
                                  request=request)

        async def ahandle(self_, request):
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=_ASlowBody(),
                                  request=request)
        with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", ahandle), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                mock.patch.object(safeurl, "_resolve_safe_ip",
                                  lambda host: ("93.184.216.34", socket.AF_INET, None)):
            yield

    @contextlib.contextmanager
    def _fresh_clients(self):
        """The shared clients rebuilt for the test (the sync one before any race) and restored after."""
        saved = (http._client, http._aclient, http._aclient_loop)
        http._client = http._aclient = None
        try:
            yield
        finally:
            if http._client is not None:
                http._client.close()
            http._client, http._aclient, http._aclient_loop = saved

    def test_a_request_making_progress_keeps_its_lease_on_every_path(self):
        """Every block of the body renews the lease to that moment plus 0.3 s, so the other caller gets in
        when the transfer is over (about 1 s after it asked), not when the first lease would have run out
        (0.6 s), and no lease is taken back."""
        with self._fresh_clients(), self._wire():
            http._get_client()   # built before the races (a build takes a moment; see the first-request test)
            for name, send in self._paths():
                with self.subTest(path=name):
                    got_in, reclaimed = self._race(send)
                    self.assertIsInstance(got_in, float, f"{name}: {got_in!r}")
                    self.assertGreater(got_in, 0.85, f"{name}: the other caller got in after {got_in:.2f}s")
                    self.assertEqual(0, reclaimed, name)

    def test_a_request_that_stops_making_progress_is_reclaimed(self):
        """The same race, but the body stalls for 1.5 s after its sixth block: those blocks moved the lease
        to 0.3 s after the last of them (about 0.9 s in), and with no progress after that the lease runs
        out there and the gate gives the permit to the caller in line."""
        with self._fresh_clients(), \
                self._wire(lambda: _SlowBody(n=10, gap=0.1, stall_at=6, stall=1.5)):
            http._get_client()
            got_in, reclaimed = self._race(lambda url: http.get_text(url, timeout=0.3))
        self.assertIsInstance(got_in, float, repr(got_in))
        self.assertGreater(got_in, 0.8, f"the blocks before the stall did not renew the lease ({got_in:.2f}s)")
        self.assertLess(got_in, 1.3, f"the stalled holder kept the gate {got_in:.2f}s")
        self.assertEqual(1, reclaimed)

    def test_a_download_with_a_fixed_timeout_renews_its_lease_as_it_arrives(self):
        """docreader's download (its own client with http.progress_hooks(), a 90 s request): at every block
        the gate's lease moves to that moment plus 90 s (the waiting budget here is 0.01 s, so the first
        block already moves it past where the gate put it)."""
        from omniseek.core import docreader
        host = "doc.test.invalid"
        seen = []
        with _temp_upstream("t-doc", host, {"max_inflight": 1, "max_wait_s": 0.01}) as g:
            _reset_guard(g)

            def probe():
                with g.sema._lock:
                    seen.append((time.monotonic(), max((x.expires_at for x in g.sema._leases), default=None)))
            with self._wire(lambda: _SlowBody(n=6, gap=0.05, probe=probe)):
                path, _, _ = docreader._download(f"https://{host}/paper.pdf", "pdf")
            path.unlink()
            _reset_guard(g)
        ends = [e for _, e in seen]
        self.assertEqual(6, len(ends))
        self.assertNotIn(None, ends)
        self.assertGreater(ends[1], ends[0], "the first block did not move the lease")
        self.assertTrue(all(b > a for a, b in zip(ends[1:], ends[2:])), ends)
        self.assertAlmostEqual(seen[-2][0] + 90.0, ends[-1], delta=0.03)

    def test_a_library_request_is_cut_at_its_total_deadline(self):
        """semanticscholar reads each response inside its own httpx.AsyncClient: OmniSeek cannot see the
        body arrive, so each of its requests is cut at TIMEOUT from its start (here 0.3 s), within the
        permit's lease; the permit is back at once and nothing is reclaimed."""
        from omniseek.core import _s2

        async def hang(self_, request):
            await asyncio.sleep(5)
            return httpx.Response(200, json={}, request=request)
        g = _s2._guard
        saved = _s2._client
        _s2._client = None
        _reset_guard(g)
        before = g.snapshot()["leases_reclaimed"]
        try:
            with mock.patch.object(_s2, "TIMEOUT", 0.3), \
                    mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", hang):
                t0 = time.monotonic()
                out = _s2.get_paper("CorpusId:1")
                took = time.monotonic() - t0
            free, reclaimed = g.sema._value, g.snapshot()["leases_reclaimed"] - before
        finally:
            _s2._client = saved
            _reset_guard(g)
        self.assertLess(took, 1.5, f"the library's request ran {took:.2f}s past a 0.3 s deadline")
        self.assertIsNone(out)
        self.assertEqual((g.max_inflight, 0), (free, reclaimed))

    def test_each_request_of_a_paginated_library_call_renews_the_hold_and_is_cut(self):
        """A paginated semanticscholar call reads its pages under one hold (an author's 200 works come 100
        to a page). Each request the library sends starts only after the one before it returned: it renews
        the hold's lease to TIMEOUT from its start and is cut there (TIMEOUT 0.3 s here; the library's
        requester is a stand-in)."""
        import types
        from omniseek.core import _s2

        class _Requester:
            delay = 0.2

            async def get_data_async(self, url, parameters, headers, payload=None):
                await asyncio.sleep(self.delay)
                return {"url": url}
        req = _Requester()
        client = types.SimpleNamespace(_AsyncSemanticScholar=types.SimpleNamespace(_requester=req))
        with mock.patch.object(_s2, "TIMEOUT", 0.3), \
                _temp_upstream("t-pages", "pages.test.invalid", {"max_inflight": 1, "max_wait_s": 0.05}) as g:
            _s2._bound_requests(client)
            _reset_guard(g)
            with g.hold(0.05, lambda w: upstreams.UpstreamBusy("busy"), request_s=0.3):
                lease = g.sema._leases[0]
                first_end = lease.expires_at
                self.assertEqual({"url": "p1"}, asyncio.run(req.get_data_async("p1", "", {})))
                started = time.monotonic()
                self.assertEqual({"url": "p2"}, asyncio.run(req.get_data_async("p2", "", {})))
                second_end = lease.expires_at
                req.delay = 5.0
                t0 = time.monotonic()
                with self.assertRaises(TimeoutError):
                    asyncio.run(req.get_data_async("p3", "", {}))
                took = time.monotonic() - t0
            _reset_guard(g)
        self.assertGreater(second_end, first_end, "the second page did not renew the lease")
        self.assertAlmostEqual(started + 0.3, second_end, delta=0.05)
        self.assertLess(took, 0.6, f"a hanging page ran {took:.2f}s past a 0.3 s deadline")

    def test_the_curl_tier_cuts_a_trickling_body_at_its_timeout(self):
        """The curl tier reads a non-streamed body inside libcurl, where OmniSeek cannot see it arrive: its
        timeout is the whole request's (curl_cffi sets libcurl's TIMEOUT_MS). A local server that sends
        one byte every 0.1 s is cut at 0.6 s, within the lease; the gate is free and nothing is reclaimed."""
        try:
            from curl_cffi import requests as _creq  # noqa: F401
        except Exception:  # noqa: BLE001
            self.skipTest("curl_cffi is not installed")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(5)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100000\r\n\r\n")
                for _ in range(50):   # one byte every 0.1 s, for at most 5 s
                    if stop.is_set():
                        return
                    try:
                        conn.sendall(b"x")
                    except OSError:
                        return
                    time.sleep(0.1)
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            with _temp_upstream("t-curl", "127.0.0.1", {"max_inflight": 1, "max_wait_s": 1.0}) as g, \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None):
                _reset_guard(g)
                t0 = time.monotonic()
                out = http._impersonated_request("GET", f"http://127.0.0.1:{port}/slow", timeout=0.6)
                took = time.monotonic() - t0
                free, reclaimed = g.sema._value, g.snapshot()["leases_reclaimed"]
                _reset_guard(g)
        finally:
            stop.set()
            srv.close()
            t.join(5)
        self.assertIsNone(out)
        self.assertLess(took, 2.0, f"the trickling body ran {took:.2f}s past a 0.6 s timeout")
        self.assertEqual((1, 0), (free, reclaimed))

    def test_the_bluesky_library_client_gets_the_progress_hook(self):
        """atproto sends through an httpx.Client of its own (a private attribute, checked here on the
        installed version): the adapter gives it the progress hook and reads its timeout for the lease."""
        try:
            from atproto import Client
        except Exception:  # noqa: BLE001
            self.skipTest("atproto is not installed")
        from omniseek.core.sources.api import bluesky_source
        client = Client()
        request_s = bluesky_source.BlueskyAdapter._wire_progress(client)
        hc = client.request._client
        self.assertIn(http._watch_progress, hc.event_hooks["response"])
        self.assertIn(http._watch_send, hc.event_hooks["request"])   # the request sent (review Q1)
        self.assertIsNotNone(request_s)
        self.assertEqual(http._timeout_s(hc.timeout), request_s)

    # ── the first request's start slot is the moment it goes on the wire (section 17.4) ────────────────
    def _first_request(self, kind: str) -> "tuple[float, int]":
        """The first request in the process to a Crawl-delay host (0.5 s), while the first build of the
        shared client takes 0.4 s. Returns (how long after its start slot it reached the wire, requests
        on the wire)."""
        host = f"first-{kind}.test.invalid"
        url = f"https://{host}/a"
        wire = []

        def handle(self_, request):
            wire.append(time.monotonic())
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  stream=httpx.ByteStream(b'{"jsonrpc": "2.0", "id": 1, "result": {}}'),
                                  request=request)
        real_sync, real_async = http._get_client, http._aget_client

        def slow_sync():
            if http._client is None:
                time.sleep(0.4)
            return real_sync()

        def slow_async():
            if http._aclient is None:
                time.sleep(0.4)
            return real_async()
        with self._fresh_clients(), _temp_host_delay(host, 0.5) as hg, \
                mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _async_of(handle)), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                mock.patch.object(http, "_get_client", slow_sync), \
                mock.patch.object(http, "_aget_client", slow_async):
            _reset_guard(hg)
            if kind == "async":
                async def run():
                    try:
                        return await http.aget_text(url)
                    finally:
                        await http.aclose_client()
                self.assertIsNotNone(anyio.run(run))
            elif kind == "mcp":
                from omniseek.core.sources import _mcp
                parsed, _ = _mcp.MCPClient(url, timeout_s=5)._post(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual({}, parsed.get("result"))
            else:
                self.assertIsNotNone(http.get_text(url))
            start = hg.pace_state["next_at"] - 0.5      # the start slot the gate gave the request
            _reset_guard(hg)
        return (wire[0] - start if wire else float("nan")), len(wire)

    def test_the_first_request_goes_on_the_wire_at_its_start_slot(self):
        """The shared client is built before the gates are entered, so even the process's first request
        reaches the wire at the start slot it was given (not a client build later)."""
        for kind in ("sync", "async", "mcp"):
            with self.subTest(client=kind):
                late, sent = self._first_request(kind)
                self.assertEqual(1, sent)
                self.assertLess(late, 0.1, f"{kind}: on the wire {late:.2f}s after its start slot")


_GATE_CALLS = {"egress", "aegress", "hold", "ahold", "slot", "aslot", "bounded_slot", "bounded_async_slot",
               "hop_gates", "ahop_gates", "browser_turn", "HopGates", "AsyncHopGates"}
_RAW_HTTPX = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}


def _call_name(node):
    """The called name of a call node (``x.y(...)`` -> y, ``y(...)`` -> y)."""
    f = node.func
    return f.attr if hasattr(f, "attr") else getattr(f, "id", None)


class ProgressCoverageTests(unittest.TestCase):
    """Static companions of ProgressLeaseTests: no request under a gate is sent in a way whose body the
    eye cannot see arrive (so its lease could not be renewed by its progress)."""

    def test_no_request_under_a_gate_is_sent_with_a_raw_httpx_call(self):
        """A raw ``httpx.get``/``post``/... reads its body inside httpx, where no hook of OmniSeek sees it:
        under a gate, a module sends with ``http.direct`` (or its own client with
        ``http.progress_hooks()``) instead. Checked on the source: every ``with``/``async with`` that
        takes a gate, and the raw calls inside it."""
        import ast
        found = []

        def visit(node, gated, rel):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                gated = gated or any(isinstance(n, ast.Call) and _call_name(n) in _GATE_CALLS
                                     for item in node.items for n in ast.walk(item.context_expr))
            f = getattr(node, "func", None)
            if (gated and isinstance(node, ast.Call) and isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name) and f.value.id == "httpx" and f.attr in _RAW_HTTPX):
                found.append(f"{rel}:{node.lineno} httpx.{f.attr}")
            for child in ast.iter_child_nodes(node):
                visit(child, gated, rel)
        for path in sorted(EYE.rglob("*.py")):
            visit(ast.parse(path.read_text(encoding="utf-8")), False, path.relative_to(EYE).as_posix())
        self.assertEqual([], found, "under a gate, send with http.direct so the body renews the lease")

    def test_every_module_client_in_a_gated_module_renews_by_progress(self):
        """A module that takes a gate and builds its own ``httpx.Client``/``AsyncClient`` gives it the
        progress hooks (``http.progress_hooks()``/``aprogress_hooks()``, or the redirect-rule hooks
        ``hop_hooks``/``ahop_hooks`` and the shared clients' ``_observe_hop``/``_aobserve_hop``, which
        include them). A module that takes no gate is not checked."""
        import ast
        renews = {"progress_hooks", "aprogress_hooks", "hop_hooks", "ahop_hooks", "_observe_hop",
                  "_aobserve_hop"}
        missing = []
        for path in sorted(EYE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            if not any(_call_name(n) in _GATE_CALLS for n in calls):
                continue
            for n in calls:
                f = n.func
                if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                        and f.value.id == "httpx" and f.attr in ("Client", "AsyncClient")):
                    continue
                hooks = next((k.value for k in n.keywords if k.arg == "event_hooks"), None)
                names = {getattr(x, "attr", None) or getattr(x, "id", None)
                         for x in (ast.walk(hooks) if hooks is not None else ())}
                if not names & renews:
                    missing.append(f"{path.relative_to(EYE).as_posix()}:{n.lineno} httpx.{f.attr}")
        self.assertEqual([], missing,
                         "give the client event_hooks=http.progress_hooks() (or aprogress_hooks)")


class FirstByteLeaseTests(unittest.TestCase):
    """Review X section 12.8, Q1, and the driver ruling of 2026-09-29: before its first byte a request makes
    progress twice, when it has been sent and when its headers arrive; both renew its leases, so a slow
    connection or a slow first byte inside the request's own timeout keeps the permit, and a one-permit
    gate never has two requests on the wire."""

    HOST = "slow-first-byte.test.invalid"

    def _first_byte_race(self, send_a, *, sent_at=0.5, headers_at=1.4):
        """X's probe_u as a test. A gate of 1 permit, declared max_wait_s 0.3 s; request A (its timeout
        1.0 s, so its lease ends 1.3 s after it took the permit) is sent ``sent_at`` seconds in (connecting
        and writing) and answered at ``headers_at`` (its first byte), each phase inside its 1.0 s timeout.
        1.35 s after A took the permit, B (a plain http.get) asks for the gate. Returns (the most requests
        on the wire at once, leases taken back, what B got)."""
        url_a, url_b = f"https://{self.HOST}/a", f"https://{self.HOST}/b"
        wire = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def enter():
            with lock:
                wire["now"] += 1
                wire["peak"] = max(wire["peak"], wire["now"])

        def leave():
            with lock:
                wire["now"] -= 1

        def ok(request):
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"ok"),
                                  request=request)

        def handle(self_, request):   # httpcore's order: connect, send (trace event), wait for the headers
            enter()
            try:
                if request.url.path != "/a":
                    time.sleep(0.05)
                    return ok(request)
                time.sleep(sent_at)
                trace = request.extensions.get("trace")
                if trace is not None:
                    trace("http11.send_request_body.complete", {"request": request})
                time.sleep(headers_at - sent_at)
                return ok(request)
            finally:
                leave()

        async def ahandle(self_, request):
            enter()
            try:
                await asyncio.sleep(sent_at)
                trace = request.extensions.get("trace")
                if trace is not None:
                    await trace("http11.send_request_body.complete", {"request": request})
                await asyncio.sleep(headers_at - sent_at)
                return ok(request)
            finally:
                leave()
        from omniseek.core import safeurl
        got_b = []
        with _temp_upstream("t-first-byte", self.HOST, {"max_inflight": 1, "max_wait_s": 0.3}) as g, \
                mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", ahandle), \
                mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                mock.patch.object(safeurl, "_resolve_safe_ip",
                                  lambda host: ("93.184.216.34", socket.AF_INET, None)):
            _reset_guard(g)

            def caller_b():
                give_up = time.monotonic() + 3.0
                while g.sema._value > 0 and time.monotonic() < give_up:   # until A holds the permit
                    time.sleep(0.005)
                time.sleep(1.35)
                got_b.append(http.get_text(url_b, timeout=1.0))
            t = threading.Thread(target=caller_b)
            t.start()
            try:
                send_a(url_a)
            finally:
                t.join(5)
            reclaimed = g.snapshot()["leases_reclaimed"]
            _reset_guard(g)
        return wire["peak"], reclaimed, (got_b or ["B never finished"])[0]

    def _paths(self):
        from omniseek.core import safeurl

        def shared(url):
            http.get_text(url, timeout=1.0)

        def shared_async(url):
            http._aget_client()   # built before the gate is taken (outside a loop: no loop binding)

            async def run():
                try:
                    return await http.aget_text(url, timeout=1.0)
                finally:
                    await http.aclose_client()
            anyio.run(run)

        def direct(url):
            http.direct("GET", url, timeout=1.0)

        def adirect(url):
            async def run():
                return await http.adirect("GET", url, timeout=1.0)
            anyio.run(run)

        def hop_client(url):
            with http.direct_stream("GET", url, timeout=1.0) as r:
                r.read()

        def async_hop_client(url):
            async def run():
                async with http.AsyncHopClient(timeout=1.0) as c:
                    return await c.get(url)
            anyio.run(run)

        def module_client(url):
            with httpx.Client(timeout=1.0, event_hooks=http.progress_hooks()) as c, \
                    upstreams.egress(url, request_s=1.0):
                c.get(url)

        def module_async_client(url):
            async def run():
                async with httpx.AsyncClient(timeout=1.0, event_hooks=http.aprogress_hooks()) as c, \
                        upstreams.aegress(url, request_s=1.0):
                    return await c.get(url)
            anyio.run(run)

        def safe_fetch(url):
            safeurl.safe_fetch(url, timeout_total=1.0)
        return [("shared client", shared), ("shared async client", shared_async), ("http.direct", direct),
                ("http.adirect", adirect), ("HopClient (http.direct_stream)", hop_client),
                ("AsyncHopClient", async_hop_client), ("module client with progress_hooks", module_client),
                ("module async client with aprogress_hooks", module_async_client), ("safe_fetch", safe_fetch)]

    def test_Q1_a_slow_first_byte_keeps_its_permit_on_every_path(self):
        """A is sent 0.5 s in and answered 1.4 s in; the moment it is sent renews its lease to 1.5 s, so B
        (1.35 s) waits for A instead of going out beside it: one request on the wire at a time, nothing
        taken back, and B is served."""
        with ProgressLeaseTests._fresh_clients(self):
            http._get_client()
            for name, send in self._paths():
                with self.subTest(path=name):
                    peak, reclaimed, got_b = self._first_byte_race(send)
                    self.assertEqual((1, 0), (peak, reclaimed), f"{name}: requests on the wire at once, "
                                                                f"leases taken back")
                    self.assertEqual("ok", got_b, name)

    def test_Q1_the_control_answers_inside_the_first_lease(self):
        """X's control: A answered 1.2 s in, inside the lease it had without any renewal."""
        with ProgressLeaseTests._fresh_clients(self):
            peak, reclaimed, got_b = self._first_byte_race(lambda url: http.get_text(url, timeout=1.0),
                                                           headers_at=1.2)
        self.assertEqual((1, 0, "ok"), (peak, reclaimed, got_b))

    def test_Q1_the_headers_renew_the_lease(self):
        """The response headers are progress too: when they arrive (0.2 s in, waiting budget 0.05 s,
        timeout 1.0 s), the lease moves to that moment plus 1.0 s, before any block of the body."""
        host = "headers.test.invalid"
        url = f"https://{host}/h"

        def run(send):
            seen = []
            with _temp_upstream("t-headers", host, {"max_inflight": 1, "max_wait_s": 0.05}) as g:
                _reset_guard(g)

                def probe():
                    seen.append(g.sema._leases[0].expires_at if g.sema._leases else None)

                class _Body(httpx.SyncByteStream):
                    def __iter__(self):
                        probe()
                        yield b"ok"

                class _ABody(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        probe()
                        yield b"ok"
                headers_at = []

                def handle(self_, request):
                    time.sleep(0.2)
                    headers_at.append(time.monotonic())
                    return httpx.Response(200, stream=_Body(), request=request)

                async def ahandle(self_, request):
                    await asyncio.sleep(0.2)
                    headers_at.append(time.monotonic())
                    return httpx.Response(200, stream=_ABody(), request=request)
                with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", ahandle), \
                        mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                        mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                    send()
                _reset_guard(g)
            return seen[0] - headers_at[0] if seen and seen[0] is not None and headers_at else None

        def shared():
            http.get_text(url, timeout=1.0)

        def shared_async():
            http._aget_client()

            async def go():
                try:
                    return await http.aget_text(url, timeout=1.0)
                finally:
                    await http.aclose_client()
            anyio.run(go)

        def hop_client():
            with http.HopClient(timeout=1.0) as c:
                c.get(url)

        def module_client():
            with httpx.Client(timeout=1.0, event_hooks=http.progress_hooks()) as c, \
                    upstreams.egress(url, request_s=1.0):
                c.get(url)
        with ProgressLeaseTests._fresh_clients(self):
            http._get_client()
            for name, send in (("shared client", shared), ("shared async client", shared_async),
                               ("HopClient", hop_client), ("module client with progress_hooks", module_client)):
                with self.subTest(path=name):
                    ahead = run(send)
                    self.assertIsNotNone(ahead, name)
                    self.assertAlmostEqual(1.0, ahead, delta=0.05, msg=f"{name}: lease end minus the headers")

    def test_Q1_a_slow_connection_with_little_budget_left_keeps_its_permit(self):
        """The permit comes with little of the waiting budget left (declared max_wait_s 0.05 s, timeout 1.0 s:
        the lease ends 1.05 s later), and the connection is slow: connected 0.9 s in, TLS set up 1.8 s in,
        the request written 2.0 s in, answered 2.1 s in (every phase inside its 1.0 s timeout). Connecting
        and the TLS handshake are progress, so the lease is still live when the request is written; B, who
        asked 0.5 s in and may wait 3 s, gets the permit only after A is answered. Sync and async."""
        host = "slow-connect.test.invalid"
        steps = (("connection.connect_tcp.complete", 0.9), ("connection.start_tls.complete", 0.9),
                 ("http11.send_request_body.complete", 0.2))

        def ok(request):
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)

        def run(send_a):
            wire = {"now": 0, "peak": 0, "live_when_sent": None}
            lock = threading.Lock()
            with _temp_upstream("t-slow-connect", host, {"max_inflight": 1, "max_wait_s": 0.05}) as g:
                _reset_guard(g)

                def mark(name):
                    if name.endswith("send_request_body.complete"):
                        wire["live_when_sent"] = bool(g.sema._leases) and g.sema._leases[0].active

                def handle(self_, request):
                    with lock:
                        wire["now"] += 1
                        wire["peak"] = max(wire["peak"], wire["now"])
                    try:
                        trace = request.extensions.get("trace")
                        for name, wait in steps:
                            time.sleep(wait)
                            if trace is not None:
                                trace(name, {})
                            with g.sema._lock:
                                g.sema._reclaim_locked(time.monotonic())   # what a waiter would do now
                            mark(name)
                        time.sleep(0.1)
                        return ok(request)
                    finally:
                        with lock:
                            wire["now"] -= 1

                async def ahandle(self_, request):
                    with lock:
                        wire["now"] += 1
                        wire["peak"] = max(wire["peak"], wire["now"])
                    try:
                        trace = request.extensions.get("trace")
                        for name, wait in steps:
                            await asyncio.sleep(wait)
                            if trace is not None:
                                await trace(name, {})
                            with g.sema._lock:
                                g.sema._reclaim_locked(time.monotonic())
                            mark(name)
                        await asyncio.sleep(0.1)
                        return ok(request)
                    finally:
                        with lock:
                            wire["now"] -= 1
                got_b = []

                def caller_b():
                    give_up = time.monotonic() + 3.0
                    while g.sema._value > 0 and time.monotonic() < give_up:
                        time.sleep(0.005)
                    time.sleep(0.5)
                    try:
                        with g.hold(3.0, lambda w: upstreams.UpstreamBusy("busy"),
                                    lambda w: upstreams.UpstreamBusy("late")):
                            with lock:
                                got_b.append(wire["now"])   # A still on the wire when B got in?
                    except upstreams.UpstreamBusy as exc:
                        got_b.append(exc)
                with mock.patch.object(httpx.HTTPTransport, "handle_request", handle), \
                        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", ahandle), \
                        mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                        mock.patch.object(http._netguard, "resolve_pin", _no_pin):
                    t = threading.Thread(target=caller_b)
                    t.start()
                    try:
                        send_a(f"https://{host}/a")
                    finally:
                        t.join(5)
                reclaimed = g.snapshot()["leases_reclaimed"]
                _reset_guard(g)
            return wire["live_when_sent"], reclaimed, (got_b or ["B never finished"])[0]

        def sync(url):
            http.get_text(url, timeout=1.0)

        def async_(url):
            http._aget_client()

            async def go():
                try:
                    return await http.aget_text(url, timeout=1.0)
                finally:
                    await http.aclose_client()
            anyio.run(go)
        with ProgressLeaseTests._fresh_clients(self):
            http._get_client()
            for name, send in (("shared client", sync), ("shared async client", async_)):
                with self.subTest(client=name):
                    live, reclaimed, a_on_wire_when_b_got_in = run(send)
                    self.assertEqual((True, 0, 0), (live, reclaimed, a_on_wire_when_b_got_in),
                                     f"{name}: (lease live when the request was written, leases taken back, "
                                     f"A on the wire when B got in)")

    def _loopback_renewals(self, run_async: bool) -> "tuple[list, float]":
        """A real httpx and httpcore against a server on 127.0.0.1 that answers 0.4 s after the request:
        the moments (after the start) at which the request's holds were renewed, and when it returned."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        srv.settimeout(5)
        port = srv.getsockname()[1]

        def serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                time.sleep(0.4)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n"
                             b"Connection: close\r\n\r\nok")
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        renewed, real = [], guard_mod.renew_handles
        t0 = time.monotonic()

        def spy(handles):
            if handles:
                renewed.append(round(time.monotonic() - t0, 3))
            real(handles)
        url = f"http://127.0.0.1:{port}/x"
        try:
            with ProgressLeaseTests._fresh_clients(self), \
                    _temp_upstream("t-loopback", "127.0.0.1", {"max_inflight": 1, "max_wait_s": 1.0}) as g, \
                    mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    mock.patch.object(http._netguard, "resolve_pin", _no_pin), \
                    mock.patch.object(guard_mod, "renew_handles", spy):
                http._get_client()
                http._aget_client()
                _reset_guard(g)
                t0 = time.monotonic()
                if run_async:
                    async def run():
                        try:
                            return await http.aget_text(url, timeout=2.0)
                        finally:
                            await http.aclose_client()
                    out = anyio.run(run)
                else:
                    out = http.get_text(url, timeout=2.0)
                took = time.monotonic() - t0
                _reset_guard(g)
        finally:
            srv.close()
            th.join(5)
        self.assertEqual("ok", out)
        return renewed, took

    def test_Q1_httpcore_reports_the_moment_the_request_is_sent(self):
        """Against a real httpcore (no stubbed transport): the request's holds are renewed when its
        connection is made and when it has been sent, well before its headers (0.4 s), and again when the
        headers arrive."""
        for kind in (False, True):
            with self.subTest(client="async" if kind else "sync"):
                renewed, took = self._loopback_renewals(kind)
                self.assertTrue(renewed and renewed[0] < 0.3, f"renewed at {renewed} (answer at 0.4 s)")
                # connected, then the request written: two renewals before the headers (plain HTTP, no TLS)
                self.assertGreaterEqual(sum(x < 0.3 for x in renewed), 2, f"renewed at {renewed}")
                self.assertTrue(any(x >= 0.35 for x in renewed), f"renewed at {renewed}: not at the headers")

    def test_Q1_the_curl_tier_renews_each_hop_when_sent_and_when_answered(self):
        """The curl tier follows redirects hop by hop: a 302 answered 0.3 s in, then the page 0.6 s in
        (a stand-in for curl_cffi, timeout 1.0 s, waiting budget 0.05 s). The second hop goes out with its
        lease renewed by the first hop's answer, and the page's answer renews it again."""
        import types
        host = "curl-hops.test.invalid"
        seen = []
        with _temp_upstream("t-curl-hops", host, {"max_inflight": 1, "max_wait_s": 0.05}) as g:
            _reset_guard(g)

            class _Resp:
                def __init__(self, status, headers):
                    self.status_code, self.headers = status, headers

                def close(self):
                    return None

            def request(method, url, **kw):
                seen.append((time.monotonic(), g.sema._leases[0].expires_at))
                time.sleep(0.3)
                if url.endswith("/a"):
                    return _Resp(302, {"location": f"https://{host}/b"})
                return _Resp(200, {})
            creq = types.SimpleNamespace(request=request)
            with mock.patch.object(http._netguard, "security_block_reason", lambda url: None), \
                    upstreams.hop_gates(request_s=1.0) as gates:
                r = http._curl_hops(creq, "GET", f"https://{host}/a", gates, headers=None, timeout=1.0)
                done, end = time.monotonic(), g.sema._leases[0].expires_at
            _reset_guard(g)
        self.assertEqual(200, r.status_code)
        self.assertEqual(2, len(seen))
        (t1, _), (t2, e2) = seen
        self.assertAlmostEqual(t2 + 1.0, e2, delta=0.05)     # the first hop's answer, just before hop 2
        self.assertAlmostEqual(done + 1.0, end, delta=0.05)  # the page's answer


if __name__ == "__main__":
    unittest.main()
