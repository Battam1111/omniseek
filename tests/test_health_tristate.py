"""Contract: a health check has THREE results, and nobody collapses the third into the other two.

True = verified working (this check asked the upstream and the answer shows it serves), False =
verified broken, None = NOT verified (this check sent the upstream no request whose answer could tell
good from bad: a metered quota it must not spend, a busy declared gate, a breaker or back-off, an
answer served from cache or from stored state).

The defect this pins (2026-10-04): every such "nothing was asked" path returned True with a message
saying "not probed", and every reader counted it as healthy. context7 is never probed (its quota is
200 requests a month) and read as healthy forever. The fix has two halves, and both are pinned here:

  (a) the no_live_probe path returns None;
  (b) a busy declared gate returns None (and the cache-served checks return None, with the SAME cache
      key their data helper reads, so a later key bump cannot silently turn them back into True);
  (c) the watchdog, given None from a probe that COMPLETED: moves no fail counter in either direction,
      sends no alert, and records the source as ``unverified`` (apart from ``unmeasured``, which is
      our own probe timing out);
  (d) omniseek_sources(check_health=True) reports healthy = null for it;
  (e) every count keeps "not verified" apart from "healthy";
  (f) the True and False paths behave exactly as before.

Harness as in test_health_watchdog_prune.py: a TEMP state file with a write tripwire, stubbed probes,
captured alerts, a stubbed registry. No network, no CDP, no launchctl.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import omniseek.server as server  # noqa: E402
from omniseek.core import cache, fetcher, infra_jobs, upstreams  # noqa: E402
from omniseek.core.curator import source_audit  # noqa: E402


def _busy(*_a, **_k):
    raise upstreams.UpstreamBusy("test: no permit within 0.1s (gate saturated); not sent")


def _no_network(*_a, **_k):
    raise AssertionError("a health check that must not send anything sent a request")


# ── (a) no_live_probe ────────────────────────────────────────────────────────────────────────────
class NoLiveProbeIsNotVerified(unittest.TestCase):
    def test_a_no_live_probe_row_returns_none_and_sends_nothing(self):
        from omniseek.core.sources._declarative import DeclarativeAPIAdapter
        from omniseek.core import http
        ad = DeclarativeAPIAdapter(name="_tristate_metered", description="probe",
                                   endpoint="https://x.example/api", field_map={"title": "t", "url": "u"},
                                   results_path="items", no_live_probe="200 requests a month")
        with mock.patch.object(http, "get_json", _no_network), mock.patch.object(http, "get", _no_network):
            ok, msg = ad.health_check()
        self.assertIsNone(ok)
        self.assertIn("not probed", msg)
        self.assertIn("200 requests a month", msg)

    def test_a_normal_declarative_row_still_probes_and_reads_true_or_false(self):
        from omniseek.core.sources._declarative import DeclarativeAPIAdapter
        from omniseek.core import http
        ad = DeclarativeAPIAdapter(name="_tristate_plain", description="probe",
                                   endpoint="https://x.example/api", field_map={"title": "t", "url": "u"},
                                   results_path="items")
        with mock.patch.object(http, "get_json", lambda *a, **k: {"items": []}):
            self.assertIs(ad.health_check()[0], True)
        with mock.patch.object(http, "get_json", lambda *a, **k: None):
            self.assertIs(ad.health_check()[0], False)


# ── (b) busy gate / cache-served ─────────────────────────────────────────────────────────────────
class BusyGateIsNotVerified(unittest.TestCase):
    def _assert_none_degraded(self, result):
        ok, msg = result
        self.assertIsNone(ok, msg)
        self.assertIn("not probed", msg)

    def test_crossref_hackernews_and_huggingface_busy_gates(self):
        from omniseek.core.sources.api import (crossref_source, hackernews_source,
                                             hf_daily_papers_source, huggingface_hub_source)
        for mod, cls in ((crossref_source, "CrossrefAdapter"), (hackernews_source, "HackerNewsAdapter"),
                         (hf_daily_papers_source, "HFDailyPapersAdapter"),
                         (huggingface_hub_source, "HuggingFaceHubAdapter")):
            with self.subTest(source=cls), \
                    mock.patch.object(mod.upstreams, "egress", _busy), \
                    mock.patch.object(mod.http, "direct", _no_network):
                self._assert_none_degraded(getattr(mod, cls)().health_check())

    def test_dblp_and_s2_authors_busy_gates(self):
        from omniseek.core.sources.api import dblp_source, s2_authors_source
        with mock.patch.object(dblp_source.upstreams, "egress", _busy), \
                mock.patch.object(dblp_source.http, "get", _no_network):
            self._assert_none_degraded(dblp_source.DBLPAdapter().health_check())
        with mock.patch.object(s2_authors_source.http, "direct", _busy):
            self._assert_none_degraded(s2_authors_source.S2AuthorsAdapter().health_check())

    def test_arxiv_busy_gate(self):
        from omniseek.core.sources.api import arxiv_source

        def _hold(*_a, **_k):
            raise arxiv_source._busy(1.0)
        with mock.patch.object(arxiv_source._guard, "is_open", lambda: False), \
                mock.patch.object(arxiv_source._guard, "hold", _hold), \
                mock.patch.object(arxiv_source.http, "direct", _no_network):
            ok, msg = arxiv_source.ArxivAdapter().health_check()
        self.assertIsNone(ok, msg)
        self.assertIn("did not probe", msg)

    def test_search_backend_cooling_and_its_nowcoder_consumer(self):
        from omniseek.core.sources.api import _search_backend as sb, nowcoder_source
        cooling = {"active": "none", "nominal": False, "ddg": {"disabled": "", "cooling_s": 30},
                   "brave": {"keyed": True, "cooling_s": 30}}
        with mock.patch.object(sb, "backend_state", lambda: cooling), \
                mock.patch.dict(sb._ping, {"t": 0.0, "ok": None, "msg": ""}), \
                mock.patch.object(sb, "search_web", _no_network):
            ok, msg = sb.backend_ping()
            self.assertIsNone(ok, msg)
            # nowcoder with its CDP path down falls back to the ping: a not-probed ping must not become
            # a False (the old `if ok else False` read None as a failure) nor a True.
            with mock.patch.object(nowcoder_source, "cdp_health", lambda **k: (False, "down")):
                nok, nmsg = nowcoder_source.NowcoderAdapter().health_check()
        self.assertIsNone(nok, nmsg)
        self.assertIn("not verified", nmsg)

    def test_cache_served_checks_return_none_and_peek_the_helpers_own_key(self):
        """Each of these health checks used to call a data helper that answers from OmniSeek's cache
        when it can, and report True from that cached answer. Now the check peeks the cache first and
        says None. The peeked key must be the helper's key: record the key the HELPER reads, then the
        key the CHECK reads, and require them equal."""
        from omniseek.core.sources.api import (layoffs_tracker_source as lt, llm_leaderboard_source as ll,
                                             nowcoder_source as nc, openrouter_rankings_source as orr)
        from omniseek.core.sources.scrape import (ajo_source as aj, conference_deadlines_source as cd,
                                                page_watch_source as pw, xiaoyuzhou_source as xy,
                                                youtube_channels_source as yc)
        from omniseek.core.sources.walled import (bytedance_seed_source as bd, douban_groups_source as db,
                                                zhihu_users_source as zu)
        page_row = {"url": "https://example.invalid/page", "name": "p", "label": "P"}
        user = {"handle": "someone", "display_name": "Someone"}
        cases = [
            ("layoffs_tracker", lt.LayoffsTrackerAdapter(), lambda a: a._rows(), []),
            ("llm_leaderboard", ll.LLMLeaderboardAdapter(), lambda a: a._models(),
             [mock.patch.object(ll.auth, "is_configured", lambda n: True)]),
            ("openrouter_rankings", orr.OpenRouterRankingsAdapter(), lambda a: a._rankings(), []),
            ("ajo", aj.AJOAdapter(), lambda a: a._positions(), []),
            ("conference_deadlines", cd.ConferenceDeadlinesAdapter(), lambda a: a._fetch_confs(), []),
            ("bytedance_seed", bd.BytedanceSeedAdapter(), lambda a: a._fetch_filters_meta(), []),
            ("xiaoyuzhou", xy.XiaoyuzhouAdapter(),
             lambda a: a._fetch_podcast(a._podcasts()[0].get("id"), "x"), []),
            ("youtube_channels", yc.YoutubeChannelsAdapter(),
             lambda a: a._fetch_channel(yc.CHANNELS[2][0], yc.CHANNELS[2][1]), []),
            ("douban_groups", db.DoubanGroupsAdapter(), lambda a: a.search("上海租房", limit=3),
             [mock.patch.object(db, "cdp_health", lambda **k: (True, "ok"))]),
            ("zhihu_users", zu.ZhihuUsersAdapter(),
             lambda a: a._fetch_user_posts(user["handle"], user["display_name"]),
             [mock.patch.object(zu, "cdp_health", lambda **k: (True, "ok")),
              mock.patch.object(zu.ZhihuUsersAdapter, "_load_users", lambda self: [user])]),
            ("page_watch", pw.PageWatchAdapter(), lambda a: a._doc_for(page_row),
             [mock.patch.object(pw.PageWatchAdapter, "_rows", lambda self: [page_row])]),
            ("nowcoder", nc.NowcoderAdapter(), lambda a: a._fetch_job(nc.DEFAULT_JOB_IDS[0], pages=1),
             [mock.patch.object(nc, "cdp_health", lambda **k: (True, "ok"))]),
        ]
        for name, adapter, helper, extra in cases:
            with self.subTest(source=name):
                seen: list = []

                def _rec(key, *a, **k):
                    seen.append(key)
                    return []  # a non-None cached value: the helper answers from it, no network

                patches = [mock.patch.object(cache, "get", _rec), mock.patch.object(cache, "get_docs", _rec),
                           *extra]
                for p in patches:
                    p.start()
                try:
                    helper(adapter)
                    helper_key = seen[0] if seen else None
                    seen.clear()
                    ok, msg = adapter.health_check()
                    check_key = seen[0] if seen else None
                finally:
                    for p in reversed(patches):
                        p.stop()
                self.assertIsNotNone(helper_key, "the helper read no cache key")
                self.assertEqual(helper_key, check_key, "the health check peeks a different key than its helper")
                self.assertIsNone(ok, msg)
                self.assertIn("not probed", msg)


# ── (c)(e)(f) the watchdog ───────────────────────────────────────────────────────────────────────
class HealthProbeRetries(unittest.TestCase):
    """_health_probe: the one retry absorbs a blip for True/False exactly as before; an unverified
    answer is returned at once (a retry would ask nothing new or spend what the first declined)."""

    def _run(self, answers):
        calls: list = []
        sleeps: list = []

        def outcome(_adapter, timeout=None):
            calls.append(1)
            return answers[len(calls) - 1]
        with mock.patch.object(fetcher, "health_check_outcome", outcome), \
                mock.patch.object(infra_jobs.time, "sleep", lambda s: sleeps.append(s)):
            res = infra_jobs._health_probe(SimpleNamespace(name="x"))
        return res, len(calls), len(sleeps)

    def test_unverified_is_returned_at_once(self):
        self.assertEqual(self._run([(None, "not probed", True)]), ((None, "not probed", True), 1, 0))

    def test_timeout_is_retried_and_stays_unmeasured(self):
        self.assertEqual(self._run([(None, "timeout", False), (None, "timeout", False)]),
                         ((None, "timeout", False), 2, 1))

    def test_true_and_false_paths_are_unchanged(self):
        self.assertEqual(self._run([(True, "OK", True)]), ((True, "OK", True), 1, 0))
        self.assertEqual(self._run([(False, "HTTP 500", True), (True, "OK", True)]), ((True, "OK", True), 2, 1))
        self.assertEqual(self._run([(False, "HTTP 500", True), (False, "HTTP 503", True)]),
                         ((False, "HTTP 503", True), 2, 1))


class BoundedOutcome(unittest.TestCase):
    def test_completed_flag_separates_the_two_nones(self):
        class Unverified:
            name = "_tristate_unverified"

            def health_check(self):
                return None, "not probed"

        class Hung:
            name = "_tristate_hung"

            def health_check(self):
                import time
                time.sleep(2)
                return True, "late"

        self.assertEqual(fetcher.health_check_outcome(Unverified(), 1.0), (None, "not probed", True))
        ok, msg, completed = fetcher.health_check_outcome(Hung(), 0.2)
        self.assertIsNone(ok)
        self.assertFalse(completed)
        self.assertIn("timeout", msg)
        self.assertEqual(fetcher.health_check_bounded(Unverified(), 1.0), (None, "not probed"))


ANSWERS = {
    "verified_ok": (True, "OK", True),
    "verified_bad": (False, "HTTP 500", True),
    "unverified_src": (None, "not probed (would spend the metered quota): 200 a month", True),
    "streak_src": (None, "degraded: declared gate busy, not probed this cycle", True),
    "timed_out": (None, "timeout (>25s): health_check did not return", False),
    "bundle_src": (True, "1/2 feeds OK (degraded; dead: dead.example)", True),
}


class _FakeAdapter:
    needs_credentials = False
    explicit_only = False
    description = "fake"

    def __init__(self, name):
        self.name = name

    def search(self, query, limit=10):
        return []

    def fetch_url(self, url):
        return None

    def health_check(self):
        ok, msg, _completed = ANSWERS[self.name]
        return ok, msg


class WatchdogThroughRunSourceHealth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="omniseek-wd-tristate-")
        self.tmpdir = Path(self._tmp.name).resolve()
        self.statefile = self.tmpdir / "health-watchdog-state.json"
        # streak_src arrives with a REAL failure streak (3 consecutive fails, already alerted);
        # unverified_src with one fail. Neither may move on an unverified answer.
        self.statefile.write_text(json.dumps({"fails": {"unverified_src": 1, "streak_src": 3},
                                              "_alerts": {}, "last_status": {}}), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        self.alerts: list[tuple[str, str]] = []

    def _guarded_save(self, real_save):
        def guarded(path, data):
            p = Path(path).resolve()
            if self.tmpdir not in p.parents:
                raise AssertionError(f"STATE ISOLATION BREACH: watchdog write escaped to {p}")
            return real_save(path, data)
        return guarded

    def _run(self, scope: str = "noncdp"):
        adapters = {n: SimpleNamespace(name=n, explicit_only=False) for n in ANSWERS}
        with mock.patch.object(infra_jobs, "_HEALTH_STATE", self.statefile), \
                mock.patch.object(infra_jobs, "_save_state", self._guarded_save(infra_jobs._save_state)), \
                mock.patch.object(infra_jobs, "_health_probe", lambda a: ANSWERS[a.name]), \
                mock.patch.object(infra_jobs, "_heal_cdp_chrome", lambda: []), \
                mock.patch.object(infra_jobs, "_CDP_INSTANCES", {}), \
                mock.patch.object(infra_jobs, "_alert",
                                  lambda title, body="", **kw: self.alerts.append((title, body))), \
                mock.patch.object(server, "load_sources", lambda: None), \
                mock.patch.object(fetcher, "all_adapter_names", lambda: sorted(adapters)), \
                mock.patch.object(fetcher, "get_adapter", adapters.get), \
                mock.patch.object(fetcher, "retired_reason", lambda a: ""):
            summary = infra_jobs.run_source_health(scope=scope)
        return summary, json.loads(self.statefile.read_text(encoding="utf-8"))

    def test_an_unverified_answer_moves_no_counter_and_sends_no_alert(self):
        for _ in range(infra_jobs.N_CONSECUTIVE + 1):
            _summary, saved = self._run()
        self.assertEqual(saved["fails"]["unverified_src"], 1, "an unverified answer moved the counter")
        self.assertEqual(saved["fails"]["streak_src"], 3, "an unverified answer erased a real fail streak")
        mentioned = " ".join(t + b for t, b in self.alerts)
        self.assertNotIn("unverified_src", mentioned)
        self.assertNotIn("streak_src", mentioned, "no recovery and no re-nag on an unverified answer")

    def test_unverified_and_unmeasured_are_recorded_apart(self):
        _summary, saved = self._run()
        self.assertEqual(sorted(saved["unverified"]), ["streak_src", "unverified_src"])
        self.assertIn("not probed", saved["unverified"]["unverified_src"])
        self.assertEqual(sorted(saved["unmeasured"]), ["timed_out"])
        self.assertIsNone(saved["last_status"]["unverified_src"])

    def test_the_full_lane_writes_the_same_maps(self):
        _summary, saved = self._run(scope="all")
        self.assertEqual(sorted(saved["unverified"]), ["streak_src", "unverified_src"])
        self.assertEqual(sorted(saved["unmeasured"]), ["timed_out"])

    def test_a_verified_answer_clears_an_old_unverified_row(self):
        self._run()
        saved_answer = ANSWERS["unverified_src"]
        try:
            ANSWERS["unverified_src"] = (True, "OK", True)
            _summary, saved = self._run()
        finally:
            ANSWERS["unverified_src"] = saved_answer
        self.assertNotIn("unverified_src", saved["unverified"])
        self.assertEqual(saved["fails"]["unverified_src"], 0)

    def test_the_summary_counts_unverified_apart_from_healthy(self):
        summary, _saved = self._run()
        self.assertEqual((summary["healthy"], summary["failed"], summary["unverified"], summary["unmeasured"],
                          summary["probed"]), (2, 1, 2, 1, 6))

    def test_a_degraded_word_on_an_unverified_answer_is_not_member_rot(self):
        _summary, saved = self._run()
        self.assertEqual(saved["degraded"], ["bundle_src"])
        rot = [b for t, b in self.alerts if t.startswith("源降级")]
        self.assertEqual(len(rot), 1)
        self.assertIn("bundle_src", rot[0])
        self.assertNotIn("streak_src", rot[0])

    def test_true_and_false_paths_are_unchanged(self):
        for _ in range(infra_jobs.N_CONSECUTIVE):
            _summary, saved = self._run()
        self.assertEqual(saved["fails"]["verified_ok"], 0)
        self.assertEqual(saved["fails"]["verified_bad"], infra_jobs.N_CONSECUTIVE)
        down = [b for t, b in self.alerts if t.startswith("源故障")]
        self.assertEqual(len(down), 1)
        self.assertIn("verified_bad", down[0])
        self.assertIs(saved["last_status"]["verified_ok"], True)
        self.assertIs(saved["last_status"]["verified_bad"], False)

    def test_list_sources_reports_unverified_and_keeps_down_for_a_real_streak(self):
        for _ in range(infra_jobs.N_CONSECUTIVE):
            self._run()
        fakes = {n: _FakeAdapter(n) for n in ANSWERS}
        with mock.patch.object(fetcher, "_WATCHDOG_STATE", self.statefile), \
                mock.patch.object(fetcher, "_explicit_only_overrides", lambda: {}), \
                mock.patch.dict(fetcher._adapters, fakes, clear=True):
            health = {e["name"]: e["health"] for e in fetcher.list_sources(verbose=True)}
        self.assertEqual(health, {"verified_ok": "ok", "verified_bad": "down", "unverified_src": "unverified",
                                  "streak_src": "down", "timed_out": "unmeasured", "bundle_src": "ok"})


# ── (d)(e) omniseek_sources(check_health=True) and fetcher.health_check ─────────────────────────────────
class LiveProbeOutput(unittest.TestCase):
    def test_healthy_is_true_false_or_null_never_a_collapsed_bool(self):
        fakes = {n: _FakeAdapter(n) for n in ("verified_ok", "verified_bad", "unverified_src")}
        with mock.patch.dict(fetcher._adapters, fakes, clear=True), \
                mock.patch.object(fetcher, "_explicit_only_overrides", lambda: {}), \
                mock.patch.object(fetcher, "_WATCHDOG_STATE", Path(tempfile.gettempdir()) / "_absent_wd.json"):
            entries = {e["name"]: e for e in fetcher.list_sources(check_health=True, verbose=True)}
            flat = fetcher.health_check()
        self.assertIs(entries["verified_ok"]["healthy"], True)
        self.assertIs(entries["verified_bad"]["healthy"], False)
        self.assertIsNone(entries["unverified_src"]["healthy"])
        self.assertIn("not probed", entries["unverified_src"]["status"])
        self.assertIn('"healthy": null', json.dumps(entries["unverified_src"]))
        self.assertEqual({n: v["healthy"] for n, v in flat.items()},
                         {"verified_ok": True, "verified_bad": False, "unverified_src": None})
        healthy_count = sum(1 for e in entries.values() if e["healthy"] is True)
        self.assertEqual(healthy_count, 1)

    def test_the_tool_description_says_what_null_means(self):
        doc = server.omniseek_sources.__doc__ or ""
        self.assertIn("null = NOT verified", doc)
        self.assertIn("unverified", doc)


# ── (e) the curator audit ────────────────────────────────────────────────────────────────────────
class CuratorAuditKeepsUnverifiedApart(unittest.TestCase):
    def test_unverified_has_no_live_evidence_and_ok_does(self):
        self.assertIn("unverified", source_audit._NO_LIVE_EVIDENCE)
        self.assertIn("unknown", source_audit._NO_LIVE_EVIDENCE)
        self.assertNotIn("ok", source_audit._NO_LIVE_EVIDENCE)
        self.assertNotIn("down", source_audit._NO_LIVE_EVIDENCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
