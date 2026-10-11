"""field_skeleton(source="s2") must not silently demote a seed whose own record S2 did not return.

OmniSeek P6 run R08 (2026-10-11): seeds=[Leviathan 2022 paperId, Chen 2023 paperId]; S2 answered
the Leviathan record call with HTTP 429 on all three tries while its /references and /citations a
second later returned 200. The map came back partial:true, Leviathan is_seed:false, no reason, and
the 6h cache kept it. These tests pin the repaired contract offline (no network): a throttled seed is
retried once after the other seeds, a still-missing S2 paperId seed is kept as a seed with its edges,
every missing seed is NAMED with its reason, a transient miss is not cached, and an arXiv-form seed
keys its edges on the resolved paperId."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import _s2, cache, cartographer

LEV = "d8e9f8c8a37cb4cd26b92ad0d942d641cd512644"
CHEN = "a1f8082505c7e90b0a033e1b9da0a97d67aad66c"


class _P:
    def __init__(self, pid, title=None):
        self.paperId = pid
        self.title = title or f"paper {pid[:6]}"
        self.year = 2023
        self.publicationDate = "2023-01-01"
        self.citationCount = 5
        self.authors = []
        self.externalIds = {}
        self.fieldsOfStudy = []


class _E:
    def __init__(self, paper):
        self.paper = paper
        self.intents = []
        self.isInfluential = False
        self.contextsWithIntent = []
        self.contexts = []


class _FakeS2:
    """Scripted S2: fetch_paper answers from a per-id queue of (record, reason) results."""

    def __init__(self, script, refs=None, cites=None):
        self.script = {k: list(v) for k, v in script.items()}
        self.refs = refs or {}
        self.cites = cites or {}
        self.fetch_calls: list[str] = []
        self.edge_calls: list[str] = []

    def fetch_paper(self, sid, fields=None):
        self.fetch_calls.append(sid)
        q = self.script.get(sid) or [(None, "not_found")]
        return q.pop(0) if len(q) > 1 else q[0]

    def get_paper_references(self, sid, cap, fields=None):
        self.edge_calls.append(("refs", sid))
        return [_E(p) for p in self.refs.get(sid, [])]

    def get_paper_citations(self, sid, cap, fields=None):
        self.edge_calls.append(("cites", sid))
        return [_E(p) for p in self.cites.get(sid, [])]


class SkeletonSeedResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(cache, "CACHE_DIR", Path(self._tmp.name))
        p.start()
        self.addCleanup(p.stop)
        for name, val in (("breaker_open", lambda: False), ("recently_throttled", lambda: False)):
            p = mock.patch.object(_s2, name, val)
            p.start()
            self.addCleanup(p.stop)

    def _patch(self, fake):
        stack = [mock.patch.object(_s2, n, getattr(fake, n))
                 for n in ("fetch_paper", "get_paper_references", "get_paper_citations")]
        stack.append(mock.patch.object(cartographer, "_graph_tap", lambda *a, **k: None))
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def test_throttled_seed_recovers_on_the_deferred_pass(self):
        fake = _FakeS2({LEV: [(None, "rate_limited"), (_P(LEV, "Leviathan"), None)],
                        CHEN: [(_P(CHEN, "Chen"), None)]},
                       cites={LEV: [_P("c" * 40)]})
        self._patch(fake)
        out = cartographer.field_skeleton(seeds=[LEV, CHEN], source="s2", fresh=True)
        self.assertEqual(fake.fetch_calls, [LEV, CHEN, LEV])
        seeds = {n["id"] for n in out["nodes"] if n["is_seed"]}
        self.assertEqual(seeds, {LEV, CHEN})
        self.assertEqual(out["_meta"]["seeds_resolved"], 2)
        self.assertFalse(out["_meta"]["partial"])
        self.assertNotIn("seeds_unresolved", out["_meta"])

    def test_still_throttled_paperid_seed_is_kept_named_and_not_cached(self):
        ref = _P("r" * 40, "shared reference")
        fake = _FakeS2({LEV: [(None, "rate_limited")], CHEN: [(_P(CHEN, "Chen"), None)]},
                       refs={LEV: [ref], CHEN: [ref]}, cites={LEV: [_P("c" * 40)]})
        self._patch(fake)
        out = cartographer.field_skeleton(seeds=[LEV, CHEN], source="s2")
        node = {n["id"]: n for n in out["nodes"]}
        self.assertTrue(node[LEV]["is_seed"], "a throttled seed must stay a seed")
        self.assertIn([LEV, ref.paperId], out["edges"])
        self.assertIn(["c" * 40, LEV], out["edges"])
        meta = out["_meta"]
        self.assertTrue(meta["partial"])
        self.assertEqual(meta["seeds_resolved"], 1)
        self.assertEqual(meta["seeds_unresolved"],
                         [{"id": LEV, "reason": "rate_limited", "kept_as_seed": True,
                           "has_metadata": False}])
        self.assertIn(LEV, out["note"])
        self.assertIn("rate_limited", out["note"])
        # transient: the next call re-assembles instead of serving this map from the 6h cache
        fake2 = _FakeS2({LEV: [(_P(LEV, "Leviathan"), None)], CHEN: [(_P(CHEN, "Chen"), None)]})
        self._patch(fake2)
        out2 = cartographer.field_skeleton(seeds=[LEV, CHEN], source="s2")
        self.assertEqual(fake2.fetch_calls, [LEV, CHEN])
        self.assertNotIn("seeds_unresolved", out2["_meta"])

    def test_not_found_seed_is_named_and_skips_its_edge_calls(self):
        fake = _FakeS2({CHEN: [(_P(CHEN, "Chen"), None)]})
        self._patch(fake)
        out = cartographer.field_skeleton(seeds=[CHEN, "x"], source="s2", fresh=True)
        self.assertEqual(fake.fetch_calls, [CHEN, "x"])
        self.assertNotIn(("refs", "x"), fake.edge_calls)
        self.assertNotIn(("cites", "x"), fake.edge_calls)
        self.assertEqual(out["_meta"]["seeds_unresolved"],
                         [{"id": "x", "reason": "not_found", "kept_as_seed": False,
                           "has_metadata": False}])
        self.assertTrue(out["_meta"]["partial"])

    def test_arxiv_form_seed_keys_edges_on_resolved_paperid(self):
        pid = "e" * 40
        ref, citer = _P("r" * 40), _P("c" * 40)
        fake = _FakeS2({"2211.17192": [(_P(pid, "Leviathan"), None)]},
                       refs={"2211.17192": [ref]}, cites={"2211.17192": [citer]})
        self._patch(fake)
        out = cartographer.field_skeleton(seeds=["2211.17192"], source="s2", fresh=True)
        self.assertIn([pid, ref.paperId], out["edges"])
        self.assertIn([citer.paperId, pid], out["edges"])
        self.assertEqual(out["_meta"]["seeds_resolved"], 1)

    def test_fetch_paper_reason_codes(self):
        class ObjectNotFoundException(Exception):
            pass

        self.assertEqual(_s2.failure_reason(ObjectNotFoundException("Paper with id x not found")),
                         "not_found")
        self.assertEqual(_s2.failure_reason(_s2.S2Down("circuit open")), "circuit_open")
        self.assertEqual(_s2.failure_reason(ConnectionRefusedError("HTTP status 429 Too Many Requests")),
                         "rate_limited")
        self.assertEqual(_s2.failure_reason(ValueError("boom")), "error:ValueError")


if __name__ == "__main__":
    unittest.main()
