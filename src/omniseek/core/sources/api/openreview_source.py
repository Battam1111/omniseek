"""OpenReview adapter — uses the REST API directly (httpx).

We skip the official openreview-py package because its `editdistance`
dependency has no Python 3.13 wheel and is fragile to build. The REST
API is well-documented at https://docs.openreview.net/reference/api-v2

Reads credentials from <credentials>/openreview.json:
    {"username": "you@email.com", "password": "..."}

OpenReview is the unique source where we can read actual peer reviews,
rebuttals, and meta-reviews — irreplaceable methodology learning material.

Reviews (EYE-RSS-3, 2026-10-24): a reply note's document carries EVERY field of the note (venues name
their review forms' fields differently) except the bookkeeping in OPENREVIEW_META_FIELDS, each under its
name as a ``## `` heading; its kind comes from the invitation's last segment, rating / confidence from
their generic names (see _review_note_to_document).

Full text (2026-10-10): omniseek_read on an openreview.net /pdf?id= or /forum?id= link downloads the
submission PDF through the logged-in session (the attachment endpoint the official openreview-py
client uses), streamed to a temporary file under the document cap (http.DOC_MAX_BYTES, EYE-RSS-3),
and extracts it with the pdf adapter's extractor. Anonymous access to every OpenReview
entry now answers 403 ChallengeRequiredError, so without a session, or when the session is asked
to verify too, the read falls back to the title and abstract and says why in
metadata.fulltext_reason. OmniSeek never touches the anonymous challenge page. Reading the PDF needs
the optional [pdf] extra (PyMuPDF); an install without it skips the download and falls back the
same way, the reason naming what to install (pip install 'omniseek[pdf]'). Since 2026-11-09 a
/forum?id= link also appends the forum's discussion after the paper (every note ``fetch_reviews`` reads:
reviews, author responses, meta-review, decision; who, kind, date, full text, oldest first; count in
metadata.discussion_notes), abstract-only or not; /pdf?id= stays the paper alone. The paper and the
discussion are fetched side by side after the one login (_read_forum), and a PDF not ready by the
forum's cutoff (_forum_cutoff) leaves the abstract with the discussion instead of timing the read out.

Search without logging in (EYE-FIX-1109): ``/notes/search`` answers anonymously again (measured 2026-11-08
and 11-09, with or without ``group=<venue>``), while ``/notes?id=``, ``/notes?forum=`` and
``/notes?content.venueid=`` still answer 403 ChallengeRequiredError without a session. So searching (words,
with or without ``venue:``) never logs in unless the anonymous answer is refused; the reads that need the
session (a note, a forum's reviews, a word-less venue browse, the PDF) log in. ``/login`` allows 3 attempts a
window: a search that logged in first returned nothing whenever that login hit 429.

A note's ``authors`` is a list of names on a live submission, but a list of objects
(``{username, fullname, institutions}``) on some notes, e.g. withdrawn ones (6XdN8dldnU, 2026-11-08);
``_author_names`` reads both.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse

import anyio
import httpx

from omniseek.core import _guard, _probe, auth, cache, diag, fetcher, http, portal, upstreams
from omniseek.core.normalize import Document, jsonsafe
from omniseek import where
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

API_BASE = "https://api2.openreview.net"
# OpenReview runs TWO live backends and a forum lives on exactly ONE of them: api2 (v2) holds the
# newer venues, api (v1) still serves everything from roughly pre-2023. Measured 2026-07-25 with the
# adapter's own auth: BrnlCSqO6n (ICLR 2026) -> 30 notes on v2 / 0 on v1; qGvMv3undNJ (NeurIPS 2021)
# -> 0 on v2 / 16 on v1; likewise knKJgksd7kA 13, QkljT4mrfs 22, uJGObgFU0lU 19, all v1-only. Querying
# v2 alone therefore returned a well-formed EMPTY for every older forum, which reads as "no such
# paper" instead of "wrong backend". fetch_reviews now tries v2 then falls back to v1.
API_BASE_V1 = "https://api.openreview.net"
# A busy forum carries 15-30 notes (reviews + author responses + meta-review + decision). The caller's
# default limit of 10 silently dropped the author responses and the decision, which is exactly the
# half a rebuttal study needs, so the reviews path floors the limit here instead.
_REVIEWS_MIN_LIMIT = 60
DEFAULT_TIMEOUT = 30
# Health probe: the search path itself, for ONE paper on a term that always has papers.
_PROBE_TERM = "learning"
_RELOGIN = object()   # _probe_search's answer when the token itself was refused
# The submission PDF: openreview-py (api/client.py, get_attachment) asks
# GET {baseurl}/attachment with params {"id": <note id>, "name": "pdf"} and the Bearer token.
_ATTACHMENT_PATH = "/attachment"
# Keep an error message readable in a reason line; the status and error name always come first.
_REASON_MSG_CHARS = 200
# One PDF download's own timeout: a 31MB paper at a slow 1 MB/s still fits (the adapter's fetch
# budget is fetch_timeout below).
_PDF_TIMEOUT_S = 60


class _ErrorAnswer:
    """A refused download (http.DownloadError) read like a response by ``_answer_reason``."""

    def __init__(self, exc) -> None:
        self.status_code, self._body = exc.status, exc.body or ""

    def json(self):
        return json.loads(self._body)

auth.register_template(
    "openreview",
    {
        "username": "you@email.com",
        "password": "your-password",
        "_help": "OpenReview account credentials (email + password)",
    },
)


# `venue:` qualifier — browse a venue's notes via the content.venueid filter
# (keyless-verified 2026-06-10 against COLM 2025). Accepts an alias+year
# (`venue:colm2025`, `venue:iclr/2026`) or a raw venueid
# (`venue:colmweb.org/COLM/2025/Conference`). Stripped from the keyword part.
_VENUE_RE = re.compile(r"(?:^|\s)venue\s*:\s*(\S+)", re.IGNORECASE)
_VENUE_ALIASES = {
    "colm": "colmweb.org/COLM/{year}/Conference",
    "iclr": "ICLR.cc/{year}/Conference",
    "neurips": "NeurIPS.cc/{year}/Conference",
    "icml": "ICML.cc/{year}/Conference",
}


# `reviews:` qualifier: fetch the actual PEER REVIEWS / rebuttals / meta-reviews of one
# submission (a forum id). The reply notes of a forum carry the reviewer ratings + text; this
# is the unique-to-OpenReview signal (the raw material for peer-review analysis).
# Accepts a bare forum id (`reviews:zzz123`) or an openreview.net/forum?id=… URL.
_REVIEWS_RE = re.compile(r"(?:^|\s)reviews\s*:\s*(\S+)", re.IGNORECASE)
# EVERY FIELD OF A REPLY NOTE IS KEPT (EYE-RSS-3, 2026-10-24). Each venue names its review form's
# fields its own way (ICML 2025: claims_and_evidence, questions_for_authors, overall_recommendation;
# NeurIPS 2025: strengths_and_weaknesses, quality, clarity, significance, originality, limitations),
# and a list of names written for one venue silently dropped the rest (an ICML 2025 review came back
# as its summary alone). So the body is every field of the note's content, in the note's own order,
# except the bookkeeping below: the note's title, its attachments, who signed it, its number, and the
# declarations a reviewer ticks (conduct and responsible-reviewing acknowledgements, conflicts of
# interest). A field whose value is an attachment path (/attachment/..., /pdf/...) is bookkeeping too,
# whatever its name (ICML 2025's review form has a TEXT field named supplementary_material).
OPENREVIEW_META_FIELDS = frozenset({
    "title", "pdf", "attachment", "authors", "authorids", "signatures", "number", "paperhash",
    "venue", "venueid", "_bibtex", "code_of_conduct", "code_of_conduct_acknowledgement",
    "responsible_reviewing_acknowledgement", "reviewer_code_of_conduct", "conflict_of_interest",
    "conflicts_of_interest",
})
_ATTACHMENT_VALUE_RE = re.compile(r"^/(?:attachment|pdf)/\S+$")
# The generic names of a note's score, first match wins in this order, then the first field (in the
# note's order) whose name ends in rating / recommendation; confidence likewise. None when a note has
# none: the adapter does not guess one from another field.
_RATING_NAMES = ("rating", "recommendation", "overall_recommendation", "overall_assessment", "final_rating")
_RATING_SUFFIXES = ("rating", "recommendation")
_CONFIDENCE_NAMES = ("confidence",)
_CONFIDENCE_SUFFIXES = ("confidence",)
# What kind of reply a note is, read from the last segment of its invitation (".../-/Official_Review").
# The field-name sniff below is the fallback for a note whose invitations name no kind.
_KIND_FALLBACK_FIELDS = ("rating", "review", "summary", "confidence", "soundness", "presentation",
                         "contribution", "strengths", "weaknesses", "recommendation",
                         "metareview", "meta_review", "decision", "comment", "rebuttal")


def _parse_reviews(query: str) -> Optional[str]:
    """Extract a forum id from a `reviews:` qualifier (bare id or a /forum?id=… URL)."""
    m = _REVIEWS_RE.search(query or "")
    if not m:
        return None
    tok = m.group(1).strip()
    if "openreview.net" in tok:
        from urllib.parse import parse_qs
        return (parse_qs(urlparse(tok).query).get("id") or [None])[0]
    return tok or None


def _parse_venue(query: str) -> tuple[str, Optional[str]]:
    """Split a `venue:` qualifier out of the query; resolve aliases to venueids."""
    m = _VENUE_RE.search(query or "")
    if not m:
        return (query or "").strip(), None
    tok = m.group(1)
    clean = _VENUE_RE.sub(" ", query).strip()
    am = re.fullmatch(r"([A-Za-z]+)[-/]?(\d{4})", tok)
    if am and am.group(1).lower() in _VENUE_ALIASES:
        return clean, _VENUE_ALIASES[am.group(1).lower()].format(year=am.group(2))
    return clean, tok  # raw venueid passthrough


def _author_names(authors) -> list[str]:
    """A note's ``authors`` as names: a list of strings, or of objects with ``fullname`` / ``username``
    (``name`` / ``id`` as a last resort), or one string; empty entries dropped."""
    if not authors:
        return []
    if not isinstance(authors, list):
        authors = [authors]
    out: list[str] = []
    for a in authors:
        if isinstance(a, dict):
            a = a.get("fullname") or a.get("username") or a.get("name") or a.get("id")
        if isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def _answer_reason(resp, scrub: tuple = ()) -> str:
    """``HTTP <status> <error name>: <message>`` from an OpenReview answer, for a reason line.
    OpenReview answers errors as JSON ``{"name", "message", ...}`` (a challenged request says
    ``ChallengeRequiredError``). Every string in ``scrub`` (the token, the account name) is masked
    out of the text, so a reason never carries a credential. Never raises."""
    status = getattr(resp, "status_code", None)
    name = msg = ""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 (a non-JSON error body just has no name)
        data = None
    if isinstance(data, dict):
        name = str(data.get("name") or "")
        msg = str(data.get("message") or "")
    for secret in scrub:
        if secret:
            name, msg = name.replace(secret, "<redacted>"), msg.replace(secret, "<redacted>")
    out = f"HTTP {status}"
    if name:
        out += f" {name}"
    if msg and msg != name:
        out += f": {msg[:_REASON_MSG_CHARS]}"
    return out


def _leaf_failure(seen: list, helper: str) -> tuple[Optional[int], Optional[str]]:
    """(HTTP status, body or exception text) of the last failure the shared leaf noted under
    ``helper`` in a ``diag.tap``; (None, None) when it noted none."""
    for rec in reversed(seen or ()):
        if isinstance(rec, dict) and rec.get("helper") in (helper, "http.get_json", "http.post_json"):
            return rec.get("status"), rec.get("body") or rec.get("exc")
    return None, None


def _last_retry_after() -> Optional[float]:
    """The Retry-After of OpenReview's last 429, from the upstream's readings (the shared leaf records
    every answer's rate-limit headers there); None when the last reading is not a recent 429."""
    lane = (upstreams.readings("openreview").get("lanes") or {}).get("_") or {}
    if lane.get("status") != 429 or time.time() - float(lane.get("at") or 0) > 60:
        return None
    return upstreams.parse_retry_after((lane.get("fields") or {}).get("retry-after"))


class OpenReviewAdapter(SourceBase):
    name = "openreview"
    needs_credentials = True
    # the notes request, then a PDF download of up to _PDF_TIMEOUT_S (a 30 s default cut big papers)
    fetch_timeout = 90.0
    description = (
        "OpenReview: peer reviews, rebuttals, meta-reviews from ICLR/NeurIPS/ICML; "
        "papers by words (anonymous search); `venue:` qualifier (venue:colm2025 / venue:iclr2026 / raw "
        "venueid) with words searches every submission of that venue, alone it browses the venue's "
        "accepted papers; a submission's actual reviews via `reviews:` (reviews:<forum_id> "
        "or a /forum?id=… URL); omniseek_read on a "
        "/forum?id= or /pdf?id= link returns the paper's full PDF text through the logged-in session, "
        "a /forum?id= link followed by the forum's whole discussion (reviews, responses, decision)"
    )

    _token: Optional[str] = None
    _token_expires_at: float = 0.0
    _login_issue: Optional[str] = None  # why the last login gave no token, for health_check
    _login_status: Optional[int] = None  # the HTTP status of the last failed login (429: rate-limited)
    _login_retry_after: Optional[float] = None

    def _accept_login(self, data: dict) -> Optional[str]:
        """Take a /login answer (shared by the sync and async logins). An account with multi-factor
        login answers ``mfaPending`` and no token (measured 2026-10-03: methods ["emailOtp"]). An
        unattended service cannot pass that step, so this records the reason for health_check instead
        of a bare "login failed". It never calls /mfa/challenge: that call is what emails the account
        owner a code."""
        if data.get("mfaPending"):
            methods = ", ".join(str(m) for m in (data.get("mfaMethods") or [])) or "unknown method"
            self._login_issue = (f"account requires multi-factor login ({methods}); unattended login "
                                 "is impossible while it is on")
            diag.note("openreview.login", url=f"{API_BASE}/login", status=200, body=self._login_issue)
            self._token = None
            return None
        self._token = data.get("token")
        self._login_issue = None if self._token else "login answered without a token"
        # OpenReview tokens last ~12h; we set expiry conservatively
        self._token_expires_at = time.time() + 11 * 3600
        return self._token

    def _get_token(self) -> Optional[str]:
        """Sync entry for a caller on a thread: ``_aget_token`` on the event loop (portal.call)."""
        return portal.call(self._aget_token)

    async def _aget_token(self) -> Optional[str]:
        """Login or reuse token. Tokens last 12h; we refresh well before. The login POST goes through
        the shared leaf ``http.apost_json`` (host breaker, readings, cache_only, SSRF guard, size cap,
        failure->None). A failed login leaves its HTTP status, Retry-After and reason (the error name,
        e.g. ChallengeRequiredError, with the account name and password masked) for ``health_check``
        and ``_no_session_reason``. Instance token state is shared by every caller: a benign no-lock
        race (a redundant login at worst)."""
        if self._token and time.time() < self._token_expires_at - 600:
            return self._token
        creds = await anyio.to_thread.run_sync(auth.load, "openreview", limiter=portal.IO_LANE)  # cred-file read OFF loop
        if not creds or not creds.get("username") or not creds.get("password"):
            logger.info("OpenReview credentials not configured.")
            return None
        self._login_issue = None
        self._login_status = None
        self._login_retry_after = None
        url = f"{API_BASE}/login"
        with diag.tap() as seen:
            data = await http.apost_json(
                url,
                json={"id": creds["username"], "password": creds["password"]},
                timeout=DEFAULT_TIMEOUT,
            )
        if data is not None:
            return self._accept_login(data)
        st, why = _leaf_failure(seen, "http.post")
        self._login_status = st   # a 429 here is "not verified" for health_check, not "down"
        if st == 429:
            self._login_retry_after = _last_retry_after()
        if st is not None:
            # the error name (e.g. ChallengeRequiredError) says WHY, where "login failed" does not
            self._login_issue = _answer_reason(httpx.Response(st, content=(why or "").encode()),
                                               scrub=(creds["username"], creds["password"]))
        logger.warning("OpenReview login failed: %s", self._login_issue or why or "no answer")
        diag.note("openreview.login", url=url, status=st, body=self._login_issue or why)
        return None

    def _api_get(self, path: str, params: dict, base: str = API_BASE) -> Optional[dict]:
        """Sync entry for a caller on a thread: ``_aapi_get`` on the event loop (portal.call)."""
        return portal.call(self._aapi_get, path, params, base)

    async def _aapi_get(self, path: str, params: dict, base: str = API_BASE) -> Optional[dict]:
        """GET ``base + path`` (v2 by default, ``API_BASE_V1`` for the pre-2023 venues) through the shared
        leaf ``http.aget_json`` (host breaker, readings, cache_only, SSRF guard, 30MB cap,
        failure->None). Auth is REQUIRED on both backends: a keyless GET of /notes returns 403 for every
        forum, public ones included (measured 2026-07-25). The login degrade is kept so a broken token
        still egresses and surfaces the real upstream status rather than failing silently here. A
        failure is noted as ``openreview.api`` with OpenReview's own reason (the token masked)."""
        token = await self._aget_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{base}{path}"
        with diag.tap() as seen:
            data = await http.aget_json(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
        if data is None:
            st, why = _leaf_failure(seen, "http.get")
            body = (_answer_reason(httpx.Response(st, content=(why or "").encode()), scrub=(token,))
                    if st is not None else why)
            logger.warning("OpenReview API call failed: %s", body or "no answer")
            diag.note("openreview.api", url=url, status=st, body=body)
        return data

    async def _asearch_notes(self, params: dict) -> Optional[dict]:
        """``/notes/search`` anonymously first (EYE-FIX-1109): it answers keyless (measured 2026-11-08 and
        11-09, with or without ``group``), while ``/login`` allows 3 attempts a window and a login that
        hit 429 used to cost the whole source its results. Only an anonymous answer of 401 / 403 (the
        ChallengeRequiredError wall OpenReview put on every anonymous request from 2026-10-10) falls back
        to the logged-in request; any other failure is noted with its address and returns None."""
        url = f"{API_BASE}/notes/search"
        try:
            r = await http.adirect("GET", url, params=params, timeout=DEFAULT_TIMEOUT,
                                   headers={"User-Agent": http.USER_AGENT})
        except Exception as exc:  # noqa: BLE001
            diag.note("openreview.search", url=url, exc=exc)
            return None
        if r.status_code in (401, 403):
            logger.info("OpenReview anonymous search refused (%s); retrying with the logged-in session",
                        _answer_reason(r))
            return await self._aapi_get("/notes/search", params)
        if r.status_code != 200:
            diag.note("openreview.search", url=str(r.url), status=r.status_code, body=_answer_reason(r))
            return None
        try:
            return r.json()
        except Exception as exc:  # noqa: BLE001
            diag.note("openreview.search", url=str(r.url), status=200, exc=exc, body=r.text[:400])
            return None

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async search. ``reviews:`` delegates to the sync ``fetch_reviews`` OFF-loop in a worker
        thread (its own cache round-trip + egress, which need the logged-in session). Words, with or
        without a ``venue:`` qualifier, go to ``/notes/search`` anonymously (``_asearch_notes``; the
        venue as ``group=<venueid>``, which holds every submission of that venue: accepted, under review,
        rejected and withdrawn, each document's venue tag saying which). Only a bare ``venue:`` with no
        words browses ``/notes?content.venueid=`` (the venue's accepted papers, newest first), which
        anonymous access refuses, so only that one logs in. The disk cache round-trip runs off the loop."""
        forum = _parse_reviews(query)
        if forum:
            return await anyio.to_thread.run_sync(  # same limit floor as sync (whole thread, not 10)
                functools.partial(self.fetch_reviews, forum, max(limit, _REVIEWS_MIN_LIMIT)), limiter=fetcher._LEGACY_LIMITER)
        terms, venueid = _parse_venue(query)
        key = cache.make_key("openreview", "search_forum", venueid or "-", terms, limit)

        async def _aget() -> Optional[list[Document]]:
            if venueid and not terms:
                # Venue browse: newest notes of one venue (needs the logged-in session).
                data = await self._aapi_get(
                    "/notes",
                    {"content.venueid": venueid, "limit": min(limit, 100), "sort": "cdate:desc"},
                )
            else:
                # OpenReview v2 search. `source: "forum"` keeps the hits to papers (submissions and
                # records). Without it the default mixes in replies (reviews, comments), which carry no
                # title and map to empty docs: measured 2026-10-04 on a credit-assignment query, 2 of 8
                # hits had a title by default and 8 of 8 with "forum". Accepted values are forum, reply
                # and all; any other value is a 400. An empty term is a 400 too. A paper's reviews stay
                # reachable via `reviews:`.
                params = {"term": terms, "limit": min(limit, 100), "source": "forum"}
                if venueid:
                    params["group"] = venueid
                data = await self._asearch_notes(params)
            if not data or "notes" not in data:
                return None  # a failure: not cached

            docs: list[Document] = []
            for note in data["notes"]:
                try:
                    docs.append(self._note_to_document(note))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Skipping malformed OpenReview note: %s", exc)
            return docs[:limit]

        return await self._acached(key, 1800, _aget)

    def fetch_reviews(self, forum_id: str, limit: int = 20) -> list[Document]:
        """Fetch the actual peer reviews / rebuttals / meta-reviews of one submission. The reply
        notes of a forum (``/notes?forum=<id>``) carry the reviewer ratings + text; we keep the
        ones whose content sniffs review-ish (a review/meta-review/rebuttal/decision). Public
        forums are readable WITHOUT auth, so this degrades to keyless access."""
        forum_id = (forum_id or "").strip()
        if not forum_id:
            return []
        key = cache.make_key("openreview", "reviews", forum_id, limit)
        cached = cache.get_once(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]
        params = {"forum": forum_id, "limit": 200, "sort": "cdate:asc"}
        data = self._api_get("/notes", params)
        backend = "v2"
        if not (data or {}).get("notes"):
            # Empty on v2 means "not this backend" far more often than "no reviews": try v1, where
            # every pre-2023 venue still lives. Costs one extra GET only on a forum v2 does not hold.
            v1 = self._api_get("/notes", params, base=API_BASE_V1)
            if (v1 or {}).get("notes"):
                data, backend = v1, "v1"
        if not data or "notes" not in data:
            return []
        if not data["notes"]:
            # Genuinely on NEITHER backend: say so, so the drill does not read a wrong-backend miss
            # (the old failure mode) or a dead forum id as a well-formed "this paper has no reviews".
            diag.note("openreview.reviews",
                      url=f"{API_BASE}/notes?forum={forum_id}",
                      body=(f"forum {forum_id!r} returned 0 notes on BOTH backends "
                            f"(api2 v2 + api v1): the id is wrong, withdrawn, or non-public"))
            return []
        docs: list[Document] = []
        for note in data["notes"]:
            if note.get("id") == forum_id:  # the submission itself, not a review reply
                continue
            try:
                doc = self._review_note_to_document(note, forum_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed OpenReview review note: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
        if len(docs) > limit:
            # Truncation on a forum is LOSSY in a specific way: the notes arrive cdate:asc, so what
            # gets cut is the tail, i.e. the author responses, meta-review and decision. Say it.
            diag.note("openreview.reviews",
                      url=f"{API_BASE if backend == 'v2' else API_BASE_V1}/notes?forum={forum_id}",
                      body=(f"forum {forum_id} ({backend}) has {len(docs)} review notes, returning "
                            f"{limit}: the tail (author responses / meta-review / decision) is cut, "
                            f"pass a larger limit to get the whole thread"))
        docs = docs[:limit]
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=1800)
        return docs

    @staticmethod
    def _kind_from_invitations(invitations: list) -> Optional[str]:
        """review / meta_review / decision / rebuttal / comment from the last segment of an invitation
        (".../-/Official_Review", ".../-/Meta_Review", ".../-/Rebuttal", ".../-/Official_Comment"),
        or None when no invitation names one (an edit invitation, a withdrawal)."""
        for inv in invitations:
            seg = str(inv).rsplit("/-/", 1)[-1].rsplit("/", 1)[-1].lower()
            if "meta_review" in seg or "metareview" in seg:
                return "meta_review"
            if "decision" in seg:
                return "decision"
            if "comment" in seg:
                return "comment"
            if "rebuttal" in seg:
                return "rebuttal"
            if "review" in seg:
                return "review"
        return None

    @staticmethod
    def _pick(present: dict, names: tuple, suffixes: tuple):
        """``(field, value)`` of the first generic name present, else the first field ending in a
        suffix (in the note's order), else ``(None, None)``."""
        for n in names:
            if n in present:
                return n, present[n]
        for f, v in present.items():
            if f.endswith(suffixes):
                return f, v
        return None, None

    @staticmethod
    def _review_note_to_document(note: dict, forum_id: str) -> Optional[Document]:
        """Pure parse of one reply note → a Document: every field of its content but the
        bookkeeping (OPENREVIEW_META_FIELDS, attachment paths), in the note's order, each under its
        name as a heading. Returns None when nothing but bookkeeping is left, or when the note's
        invitations name no kind and none of the old review field names is present either (a
        withdrawal, an edit). No network."""
        content = note.get("content") or {}

        def _val(field):
            v = content.get(field)
            return v.get("value") if isinstance(v, dict) else v

        present: dict = {}
        for f in content:
            v = _val(f)
            if v in (None, "") or v == [] or f in OPENREVIEW_META_FIELDS:
                continue
            if isinstance(v, str) and _ATTACHMENT_VALUE_RE.match(v.strip()):
                continue
            present[f] = v
        if not present:
            return None  # bookkeeping only, skip
        # v2 carries invitations[] (a list); v1 carries invitation (a single string, e.g.
        # ".../Paper10022/-/Official_Review"). Read both, or every v1 note loses its kind signal and
        # falls back to content sniffing alone.
        invitations = note.get("invitations") or []
        if not invitations and note.get("invitation"):
            invitations = [str(note["invitation"])]
        kind = OpenReviewAdapter._kind_from_invitations(invitations)
        if kind is None:
            # No invitation names the kind: the old field-name sniff, which also decides whether the
            # note is a review-like reply at all (as before EYE-RSS-3).
            if not any(f in present for f in _KIND_FALLBACK_FIELDS):
                return None
            if "metareview" in present or "meta_review" in present:
                kind = "meta_review"
            elif "decision" in present:
                kind = "decision"
            elif "rebuttal" in present:
                kind = "rebuttal"
            elif "comment" in present and not ("rating" in present or "review" in present):
                kind = "comment"
            else:
                kind = "review"
        rating_field, rating = OpenReviewAdapter._pick(present, _RATING_NAMES, _RATING_SUFFIXES)
        _cf, confidence = OpenReviewAdapter._pick(present, _CONFIDENCE_NAMES, _CONFIDENCE_SUFFIXES)
        # Body = every kept field, its name as a heading, in the note's order.
        body_parts = []
        for f, v in present.items():
            if isinstance(v, (list, dict)):
                v = jsonsafe(v)
            body_parts.append(f"## {f}\n{v}")
        body = "\n\n".join(body_parts)
        note_title = _val("title")
        date = None
        cdate = note.get("cdate")
        if cdate:
            try:
                date = datetime.fromtimestamp(cdate / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None
        rating_tag = f"rating:{rating}" if rating not in (None, "") else None
        title = kind.replace("_", " ").title()
        if rating not in (None, ""):
            title += f" (rating {rating})"
        return Document(
            source="openreview",
            source_id=note.get("id", ""),
            url=f"https://openreview.net/forum?id={forum_id}&noteId={note.get('id', '')}",
            title=title,
            content=body,
            author=None,  # reviewers are anonymous; signature is in metadata.raw if present
            date=date,
            tags=[t for t in (kind, rating_tag) if t],
            metadata={
                "subtype": kind,
                "forum": forum_id,
                "rating": rating,
                "rating_field": rating_field,
                "confidence": confidence,
                "review_fields": list(present),
                "note_title": note_title if isinstance(note_title, str) and note_title else None,
                "invitations": invitations,
                "raw": jsonsafe(note),  # OpenReview reply note's original API dict
            },
        )

    async def afetch_url(self, url: str) -> Optional[Document]:
        """An openreview.net link (``/forum?id=`` or ``/pdf?id=``) as ONE document: the paper's whole
        text when the logged-in session can download its PDF, else the title and abstract with the
        reason in ``metadata.fulltext_reason`` (``metadata.fulltext`` says which). None when the
        note itself cannot be read, as before. At most one /login per call: the notes request makes
        it, the PDF request only reuses the token that login left (never a second attempt)."""
        host = urlparse(url).hostname or ""
        if "openreview.net" not in host:
            return None
        # Pattern: openreview.net/forum?id=<id> or /pdf?id=<id>
        from urllib.parse import parse_qs

        qs = parse_qs(urlparse(url).query)
        note_id = (qs.get("id") or [None])[0]
        if not note_id:
            return None
        # The login, the notes request, the PDF download and its text extraction are the sync client's
        # (shared with search and scripts/probe_openreview_pdf.py): a worker thread under the legacy limiter.
        # A /forum link is the discussion page: the paper, then its thread (_read_forum); /pdf is the paper only.
        read = self._read_forum if urlparse(url).path.rstrip("/").endswith("/forum") else self._read_note
        return await anyio.to_thread.run_sync(read, note_id, limiter=fetcher._LEGACY_LIMITER)

    def _read_forum(self, note_id: str) -> Optional[Document]:
        """The paper (full text, or the abstract with the reason) followed by the forum's discussion: every
        note ``fetch_reviews`` returns (reviews, author responses, comments, meta-review, decision), one
        section each, oldest first, headed by its kind, its writer (the signature's last segment) and its
        date. ``metadata.discussion_notes`` says how many were appended. Kept under its own key (the /pdf
        read of the same id is the paper alone), and only when the paper's full text came.

        The two halves do not depend on each other, so after the one login they run side by side and the
        read takes as long as the slower one (11-09: a v1-only forum, qGvMv3undNJ, spent about 40 s on
        its PDF, then fetched the discussion after it, and the whole read passed fetch_timeout and fell
        to the generic web read). Both are waited for until ``_forum_cutoff``; a paper not ready by then
        comes back as its abstract with the reason in ``metadata.fulltext_reason`` (its download goes on
        in the background and keeps the full text for the next read), a discussion not ready as
        ``metadata.discussion_reason``."""
        key = cache.make_key("openreview", "forum_thread", note_id)
        cached = cache.get_once(key)
        if cached is not None:
            return Document.model_validate(cached)
        cutoff = self._forum_cutoff()
        self._get_token()  # the one /login of this read, before the two halves share its token
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="openreview-forum")
        try:
            # 200 is the /notes?forum= page fetch_reviews asks for, so nothing it read is cut here.
            thread_f = pool.submit(contextvars.copy_context().run, self.fetch_reviews, note_id, 200)
            paper = self._cached_fulltext(note_id)
            if paper is None:
                note, base = self._find_note(note_id)
                if note is None:
                    return None
                doc = self._note_to_document(note)
                paper_f = pool.submit(contextvars.copy_context().run, self._paper_text, doc, note_id, base)
                try:
                    paper = paper_f.result(timeout=max(0.0, cutoff - time.monotonic()))
                except FuturesTimeout:
                    paper = self._abstract_only(
                        doc, f"the PDF did not finish downloading and extracting in the "
                             f"{self.fetch_timeout - DEFAULT_TIMEOUT:.0f} s this read gives it (it goes on in "
                             f"the background; a later read of this link gets the full text once it is done)")
            try:
                notes = thread_f.result(timeout=max(0.0, cutoff - time.monotonic()))
                late = False
            except FuturesTimeout:
                notes, late = [], True
        finally:
            pool.shutdown(wait=False)
        _far = datetime.max.replace(tzinfo=timezone.utc)
        notes = sorted(notes, key=lambda d: d.date or _far)
        parts = []
        for i, n in enumerate(notes, 1):
            raw = (n.metadata or {}).get("raw") or {}
            sigs = raw.get("signatures") or []
            writer = str(sigs[0]).rsplit("/", 1)[-1] if sigs else "unknown"
            day = n.date.strftime("%Y-%m-%d") if n.date else "undated"
            body = re.sub(r"(?m)^## ", "### ", n.content or "")
            parts.append(f"## {i}. {n.title} by {writer}, {day}\n\n{body}")
        meta = dict(paper.metadata or {})
        meta["discussion_notes"] = len(notes)
        content = paper.content or ""
        if parts:
            content += (f"\n\n# Discussion ({len(notes)} notes, oldest first)\n\n" + "\n\n".join(parts))
        elif late:
            meta["discussion_reason"] = (f"the forum's discussion did not come back in the "
                                         f"{self.fetch_timeout - DEFAULT_TIMEOUT:.0f} s this read gives it")
            diag.note("openreview.reviews", url=f"{API_BASE}/notes?forum={note_id}", body=meta["discussion_reason"])
        else:
            meta["discussion_reason"] = "the forum's discussion came back empty (no reply notes could be read)"
        doc = paper.model_copy(update={"content": content, "metadata": meta})
        if meta.get("fulltext") and parts:
            cache.set(key, doc.model_dump(mode="json"), ttl=1800)
        return doc

    def _forum_cutoff(self) -> float:
        """The monotonic time ``_read_forum`` stops waiting for its two halves: the adapter's deadline
        (the fetcher's, fetch_timeout after the attempt began; fetch_timeout from now when none is set)
        less DEFAULT_TIMEOUT. fetch_timeout is DEFAULT_TIMEOUT (the notes request) + _PDF_TIMEOUT_S
        (the download), so the halves get _PDF_TIMEOUT_S, what the /pdf read gives its download, and
        the DEFAULT_TIMEOUT kept back covers handing the answer back before the fetcher's cut."""
        deadline = _guard.current_deadline()
        if deadline is None:
            deadline = time.monotonic() + self.fetch_timeout
        return deadline - DEFAULT_TIMEOUT

    def _cached_fulltext(self, note_id: str) -> Optional[Document]:
        cached = cache.get_once(cache.make_key("openreview", "fulltext", note_id))
        # omniseek_read pages a long paper by start_char: one download serves every page
        return Document.model_validate(cached) if cached is not None else None

    def _read_note(self, note_id: str) -> Optional[Document]:
        cached = self._cached_fulltext(note_id)
        if cached is not None:
            return cached
        note, base = self._find_note(note_id)
        if note is None:
            return None
        return self._paper_text(self._note_to_document(note), note_id, base)

    def _paper_text(self, doc: Document, note_id: str, base: Optional[str]) -> Document:
        """The note's document ``doc`` with the PDF's text as its body (kept for the next read), or
        ``doc`` as the abstract with the reason."""
        key = cache.make_key("openreview", "fulltext", note_id)
        token = self._live_token()
        if token is None:
            return self._abstract_only(doc, f"no logged-in OpenReview session ({self._no_session_reason()})")
        from omniseek.core.sources.scrape import pdf_source
        missing = pdf_source.reader_missing()
        if missing:
            return self._abstract_only(doc, f"this install cannot read PDFs: {missing}")
        with tempfile.TemporaryDirectory(prefix="omniseek-openreview-") as d:
            dest = os.path.join(d, "paper.pdf")
            status, _ctype, size, reason = self._download_pdf(note_id, token, dest, base=base)
            if size is None:
                return self._abstract_only(doc, f"the PDF download failed: {reason}")
            got = pdf_source.extract_text(dest, label=f"openreview pdf {note_id}")
        if got is None:
            return self._abstract_only(doc, "the PDF has no extractable text (it did not open, or it has no text layer)")
        text, n_pages, _meta_title, truncated, quality = got
        meta = dict(doc.metadata or {})
        meta.update({"fulltext": True, "abstract": doc.content, "pages": n_pages,
                     "extracted_chars": len(text), "truncated": truncated, "pdf_bytes": size})
        if quality:
            meta["text_quality"] = quality
        full = doc.model_copy(update={"content": text, "metadata": meta})
        cache.set(key, full.model_dump(mode="json"), ttl=1800)
        return full

    def _find_note(self, note_id: str) -> tuple[Optional[dict], Optional[str]]:
        """``(note, the API base that holds it)``: v2 first, then v1, as fetch_reviews does (a paper
        lives on exactly one backend; sciA_xgYofB, ICLR 2023, only on v1). ``(None, None)`` on neither."""
        for base in (API_BASE, API_BASE_V1):
            data = self._api_get("/notes", {"id": note_id}, base=base)
            if (data or {}).get("notes"):
                return data["notes"][0], base
        return None, None

    def _live_token(self) -> Optional[str]:
        """The token the last login left, if still fresh; never logs in (``_get_token`` would)."""
        if self._token and time.time() < self._token_expires_at - 600:
            return self._token
        return None

    def _no_session_reason(self) -> str:
        if not auth.is_configured("openreview"):
            return "credentials not configured"
        if self._login_status == 429:
            return _probe.rate_limited("api2.openreview.net", self._login_retry_after)
        if self._login_issue:
            return f"login failed: {self._login_issue}"
        if self._login_status:
            return f"login failed: HTTP {self._login_status}"
        return "login gave no token"

    @staticmethod
    def _abstract_only(doc: Document, reason: str) -> Document:
        """The abstract document, saying why it is not the full text (the omniseek_read result carries
        the metadata, so the reader sees the reason beside the abstract)."""
        logger.info("OpenReview full text unavailable for %s: %s", doc.source_id, reason)
        diag.note("openreview.pdf", url=doc.url, body=reason)
        meta = dict(doc.metadata or {})
        meta.update({"fulltext": False, "fulltext_reason": reason})
        return doc.model_copy(update={"metadata": meta})

    def _download_pdf(self, note_id: str, token: str, dest: str,
                      base: str = API_BASE) -> tuple[Optional[int], str, Optional[int], str]:
        """Stream the submission PDF from the attachment endpoint of ``base`` (the backend that holds the
        note: v1 serves its own notes' PDFs the same way), with ``token``, to the file ``dest``.

        Returns ``(HTTP status or None, content type, bytes written or None, reason)``; the byte count
        is set only for a 2xx whose body starts with ``%PDF-`` and fits the DOCUMENT cap
        (``http.DOC_MAX_BYTES``, EYE-RSS-3: the 30MB cap on an API answer refused a 31MB paper), else
        ``dest`` is left absent and the reason says what came back (a challenged request reads
        ``HTTP 403 ChallengeRequiredError: ...``). The reason never carries the token. The download is
        ``http.download_to_file`` (streamed to disk, never whole in memory): every redirect hop goes
        through the redirect rule (its host's gates, the _netguard check), and the Authorization header
        is dropped when a hop leaves the host. Shared by fetch_url and scripts/probe_openreview_pdf.py."""
        url = f"{base}{_ATTACHMENT_PATH}?{urlencode({'id': note_id, 'name': 'pdf'})}"
        info: dict = {}
        try:
            size = http.download_to_file(url, dest, max_bytes=http.DOC_MAX_BYTES,
                                         timeout=_PDF_TIMEOUT_S,
                                         headers={"Authorization": f"Bearer {token}"}, info=info)
        except http.DownloadError as exc:
            if exc.status is not None and exc.status >= 400:
                return exc.status, exc.content_type, None, _answer_reason(_ErrorAnswer(exc), scrub=(token,))
            return exc.status, exc.content_type, None, f"the PDF is over the {http.DOC_MAX_BYTES} byte document cap"
        except Exception as exc:  # noqa: BLE001
            reason = f"request failed: {type(exc).__name__}"
            if isinstance(exc, upstreams.UpstreamBusy):
                reason += f": {exc}"
            logger.warning("OpenReview PDF download failed for %s: %s", note_id, reason)
            return None, "", None, reason
        status, ctype = info.get("status") or 200, info.get("content_type") or ""
        with open(dest, "rb") as f:
            head = f.read(5)
        if head != b"%PDF-":
            try:
                os.remove(dest)
            except OSError:
                pass
            return status, ctype, None, f"HTTP {status} but the body is not a PDF (content type {ctype or 'none'})"
        return status, ctype, size, "ok"

    def health_check(self) -> tuple[Optional[bool], str]:
        """Ask the search path itself, as search asks it: the same ``/notes/search`` request
        (``source=forum``), anonymously first, for ONE paper on a term that always has papers. Only
        when the anonymous request is refused (401 / 403) does it go on with the login token, as before
        (EYE-FIX-1109). Reusing a still-valid token is not evidence by itself (until 2026-10-04 it read
        None, so a long-running process never verified this source); the token is reused, never re-made
        per check (the /login endpoint allows only 3 attempts per window, see OmniSeek-recon-openreview
        note).

        True: a JSON answer with a ``notes`` list holding at least one note. None: HTTP 429 (from the
        search or from a needed login). False: any other error, an unparseable answer, zero notes. A
        kept token the search refuses (401 / 403) is replaced by ONE fresh login; a failed re-login
        is False."""
        verdict = self._probe_search(None)
        if verdict[0] is not _RELOGIN:
            return verdict
        if not auth.is_configured("openreview"):
            return False, f"credentials not configured: {where.show(where.credentials())}/openreview.json ({auth.template_hint('openreview')})"
        kept = bool(self._token and time.time() < self._token_expires_at - 600)
        token = self._get_token()
        if token is None:
            return self._login_verdict("login")
        verdict = self._probe_search(token)
        if verdict[0] is _RELOGIN and kept:
            self._token, self._token_expires_at = None, 0.0
            token = self._get_token()
            if token is None:
                return self._login_verdict(f"the kept token was refused ({verdict[1]}); re-login")
            verdict = self._probe_search(token)
        if verdict[0] is _RELOGIN:
            return False, f"search refused a fresh login token ({verdict[1]})"
        return verdict

    def _login_verdict(self, what: str) -> tuple[Optional[bool], str]:
        if self._login_status == 429:
            return None, f"{_probe.rate_limited('api2.openreview.net', self._login_retry_after)} ({what})"
        return False, f"{what} failed: {self._login_issue or 'no token'}"

    def _probe_search(self, token: Optional[str]) -> tuple:
        """One ``/notes/search`` probe, with ``token`` or anonymously (None). A failure is noted with
        its address (the exit-node sweep refetches it)."""
        url = f"{API_BASE}/notes/search"
        try:
            resp = httpx.get(
                url,
                params={"term": _PROBE_TERM, "limit": 1, "source": "forum"},
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            diag.note("openreview.search", url=url, exc=exc)
            return False, f"search probe failed: {type(exc).__name__}: {exc}"
        if resp.status_code >= 400 and resp.status_code not in (401, 403, 429):
            diag.note("openreview.search", url=str(resp.url), status=resp.status_code,
                      body=_answer_reason(resp, scrub=(token,)))
        if resp.status_code == 429:
            return None, _probe.rate_limited("api2.openreview.net", upstreams.parse_retry_after(resp.headers))
        if resp.status_code in (401, 403):
            return _RELOGIN, f"HTTP {resp.status_code}"
        if resp.status_code != 200:
            return False, f"search probe: HTTP {resp.status_code}"
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return False, "search probe: the answer is not JSON"
        notes = data.get("notes") if isinstance(data, dict) else None
        if not isinstance(notes, list):
            return False, "search answered without a notes list"
        if not notes:
            return False, f"search returned 0 papers for {_PROBE_TERM!r}"
        return True, f"OK (search answered {len(notes)} paper for {_PROBE_TERM!r})"

    @staticmethod
    def _note_to_document(note: dict) -> Document:
        content = note.get("content") or {}
        # OpenReview v2 wraps fields as {"value": ..., "readers": ...}
        def _val(field):
            v = content.get(field)
            if isinstance(v, dict):
                return v.get("value")
            return v

        title = _val("title") or "(no title)"
        abstract = _val("abstract") or ""
        authors = _author_names(_val("authors"))
        author_str = (", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")) if authors else None

        # Date from cdate (ms since epoch)
        date = None
        cdate = note.get("cdate")
        if cdate:
            try:
                date = datetime.fromtimestamp(cdate / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None

        # v1 carries one ``invitation`` string where v2 carries ``invitations`` (as in the review parse)
        invitations = note.get("invitations") or ([str(note["invitation"])] if note.get("invitation") else [])
        venue = _val("venue") or (invitations[0] if invitations else None)

        return Document(
            source="openreview",
            source_id=note.get("id", ""),
            url=f"https://openreview.net/forum?id={note.get('id', '')}",
            title=title,
            content=abstract,
            author=author_str,
            date=date,
            tags=[venue] if venue else [],
            metadata={
                "venue": venue,
                "invitations": invitations,
                "forum": note.get("forum"),
                "raw": jsonsafe(note),  # OpenReview note's original API dict
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(OpenReviewAdapter())
