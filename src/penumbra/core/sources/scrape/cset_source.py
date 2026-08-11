"""CSET (Georgetown CSET): AI-policy think-tank reports via the WordPress REST API.

The Center for Security and Emerging Technology (cset.georgetown.edu) is the leading
US think-tank on AI policy, compute / semiconductor export controls, AI safety and
national-security implications of emerging tech. Its analysis (reports, data briefs,
blog, expert commentary) is the empty think-tank cell in Penumbra: high-signal,
policy-grade primary analysis the open web buries under news re-prints.

The site runs on WordPress, whose REST API exposes every published post keyless:

    GET https://cset.georgetown.edu/wp-json/wp/v2/posts?search=<q>&per_page=N&_embed

Response: a JSON list of posts, each with ``title.rendered`` (HTML, entity-encoded),
``excerpt.rendered`` / ``content.rendered`` (HTML prose), ``link`` (the canonical
article URL), ``date`` (ISO ``YYYY-MM-DDTHH:MM:SS``), and (with ``_embed``) an
``_embedded`` block carrying the author and the post's taxonomy terms (topics / tags).

Thin subclass over BaseScrapeAdapter: the cache check / atomic set_docs /
self-registration ritual lives in the base; this adapter declares its facets and
fills the two hooks (_raw_fetch = the /posts search GET; _to_documents = the
post -> report-Document map). ``rank`` stays default-False: the WP search
endpoint already returns server relevance order, so we keep it byte-faithful and let
the eye's ranked search re-score across sources when it needs cross-source relevance.
"""

from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core import http, relevance
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

API_URL = "https://cset.georgetown.edu/wp-json/wp/v2/posts"
TIMEOUT = 15
# WordPress 'search' relevance is weak (LIKE-based + date-skewed), so a narrow per_page can
# miss CSET's own on-topic reports while returning off-topic recent posts (proven in a live
# WebSearch head-to-head). Pull a WIDE candidate pool, then re-rank locally with the eye's
# shared BM25 scorer and cap to the caller's limit (see _to_documents).
_CANDIDATE_POOL = 30


class CSETAdapter(BaseScrapeAdapter):
    name = "cset"
    needs_credentials = False
    description = "CSET (Georgetown): US AI-policy think-tank reports on compute/export controls, AI safety, and national security (keyless WordPress REST API)"
    cache_ttl = 900

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["policy", "safety"]
    modes = ["STRUCTURE", "RECALL"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # _embed pulls the author + taxonomy terms inline so we avoid N follow-up GETs.
        # Pull a WIDE candidate pool (not just `limit`): WP search relevance is weak, so the
        # on-topic report can sit past a narrow window; _to_documents re-ranks + caps.
        return http.get_json(
            API_URL,
            params={"search": query, "per_page": max(limit, _CANDIDATE_POOL), "_embed": "1"},
            timeout=TIMEOUT,
        )

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # Byte-faithful async twin of _raw_fetch: same URL/params/timeout, only the shared-http
        # egress swaps get_json -> aget_json (single call, so a single mirror).
        return await http.aget_json(
            API_URL,
            params={"search": query, "per_page": max(limit, _CANDIDATE_POOL), "_embed": "1"},
            timeout=TIMEOUT,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, list):
            return []  # WP returns a bare list of posts; an error payload is a dict -> skip
        docs: list[Document] = []
        for post in raw:  # the WIDE candidate pool (see _raw_fetch); re-ranked + capped below
            if not isinstance(post, dict):
                continue
            try:
                doc = self._post_to_doc(post)
            except Exception as exc:  # noqa: BLE001 (a malformed post is skipped, not fatal)
                logger.debug("Skipping malformed CSET post: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
        # WP 'search' is LIKE-based + date-skewed (proven to dump off-topic recent posts while
        # missing CSET's own on-topic reports). Re-rank the wide pool with the eye's shared BM25
        # scorer (title 3x + content 1x), keep only query matches, cap to limit -> a real
        # relevance search. A term-less query keeps WP's own order (relevance.query_terms empty).
        if docs and relevance.query_terms(query):
            scores = relevance.doc_scores(docs, query)
            docs = [d for _s, d in sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
                    if _s > 0.0]
        return docs[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BaseScrapeAdapter.search -> AsyncSearchCapable (the S4a fan-out awaits
        this directly; the WP /posts GET costs a COROUTINE, not a held pool thread). Shares the base async
        cache round-trip; egress via `_araw_fetch`; mapping via the SAME pure-CPU `_to_documents`
        (BM25 re-rank included) -> byte-identical to `search`."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _post_to_doc(self, post: dict) -> Optional[Document]:
        title = _clean_text((post.get("title") or {}).get("rendered"))
        if not title:
            return None

        post_id = str(post.get("id") or "")
        url = post.get("link") or ""

        # Prefer the full content body; fall back to the excerpt when content is empty.
        content = _strip_html((post.get("content") or {}).get("rendered"))
        if not content:
            content = _strip_html((post.get("excerpt") or {}).get("rendered"))

        date = _parse_date(post.get("date"))
        author = _embedded_author(post)
        tags = _embedded_terms(post)

        return Document(
            source=self.name,
            source_id=post_id or url or title,
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            tags=tags,
            metadata={
                "excerpt": _strip_html((post.get("excerpt") or {}).get("rendered")),
                "raw": jsonsafe(post),
            },
        )


def _strip_html(value: Any) -> str:
    """Lossy HTML -> text for a WordPress ``rendered`` field. Prefer markdownify (already a
    dep, keeps link/heading structure); fall back to a tag-strip + entity-unescape so the
    adapter never hard-fails on odd HTML."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        from markdownify import markdownify as html_to_md
        return html_to_md(value, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001 (markdownify can be picky on weird HTML)
        return _html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _clean_text(value: Any) -> str:
    """A WP title is short HTML (entity-encoded, occasional inline tags). Strip tags and
    unescape entities (``&#8217;`` -> the apostrophe) for a clean one-line title."""
    if not isinstance(value, str):
        return ""
    return _html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _parse_date(value: Any) -> Optional[datetime]:
    """WordPress ``date`` is local-time ISO ``YYYY-MM-DDTHH:MM:SS`` (no offset)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _embedded_author(post: dict) -> Optional[str]:
    """With ``_embed`` the author object is under ``_embedded.author[0].name`` (can be absent
    or unnamed for syndicated / external-byline posts)."""
    authors = (post.get("_embedded") or {}).get("author") or []
    for a in authors:
        if isinstance(a, dict):
            nm = (a.get("name") or "").strip()
            if nm:
                return nm
    return None


def _embedded_terms(post: dict) -> list[str]:
    """With ``_embed`` the taxonomy terms (topics / categories / tags) are grouped under
    ``_embedded['wp:term']`` as a list of term-lists. Flatten the names, drop the
    placeholder ``Uncategorized``, and de-duplicate while preserving order."""
    groups = (post.get("_embedded") or {}).get("wp:term") or []
    seen: set[str] = set()
    tags: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for term in group:
            if not isinstance(term, dict):
                continue
            nm = (term.get("name") or "").strip()
            if nm and nm.lower() != "uncategorized" and nm not in seen:
                seen.add(nm)
                tags.append(nm)
    return tags


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
