"""OpenReview adapter — uses the REST API directly (httpx).

We skip the official openreview-py package because its `editdistance`
dependency has no Python 3.13 wheel and is fragile to build. The REST
API is well-documented at https://docs.openreview.net/reference/api-v2

Reads credentials from ~/.penumbra/credentials/openreview.json:
    {"username": "you@email.com", "password": "..."}

OpenReview is the unique source where we can read actual peer reviews,
rebuttals, and meta-reviews — irreplaceable methodology learning material.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import auth, cache
from penumbra.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

API_BASE = "https://api2.openreview.net"
DEFAULT_TIMEOUT = 30

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
# is the unique-to-OpenReview signal (the Galleria peer-review dimension's raw material).
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


class OpenReviewAdapter:
    name = "openreview"
    needs_credentials = True
    description = (
        "OpenReview — peer reviews, rebuttals, meta-reviews from ICLR/NeurIPS/ICML; "
        "venue browse via `venue:` qualifier (venue:colm2025 / venue:iclr2026 / raw "
        "venueid) and a submission's actual reviews via `reviews:` (reviews:<forum_id> "
        "or a /forum?id=… URL); browse a venue's accepted papers via its venueid"
    )

    _token: Optional[str] = None
    _token_expires_at: float = 0.0

    def _get_token(self) -> Optional[str]:
        """Login or reuse token. Tokens last 12h; we refresh well before."""
        if self._token and time.time() < self._token_expires_at - 600:
            return self._token
        creds = auth.load("openreview")
        if not creds or not creds.get("username") or not creds.get("password"):
            logger.info("OpenReview credentials not configured.")
            return None
        try:
            resp = httpx.post(
                f"{API_BASE}/login",
                json={"id": creds["username"], "password": creds["password"]},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("token")
            # OpenReview tokens last ~12h; we set expiry conservatively
            self._token_expires_at = time.time() + 11 * 3600
            return self._token
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenReview login failed: %s", exc)
            return None

    def _api_get(self, path: str, params: dict) -> Optional[dict]:
        # Public notes are readable WITHOUT auth (verified live), so a missing or
        # broken login degrades to keyless access instead of returning nothing.
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = httpx.get(
                f"{API_BASE}{path}",
                params=params,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenReview API call failed: %s", exc)
            return None

    def search(self, query: str, limit: int = 10) -> list[Document]:
        forum = _parse_reviews(query)
        if forum:
            return self.fetch_reviews(forum, limit)
        terms, venueid = _parse_venue(query)
        key = cache.make_key("openreview", "search", venueid or "-", terms, limit)
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
            # OpenReview v2 search API: simplest invocation with just `term` works.
            # The `type` and `source` params from v1 are no longer accepted and
            # cause 400 Bad Request.
            data = self._api_get(
                "/notes/search",
                {"term": terms, "limit": min(limit, 100)},
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
            from penumbra.core.normalize import keyword_score_filter
            docs = keyword_score_filter(docs, terms)
        docs = docs[:limit]

        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=1800)
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
        data = self._api_get("/notes", {"forum": forum_id, "limit": 200, "sort": "cdate:asc"})
        if not data or "notes" not in data:
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
        invitations = note.get("invitations") or []
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
        host = urlparse(url).hostname or ""
        if "openreview.net" not in host:
            return None
        # Pattern: openreview.net/forum?id=<id> or /pdf?id=<id>
        from urllib.parse import parse_qs

        qs = parse_qs(urlparse(url).query)
        note_id = (qs.get("id") or [None])[0]
        if not note_id:
            return None
        data = self._api_get("/notes", {"id": note_id})
        if not data or "notes" not in data or not data["notes"]:
            return None
        return self._note_to_document(data["notes"][0])

    def health_check(self) -> tuple[bool, str]:
        if not auth.is_configured("openreview"):
            return False, "credentials not configured (see ~/.penumbra/credentials/openreview.json.template)"
        token = self._get_token()
        if token is None:
            return False, "login failed"
        return True, "OK (logged in)"

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


from penumbra.core.fetcher import register_adapter

register_adapter(OpenReviewAdapter())
