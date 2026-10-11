"""OpenReview adapter — uses the REST API directly (httpx).

We skip the official openreview-py package because its `editdistance`
dependency has no Python 3.13 wheel and is fragile to build. The REST
API is well-documented at https://docs.openreview.net/reference/api-v2

Reads credentials from ~/.omniseek/credentials/openreview.json:
    {"username": "you@email.com", "password": "..."}

OpenReview is the unique source where we can read actual peer reviews,
rebuttals, and meta-reviews — irreplaceable methodology learning material.

Full text (2026-10-10): omniseek_read on an openreview.net /pdf?id= or /forum?id= link downloads the
submission PDF through the logged-in session (the attachment endpoint the official openreview-py
client uses) and extracts it with the pdf adapter's extractor. Anonymous access to every OpenReview
entry now answers 403 ChallengeRequiredError, so without a session, or when the session is asked
to verify too, the read falls back to the title and abstract and says why in
metadata.fulltext_reason. OmniSeek never touches the anonymous challenge page.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from omniseek.core import _probe, auth, cache, diag, http
from omniseek.core.normalize import Document, jsonsafe

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

auth.write_template(
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
# Reply notes whose content carries a review-ish field → treated as a review/meta-review/rebuttal.
# (v2 wraps every field as {"value": …}; field names vary by venue, so we sniff a set of them.)
_REVIEW_FIELDS = ("rating", "review", "summary", "confidence", "soundness", "presentation",
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


class OpenReviewAdapter:
    name = "openreview"
    needs_credentials = True
    description = (
        "OpenReview: peer reviews, rebuttals, meta-reviews from ICLR/NeurIPS/ICML; "
        "venue browse via `venue:` qualifier (venue:colm2025 / venue:iclr2026 / raw "
        "venueid) and a submission's actual reviews via `reviews:` (reviews:<forum_id> "
        "or a /forum?id=… URL); browse a venue's accepted papers via its venueid; omniseek_read on a "
        "/forum?id= or /pdf?id= link returns the paper's full PDF text through the logged-in session"
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
        """Login or reuse token. Tokens last 12h; we refresh well before."""
        if self._token and time.time() < self._token_expires_at - 600:
            return self._token
        creds = auth.load("openreview")
        if not creds or not creds.get("username") or not creds.get("password"):
            logger.info("OpenReview credentials not configured.")
            return None
        self._login_issue = None
        self._login_status = None
        self._login_retry_after = None
        try:
            resp = httpx.post(
                f"{API_BASE}/login",
                json={"id": creds["username"], "password": creds["password"]},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return self._accept_login(resp.json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenReview login failed: %s", exc)
            resp_ = getattr(exc, "response", None)
            st = getattr(resp_, "status_code", None)
            self._login_status = st   # a 429 here is "not verified" for health_check, not "down"
            self._login_retry_after = _probe.retry_after_s(getattr(resp_, "headers", None))
            if resp_ is not None:
                # the error name (e.g. ChallengeRequiredError) says WHY, where "login failed" does not
                self._login_issue = _answer_reason(resp_, scrub=(creds["username"], creds["password"]))
            diag.note("openreview.login", url=f"{API_BASE}/login", status=st, exc=exc,
                      body=self._login_issue)
            return None

    async def _aget_token(self) -> Optional[str]:
        """Async twin of ``_get_token`` (S4b): same 12h-token reuse + login POST, only the raw
        ``httpx.post`` egress swaps for the shared async leaf ``http.apost_json`` (raise_for_status +
        JSON parse + failure->None, already logged + diag.note'd under its own label). Instance token
        state (``_token`` / ``_token_expires_at``) is shared with the sync path — same benign no-lock
        race as sync (a redundant login at worst)."""
        if self._token and time.time() < self._token_expires_at - 600:
            return self._token
        creds = await anyio.to_thread.run_sync(auth.load, "openreview")  # cred-file read OFF loop
        if not creds or not creds.get("username") or not creds.get("password"):
            logger.info("OpenReview credentials not configured.")
            return None
        self._login_issue = None
        data = await http.apost_json(
            f"{API_BASE}/login",
            json={"id": creds["username"], "password": creds["password"]},
            timeout=DEFAULT_TIMEOUT,
        )
        if data is None:
            return None  # login failed (http.apost_json already logged + diag.note'd)
        return self._accept_login(data)

    def _api_get(self, path: str, params: dict, base: str = API_BASE) -> Optional[dict]:
        # Auth is now REQUIRED on both backends: a keyless GET of /notes returns 403 for every forum,
        # public ones included (measured 2026-07-25; the older "public notes are readable WITHOUT
        # auth" behaviour is gone). The login degrade is kept so a broken token still egresses and
        # surfaces the real upstream status rather than failing silently here.
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = httpx.get(
                f"{base}{path}",
                params=params,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenReview API call failed: %s", exc)
            resp_ = getattr(exc, "response", None)
            st = getattr(resp_, "status_code", None)
            body = _answer_reason(resp_, scrub=(token,)) if resp_ is not None else None
            diag.note("openreview.api", url=f"{base}{path}", status=st, exc=exc, body=body)
            return None

    async def _aapi_get(self, path: str, params: dict) -> Optional[dict]:
        """Async twin of ``_api_get`` (S4b): same keyless-degrading Bearer-header logic (a missing/broken
        login still egresses without auth), only the raw ``httpx.get`` swaps for the shared async leaf
        ``http.aget_json`` (shared pool + SSRF guard + cache_only + 30MB cap + failure->None)."""
        # Public notes are readable WITHOUT auth (verified live), so a missing or
        # broken login degrades to keyless access instead of returning nothing.
        token = await self._aget_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await http.aget_json(
            f"{API_BASE}{path}",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        forum = _parse_reviews(query)
        if forum:
            # Floor the limit: a reviews: lookup wants the WHOLE thread, and the generic default of
            # 10 cuts the tail (author responses / meta-review / decision) on any busy forum.
            return self.fetch_reviews(forum, max(limit, _REVIEWS_MIN_LIMIT))
        terms, venueid = _parse_venue(query)
        key = cache.make_key("openreview", "search_forum", venueid or "-", terms, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        if venueid:
            # Venue browse: newest notes of one venue; remaining terms filter within.
            data = self._api_get(
                "/notes",
                {"content.venueid": venueid, "limit": 100, "sort": "cdate:desc"},
            )
        else:
            # OpenReview v2 search. `source: "forum"` keeps the hits to papers (submissions and
            # records). Without it the default mixes in replies (reviews, comments), which carry no
            # title and map to empty docs: measured 2026-10-04 on a credit-assignment query, 2 of 8
            # hits had a title by default and 8 of 8 with "forum". Accepted values are forum, reply
            # and all; any other value is a 400 (the older note here said `source` itself was
            # refused, which no longer holds). A paper's reviews stay reachable via `reviews:`.
            data = self._api_get(
                "/notes/search",
                {"term": terms, "limit": min(limit, 100), "source": "forum"},
            )
        if not data or "notes" not in data:
            return []

        docs: list[Document] = []
        for note in data["notes"]:
            try:
                docs.append(self._note_to_document(note))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed OpenReview note: %s", exc)
        if venueid and terms:
            from omniseek.core.normalize import keyword_score_filter
            docs = keyword_score_filter(docs, terms)
        docs = docs[:limit]

        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=1800)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b) -> AsyncSearchCapable. Mirrors ``search`` line-for-line;
        only the BLOCKING work moves off the loop:
          - the disk cache read/write -> ``anyio.to_thread.run_sync`` (SAME cache key + raw value shape);
          - the raw ``httpx`` egress (login POST + notes GET) -> the shared async leaf via
            ``_aget_token`` / ``_aapi_get`` (await ``http.apost_json`` / ``http.aget_json``);
          - the ``reviews:`` branch delegates to the sync ``fetch_reviews`` OFF-loop in a worker thread
            (its own cache round-trip + egress run byte-identically, just not on the loop — the reddit
            comment-mode precedent), keeping only the DOMINANT notes-search egress native.
        Pure-CPU parse/filter (``_note_to_document`` / ``keyword_score_filter``) stays ON the loop,
        byte-identical to ``search``."""
        forum = _parse_reviews(query)
        if forum:
            return await anyio.to_thread.run_sync(  # same limit floor as sync (whole thread, not 10)
                functools.partial(self.fetch_reviews, forum, max(limit, _REVIEWS_MIN_LIMIT)))
        terms, venueid = _parse_venue(query)
        key = cache.make_key("openreview", "search_forum", venueid or "-", terms, limit)
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        if venueid:
            # Venue browse: newest notes of one venue; remaining terms filter within.
            data = await self._aapi_get(
                "/notes",
                {"content.venueid": venueid, "limit": 100, "sort": "cdate:desc"},
            )
        else:
            # OpenReview v2 search. `source: "forum"` keeps the hits to papers (submissions and
            # records). Without it the default mixes in replies (reviews, comments), which carry no
            # title and map to empty docs: measured 2026-10-04 on a credit-assignment query, 2 of 8
            # hits had a title by default and 8 of 8 with "forum". Accepted values are forum, reply
            # and all; any other value is a 400 (the older note here said `source` itself was
            # refused, which no longer holds). A paper's reviews stay reachable via `reviews:`.
            data = await self._aapi_get(
                "/notes/search",
                {"term": terms, "limit": min(limit, 100), "source": "forum"},
            )
        if not data or "notes" not in data:
            return []

        docs: list[Document] = []
        for note in data["notes"]:
            try:
                docs.append(self._note_to_document(note))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed OpenReview note: %s", exc)
        if venueid and terms:
            from omniseek.core.normalize import keyword_score_filter
            docs = keyword_score_filter(docs, terms)
        docs = docs[:limit]

        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, [d.model_dump(mode="json") for d in docs], ttl=1800))
        return docs

    def fetch_reviews(self, forum_id: str, limit: int = 20) -> list[Document]:
        """Fetch the actual peer reviews / rebuttals / meta-reviews of one submission. The reply
        notes of a forum (``/notes?forum=<id>``) carry the reviewer ratings + text; we keep the
        ones whose content sniffs review-ish (a review/meta-review/rebuttal/decision). Public
        forums are readable WITHOUT auth, so this degrades to keyless access."""
        forum_id = (forum_id or "").strip()
        if not forum_id:
            return []
        key = cache.make_key("openreview", "reviews", forum_id, limit)
        cached = cache.get(key)
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
    def _review_note_to_document(note: dict, forum_id: str) -> Optional[Document]:
        """Pure parse of one reply note → a Document (reviewer rating + text). Returns None
        when the note carries no review-ish field (a bare comment with no signal). No network."""
        content = note.get("content") or {}

        def _val(field):
            v = content.get(field)
            return v.get("value") if isinstance(v, dict) else v

        present = [f for f in _REVIEW_FIELDS if _val(f) not in (None, "")]
        if not present:
            return None  # not a review/meta-review/rebuttal, skip
        # v2 carries invitations[] (a list); v1 carries invitation (a single string, e.g.
        # ".../Paper10022/-/Official_Review"). Read both, or every v1 note loses its kind signal and
        # falls back to content sniffing alone.
        invitations = note.get("invitations") or []
        if not invitations and note.get("invitation"):
            invitations = [str(note["invitation"])]
        kind = "review"
        inv_blob = " ".join(invitations).lower()
        if "meta" in inv_blob or _val("metareview") or _val("meta_review"):
            kind = "meta_review"
        elif "decision" in inv_blob or _val("decision"):
            kind = "decision"
        elif "rebuttal" in inv_blob or _val("rebuttal"):
            kind = "rebuttal"
        elif "comment" in inv_blob and not (_val("rating") or _val("review")):
            kind = "comment"
        rating = _val("rating") or _val("recommendation")
        confidence = _val("confidence")
        # Body = every present review field, labeled (rating/soundness/strengths/weaknesses/…).
        body_parts = []
        for f in present:
            v = _val(f)
            if isinstance(v, (list, dict)):
                v = jsonsafe(v)
            body_parts.append(f"{f}: {v}")
        body = "\n".join(body_parts)
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
                "confidence": confidence,
                "review_fields": present,
                "invitations": invitations,
                "raw": jsonsafe(note),  # OpenReview reply note's original API dict
            },
        )

    def fetch_url(self, url: str) -> Optional[Document]:
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
        key = cache.make_key("openreview", "fulltext", note_id)
        cached = cache.get(key)
        if cached is not None:
            # omniseek_read pages a long paper by start_char: one download serves every page
            return Document.model_validate(cached)
        data = self._api_get("/notes", {"id": note_id})
        if not data or "notes" not in data or not data["notes"]:
            return None
        doc = self._note_to_document(data["notes"][0])
        token = self._live_token()
        if token is None:
            return self._abstract_only(doc, f"no logged-in OpenReview session ({self._no_session_reason()})")
        status, _ctype, content, reason = self._download_pdf(note_id, token)
        if content is None:
            return self._abstract_only(doc, f"the PDF download failed: {reason}")
        from omniseek.core.sources.scrape.pdf_source import extract_text
        got = extract_text(content, label=f"openreview pdf {note_id}")
        if got is None:
            return self._abstract_only(doc, "the PDF has no extractable text (or PyMuPDF is missing)")
        text, n_pages, _meta_title, truncated = got
        meta = dict(doc.metadata or {})
        meta.update({"fulltext": True, "abstract": doc.content, "pages": n_pages,
                     "extracted_chars": len(text), "truncated": truncated, "pdf_bytes": len(content)})
        full = doc.model_copy(update={"content": text, "metadata": meta})
        cache.set(key, full.model_dump(mode="json"), ttl=1800)
        return full

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

    def _download_pdf(self, note_id: str, token: str) -> tuple[Optional[int], str, Optional[bytes], str]:
        """GET the submission PDF through the attachment endpoint with ``token``.

        Returns ``(HTTP status or None, content type, PDF bytes or None, reason)``; the bytes are
        set only for a 200 whose body starts with ``%PDF-`` and fits the shared 30MB body cap
        (``http.MAX_BYTES``), else the reason says what came back (a challenged request reads
        ``HTTP 403 ChallengeRequiredError: ...``). The reason never carries the token. A redirect
        is followed under the shared redirect rule (``http.direct``: every hop takes its host's gate);
        httpx drops the Authorization header when a hop leaves the host. Shared by
        fetch_url and scripts/probe_openreview_pdf.py."""
        try:
            resp = http.direct(
                "GET",
                f"{API_BASE}{_ATTACHMENT_PATH}",
                params={"id": note_id, "name": "pdf"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"request failed: {type(exc).__name__}"
            logger.warning("OpenReview PDF download failed for %s: %s", note_id, reason)
            return None, "", None, reason
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200:
            return resp.status_code, ctype, None, _answer_reason(resp, scrub=(token,))
        body = resp.content
        if len(body) > http.MAX_BYTES:
            return resp.status_code, ctype, None, f"the PDF is over the {http.MAX_BYTES} byte cap ({len(body)} bytes)"
        if not body.startswith(b"%PDF-"):
            return resp.status_code, ctype, None, f"HTTP 200 but the body is not a PDF (content type {ctype or 'none'})"
        return resp.status_code, ctype, body, "ok"

    def health_check(self) -> tuple[Optional[bool], str]:
        """Ask the search path itself: the same ``/notes/search`` request (``source=forum``) with the
        same login token, for ONE paper on a term that always has papers. Reusing a still-valid
        token is not evidence by itself (until 2026-10-04 it read None, so a long-running process
        never verified this source); the token is reused, never re-made per check (the /login
        endpoint allows only 3 attempts per window, see OmniSeek-recon-openreview note).

        True: a JSON answer with a ``notes`` list holding at least one note. None: HTTP 429 (from the
        search or from a needed login). False: any other error, an unparseable answer, zero notes. A
        kept token the search refuses (401 / 403) is replaced by ONE fresh login; a failed re-login
        is False."""
        if not auth.is_configured("openreview"):
            return False, "credentials not configured (see ~/.omniseek/credentials/openreview.json.template)"
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

    def _probe_search(self, token: str) -> tuple:
        try:
            resp = httpx.get(
                f"{API_BASE}/notes/search",
                params={"term": _PROBE_TERM, "limit": 1, "source": "forum"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"search probe failed: {type(exc).__name__}: {exc}"
        if resp.status_code == 429:
            return None, _probe.rate_limited("api2.openreview.net", _probe.retry_after_s(resp.headers))
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
        authors = _val("authors") or []
        if isinstance(authors, list):
            author_str = ", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")
        else:
            author_str = str(authors) if authors else None

        # Date from cdate (ms since epoch)
        date = None
        cdate = note.get("cdate")
        if cdate:
            try:
                date = datetime.fromtimestamp(cdate / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None

        invitations = note.get("invitations") or []
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
                "invitations": note.get("invitations"),
                "forum": note.get("forum"),
                "raw": jsonsafe(note),  # OpenReview note's original API dict
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(OpenReviewAdapter())
