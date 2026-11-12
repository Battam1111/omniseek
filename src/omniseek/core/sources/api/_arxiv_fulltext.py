"""arXiv full text through export.arxiv.org (2026-10-24).

An arxiv.org /pdf/<id> or /html/<id> link is read from export.arxiv.org, the host arXiv sets aside for
programs (info.arxiv.org/help/bulk_data.html: "specifically set aside for programmatic access"), under
that host's own full-text gate (upstreams.json "arxiv_export", path-scoped to /pdf/ and /html/): not
arxiv.org's robots.txt Crawl-delay of 15 s, which is meant for the interactive site, and not the 3 s
gate of the API on the same host. Both formats are documents: they stream to a temporary file through
http.download_to_file under http.DOC_MAX_BYTES and never sit whole in memory.

The /html/ page is arXiv's LaTeXML render. ``html_to_markdown`` turns its article into text whose
section titles are ``#`` lines (title 1, sections, appendices, references 2, subsections 3, deeper 4),
so omniseek_read's layered reading (level="head" / "section") cuts it by section; math keeps its TeX as
``$...$``. A page that is not a LaTeXML render (a paper with no HTML version) gives None.

This module holds no adapter: ArxivAdapter.fetch_url (arxiv_source.py) decides what to read and when to
fall back; the document reader (docreader.py) uses ``export_url`` to send its arXiv downloads here too.
The file name does not end in ``_source``, so the auto-discovery walk skips it.
"""

from __future__ import annotations

import logging
import re
import tempfile
from typing import Optional
from urllib.parse import urlsplit

from omniseek.core import http, upstreams
from omniseek.core.normalize import html_fragment_text

logger = logging.getLogger(__name__)

EXPORT_HOST = "export.arxiv.org"
_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", EXPORT_HOST})
_KINDS = ("pdf", "html")
# A new-style id (2305.18290, 2305.18290v3) or an old-style one (hep-th/9901001, math.GT/0309136v2).
_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")
# One download's own timeout. A PDF and then the html of the same id fit the adapter's fetch_timeout.
DOWNLOAD_TIMEOUT_S = 40
# Same cap as an extracted PDF (pdf_source._MAX_CHARS), so the two formats of one paper are alike.
MAX_CHARS = 250_000


def parse(url: str) -> Optional[tuple[str, str]]:
    """``(kind, arxiv id)`` for an arXiv full-text link (kind "pdf" or "html"), else None. Accepts
    arxiv.org, www.arxiv.org and export.arxiv.org, a trailing slash and a ".pdf" suffix; an id must
    look like an arXiv id, so a figure under /html/<id>/x1.png is not a paper."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in _ARXIV_HOSTS:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2 or segs[0].lower() not in _KINDS:
        return None
    kind = segs[0].lower()
    rest = "/".join(segs[1:])
    if kind == "pdf" and rest.lower().endswith(".pdf"):
        rest = rest[:-4]
    return (kind, rest) if _ID_RE.match(rest) else None


def export_url_for(kind: str, arxiv_id: str) -> str:
    return f"https://{EXPORT_HOST}/{kind}/{arxiv_id}"


def export_url(url: str) -> Optional[str]:
    """The export.arxiv.org URL of an arXiv full-text link, or None when ``url`` is not one."""
    got = parse(url)
    return export_url_for(*got) if got else None


def download(src: str, dest: str) -> int:
    """``src`` (an export.arxiv.org full-text URL) streamed to ``dest`` under the document cap, with
    OmniSeek's own User-Agent. Raises what http.download_to_file raises (UpstreamBusy when the gate
    cannot admit it, DownloadError for a refused answer)."""
    return http.download_to_file(src, dest, max_bytes=http.DOC_MAX_BYTES, timeout=DOWNLOAD_TIMEOUT_S,
                                 headers={"User-Agent": http.USER_AGENT})


def failure_reason(exc: BaseException) -> str:
    """One line for a download that did not give a file: a gate that held it back says so (nothing
    was sent), an answer says its HTTP status."""
    if isinstance(exc, upstreams.UpstreamBusy):
        return f"not sent: {exc}"
    if isinstance(exc, http.DownloadError):
        return f"download failed: {exc}"
    return f"download failed: {type(exc).__name__}: {exc}"


def temp_dir():
    """A private temporary directory for one read (removed with its files when the block ends)."""
    return tempfile.TemporaryDirectory(prefix="omniseek-arxiv-")


def read_html_file(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


# LaTeXML title classes -> markdown heading level. Anything else that LaTeXML renders as a heading (a
# theorem, a proof, a run-in paragraph title) stays in the text as bold, not as a section.
_HEAD_LEVEL = (
    ("ltx_title_document", 1), ("ltx_title_part", 1), ("ltx_title_chapter", 1),
    ("ltx_title_abstract", 2), ("ltx_title_section", 2), ("ltx_title_appendix", 2),
    ("ltx_title_bibliography", 2), ("ltx_title_index", 2), ("ltx_title_glossary", 2),
    ("ltx_title_subsection", 3), ("ltx_title_subsubsection", 4),
)
_DROP = ("script", "style", "button", "nav", "img", "svg", "header", "footer",
         ".ltx_page_navbar", ".ltx_page_footer", ".ltx_page_header")


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def html_to_markdown(page: str) -> Optional[tuple[str, str]]:
    """``(title, text)`` for an arXiv LaTeXML page, the text in markdown with ``#`` section lines;
    None when the page holds no LaTeXML document (``article.ltx_document``)."""
    from bs4 import BeautifulSoup, NavigableString
    from markdownify import markdownify

    soup = BeautifulSoup(page or "", "lxml")
    art = soup.select_one("article.ltx_document") or soup.select_one(".ltx_document")
    if art is None:
        return None
    for sel in _DROP:
        for el in art.select(sel):
            el.decompose()
    for m in art.find_all("math"):
        tex = _flat(m.get("alttext") or m.get_text(" ", strip=True))
        m.replace_with(NavigableString(f"$${tex}$$" if m.get("display") == "block" else f"${tex}$"))
    title = ""
    for h in art.find_all(re.compile(r"^h[1-6]$")):
        classes = h.get("class") or []
        level = next((lv for cls, lv in _HEAD_LEVEL if cls in classes), None)
        text = _flat(h.get_text(" ", strip=True))
        if level == 1 and not title and "ltx_title_document" in classes:
            title = text
        new = soup.new_tag(f"h{level}" if level else "strong")
        new.string = text
        h.replace_with(new)
    md = markdownify(str(art), heading_style="ATX", escape_asterisks=False, escape_underscores=False,
                     escape_misc=False, strip=["a"])
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if not title:
        title = _flat(soup.title.get_text()) if soup.title else ""
    return title, md


def page_gist(page: str, limit: int = 120) -> str:
    """The first words of a page's visible text, for a reason line."""
    return html_fragment_text(page)[:limit]
