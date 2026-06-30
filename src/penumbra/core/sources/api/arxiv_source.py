"""arXiv adapter — queries the arXiv Atom API directly through the shared http helpers.

arXiv has a free public API (``export.arxiv.org/api/query``) with no auth. We hit it via
``penumbra.core.http`` (shared UA / timeout / 30MB size-cap / streaming) and parse the Atom feed with
``feedparser`` — the SAME parser the official ``arxiv`` package uses internally, so the
field mapping is well-understood.

**2026-06-04 (P29):** migrated OFF the ``arxiv`` library. That library fetches via
``urllib`` with NO request timeout (it blocked all the way to our outer ``fetch_one``
deadline under arXiv's throttling) and uses its own dedicated User-Agent that arXiv
rate-limits independently. Routing through ``http.py`` gives a real per-request timeout,
our own UA, and the body-size cap — and degrades to ``[]`` on failure instead of blocking.

Native query syntax passes straight through: the API's ``search_query`` forwards the
string verbatim, so field prefixes + booleans work as-is — ``cat:cs.LG``, ``au:bengio``,
``ti:transformer``, ``abs:diffusion``, ``ti:llm AND cat:cs.CL``.

**B2 migration:** now rides ``BaseAPIAdapter`` (template-method base). The two hooks
``_raw_fetch`` / ``_to_document`` carry the verbatim arXiv I/O + field mapping; the base
supplies the cache-checked ``search`` skeleton + auto-registration. ``rank_locally=False``
preserves the server's ``sortBy=relevance`` order byte-for-byte (no local re-rank, exactly
as the hand-written form did). ``fetch_url`` (id_list by-id lookup) and ``health_check``
(treats HTTP 429 as alive) are overridden because they differ from the base defaults.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import feedparser
import httpx

from penumbra.core import http
from penumbra.core.normalize import Document
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

_API = "https://export.arxiv.org/api/query"

# Global in-flight cap on the arXiv host (export.arxiv.org), which rate-limits aggressively (this
# adapter migrated off the arxiv lib for exactly that). arXiv rides the shared http helper, which has
# NO per-host cap, so under the 64-worker broad fan-out / many agents it storms one throttled host.
# This semaphore (held only around the egress) caps concurrent arXiv requests so a burst paces through
# instead of cascading into 429s, mirroring _s2 / _openalex / reddit.
_ARXIV_MAX_INFLIGHT = 4
_arxiv_sema = threading.BoundedSemaphore(_ARXIV_MAX_INFLIGHT)


def _arxiv_get_text(url: str, **kwargs):
    """Single arXiv egress chokepoint: both http.get_text calls (search + by-id) pass through here so
    the global in-flight cap (_arxiv_sema) bounds concurrent requests to the throttled arXiv host."""
    with _arxiv_sema:
        return http.get_text(url, **kwargs)


class ArxivAdapter(BaseAPIAdapter):
    name = "arxiv"
    needs_credentials = False
    description = "arXiv preprints — 3M+ papers across physics, math, CS, biology"

    # arXiv's API returns relevance-sorted results (sortBy=relevance); keep that
    # server order verbatim — no local re-rank — exactly as the hand form did.
    rank_locally = False
    cache_ttl = 3600
    url_host = "arxiv.org"

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        xml = _arxiv_get_text(_API, params={
            "search_query": query,
            "max_results": max(1, min(limit, 100)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        if not xml:
            return []  # network failure / timeout / oversize → empty (do NOT cache)
        feed = feedparser.parse(xml)
        return feed.entries

    def _to_document(self, raw) -> Document:
        return self._entry_to_document(raw)

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        # arXiv URLs: arxiv.org/abs/XXXX.YYYYY or arxiv.org/pdf/XXXX.YYYYY
        host = urlparse(url).hostname or ""
        if "arxiv.org" not in host:
            return None
        # /pdf/ URLs are full-text PDFs → defer to the pdf adapter (it extracts the WHOLE paper);
        # arxiv here only serves the abstract+metadata for /abs/. Skipping /pdf/ avoids shadowing it.
        if "/pdf/" in (urlparse(url).path or "").lower():
            return None
        arxiv_id = urlparse(url).path.rstrip("/").split("/")[-1].replace(".pdf", "")
        if not arxiv_id:
            return None
        xml = _arxiv_get_text(_API, params={"id_list": arxiv_id, "max_results": 1})
        if not xml:
            return None
        feed = feedparser.parse(xml)
        if not feed.entries:
            return None
        return self._entry_to_document(feed.entries[0])

    def health_check(self) -> tuple[bool, str]:
        # Light DIRECT probe with its OWN short timeout. A 429 means the API is UP and
        # merely throttling us → report healthy (search falls back to cache / degrades).
        try:
            with _arxiv_sema:  # count the health probe against the same global arXiv in-flight cap
                resp = httpx.get(
                    _API,
                    params={"search_query": "all:machine learning", "max_results": 1},
                    timeout=15,
                )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if resp.status_code == 200:
            return True, "OK"
        if resp.status_code == 429:
            return True, "OK (HTTP 429 — API alive, rate-limiting us)"
        return False, f"HTTP {resp.status_code}"

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _dt(parsed) -> Optional[datetime]:
        """feedparser ``*_parsed`` struct_time (UTC) → tz-aware datetime."""
        if not parsed:
            return None
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _entry_to_document(e) -> Document:
        entry_id = e.get("id", "") or ""
        arxiv_id = entry_id.rsplit("/", 1)[-1]
        authors = [a.get("name", "").strip() for a in (e.get("authors") or []) if a.get("name")]
        cats = [t.get("term") for t in (e.get("tags") or []) if t.get("term")]
        primary = (e.get("arxiv_primary_category") or {}).get("term")
        pdf_url = next(
            (l.get("href") for l in (e.get("links") or [])
             if l.get("title") == "pdf" or l.get("type") == "application/pdf"),
            None,
        )
        title = (e.get("title") or "").strip().replace("\n", " ")
        summary = (e.get("summary") or "").strip()
        return Document(
            source="arxiv",
            source_id=arxiv_id,
            url=entry_id,
            title=title or "(untitled)",
            content=summary,  # full abstract — no truncation
            author=", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "") or None,
            date=ArxivAdapter._dt(e.get("published_parsed")),
            tags=cats,
            metadata={
                "pdf_url": pdf_url,
                "doi": e.get("arxiv_doi"),
                "journal_ref": e.get("arxiv_journal_ref"),
                "primary_category": primary,
                "comment": e.get("arxiv_comment"),
                "updated": ArxivAdapter._dt(e.get("updated_parsed")).isoformat()
                if e.get("updated_parsed") else None,
                "all_authors": authors,
            },
        )
