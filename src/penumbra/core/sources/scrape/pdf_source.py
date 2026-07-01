"""PDF full-text adapter — turn a paper's PDF URL into readable full text.

The citation sources give an abstract + (via eye_paper_enrich) the open-access PDF *url*, but
not the WHOLE paper. This closes that gap: hand it a PDF url (arxiv.org/pdf/..., or any *.pdf)
via ``eye_add_url`` and it downloads the PDF (size-capped via eye.http) and extracts the text
with PyMuPDF, so the AGENT can read the full paper and synthesize itself. This is the thin
"read the PDF" primitive we chose INSTEAD of a heavy synthesis engine: minimal code, max agent.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument

logger = logging.getLogger(__name__)

_MAX_CHARS = 250_000  # extracted-text cap for a pathologically long PDF — keep the payload sane


class PdfAdapter:
    name = "pdf"
    needs_credentials = False
    description = (
        "PDF full-text — download a paper PDF (arxiv.org/pdf/… or any *.pdf) and extract its text "
        "so you can read the WHOLE paper, not just the abstract (pair with eye_paper_enrich's pdf_url)"
    )

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        # URL-only adapter: there is no index to search, BUT the description tells the agent
        # to hand this source a PDF url, and eye_fetch routes the query here (not to fetch_url).
        # So when the query IS a PDF url, do exactly what the description promises: download +
        # extract via the same fetch_url path (and the same fitz engine eye_read_document uses).
        # A non-url topic query has nothing to match, so it still correctly returns [].
        path = (urlparse(query).path or "").lower()
        if path.endswith(".pdf") or "/pdf/" in path:
            doc = self.fetch_url(query)
            return [doc] if doc is not None else []
        return []

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        path = (urlparse(url).path or "").lower()
        if not (path.endswith(".pdf") or "/pdf/" in path):
            return None
        try:
            import fitz  # PyMuPDF; lazy so a missing dep can never break server import
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf adapter: PyMuPDF unavailable: %s", exc)
            return None

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
        return PolarisDocument(
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


from penumbra.core.fetcher import register_adapter

register_adapter(PdfAdapter())
