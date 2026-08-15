"""PDF full-text adapter — turn a paper's PDF URL into readable full text.

The citation sources give an abstract + (via omniseek_paper_enrich) the open-access PDF *url*, but
not the WHOLE paper. This closes that gap: hand it a PDF url (arxiv.org/pdf/..., or any *.pdf)
via ``omniseek_read`` and it downloads the PDF (size-capped via eye.http) and extracts the text
with PyMuPDF, so the AGENT can read the full paper and synthesize itself. This is the thin
"read the PDF" primitive we chose INSTEAD of a heavy synthesis engine: minimal code, max agent.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from omniseek.core import http
from omniseek.core.normalize import Document

logger = logging.getLogger(__name__)

_MAX_CHARS = 250_000  # extracted-text cap for a pathologically long PDF — keep the payload sane


class PdfAdapter:
    name = "pdf"
    needs_credentials = False
    description = (
        "PDF full-text — download a paper PDF (arxiv.org/pdf/… or any *.pdf) and extract its text "
        "so you can read the WHOLE paper, not just the abstract (pair with omniseek_paper_enrich's pdf_url)"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # URL-only adapter: there is no index to search, BUT the description tells the agent
        # to hand this source a PDF url, and omniseek_search 单源钻取 routes the query here (not to fetch_url).
        # So when the query IS a PDF url, do exactly what the description promises: download +
        # extract via the same fetch_url path (and the same fitz engine omniseek_read uses).
        # A non-url topic query has nothing to match, so it still correctly returns [].
        path = (urlparse(query).path or "").lower()
        if path.endswith(".pdf") or "/pdf/" in path:
            doc = self.fetch_url(query)
            return [doc] if doc is not None else []
        return []

    def fetch_url(self, url: str) -> Optional[Document]:
        path = (urlparse(url).path or "").lower()
        if not (path.endswith(".pdf") or "/pdf/" in path):
            return None
        try:
            import fitz  # PyMuPDF; lazy so a missing dep can never break server import
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf adapter: PyMuPDF unavailable: %s", exc)
            return None

        # C3 CLOSED (review finding H3): this CLAIMING adapter fetches an agent-controlled URL (any
        # /pdf/ path) through the MAINLINE http.get. Redirect-SSRF on that lane is now closed by
        # safeurl.SSRFGuardTransport on the shared pooled client (S1-C3): the transport revalidates
        # EVERY redirect hop via _netguard, so a /pdf/ URL that 302s to 169.254.169.254 / a private IP
        # is refused at the connection layer for this and every other http.get caller at once (the
        # structural choke-point fix, not a one-off manual walk here). C2 closed the arbitrary-user-URL
        # lanes that use their own httpx (web_fallback, docreader); C3 closes the shared-client lane.
        resp = http.get(url)  # shared UA + redirects + 30MB cap; None on failure
        if resp is None:
            return None
        try:
            doc = fitz.open(stream=resp.content, filetype="pdf")
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf adapter: open failed (%s): %s", url, exc)
            return None
        try:
            parts: list[str] = []
            total = 0
            for page in doc:
                t = page.get_text() or ""
                parts.append(t)
                total += len(t)
                if total > _MAX_CHARS:
                    break
            n_pages = doc.page_count
            meta_title = (doc.metadata or {}).get("title") or ""
        finally:
            doc.close()

        text = "\n\n".join(parts).strip()
        truncated = len(text) > _MAX_CHARS
        text = text[:_MAX_CHARS]
        if not text:
            return None  # scanned / image-only PDF with no extractable text layer

        leaf = urlparse(url).path.rstrip("/").split("/")[-1] or url
        return Document(
            source="pdf",
            source_id=leaf,
            url=url,
            title=(meta_title.strip() or leaf),
            content=text,
            metadata={"pages": n_pages, "extracted_chars": len(text), "truncated": truncated},
        )

    def health_check(self) -> tuple[bool, str]:
        try:
            import fitz  # noqa: F401
            return True, "OK (PyMuPDF ready)"
        except Exception as exc:  # noqa: BLE001
            return False, f"PyMuPDF missing: {exc}"


from omniseek.core.fetcher import register_adapter

register_adapter(PdfAdapter())
