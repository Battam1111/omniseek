"""Reproducibility-themed methodology papers — broad query, not strictly MLRC.

Original intent was to surface ML Reproducibility Challenge (MLRC) reports.
Reality (probed 2026-05-28): MLRC venue groups (`ML_Reproducibility_Challenge/<year>`)
exist on OpenReview but no notes are filed under them. Actual MLRC reports
were published in ReScience C journal (rescience.github.io) and TMLR,
neither of which has a clean OpenReview API.

So this adapter pivots from "find MLRC reports" to "find any reproducibility-
themed methodology paper from credible ML venues" — which serves the same
underlying user need (reproducibility methodology training material).

Query: OpenReview full-text search for "reproducibility" + the user's terms,
filtered to ICLR/NeurIPS/ICML/TMLR venues where rigor is reasonable.

Migrated to ``BaseScrapeAdapter`` (template method): the cache/search/registration
boilerplate now lives in the base. ``_raw_fetch`` holds the (optional) OpenReview
login + ``/notes/search`` call with the composite ``"{query} reproducibility"``
term (returns ``None`` on no-data so the base maps it to ``[]``). ``_to_documents``
holds the verbatim trusted-venue + has-title filter loop with the break-at-limit
guard — a deterministic, scorer-free filter, so ``rank=False`` keeps it
byte-identical to the hand-written form. ``fetch_url`` (reproducibility-themed
only) and ``health_check`` (the ``/notes/search`` probe) stay overridden verbatim.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import auth, http
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

API_BASE = "https://api2.openreview.net"
DEFAULT_TIMEOUT = 30

# Trusted ML venues for reproducibility content
TRUSTED_VENUE_KEYWORDS = (
    "iclr",
    "neurips",
    "icml",
    "tmlr",  # Transactions on Machine Learning Research
    "rescience",
    "reproducibility",
)


class MLRCAdapter(BaseScrapeAdapter):
    name = "mlrc"
    needs_credentials = False
    description = "Reproducibility methodology papers (broad search across ICLR/NeurIPS/ICML/TMLR)"

    cache_ttl = 3600

    def _api_get(self, path: str, params: dict) -> Optional[dict]:
        # Use openreview creds if available (better rate limit)
        creds = auth.load("openreview")
        headers = {}
        if creds and creds.get("username") and creds.get("password"):
            try:
                login = httpx.post(
                    f"{API_BASE}/login",
                    json={"id": creds["username"], "password": creds["password"]},
                    timeout=DEFAULT_TIMEOUT,
                )
                login.raise_for_status()
                token = login.json().get("token")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            except Exception as exc:  # noqa: BLE001
                logger.debug("OpenReview login failed for MLRC, using public: %s", exc)
        try:
            resp = httpx.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenReview MLRC API call failed: %s", exc)
            return None

    async def _aapi_get(self, path: str, params: dict) -> Optional[dict]:
        """Async twin of ``_api_get`` (S4b): SAME optional-login-then-GET logic (a missing/broken login
        still egresses to the public endpoint), only the raw ``httpx`` egress swaps for the shared async
        leaves — the login ``httpx.post`` → ``await http.apost_json`` and the data ``httpx.get`` →
        ``await http.aget_json`` (shared pool + SSRF guard + cache_only + 30MB cap + failure→None, each
        already logged + diag.note'd under its own label). Mirrors the sibling openreview_source._aapi_get."""
        # Use openreview creds if available (better rate limit)
        creds = auth.load("openreview")
        headers = {}
        if creds and creds.get("username") and creds.get("password"):
            login = await http.apost_json(
                f"{API_BASE}/login",
                json={"id": creds["username"], "password": creds["password"]},
                timeout=DEFAULT_TIMEOUT,
            )
            token = login.get("token") if login else None  # login failed → apost_json→None → public GET
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return await http.aget_json(
            f"{API_BASE}{path}", params=params, headers=headers, timeout=DEFAULT_TIMEOUT
        )

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Compose query + OpenReview /notes/search. None on no-data → base → []."""
        # Compose query: user's terms + "reproducibility" as a sticky hook
        composite_query = f"{query} reproducibility"
        # Over-fetch heavily — OpenReview search returns mostly review/decision
        # notes (no title), so we need a big pool to filter from. Use API max.
        data = self._api_get(
            "/notes/search",
            {"term": composite_query, "limit": 100},
        )
        if not data or "notes" not in data:
            return None
        return data["notes"]

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of ``_raw_fetch``: SAME composite ``"{query} reproducibility"`` term + /notes/search
        over-fetch (limit=100), only the egress goes async via ``_aapi_get``. None on no-data (base → [])."""
        # Compose query: user's terms + "reproducibility" as a sticky hook
        composite_query = f"{query} reproducibility"
        # Over-fetch heavily — OpenReview search returns mostly review/decision
        # notes (no title), so we need a big pool to filter from. Use API max.
        data = await self._aapi_get(
            "/notes/search",
            {"term": composite_query, "limit": 100},
        )
        if not data or "notes" not in data:
            return None
        return data["notes"]

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        docs: list[Document] = []
        for note in raw:
            # Filter to trusted venues by invitation OR venue text
            invitations = note.get("invitations") or []
            invitation_blob = " ".join(invitations).lower() if isinstance(invitations, list) else str(invitations).lower()
            content = note.get("content") or {}
            venue = content.get("venue", {})
            venue_str = (venue.get("value", "") if isinstance(venue, dict) else str(venue)).lower()
            blob = invitation_blob + " " + venue_str

            if not any(k in blob for k in TRUSTED_VENUE_KEYWORDS):
                continue

            # Skip review/decision notes — they have no title (they're
            # comments on a paper). We want the actual paper submissions.
            title_field = content.get("title")
            title_val = (title_field.get("value") if isinstance(title_field, dict) else title_field) or ""
            if not title_val.strip():
                continue

            try:
                docs.append(self._note_to_document(note))
                if len(docs) >= limit:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping MLRC note: %s", exc)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of the base ``search`` (S4b) → AsyncSearchCapable. Delegates to the base's
        ``_asearch_via`` (the async twin of ``search``'s mechanism: SAME cache key, cache get/set OFF the
        loop, opt-in ``rank`` on it) so parse/cache can never drift from ``search``. Only this source's
        egress goes async: ``afetch`` = the async ``_araw_fetch`` (login POST + /notes/search GET via the
        shared async leaves); ``abuild`` = the SYNC pure-CPU ``_to_documents`` (the deterministic
        trusted-venue + has-title filter, no network in it) used verbatim on the loop."""
        return await self._asearch_via(
            query,
            limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit),
        )

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "openreview.net" not in host:
            return None
        # Don't claim arbitrary OpenReview URLs — only those that look reproducibility-themed
        if "reproducib" not in url.lower() and "ML_Reproducibility" not in url:
            return None
        from urllib.parse import parse_qs

        qs = parse_qs(urlparse(url).query)
        note_id = (qs.get("id") or [None])[0]
        if not note_id:
            return None
        data = self._api_get("/notes", {"id": note_id})
        if not data or "notes" not in data or not data["notes"]:
            return None
        return self._note_to_document(data["notes"][0])

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[bool, str]:
        data = self._api_get("/notes/search", {"term": "reproducibility methodology", "limit": 1})
        if data is None:
            return False, "API call failed"
        hits = len(data.get("notes") or [])
        return True, f"OK ({hits} hits on probe)"

    @staticmethod
    def _note_to_document(note: dict) -> Document:
        content = note.get("content") or {}

        def _val(field):
            v = content.get(field)
            if isinstance(v, dict):
                return v.get("value")
            return v

        title = _val("title") or "(no title)"
        abstract = _val("abstract") or _val("summary") or ""
        authors = _val("authors") or []
        if isinstance(authors, list):
            author_str = ", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")
        else:
            author_str = str(authors) if authors else None

        date = None
        if note.get("cdate"):
            try:
                date = datetime.fromtimestamp(note["cdate"] / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None

        venue = _val("venue") or ""
        invitations = note.get("invitations") or []

        return Document(
            source="mlrc",
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
                "year": _val("year"),
                "raw": jsonsafe(note),
            },
        )
