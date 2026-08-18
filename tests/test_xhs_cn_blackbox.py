"""Contract for the xhs_cn black box (2026-08-12).

Two failures survived three fix attempts unexplained (the recurring signed 461, and the browser
path silently falling through to it) for one reason: the evidence was never written down. This
holds the recorder to the two things that make it worth having, namely that it captures enough to
NAME a cause, and that it never captures a secret while doing so.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from omniseek.core.sources.walled import xiaohongshu_cn_source as X

SECRET_COOKIE = "ACW-SECRET-VALUE-0a4ad9c9"
SECRET_TOKEN = "XSEC-SECRET-VALUE-AB7F1HTM"


class _Resp:
    def __init__(self, status=461, headers=None, text="<html>verify</html>"):
        self.status_code = status
        self.headers = headers or {"content-type": "text/html", "server": "nginx",
                                   "set-cookie": f"acw_tc={SECRET_COOKIE}; path=/"}
        self.text = text


class BlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "xhs-cn-incidents.jsonl"
        self.p = [patch.object(X, "_INCIDENT_PATH", self.log),
                  patch.object(X, "_cookies", {"acw_tc": SECRET_COOKIE, "web_session": "sess-secret"}),
                  patch.object(X, "_cookie_exp", {"acw_tc": 4102444800.0}),
                  patch.object(X, "_cookies_at", 1.0)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def _rows(self):
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _guard461(self):
        params = {"note_id": "abc", "xsec_token": SECRET_TOKEN, "cursor": ""}
        ev = X._response_evidence(_Resp(), "/api/sns/web/v2/comment/page", "GET", params)
        with self.assertRaises(X.XhsRiskSignal):
            X._guard(461, {}, ev)

    # ── it captures enough to name a cause ────────────────────────────────────
    def test_a_461_records_which_endpoint_and_whether_a_token_rode_along(self):
        old = (X._signed_tripped_until, X._signed_trip_streak, X._last_signed_signal)
        try:
            self._guard461()
        finally:
            X._signed_tripped_until, X._signed_trip_streak, X._last_signed_signal = old
        row = self._rows()[-1]
        self.assertEqual(row["kind"], "signed_http_461")
        # THE discriminator the three unexplained recurrences lacked: WHICH call failed.
        self.assertEqual(row["path"], "/api/sns/web/v2/comment/page")
        self.assertEqual(row["method"], "GET")
        self.assertTrue(row["has_xsec_token"])
        self.assertIn("xsec_token", row["param_keys"])
        self.assertEqual(row["status"], 461)
        self.assertIn("server", row["resp_headers"])
        self.assertTrue(row["cookies"]["acw_tc_present"])
        self.assertIn("acw_tc", row["cookies"]["cookie_names"])
        self.assertIsNotNone(row["cookies"]["acw_tc_ttl_s"])

    def test_a_browser_fallthrough_records_the_exception_it_used_to_swallow(self):
        X._record_incident("browser_search_error", exc_type="TimeoutError",
                           exc="Timeout 85000ms exceeded", flow="search")
        row = self._rows()[-1]
        self.assertEqual(row["exc_type"], "TimeoutError")
        self.assertIn("85000ms", row["exc"])

    # ── and it never captures a secret ────────────────────────────────────────
    def test_no_cookie_or_token_VALUE_ever_reaches_the_file(self):
        old = (X._signed_tripped_until, X._signed_trip_streak, X._last_signed_signal)
        try:
            self._guard461()
        finally:
            X._signed_tripped_until, X._signed_trip_streak, X._last_signed_signal = old
        raw = self.log.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_COOKIE, raw, "an acw_tc value leaked into the black box")
        self.assertNotIn(SECRET_TOKEN, raw, "an xsec_token value leaked into the black box")
        self.assertNotIn("sess-secret", raw, "a web_session value leaked into the black box")
        # ... while still recording that those cookies were PRESENT, which is the useful half.
        self.assertIn("web_session", raw)

    # ── and it can never break the flight it records ──────────────────────────
    def test_an_unwritable_path_never_raises(self):
        with patch.object(X, "_INCIDENT_PATH", Path(self.tmp.name) / "nodir"):
            (Path(self.tmp.name) / "nodir").mkdir()          # a directory where the file should be
            X._record_incident("selftest")                    # must not raise

    def test_a_broken_response_object_still_yields_a_usable_row(self):
        ev = X._response_evidence(object(), "/api/sns/web/v1/feed", "POST", {"a": 1})
        self.assertEqual(ev["path"], "/api/sns/web/v1/feed")
        self.assertEqual(ev["method"], "POST")

    def test_the_log_is_size_bounded(self):
        with patch.object(X, "_INCIDENT_MAX_BYTES", 400):
            with patch.object(X, "_INCIDENT_KEEP_LINES", 3):
                for i in range(60):
                    X._record_incident("selftest", i=i, pad="x" * 50)
        self.assertLess(self.log.stat().st_size, 20_000)
        self.assertTrue(self._rows())

    def test_a_healthy_200_records_nothing(self):
        X._guard(200, {"code": 0, "data": {}}, {"path": "/x"})
        self.assertFalse(self.log.exists() and self.log.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
