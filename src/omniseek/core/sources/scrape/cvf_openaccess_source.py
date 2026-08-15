"""CVF Open Access — the computer-vision venue-of-record, browsable by conference.

The Computer Vision Foundation hosts the canonical open-access proceedings for
CVPR / ICCV / WACV (and older ECCV years) at openaccess.thecvf.com: for every
accepted paper an author list, the abstract, a watermark-free PDF, the BibTeX,
and supp/code links. This gives the eye what a keyword search cannot: "list what
actually appeared at CVPR 2024", with titles, authors and canonical
openaccess.thecvf.com links. MODE: STRUCTURE (venue-of-record browse), the
computer-vision sibling of acl_anthology (NLP).

Query syntax (a venue+year token is REQUIRED; this is a browser, not a search
engine: cross-venue keyword search belongs to dblp / semantic_scholar / arxiv):
  "cvpr 2024 diffusion"        venue+year token + optional keyword filter
  "iccv2023 segmentation"      same, compact form
  "venue:WACV2024"             raw conference id passthrough
Supported acronyms: cvpr, iccv, wacv, eccv (ECCV coverage on CVF is spotty: only
some years are hosted here, the rest live on SpringerLink; a missing year -> []).

The per-conference listing page carries title / authors / PDF / BibTeX but NOT
the abstract (that lives on each paper's own HTML page). ``search`` therefore
returns those listing fields; ``fetch_url`` (or omniseek_read on a paper URL) enriches
a single paper with its full abstract from the paper page.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import anyio
import httpx
from bs4 import BeautifulSoup

from omniseek.core import cache, diag, http
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

BASE = "https://openaccess.thecvf.com"
TIMEOUT = 60  # a full listing (e.g. ICCV2023) is several MB
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")
CACHE_TTL = 7 * 86400  # published proceedings are immutable; a week is conservative

_VENUES = "cvpr|iccv|wacv|eccv"
_TOKEN_RE = re.compile(rf"\b({_VENUES})[\s-]?(\d{{4}})\b", re.IGNORECASE)
_RAW_RE = re.compile(r"(?:^|\s)venue\s*:\s*([A-Za-z]+\d{4})\b", re.IGNORECASE)


def _parse_conference(query: str) -> tuple[str, Optional[str]]:
    """Split a conference id out of the query: raw `venue:` wins, else venue+year.

    Returns (remaining_keyword_terms, conference_id | None). The conference id is
    the CVF path segment, e.g. 'CVPR2024' / 'ICCV2023' / 'WACV2024'.
    """
    m = _RAW_RE.search(query or "")
    if m:
        return _RAW_RE.sub(" ", query).strip(), m.group(1).upper()
    m = _TOKEN_RE.search(query or "")
    if m:
        conf = f"{m.group(1).upper()}{m.group(2)}"
        return _TOKEN_RE.sub(" ", query).strip(), conf
    return (query or "").strip(), None


class CVFOpenAccessAdapter:
    name = "cvf_openaccess"
    needs_credentials = False
    kind = "lookup"
    domains = ["papers"]
    regions = ["global"]
    modes = ["STRUCTURE"]
    description = (
        "CVF Open Access — 计算机视觉 venue-of-record 按会议浏览 (CVPR/ICCV/WACV, "
        "官方开放获取, keyless). 查某届会议实际收了什么: 'cvpr 2024 diffusion' / "
        "'iccv2023 segmentation' / 'venue:WACV2024'(裸会议 id). 必须带 venue+年份 "
        "token (这是会议浏览器; 跨会关键词搜索请用 dblp / semantic_scholar / arxiv). "
        "列表页给标题/作者/PDF/BibTeX, 摘要在每篇论文页 (对论文 URL 用 omniseek_read 补全)"
    )

    # ------------------------------------------------------------- listing parse
    def _papers(self, conf: str) -> list[dict]:
        """Fetch + parse a conference listing into a list of paper dicts (cached
        per conference, since the proceedings are immutable). `?day=all` collapses
        the day-paginated venues (CVPR/ICCV) into one page and is a harmless no-op
        on the single-page venues (WACV)."""
        key = cache.make_key("cvf_openaccess", "conf", conf)
        cached = cache.get(key)
        if cached is not None:
            return cached
        url = f"{BASE}/{conf}?day=all"
        try:
            resp = httpx.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:  # noqa: BLE001 — failure degrades to [] (adapter contract)
            logger.warning("cvf_openaccess: fetch/parse failed for %s: %s", conf, exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("cvf_openaccess.fetch", url=url, status=st, exc=exc)
            return []
        year = conf[-4:]
        papers: list[dict] = []
        for dt in soup.select("dt.ptitle"):
            a = dt.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            html_url = urljoin(BASE, a["href"])
            authors: list[str] = []
            pdf_url = ""
            bibtex = ""
            # walk the two <dd> siblings that follow this <dt> (authors, then links)
            sib = dt.find_next_sibling()
            while sib is not None and getattr(sib, "name", None) == "dd":
                for inp in sib.select("input[name='query_author']"):
                    val = (inp.get("value") or "").strip()
                    if val:
                        authors.append(val)
                if not pdf_url:
                    for link in sib.find_all("a", href=True):
                        if link["href"].lower().endswith("_paper.pdf") or \
                                link.get_text(strip=True).lower() == "pdf":
                            pdf_url = urljoin(BASE, link["href"])
                            break
                if not bibtex:
                    bib = sib.select_one("div.bibref")
                    if bib:
                        bibtex = bib.get_text("\n", strip=True)
                sib = sib.find_next_sibling()
            papers.append({
                "title": title,
                "authors": authors[:12],
                "url": html_url,
                "pdf_url": pdf_url,
                "bibtex": bibtex,
                "year": year,
                "conference": conf,
                "id": urlparse(html_url).path.rsplit("/", 1)[-1].removesuffix(".html"),
            })
        cache.set(key, papers, ttl=CACHE_TTL)
        return papers

    async def _apapers(self, conf: str) -> list[dict]:
        """Async twin of ``_papers`` (S4b): BYTE-FAITHFUL mirror changing ONLY the blocking parts.
          - the disk cache read + write -> ``anyio.to_thread.run_sync`` (SAME cache key, so the sync
            and async paths share one warmed per-conference entry);
          - the raw ``httpx.get`` listing fetch -> the shared async leaf ``http.aget_text`` (shared
            pool + SSRF guard + 30MB cap; a full listing is several MB, well under the cap). It keeps
            the sync client's browser UA (CVF walls a non-browser fingerprint) + timeout +
            follow_redirects (client-level), returns the decoded ``.text`` byte-identically to
            ``_papers``' ``resp.text``, and degrades to None on any failure (already logged +
            ``diag.note``'d as "http.get"), mirroring ``_papers``' fetch-fail -> [];
          - the HTML parse (BeautifulSoup + the dt.ptitle / dd sibling walk) is pure CPU,
            byte-identical, stays ON the loop.
        A fetch/parse failure returns [] WITHOUT caching, exactly as ``_papers`` does."""
        key = cache.make_key("cvf_openaccess", "conf", conf)
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return cached
        url = f"{BASE}/{conf}?day=all"
        text = await http.aget_text(url, headers={"User-Agent": USER_AGENT},
                                    timeout=TIMEOUT)  # async network, ON loop
        if text is None:
            return []  # egress failed (http.aget_text logged + diag.note'd as "http.get"); mirror -> []
        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception as exc:  # noqa: BLE001 — failure degrades to [] (adapter contract)
            logger.warning("cvf_openaccess: parse failed for %s: %s", conf, exc)
            diag.note("cvf_openaccess.fetch", url=url, exc=exc)
            return []
        year = conf[-4:]
        papers: list[dict] = []
        for dt in soup.select("dt.ptitle"):
            a = dt.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            html_url = urljoin(BASE, a["href"])
            authors: list[str] = []
            pdf_url = ""
            bibtex = ""
            # walk the two <dd> siblings that follow this <dt> (authors, then links)
            sib = dt.find_next_sibling()
            while sib is not None and getattr(sib, "name", None) == "dd":
                for inp in sib.select("input[name='query_author']"):
                    val = (inp.get("value") or "").strip()
                    if val:
                        authors.append(val)
                if not pdf_url:
                    for link in sib.find_all("a", href=True):
                        if link["href"].lower().endswith("_paper.pdf") or \
                                link.get_text(strip=True).lower() == "pdf":
                            pdf_url = urljoin(BASE, link["href"])
                            break
                if not bibtex:
                    bib = sib.select_one("div.bibref")
                    if bib:
                        bibtex = bib.get_text("\n", strip=True)
                sib = sib.find_next_sibling()
            papers.append({
                "title": title,
                "authors": authors[:12],
                "url": html_url,
                "pdf_url": pdf_url,
                "bibtex": bibtex,
                "year": year,
                "conference": conf,
                "id": urlparse(html_url).path.rsplit("/", 1)[-1].removesuffix(".html"),
            })
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, papers, ttl=CACHE_TTL))
        return papers

    def search(self, query: str, limit: int = 10) -> list[Document]:
        terms, conf = _parse_conference(query)
        if not conf:
            return []  # no venue token: not this source's job (see description)
        docs = [self._to_doc(p) for p in self._papers(conf)]
        docs = keyword_score_filter(docs, terms)
        return docs[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> AsyncSearchCapable. Mirrors ``search`` line-for-line,
        awaiting ``_apapers`` (disk cache OFF the loop, listing fetch via the async leaf) instead of
        the sync ``_papers``. The ``_to_doc`` mapping + ``keyword_score_filter`` are pure CPU, so this
        is behavior-identical to ``search`` (same conference parse, same BM25 filter, same limit)."""
        terms, conf = _parse_conference(query)
        if not conf:
            return []  # no venue token: not this source's job (see description)
        docs = [self._to_doc(p) for p in await self._apapers(conf)]
        docs = keyword_score_filter(docs, terms)
        return docs[:limit]

    # ------------------------------------------------------- single-paper enrich
    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "openaccess.thecvf.com" not in host:
            return None
        path = urlparse(url).path
        if "/html/" not in path or not path.endswith(".html"):
            return None  # only claim per-paper landing pages
        try:
            resp = httpx.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=30, follow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cvf_openaccess: paper fetch failed for %s: %s", url, exc)
            return None
        title = self._meta(soup, "citation_title") or \
            (soup.title.get_text(strip=True) if soup.title else "")
        if not title:
            return None
        authors = [m["content"].strip() for m in soup.find_all(
            "meta", attrs={"name": "citation_author"}) if m.get("content")]
        year = self._meta(soup, "citation_publication_date")
        if not year:  # fallback: the id tail is ..._<VENUE>_<YEAR>_paper
            ym = re.search(r"_(\d{4})_paper$", path.rsplit("/", 1)[-1].removesuffix(".html"))
            year = ym.group(1) if ym else ""
        abstract_el = soup.find(id="abstract")
        abstract = abstract_el.get_text(" ", strip=True) if abstract_el else ""
        segs = [s for s in path.split("/") if s]  # /content/<CONF>/html/<id>.html
        p = {
            "title": title,
            "authors": authors[:12],
            "url": url,
            "pdf_url": self._meta(soup, "citation_pdf_url") or "",
            "bibtex": "",
            "abstract": abstract,
            "year": year[:4],
            "conference": segs[1] if len(segs) > 1 else "",
            "id": path.rsplit("/", 1)[-1].removesuffix(".html"),
        }
        return self._to_doc(p)

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.head(f"{BASE}/CVPR2024", headers={"User-Agent": USER_AGENT},
                              timeout=10, follow_redirects=True)
            return resp.status_code == 200, f"HTTP {resp.status_code} (CVPR2024 probe)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------- helpers
    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        el = soup.find("meta", attrs={"name": name})
        return (el.get("content") or "").strip() if el else ""

    @staticmethod
    def _to_doc(p: dict) -> Document:
        date = None
        try:
            date = datetime(int(p["year"]), 1, 1, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
        parts = [f"Conference: {p['conference']}",
                 f"Authors: {', '.join(p['authors']) or '(unknown)'}"]
        if p.get("pdf_url"):
            parts.append(f"PDF: {p['pdf_url']}")
        abstract = p.get("abstract") or ""
        body = abstract or "(no abstract in listing; use omniseek_read on the paper URL)"
        content = "\n".join(parts) + "\n\n" + body
        return Document(
            source="cvf_openaccess",
            source_id=p["id"],
            url=p["url"],
            title=p["title"],
            content=content,
            author=", ".join(p["authors"][:4]) or None,
            date=date,
            tags=[p["conference"], "paper", "cv"],
            metadata={"conference": p["conference"], "year": p["year"],
                      "pdf_url": p.get("pdf_url") or "", "paper_id": p["id"],
                      "bibtex": p.get("bibtex") or ""},
        )


from omniseek.core.fetcher import register_adapter

register_adapter(CVFOpenAccessAdapter())
