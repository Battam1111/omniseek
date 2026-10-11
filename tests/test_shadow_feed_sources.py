"""Offline tests for the shadow-run sources (cubox, github_starred) and the shadow file.

No network: Cubox egress (http.post_json), the credential read (auth.load) and GitHub egress
(_github.get_json) are patched; every state path goes to a temp dir through the env overrides.
Golden fixtures: tests/fixtures/cubox_filter_page.json (synthetic, see its _provenance) and
tests/fixtures/github_starred_page.json (one recorded real page, trimmed).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omniseek.core import cache as _cache
from omniseek.core import fetcher, shadow_feed, sensor as sensor_mod
from omniseek.core.normalize import Document
from omniseek.core.sources.api import cubox_source as cx
from omniseek.core.sources.api import github_starred_source as gs

FIX = Path(__file__).parent / "fixtures"
CUBOX = json.loads((FIX / "cubox_filter_page.json").read_text(encoding="utf-8"))
STARS = json.loads((FIX / "github_starred_page.json").read_text(encoding="utf-8"))["page"]
NOW = dt.datetime(2026, 10, 10, 6, 0, tzinfo=dt.timezone.utc)
TOKEN = {"token": "https://cubox.pro/c/api/save/abc123TESTKEY"}


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shadow_feed_test_"))
        self.env = mock.patch.dict(os.environ, {
            cx.ENV_BUDGET: str(self.tmp / "cubox_budget.json"),
            cx.ENV_PULLER_STATE: str(self.tmp / "puller_state.json"),
            shadow_feed.ENV_DIR: str(self.tmp / "shadow"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for mod in (cx, gs):
            p = mock.patch.object(mod, "_now", return_value=NOW)
            p.start()
            self.addCleanup(p.stop)
        # a fresh, private result cache per test (the base caches non-empty results on disk)
        p = mock.patch.object(_cache, "CACHE_DIR", self.tmp / "cache")
        p.start()
        self.addCleanup(p.stop)
        self.cubox = fetcher.get_adapter("cubox")
        self.stars = fetcher.get_adapter("github_starred")


def _cubox_answers(calls):
    def post_json(url, json=None, timeout=None, headers=None):  # noqa: A002
        calls.append({"url": url, "body": dict(json or {}), "headers": dict(headers or {})})
        page = CUBOX["archived"] if (json or {}).get("archived") else CUBOX["unarchived"]
        return copy.deepcopy(page)
    return post_json


class CuboxSourceTests(_Env):
    def test_facets_and_flags(self):
        a = self.cubox
        self.assertIsNotNone(a)
        self.assertTrue(fetcher._explicit_only_reason(a))
        self.assertEqual((a.kind, a.domains, a.regions, a.modes), ("stream", ["bookmarks"], [], ["MONITOR", "RECALL"]))
        self.assertTrue(a.needs_credentials)
        self.assertTrue(a.shadow_feed)
        self.assertGreaterEqual(a.sensor_window, 2 * cx.PAGE)

    def test_no_credential_returns_empty_with_reason_and_sends_nothing(self):
        calls = []
        with mock.patch.object(cx.auth, "load", return_value=None), \
                mock.patch.object(cx.http, "post_json", side_effect=_cubox_answers(calls)), \
                mock.patch.object(cx, "_note") as note:
            self.assertEqual(self.cubox._raw_fetch("*", 10), [])
        self.assertEqual(calls, [])
        self.assertIn("no credential", note.call_args[0][0])
        self.assertIn("cubox.json", note.call_args[0][0])
        with mock.patch.object(cx.auth, "load", return_value=None):
            ok, msg = self.cubox.health_check()
        self.assertIs(ok, False)
        self.assertIn("no credential", msg)

    def test_health_with_credential_spends_nothing(self):
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=AssertionError("egress")):
            ok, msg = self.cubox.health_check()
        self.assertIsNone(ok)
        self.assertIn("not probed", msg)

    def test_golden_list_both_archive_states(self):
        calls = []
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=_cubox_answers(calls)):
            raw = self.cubox._raw_fetch("*", 800)
        self.assertEqual([c["body"]["archived"] for c in calls], [False, True])
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer " + "abc123TESTKEY")
        self.assertTrue(calls[0]["url"].endswith("/c/api/cli/card/filter"))
        # the 2026-09-01 card is older than the 7-day lookback and is dropped
        self.assertEqual([c["id"] for c in raw],
                         ["7330000000000000003", "7330000000000000002", "7330000000000000001"])
        docs = [self.cubox._to_document(c) for c in raw]
        d0, d1, d2 = docs
        self.assertEqual((d0.source, d0.source_id, d0.url), ("cubox", "7330000000000000003",
                                                               "https://example.org/blog/test-time-compute"))
        self.assertEqual(d0.title, "Scaling test-time compute")
        self.assertEqual(d0.date, dt.datetime(2026, 10, 9, 13, 15, 2, 481000, tzinfo=dt.timezone.utc))
        self.assertEqual(d0.tags, ["AI", "reasoning"])
        self.assertEqual(d0.metadata["folder"], "Inbox")
        self.assertFalse(d0.metadata["archived"])
        self.assertEqual(d1.title, "微信公众号文章标题")       # empty title falls back to article_title
        self.assertEqual(d1.tags, ["待读/读"])
        self.assertEqual(d1.metadata["folder"], "Research/Papers")
        self.assertEqual(d2.url, "https://cubox.pro/web/card/7330000000000000001")  # no url -> card link
        self.assertTrue(d2.metadata["archived"])
        self.assertIsNone(d2.metadata["folder"])
        b = cx.load_budget()
        self.assertEqual(b["calls"], 2)
        self.assertIsInstance(b["last_run_at"], float)

    def test_paging_uses_last_card_id(self):
        full = {"code": 200, "data": [
            {"id": f"9{i:03d}", "url": f"https://e.org/{i}", "create_time": "2026-10-09T10:00:00.000+0800"}
            for i in range(cx.PAGE)]}
        calls = []

        def post_json(url, json=None, timeout=None, headers=None):  # noqa: A002
            calls.append(dict(json))
            if json.get("archived"):
                return {"code": 200, "data": []}
            return copy.deepcopy(full) if "last_card_id" not in json else {"code": 200, "data": []}
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=post_json):
            raw = self.cubox._raw_fetch("*", 800)
        self.assertEqual(len(raw), cx.PAGE)
        self.assertEqual(calls[1].get("last_card_id"), f"9{cx.PAGE - 1:03d}")
        self.assertEqual(len(calls), 3)

    def test_two_hour_gate(self):
        cx.save_budget({"day": cx._today(), "calls": 2, "last_run_at": time.time() - 600})
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=AssertionError("egress")), \
                mock.patch.object(cx, "_note") as note:
            self.assertEqual(self.cubox._raw_fetch("*", 10), [])
        self.assertIn("2 hours", note.call_args[0][0])

    def test_daily_cap_gate(self):
        cx.save_budget({"day": cx._today(), "calls": cx.DAILY_CALL_CAP, "last_run_at": None})
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=AssertionError("egress")), \
                mock.patch.object(cx, "_note") as note:
            self.assertEqual(self.cubox._raw_fetch("*", 10), [])
        self.assertIn("daily cap", note.call_args[0][0])

    def test_yesterdays_budget_resets(self):
        cx.save_budget({"day": "2000-01-01", "calls": 999, "capped": True, "last_run_at": 1.0})
        b = cx.load_budget()
        self.assertEqual((b["day"], b["calls"], b.get("capped")), (cx._today(), 0, None))

    def _puller(self, **budget):
        (self.tmp / "puller_state.json").write_text(json.dumps({"budget": {"day": cx._today(), **budget}}))

    def test_puller_capped_gate(self):
        self._puller(calls=120, capped_at=120)
        self.assertIn("digest puller hit", cx.gate_reason(cx.load_budget()))

    def test_estimated_remaining_gate(self):
        self._puller(calls=cx.ASSUMED_DAILY_QUOTA - cx.STOP_BELOW_REMAINING + 1)
        self.assertIn("estimated remaining", cx.gate_reason(cx.load_budget()))
        self._puller(calls=cx.ASSUMED_DAILY_QUOTA - cx.STOP_BELOW_REMAINING - 5)
        self.assertIsNone(cx.gate_reason(cx.load_budget()))

    def test_puller_state_of_another_day_is_ignored(self):
        (self.tmp / "puller_state.json").write_text(json.dumps({"budget": {"day": "2000-01-01", "calls": 499}}))
        self.assertIsNone(cx.gate_reason(cx.load_budget()))

    def test_no_default_puller_state_means_no_puller(self):
        # A build with no default puller path (None) and no override reads no puller and is not gated by one.
        with mock.patch.object(cx, "_DEFAULT_PULLER_STATE", None), \
                mock.patch.dict(os.environ, {cx.ENV_PULLER_STATE: ""}):
            self.assertIsNone(cx.puller_budget())
            self.assertIsNone(cx.gate_reason(cx.load_budget()))

    def test_quota_answer_marks_the_day_capped(self):
        calls = []

        def post_json(url, json=None, timeout=None, headers=None):  # noqa: A002
            calls.append(1)
            return copy.deepcopy(CUBOX["quota_used_up"])
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=post_json), \
                mock.patch.object(cx, "_note") as note:
            self.assertEqual(self.cubox._raw_fetch("*", 10), [])
        self.assertEqual(len(calls), 1)
        self.assertIn("-3030", note.call_args[0][0])
        b = cx.load_budget()
        self.assertTrue(b["capped"])
        b["last_run_at"] = None
        cx.save_budget(b)
        self.assertIn("-3030", cx.gate_reason(cx.load_budget()))

    def test_search_one_cache_key_and_filter(self):
        calls = []
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=_cubox_answers(calls)):
            all_docs = self.cubox.search("*", limit=50)
            again = self.cubox.search("", limit=1)
            hit = self.cubox.search("verifier", limit=10)
        self.assertEqual(len(calls), 2)               # one run (two archive states), then cache
        self.assertEqual(len(all_docs), 3)
        self.assertEqual(len(again), 1)
        self.assertEqual([d.source_id for d in hit], ["7330000000000000003"])

    def test_parse_time_offsets(self):
        self.assertEqual(cx.parse_time("2026-09-24T20:32:25.123+0800"),
                         dt.datetime(2026, 9, 24, 12, 32, 25, 123000, tzinfo=dt.timezone.utc))
        self.assertIsNone(cx.parse_time("not a time"))
        self.assertIsNone(cx.parse_time(None))


class GitHubStarredTests(_Env):
    def test_facets_and_flags(self):
        a = self.stars
        self.assertIsNotNone(a)
        self.assertTrue(fetcher._explicit_only_reason(a))
        self.assertEqual((a.kind, a.domains, a.modes), ("stream", ["bookmarks", "code"], ["MONITOR", "RECALL"]))
        self.assertTrue(a.shadow_feed)

    def test_golden_page(self):
        seen = []

        def get_json(path, params=None, headers=None, timeout=None):
            seen.append((path, dict(params or {}), dict(headers or {})))
            return copy.deepcopy(STARS)
        with mock.patch.object(gs._github, "get_json", side_effect=get_json), \
                mock.patch.dict(os.environ, {gs.ENV_USER: ""}):
            raw = self.stars._raw_fetch("*", 300)
        self.assertEqual(seen[0][0], "/users/Battam1111/starred")
        self.assertEqual(seen[0][1], {"per_page": 100, "sort": "created", "direction": "desc", "page": 1})
        self.assertEqual(seen[0][2]["Accept"], "application/vnd.github.star+json")
        self.assertEqual(len(seen), 1)                 # short page: no second request
        docs = [self.stars._to_document(r) for r in raw]
        self.assertEqual([d.source_id for d in docs],
                         ["verl-project/uni-agent", "microsoft/CUAWright", "openai/math"])
        d = docs[0]
        self.assertEqual(d.url, "https://github.com/verl-project/uni-agent")
        self.assertEqual(d.date, dt.datetime(2026, 10, 8, 3, 20, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(d.author, "verl-project")
        self.assertEqual(d.metadata["starred_at"], "2026-10-08T03:20:12Z")
        self.assertEqual(d.metadata["starred_by"], "Battam1111")

    def test_thirty_day_cut_and_paging(self):
        recent = {"starred_at": "2026-10-01T00:00:00Z", "repo": {"full_name": "a/new", "html_url": "https://github.com/a/new"}}
        old = {"starred_at": "2026-08-01T00:00:00Z", "repo": {"full_name": "b/old", "html_url": "https://github.com/b/old"}}
        pages = {1: [recent] * gs.PER_PAGE, 2: [recent, old, recent]}
        seen = []

        def get_json(path, params=None, headers=None, timeout=None):
            seen.append(params["page"])
            return copy.deepcopy(pages[params["page"]])
        with mock.patch.object(gs._github, "get_json", side_effect=get_json):
            raw = self.stars._raw_fetch("*", 300)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(len(raw), gs.PER_PAGE + 1)
        self.assertNotIn("b/old", {r["repo"]["full_name"] for r in raw})

    def test_user_override(self):
        seen = []
        with mock.patch.object(gs._github, "get_json", side_effect=lambda p, **k: seen.append(p) or []), \
                mock.patch.dict(os.environ, {gs.ENV_USER: "someone"}):
            self.stars._raw_fetch("*", 300)
        self.assertEqual(seen, ["/users/someone/starred"])

    def test_first_page_failure_raises_into_base_fail_open(self):
        with mock.patch.object(gs._github, "get_json", return_value=None):
            with self.assertRaises(RuntimeError):
                self.stars._raw_fetch("*", 300)
            self.assertEqual(self.stars.search("*", limit=5), [])

    def test_missing_starred_at_refuses_the_page(self):
        bare = [{"repo": {"full_name": "x/y"}}]
        with mock.patch.object(gs._github, "get_json", return_value=bare):
            with self.assertRaises(RuntimeError):
                self.stars._raw_fetch("*", 300)


def _doc(source, sid, url, date=None, title="t"):
    return Document(source=source, source_id=sid, url=url, title=title, content="", date=date)


class ShadowFileTests(_Env):
    def _sensor(self, sid="s1"):
        return mock.Mock(id=sid)

    def test_format_and_append(self):
        run1 = "2026-10-10T04:00:00+00:00"
        d = _doc("cubox", "1", "https://e.org/a", dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc), "标题")
        out = shadow_feed.record_new(self._sensor(), [d, _doc("arxiv", "2", "https://arxiv.org/abs/2")], run1, True)
        self.assertEqual(out, {"cubox": 1})        # arxiv did not opt in: nothing written for it
        out = shadow_feed.record_new(self._sensor(), [_doc("cubox", "3", "https://e.org/b")],
                                       "2026-10-11T04:00:00+00:00", False)
        self.assertEqual(out, {"cubox": 1})
        path = self.tmp / "shadow" / "cubox.jsonl"
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[0], {
            "schema": 1, "source": "cubox", "source_id": "1", "url": "https://e.org/a",
            "first_seen_at": run1, "item_time": "2026-10-09T12:00:00+00:00", "title": "标题",
            "sensor_id": "s1", "first_run": True})
        self.assertEqual((lines[1]["source_id"], lines[1]["first_run"], lines[1]["item_time"]), ("3", False, None))
        self.assertFalse((self.tmp / "shadow" / "arxiv.jsonl").exists())
        self.assertEqual(sorted(p.name for p in (self.tmp / "shadow").iterdir()),
                         ["cubox.jsonl", "cubox.jsonl.lock"])   # no temp file left behind

    def test_write_failure_is_swallowed(self):
        with mock.patch.object(shadow_feed, "append_lines", side_effect=OSError("disk full")):
            self.assertEqual(shadow_feed.record_new(self._sensor(), [_doc("cubox", "1", "u")], "t", False), {})

    def test_default_dir(self):
        with mock.patch.dict(os.environ, {shadow_feed.ENV_DIR: ""}):
            self.assertEqual(shadow_feed.shadow_path("cubox"),
                             Path.home() / ".omniseek" / "state" / "shadow_feed" / "cubox.jsonl")


class SensorIntegrationTests(_Env):
    def test_run_sensor_windows_and_writes_shadow(self):
        store = sensor_mod.SensorStore(path=self.tmp / "sensors.json")
        s = store.create(query="*", sources=["cubox"], schedule="daily", notify=False)
        calls = []
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=_cubox_answers(calls)):
            real = fetcher.search_ranked
            seen = {}

            def spy(*a, **k):
                seen.update(k)
                return real(*a, **k)
            with mock.patch.object(fetcher, "search_ranked", side_effect=spy):
                r1 = sensor_mod.run_sensor(s, store)
        self.assertEqual(seen.get("per_source"), self.cubox.sensor_window)
        self.assertEqual(r1["new_count"], 3)
        path = self.tmp / "shadow" / "cubox.jsonl"
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(x["source_id"] for x in lines),
                         ["7330000000000000001", "7330000000000000002", "7330000000000000003"])
        self.assertTrue(all(x["first_run"] and x["sensor_id"] == s.id for x in lines))
        self.assertTrue(all(x["first_seen_at"] == store.get(s.id).last_run_at for x in lines))

        # second run inside the 2 h window: the cached list, nothing new, nothing appended
        r2 = sensor_mod.run_sensor(store.get(s.id), store)
        self.assertEqual(r2["new_count"], 0)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_empty_runs_before_credential_do_not_spend_the_seed(self):
        store = sensor_mod.SensorStore(path=self.tmp / "sensors.json")
        s = store.create(query="*", sources=["cubox"], schedule="daily", notify=False)
        with mock.patch.object(cx.auth, "load", return_value=None):
            r0 = sensor_mod.run_sensor(s, store)
        self.assertEqual(r0["new_count"], 0)
        self.assertFalse((self.tmp / "shadow" / "cubox.jsonl").exists())
        calls = []
        with mock.patch.object(cx.auth, "load", return_value=TOKEN), \
                mock.patch.object(cx.http, "post_json", side_effect=_cubox_answers(calls)):
            r1 = sensor_mod.run_sensor(store.get(s.id), store)
        self.assertEqual(r1["new_count"], 3)
        lines = [json.loads(x) for x in
                 (self.tmp / "shadow" / "cubox.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(lines and all(x["first_run"] for x in lines))

    def test_sensor_without_window_keeps_old_call(self):
        store = sensor_mod.SensorStore(path=self.tmp / "sensors.json")
        s = store.create(query="x", sources=["arxiv"], schedule="daily")
        seen = []
        with mock.patch.object(fetcher, "search_ranked",
                               side_effect=lambda q, sources=None, limit=15: (seen.append(limit) or ([], {}))):
            sensor_mod.run_sensor(s, store)
        self.assertEqual(seen, [15])
        self.assertFalse((self.tmp / "shadow").exists())


if __name__ == "__main__":
    unittest.main()
