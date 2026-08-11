"""Project Gutenberg — full-text public-domain books via the Gutendex API.

Project Gutenberg is the canonical corpus of >70k public-domain books (literature,
philosophy, history, classics, foundational texts). Gutendex (gutendex.com) is a
keyless JSON wrapper over the Gutenberg catalog: a single search endpoint returns
book metadata plus the actual download URLs (incl. plain-text), so the eye can both
surface a book AND hand the agent the link to UNWALL the full text.

API (keyless, no quota): GET https://gutendex.com/books/?search=<query>
Response: {count, results: [{id, title, authors:[{name,birth_year,death_year}],
           subjects:[str], languages:[str], download_count, formats:{mimetype: url}}]}

The plain-text download lives under a ``text/plain`` mimetype key (often suffixed
with a charset, e.g. ``"text/plain; charset=utf-8"``), so we match on the prefix.
The human-readable book page is ``https://www.gutenberg.org/ebooks/<id>``.

BaseScrapeAdapter (template method): the cache check / atomic set_docs /
self-registration ritual lives in the base; this adapter fills the two hooks
(_raw_fetch = the /books/ search GET; _to_documents = the book→Document map).
``rank`` stays default-False: Gutendex sorts by descending popularity for a search,
a sane server order, so we keep it byte-faithful rather than re-ranking locally.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

API_URL = "https://gutendex.com/books/"  # trailing slash: skip the /books → /books/ 301
PAGE_BASE = "https://www.gutenberg.org/ebooks"
TIMEOUT = 15


class GutenbergAdapter(BaseScrapeAdapter):
    name = "gutenberg"
    needs_credentials = False
    description = "Project Gutenberg — full-text public-domain books (literature/philosophy/classics) via the keyless Gutendex API"
    cache_ttl = 900

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["books"]
    explicit_only = (
        "gutenberg: named drill only. MEASURED 2026-07-25 over 1986 recorded searches: timed out 959 "
        "times (48% of all searches) and reached the ranked top-k ZERO times, sole-contributed ZERO "
        "times. It is not broken (a named drill returns real results), it is simply slower than the "
        "broad deadline while its domain (public-domain books) sits outside the queries this eye "
        "actually serves. Kept fully reachable by name, and excluded_relevant still recommends it when "
        "a query genuinely matches. Captain's call 2026-07-25.")
    modes = ["STRUCTURE", "UNWALL"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return http.get_json(API_URL, params={"search": query}, timeout=TIMEOUT)

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: byte-faithful mirror (same URL/params/timeout), only the
        shared-http egress swapped for its async twin (http.get_json -> await http.aget_json)."""
        return await http.aget_json(API_URL, params={"search": query}, timeout=TIMEOUT)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip;
        egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents (byte-identical to search)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        results = raw.get("results") or []
        docs: list[Document] = []
        for book in results[:limit]:
            try:
                docs.append(self._book_to_document(book))
            except Exception as exc:  # noqa: BLE001 — a malformed book entry is skipped, not fatal
                logger.debug("Skipping malformed Gutenberg book: %s", exc)
        return docs

    @staticmethod
    def _book_to_document(book: dict) -> Document:
        book_id = book.get("id") or 0
        title = book.get("title") or "(untitled)"
        page_url = f"{PAGE_BASE}/{book_id}"

        # authors: list of {name, birth_year, death_year}; Gutenberg names are "Last, First"
        authors = book.get("authors") or []
        author_names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
        author = "; ".join(author_names) if author_names else None

        subjects = [s for s in (book.get("subjects") or []) if isinstance(s, str)]
        languages = book.get("languages") or []

        # plain-text download lives under a "text/plain..." mimetype key (charset suffix varies)
        formats = book.get("formats") or {}
        text_url = next(
            (url for mime, url in formats.items()
             if isinstance(mime, str) and mime.startswith("text/plain") and isinstance(url, str)),
            None,
        )

        # content = subjects + a note + the full-text download URL (the UNWALL handle)
        content_lines: list[str] = []
        if subjects:
            content_lines.append("Subjects: " + "; ".join(subjects))
        if languages:
            content_lines.append("Language(s): " + ", ".join(languages))
        content_lines.append(
            "Public-domain book on Project Gutenberg. Read the full text at the page below; "
            "the plain-text download (if present) is the direct full-text UNWALL handle."
        )
        if text_url:
            content_lines.append(f"Full text (text/plain): {text_url}")
        content_lines.append(f"Book page: {page_url}")
        content = "\n\n".join(content_lines)

        return Document(
            source="gutenberg",
            source_id=str(book_id),
            url=page_url,
            title=title,
            content=content,
            author=author,
            signals=mk_signal(
                "downloads", book.get("download_count"),
                kind="engagement", by="gutenberg/download_count",
            ),
            tags=subjects,
            metadata={
                "languages": languages,
                "text_url": text_url,
                "formats": jsonsafe(formats),
                "raw": jsonsafe(book),
            },
        )

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
