"""OpenReview full text through the logged-in session (2026-10-10), offline.

Every upstream is a stub (no network, no real credential). Pins:

  (a) a /forum?id= or /pdf?id= link with a working session returns the PDF's whole text (the
      fixture below is a tiny valid PDF), with the abstract kept in metadata, and the PDF is
      asked from the attachment endpoint the official client uses (GET /attachment, id + name=pdf);
  (b) a download answered 403 ChallengeRequiredError falls back to the abstract and says why;
  (c) no session (no credentials; a login answered mfaPending; a login answered
      ChallengeRequiredError) falls back to the abstract and says why, never calls /mfa/, and
      makes at most one /login;
  (d) the token appears in no output: not in the document, the diagnostics, the log records, or
      the probe script's stdout.

(e), the probe script scripts/probe_openreview_pdf.py, is pinned in test_openreview_pdf_probe.py: the
script lives only in this repository's scripts/, so its suite stays here while this one also runs
where the adapter ships without that script.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from omniseek.core import diag  # noqa: E402
from omniseek.core.sources.api import openreview_source as ors  # noqa: E402

TOKEN = "tok-SECRET-0123456789"
USER, PASSWORD = "someone@example.org", "pw-SECRET-xyz"
FORUM = "5gA4AaEUiN"
ABSTRACT = "We study depth before breadth."

# A one-page PDF whose text layer reads "Depth Before Breadth fixture" (602 bytes, xref offsets exact).
PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n"
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n"
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R "
    b"/Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n4 0 obj\n<< /Length 58 >>\nstream\n"
    b"BT /F1 12 Tf 10 50 Td (Depth Before Breadth fixture) Tj ET\nendstream\nendobj\n5 0 obj\n"
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\nxref\n0 6\n"
    b"0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
    b"0000000241 00000 n \n0000000349 00000 n \ntrailer\n<< /Size 6 /Root 1 0 R >>\n"
    b"startxref\n419\n%%EOF\n"
)
CHALLENGE = {"name": "ChallengeRequiredError", "message": "Please complete the challenge", "status": 403}
NOTE = {"id": FORUM, "forum": FORUM, "cdate": 1760000000000, "invitations": ["ICLR.cc/2027/Conference/-/Submission"],
        "content": {"title": {"value": "Depth Before Breadth"}, "abstract": {"value": ABSTRACT},
                    "authors": {"value": ["Anonymous"]}, "pdf": {"value": "/pdf/abc.pdf"}}}


def _resp(status, *, url, json_body=None, content=b"", ctype=None):
    req = httpx.Request("GET", url)
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    headers = {"content-type": ctype} if ctype else None
    return httpx.Response(status, content=content, headers=headers, request=req)


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class _Upstream:
    """A scripted OpenReview: /login, /notes, /attachment. Records every request."""

    def __init__(self, *, login=None, attachment=None, configured=True):
        self.login = login if login is not None else (200, {"token": TOKEN})
        self.attachment = attachment if attachment is not None else (200, PDF_BYTES, "application/pdf")
        self.configured = configured
        self.posts: list = []
        self.gets: list = []

    def post(self, url, **kw):
        self.posts.append(url)
        status, body = self.login
        return _resp(status, url=url, json_body=body)

    def get(self, url, **kw):
        self.gets.append((url, kw))
        if url.endswith("/notes"):
            return _resp(200, url=url, json_body={"notes": [NOTE]})
        if url.endswith("/attachment"):
            status, body, ctype = self.attachment
            if isinstance(body, dict):
                return _resp(status, url=url, json_body=body)
            return _resp(status, url=url, content=body, ctype=ctype)
        return _resp(404, url=url, json_body={"name": "NotFoundError"})

    def direct(self, method, url, **kw):
        assert method == "GET", method
        return self.get(url, **kw)

    def patches(self):
        creds = {"username": USER, "password": PASSWORD} if self.configured else None
        return [
            mock.patch.object(ors.httpx, "post", self.post),
            mock.patch.object(ors.httpx, "get", self.get),
            mock.patch.object(ors.http, "direct", self.direct),
            mock.patch.object(ors.auth, "is_configured", lambda n: self.configured),
            mock.patch.object(ors.auth, "load", lambda n: creds),
            mock.patch.object(ors.cache, "get", lambda key: None),
            mock.patch.object(ors.cache, "set", lambda *a, **k: None),
        ]


class OpenReviewFullText(unittest.TestCase):
    def _read(self, up: _Upstream, url: str = f"https://openreview.net/pdf?id={FORUM}"):
        rec = _Records()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(rec)
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.ExitStack() as stack:
                for p in up.patches():
                    stack.enter_context(p)
                diag.enable()
                try:
                    doc = ors.OpenReviewAdapter().fetch_url(url)
                finally:
                    captures = diag.drain()
        finally:
            root.removeHandler(rec)
            root.setLevel(old_level)
        return doc, captures, rec.lines

    def _assert_no_secret(self, *outputs):
        blob = json.dumps(outputs, default=str)
        for secret in (TOKEN, PASSWORD):
            self.assertNotIn(secret, blob)

    def test_a_full_text_from_the_attachment_endpoint(self):
        for url in (f"https://openreview.net/pdf?id={FORUM}", f"https://openreview.net/forum?id={FORUM}"):
            with self.subTest(url=url):
                up = _Upstream()
                doc, captures, logs = self._read(up, url)
                self.assertIsNotNone(doc)
                self.assertIn("Depth Before Breadth fixture", doc.content)
                self.assertNotEqual(doc.content, ABSTRACT)
                self.assertIs(doc.metadata["fulltext"], True)
                self.assertEqual(doc.metadata["abstract"], ABSTRACT)
                self.assertEqual(doc.metadata["pages"], 1)
                self.assertEqual(doc.metadata["pdf_bytes"], len(PDF_BYTES))
                self.assertEqual(doc.title, "Depth Before Breadth")
                att = [(u, kw) for u, kw in up.gets if u.endswith("/attachment")]
                self.assertEqual(len(att), 1)
                self.assertEqual(att[0][0], f"{ors.API_BASE}/attachment")
                self.assertEqual(att[0][1]["params"], {"id": FORUM, "name": "pdf"})
                self.assertEqual(att[0][1]["headers"], {"Authorization": "Bearer " + TOKEN})
                self.assertEqual(len(up.posts), 1)
                self._assert_no_secret(doc.to_tool_dict(full=True), captures, logs)

    def test_a_eye_read_windows_the_full_text(self):
        from omniseek import server
        up = _Upstream()
        with contextlib.ExitStack() as stack:
            for p in up.patches():
                stack.enter_context(p)
            # the first tool call of a process would bind the portal and fire a LIVE shadow search;
            # mark it done, as tests/test_executor_lanes.py does, so this test stays offline
            stack.enter_context(mock.patch.object(server, "_portal_bound_once", True))
            stack.enter_context(mock.patch.object(server.fetcher, "fetch_url_with_reason",
                                                  lambda u: (ors.OpenReviewAdapter().fetch_url(u), None)))
            out = asyncio.run(server.omniseek_read(target=f"https://openreview.net/pdf?id={FORUM}",
                                              start_char=6, max_chars=6))
        self.assertTrue(out["matched"])
        self.assertEqual(out["document"]["content"], "Before")
        self.assertEqual(out["total_chars"], len("Depth Before Breadth fixture"))
        self.assertTrue(out["truncated"])
        self.assertIs(out["document"]["metadata"]["fulltext"], True)
        self._assert_no_secret(out)

    def test_b_a_challenged_download_falls_back_to_the_abstract_with_the_reason(self):
        up = _Upstream(attachment=(403, CHALLENGE, None))
        doc, captures, logs = self._read(up)
        self.assertEqual(doc.content, ABSTRACT)
        self.assertIs(doc.metadata["fulltext"], False)
        self.assertIn("ChallengeRequiredError", doc.metadata["fulltext_reason"])
        self.assertIn("403", doc.metadata["fulltext_reason"])
        self.assertEqual(len(up.posts), 1)
        self._assert_no_secret(doc.to_tool_dict(full=True), captures, logs)

    def test_b_a_200_that_is_not_a_pdf_falls_back(self):
        up = _Upstream(attachment=(200, b"<html>verify you are human</html>", "text/html"))
        doc, _c, _l = self._read(up)
        self.assertEqual(doc.content, ABSTRACT)
        self.assertIn("not a PDF", doc.metadata["fulltext_reason"])

    def test_c_no_session_falls_back_with_the_reason_and_one_login_at_most(self):
        cases = (
            ("no credentials", _Upstream(configured=False), "credentials not configured", 0),
            ("mfa", _Upstream(login=(200, {"mfaPending": True, "mfaMethods": ["emailOtp"]})), "multi-factor", 1),
            ("challenged login", _Upstream(login=(403, dict(CHALLENGE, message=f"challenge for {USER}"))),
             "ChallengeRequiredError", 1),
        )
        for label, up, why, logins in cases:
            with self.subTest(case=label):
                doc, captures, logs = self._read(up)
                self.assertIsNotNone(doc)
                self.assertEqual(doc.content, ABSTRACT)
                self.assertIs(doc.metadata["fulltext"], False)
                self.assertIn("no logged-in OpenReview session", doc.metadata["fulltext_reason"])
                self.assertIn(why, doc.metadata["fulltext_reason"])
                self.assertEqual(len(up.posts), logins)
                self.assertFalse([u for u in up.posts if "/mfa" in u])
                self.assertFalse([u for u, _ in up.gets if u.endswith("/attachment")])
                self.assertNotIn(USER, doc.metadata["fulltext_reason"])
                self._assert_no_secret(doc.to_tool_dict(full=True), captures, logs)

    def test_c_a_note_that_cannot_be_read_is_still_none(self):
        up = _Upstream()
        up.get = lambda url, **kw: _resp(403, url=url, json_body=CHALLENGE)
        doc, captures, _l = self._read(up)
        self.assertIsNone(doc)
        self.assertTrue(any("ChallengeRequiredError" in (c.get("body") or "") for c in captures), captures)
        self._assert_no_secret(captures)


class DownloadEgress(unittest.TestCase):
    """The download itself, through the real http.direct with only the transport stubbed."""

    def test_d_a_redirect_is_followed_and_the_token_stays_on_openreview(self):
        seen = []

        def handle(self_, request):
            seen.append((request.url.host, request.url.path, dict(request.url.params),
                         request.headers.get("authorization")))
            if request.url.host == "api2.openreview.net":
                return httpx.Response(302, headers={"location": "https://files.test.invalid/p.pdf"},
                                      stream=httpx.ByteStream(b""), request=request)
            return httpx.Response(200, headers={"content-type": "application/pdf"},
                                  stream=httpx.ByteStream(PDF_BYTES), request=request)
        with mock.patch.object(httpx.HTTPTransport, "handle_request", handle):
            status, ctype, body, reason = ors.OpenReviewAdapter()._download_pdf("5gA4AaEUiN", TOKEN)
        self.assertEqual((200, "application/pdf", PDF_BYTES, "ok"), (status, ctype, body, reason))
        self.assertEqual(("api2.openreview.net", "/attachment", {"id": "5gA4AaEUiN", "name": "pdf"},
                          f"Bearer {TOKEN}"), seen[0])
        self.assertEqual(("files.test.invalid", "/p.pdf", {}, None), seen[1])
        self.assertEqual(2, len(seen))


if __name__ == "__main__":
    unittest.main()
