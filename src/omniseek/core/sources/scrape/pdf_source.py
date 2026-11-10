"""PDF full-text adapter — turn a paper's PDF URL into readable full text.

The citation sources give an abstract + (via omniseek_paper_enrich) the open-access PDF *url*, but
not the WHOLE paper. This closes that gap: hand it a PDF url (any *.pdf or /pdf/ link)
via ``omniseek_read`` and it downloads the PDF and extracts the text with PyMuPDF, so the AGENT can
read the full paper and synthesize itself. This is the thin "read the PDF" primitive we chose
INSTEAD of a heavy synthesis engine: minimal code, max agent.

The PDF streams to a temporary file (http.download_to_file) under the DOCUMENT cap,
http.DOC_MAX_BYTES, not the 30MB cap on an API answer held in memory (EYE-RSS-3, 2026-10-24: a 31MB
paper was refused). arXiv links are not read here: the arxiv source reads them from export.arxiv.org
(sources/api/_arxiv_fulltext.py), under that host's full-text gate.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

import anyio

from omniseek.core import _optdep, diag, fetcher, http
from omniseek.core.normalize import Document
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

_MAX_CHARS = 250_000  # extracted-text cap for a pathologically long PDF — keep the payload sane

# TEXT-LAYER QUALITY (SMALL-BATCH-4, 2026-11-09). A PDF's text layer can be garbage while the page
# reads fine: a custom-encoded font gives symbol soup (Kakade and Langford 2002, aoarl.pdf), a dvips
# Type3 font loses its ligatures and spaces words apart (Hauskrecht 2000, arXiv 1106.0234: "a tion",
# "b elief", "c" turned into a space). Handed back as text, a reader searched it, found nothing, and
# took that as "the paper does not say this". Two standard garbled-text readings, over the NFKC text
# (so the "fi" / "ff" ligature characters of a good PDF count as letters):
#   language share = (English function words among the words of 2+ letters + CJK characters)
#                    / (words of 2+ letters + CJK characters): the function-word ratio of English
#                    prose, the Chinese-character ratio of Chinese prose, one number for mixed text;
#                    single letters are left out so formula variables do not dilute it.
#   stray letters  = one-letter words other than a / I that sit between two words
#                    / (words + CJK characters): a split word ("b elief") leaves one, a formula's
#                    variable sits next to an operator, a digit or a bracket and does not count.
# Measured 2026-11-09 with PyMuPDF 1.27.2.3 on this function (language share / stray letters):
#   bad:    aoarl.pdf 0.004 / 0.009; arXiv 1106.0234 0.268 / 0.168
#   normal, 39 PDFs (35 arXiv papers from ML, math, physics and stats, the formula-dense ones among
#     them: math/0211159, math/0303109, math/0307245, math/0512047, math/0602302 (51 pages of
#     formulas), hep-th/9711200, hep-th/9802150, quant-ph/9508027, 0903.0340, 1011.1669; plus 4
#     Chinese: arXiv 2305.04838 and the CTAN lshort-zh-cn, ctex and thuthesis manuals):
#     language share lowest 1905.11946 0.201, then 2302.13971 0.235, 2006.11239 0.244, highest
#     math/0303109 0.374, the Chinese 0.561 to 0.967; stray letters highest math/0512047 0.028, then
#     0903.0340 0.024, 1406.2661 0.020, math/0307245 0.019, math/0602302 0.018, the Chinese at most
#     0.002. Every reading is in the SMALL-BATCH-4 report.
# Each threshold is the midpoint between the two groups on that reading: language share
# (0.004 + 0.201) / 2 = 0.10 (aoarl.pdf falls under it); stray letters (0.028 + 0.168) / 2 = 0.10
# (1106.0234, whose language share is normal, goes over it).
# Not measured (may be flagged although fine): prose in another Latin-script language (its function
# words are not in the English list), slide decks.
_FUNCTION_WORDS = frozenset(
    "the of and to in is that for it as with on be by this are we an not or from at which".split())
_TQ_MIN_LANGUAGE_SHARE = 0.10
_TQ_MAX_STRAY_LETTERS = 0.10
_TQ_WORD = re.compile(r"[A-Za-z]+")
_TQ_ALPHA_TOKEN = re.compile(r"^[A-Za-z]+$")
_TQ_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TEXT_QUALITY_HINT = (
    "this PDF's text layer is unreliable (garbled font encoding or words broken apart): read the "
    "pages as images with omniseek_view render_pages=\"1,2,...\"; a term not found in this text may still "
    "be in the paper")


def text_quality(text: str) -> Optional[dict]:
    """None when the text reads as prose; else ``{"level": "low", "readings", "thresholds", "hint"}``
    (the readings and thresholds above). Only a reading, it never changes the text."""
    norm = unicodedata.normalize("NFKC", text or "")
    cjk = len(_TQ_CJK.findall(norm))
    long_words = [w.lower() for w in _TQ_WORD.findall(norm) if len(w) >= 2]
    tokens = norm.split()
    alpha = [bool(_TQ_ALPHA_TOKEN.match(t)) for t in tokens]
    stray = sum(1 for k in range(1, len(tokens) - 1)
                if alpha[k] and len(tokens[k]) == 1 and tokens[k] not in "aAiI"
                and alpha[k - 1] and alpha[k + 1])
    language_share = (sum(w in _FUNCTION_WORDS for w in long_words) + cjk) / ((len(long_words) + cjk) or 1)
    stray_letters = stray / ((sum(alpha) + cjk) or 1)
    if language_share >= _TQ_MIN_LANGUAGE_SHARE and stray_letters <= _TQ_MAX_STRAY_LETTERS:
        return None
    return {"level": "low",
            "readings": {"language_share": round(language_share, 3), "stray_letters": round(stray_letters, 3)},
            "thresholds": {"language_share_min": _TQ_MIN_LANGUAGE_SHARE,
                           "stray_letters_max": _TQ_MAX_STRAY_LETTERS},
            "hint": TEXT_QUALITY_HINT}


def reader_missing() -> Optional[str]:
    """None when PyMuPDF imports; else the install hint for the optional [pdf] extra. An adapter that
    downloads a PDF through its own route asks this BEFORE the download, so an install without the
    extra neither fetches bytes it cannot read nor reports the gap as "no extractable text"."""
    try:
        _optdep.require("fitz", "pdf")
    except RuntimeError as exc:
        return str(exc)
    return None


def extract_text(content: Union[bytes, str, Path], *,
                 label: str = "") -> Optional[tuple[str, int, str, bool, Optional[dict]]]:
    """PDF bytes, or the path of a PDF file -> ``(text, pages, metadata title, truncated, quality)``
    with PyMuPDF, capped at ``_MAX_CHARS``; ``quality`` is ``text_quality(text)`` (None unless the text
    layer reads as garbled), for the caller to put in ``metadata.text_quality``. None when PyMuPDF is missing, the bytes do not open as a PDF,
    or there is no text layer (a scanned / image-only PDF). Shared by this adapter and any adapter that
    downloads a PDF through its own route (openreview, arxiv), so every PDF OmniSeek reads is extracted
    alike. A path is opened in place, so a large download is never read whole into memory here."""
    try:
        import fitz  # PyMuPDF; lazy so a missing dep can never break server import
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf adapter: PyMuPDF unavailable: %s", exc)
        return None
    try:
        if isinstance(content, (str, Path)):
            doc = fitz.open(str(content), filetype="pdf")
        else:
            doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf adapter: open failed (%s): %s", label, exc)
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
        return None
    return text, n_pages, meta_title, truncated, text_quality(text)


class PdfAdapter(SourceBase):
    name = "pdf"
    needs_credentials = False
    fetch_timeout = 90.0   # a document download (up to 60 s) is longer than an API answer
    description = (
        "PDF full-text: download a paper PDF (any *.pdf or /pdf/ link; arXiv links are read by the "
        "arxiv source) and extract its text "
        "so you can read the WHOLE paper, not just the abstract (pair with omniseek_paper_enrich's pdf_url)"
    )

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        # URL-only adapter: there is no index to search, BUT the description tells the agent
        # to hand this source a PDF url, and omniseek_search 单源钻取 routes the query here (not to the URL read).
        # So when the query IS a PDF url, do exactly what the description promises: download +
        # extract via the same afetch_url path (and the same fitz engine omniseek_read uses).
        # A non-url topic query (what every broad search sends) has nothing to match: [] with no I/O.
        if not _is_pdf_url(query):
            return []
        if _is_arxiv(query):
            ax = fetcher.get_adapter("arxiv")   # an arXiv PDF is the arxiv source's read
            doc = await ax.afetch_url(query) if ax is not None else None
        else:
            doc = await self.afetch_url(query)
        return [doc] if doc is not None else []

    async def afetch_url(self, url: str) -> Optional[Document]:
        if not _is_pdf_url(url):
            return None
        if _is_arxiv(url):
            return None   # the arxiv source owns these: export.arxiv.org under its full-text gate
        # The download (libcurl) and the PyMuPDF parse are blocking: a worker thread under the legacy limiter.
        return await anyio.to_thread.run_sync(self._read_pdf, url, limiter=fetcher._LEGACY_LIMITER)

    def _read_pdf(self, url: str) -> Optional[Document]:
        try:
            import fitz  # noqa: F401  PyMuPDF; lazy so a missing dep can never break server import
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf adapter: PyMuPDF unavailable: %s", exc)
            return None

        # SSRF (review finding H3): this CLAIMING adapter fetches an agent-controlled URL (any /pdf/
        # path). Since EYE-RSS-3 it goes through http.download_to_file, whose libcurl tier checks every
        # redirect hop against _netguard before sending it (http._acurl_hops), the same per-hop check
        # docreader and the audio download use; it does not pin the resolved IP the way the shared
        # httpx client's AsyncSSRFGuardTransport does (the DNS-rebind residual of those lanes). Every hop
        # takes its declared gates. Streamed to a private temporary file under the document cap.
        with tempfile.TemporaryDirectory(prefix="omniseek-pdf-") as d:
            dest = os.path.join(d, "doc.pdf")
            try:
                size = http.download_to_file(url, dest, max_bytes=http.DOC_MAX_BYTES, timeout=60,
                                             headers={"User-Agent": http.USER_AGENT})
            except Exception as exc:  # noqa: BLE001 (None on failure is the adapter contract)
                logger.warning("pdf adapter: download failed (%s): %s", url, exc)
                diag.note("pdf.download", url=url, exc=exc)
                return None
            got = extract_text(dest, label=url)
        if got is None:
            return None
        text, n_pages, meta_title, truncated, quality = got
        meta = {"pages": n_pages, "extracted_chars": len(text), "truncated": truncated, "pdf_bytes": size}
        if quality:
            meta["text_quality"] = quality

        leaf = urlparse(url).path.rstrip("/").split("/")[-1] or url
        return Document(
            source="pdf",
            source_id=leaf,
            url=url,
            title=(meta_title.strip() or leaf),
            content=text,
            metadata=meta,
        )

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # This source has no fixed upstream (it reads whatever PDF URL it is given), so the local
        # parser IS the whole of what can be verified: a present PyMuPDF is a verified True, not an
        # unverified None (pilot ruling 2026-10-04; None would leave it unverified forever). A
        # missing PyMuPDF stays False (the source cannot serve at all).
        try:
            import fitz  # noqa: F401
            return True, "OK (PyMuPDF ready; no fixed upstream to probe)"
        except Exception as exc:  # noqa: BLE001
            return False, f"PyMuPDF missing: {exc}"


def _is_pdf_url(s: str) -> bool:
    path = (urlparse(s or "").path or "").lower()
    return path.endswith(".pdf") or "/pdf/" in path


def _is_arxiv(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "arxiv.org" or host.endswith(".arxiv.org")


from omniseek.core.fetcher import register_adapter

register_adapter(PdfAdapter())
