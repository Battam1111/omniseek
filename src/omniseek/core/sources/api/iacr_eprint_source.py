"""IACR Cryptology ePrint Archive (eprint.iacr.org): where cryptography papers appear first.

Most cryptography preprints are posted here and nowhere else (not on arXiv), so a broad paper
search without it misses the field's working literature (PQDVRF #1553). MODE: STRUCTURE (the
venue-of-record's own metadata: paper id, category, submission and revision dates, licence).

What the operator allows a program to use (recon 2026-10-20, docs/platform-notes/iacr-eprint.md):

  * robots.txt, ``User-agent: *``: ``Disallow: /search`` and every PDF; abstract pages
    (``/<year>/<number>``) and the listings are allowed. The site's own search is therefore NOT used.
  * Metadata is offered for harvesting under CC0 (RSS, Atom, OAI-PMH), with one published rule:
    "Users should set their readers to refresh no more than once a day."

So a search is answered locally:

  1. ``/complete/compact`` (every paper's id, title and authors, one page of about 6.6 MB) is
     fetched at most once a day (the operator's rule, ``LISTING_TTL``) and cached for every query;
  2. the query is scored with OmniSeek's shared BM25 (``relevance.field_scores``, title 3x, authors
     1x) over the papers whose title or authors hold a query term;
  3. each returned paper's abstract, category, dates and licence come from OAI-PMH ``GetRecord``
     (one small XML answer per paper, cached a day by the same rule). A paper whose record cannot be
     had in time comes back with its title and authors only, its content left empty (nothing to
     check is said as such, not filled with a placeholder).

Full text: the official PDFs refuse programs (403 or a challenge page, brain note
finding-eprint-blocks-automated-fulltext-2026-10-18) and robots.txt forbids them, so this source never
requests a PDF. Each document carries ``metadata.mirror_pdf_url`` (the third-party mirror
eprint.iacr.asia) for reading ONE named paper with omniseek_read, never in bulk; check the version against
the official abstract page (``url``) as the project rule says.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

import anyio

from omniseek.core import _guard, cache, diag, http, portal, relevance
from omniseek.core.normalize import Document, html_fragment_text, to_utc
from omniseek.core.sources._base import SourceBase, aparse

logger = logging.getLogger(__name__)

BASE = "https://eprint.iacr.org"
LISTING_URL = f"{BASE}/complete/compact"
OAI_URL = f"{BASE}/oai"
MIRROR = "https://eprint.iacr.asia"
# "Users should set their readers to refresh no more than once a day." (eprint.iacr.org/rss/)
LISTING_TTL = 86400
RECORD_TTL = 86400
LISTING_TIMEOUT = 60   # one page of about 6.6 MB
RECORD_TIMEOUT = 15
# Cloudflare in front of ePrint answers 403 to a User-Agent holding "OAI-PMH" (2026-10-20, HEAD probes).
USER_AGENT = "omniseek/0.1 (automated retrieval)"

_PATH_RE = re.compile(r"^/(\d{4})/(\d{3,5})/?$")
_ROW_RE = re.compile(
    r'<div><a href="/(\d{4})/(\d+)">\d{4}/\d+</a></div>\s*'
    r'<div class="ms-5 mb-1"><strong>(.*?)</strong><br>\s*<em>(.*?)</em>', re.S)
_NS = {"oai": "http://www.openarchives.org/OAI/2.0/",
       "dc": "http://purl.org/dc/elements/1.1/"}


def _clean(text: str) -> str:
    return html_fragment_text(text)


def parse_listing(page: str) -> list[list[str]]:
    """``/complete/compact`` -> ``[[id, title, authors], ...]`` in the page's order (newest first)."""
    rows = []
    # finditer, not findall: findall over the ~6.7 MB page is one C call that holds the GIL ~45 ms;
    # match by match, other threads (the loop) get the GIL between rows.
    for m in _ROW_RE.finditer(page or ""):
        year, num, title, authors = m.groups()
        t = _clean(title)
        if t:
            rows.append([f"{year}/{num}", t, _clean(authors)])
    return rows


def parse_record(xml_text: str) -> Optional[dict]:
    """One OAI-PMH ``GetRecord`` (oai_dc) answer -> the fields the documents use; ``{"id",
    "withdrawn": True}`` for a withdrawn paper (its header says ``status="deleted"``); None when the
    answer holds no record (an OAI error such as idDoesNotExist, or not XML)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    header = root.find(".//oai:record/oai:header", _NS)
    if header is not None and header.get("status") == "deleted":
        ident = (header.findtext("oai:identifier", "", _NS) or "").strip()
        return {"id": ident.rsplit(":", 1)[-1], "withdrawn": True}
    dc = root.find(".//oai:metadata/{http://www.openarchives.org/OAI/2.0/oai_dc/}dc", _NS)
    if dc is None:
        return None

    def all_(tag: str) -> list[str]:
        return [" ".join((e.text or "").split()) for e in dc.findall(f"dc:{tag}", _NS) if (e.text or "").strip()]

    ident = next((i for i in all_("identifier") if i.startswith(BASE + "/")), "")
    title = (all_("title") or [""])[0]
    if not ident or not title:
        return None
    # The abstract keeps its paragraph breaks; only the hard wraps inside a paragraph are joined.
    desc_el = dc.find("dc:description", _NS)
    abstract = ""
    if desc_el is not None and desc_el.text:
        paras = re.split(r"\n\s*\n", desc_el.text.strip())
        abstract = "\n\n".join(" ".join(p.split()) for p in paras if p.strip())
    dates = all_("date")
    return {"id": ident[len(BASE) + 1:].strip("/"), "title": title, "authors": all_("creator"),
            "abstract": abstract, "category": (all_("subject") or [""])[0],
            "submitted": dates[0] if dates else "", "revised": dates[-1] if len(dates) > 1 else "",
            "license": (all_("rights") or [""])[0]}


_PLURAL_RE = re.compile(r"\b([A-Za-z]{2,}?)s\b")


def fold_number(text: str) -> str:
    """English plural -> singular on Latin words, applied alike to titles, authors and the query
    (PRFs -> PRF, Lattices -> Lattice, signatures -> signature; a word ending in "ss" is kept).
    Titles are all this source can rank on and the archive writes them in the plural
    ("Lattice-Based Distributed PRFs") while queries name the thing ("lattice PRF"); the shared
    tokenizer does not fold the two. Folding both sides keeps one term per word, so its BM25
    weight is counted once."""
    return _PLURAL_RE.sub(lambda m: m.group(0) if m.group(1).endswith("s") else m.group(1), text or "")


def _fields(row: list[str]) -> list[tuple[str, float]]:
    """A listing row as scored fields: the title 3x (as relevance.doc_scores weighs it), authors 1x,
    both number-folded."""
    return [(fold_number(row[1]), 3.0), (fold_number(row[2]), 1.0)]


def _listing_failed(exc: BaseException) -> None:
    logger.warning("iacr_eprint: listing fetch failed: %s", exc)
    diag.note("iacr_eprint.listing", url=LISTING_URL,
              status=getattr(getattr(exc, "response", None), "status_code", None), exc=exc)


def _new_listing(rows: list[list[str]], status: int) -> Optional[dict]:
    """The listing to cache, or None (noted) when the page parsed to no rows."""
    if not rows:
        diag.note("iacr_eprint.listing", url=LISTING_URL, status=status,
                  exc=RuntimeError("listing page parsed to no rows (page layout changed?)"))
        return None
    return {"fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": rows}


def _record_key(pid: str) -> str:
    return cache.make_key("iacr_eprint", "record", pid)


def _no_time_for_record() -> bool:
    """Not one more request when the caller's time left is shorter than that request's own timeout:
    the paper would come back after the caller has gone (seen with six broad searches at once on a
    loaded host, each queueing 15 records behind one gate)."""
    dl = _guard.current_deadline()
    return dl is not None and dl - time.monotonic() < RECORD_TIMEOUT


def _record_url(pid: str) -> str:
    return (f"{OAI_URL}?verb=GetRecord&metadataPrefix=oai_dc"
            f"&identifier={quote('oai:eprint.iacr.org:' + pid, safe=':/')}")


def _record_failed(pid: str, url: str, exc: BaseException) -> None:
    logger.info("iacr_eprint: GetRecord %s failed: %s", pid, exc)
    diag.note("iacr_eprint.record", url=url,
              status=getattr(getattr(exc, "response", None), "status_code", None), exc=exc)


class IACREprintAdapter(SourceBase):
    name = "iacr_eprint"
    needs_credentials = False
    kind = "lookup"
    domains = ["papers"]
    regions = ["global"]
    modes = ["STRUCTURE"]
    coverage = {"since": "1996", "until": None, "gaps": [],
                "note": "every paper the archive lists, withdrawn ones skipped; search is over titles and authors"}
    license = ("ePrint metadata (title, authors, abstract) is CC0; each paper's full text keeps the "
               "licence named in metadata.license")
    samples = [{"query": "threshold VRF"}, {"query": "lattice PRF"},
               {"query": "post-quantum signature aggregation"}]
    description = (
        "IACR Cryptology ePrint Archive (eprint.iacr.org): cryptography preprints, most of which are "
        "posted nowhere else. Title/author search over the whole archive (the official listing, "
        "fetched at most once a day; the site's own search is robots-disallowed), abstract, "
        "category, dates and licence via OAI-PMH. Full text is never fetched: "
        "metadata.mirror_pdf_url points at the eprint.iacr.asia mirror for reading ONE named paper. "
        "密码学论文预印本库, 按题名/作者查, 给摘要"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index_for: Optional[str] = None
        self._rows: list[list[str]] = []
        self._postings: dict[str, list[int]] = {}
        self._stats: dict = {}

    # ---------------------------------------------------------------- listing + local index
    _LISTING_KEY = cache.make_key("iacr_eprint", "listing")

    async def _alisting(self) -> Optional[dict]:
        """Async replacement of the deleted sync ``_listing`` (11-04, S9; this is the only one now): the GET is a coroutine; the 6.6 MB regex parse goes through
        ``aparse`` (a worker process: about 1.3 s of regex)."""
        cached = await cache.aget_once(self._LISTING_KEY)
        if cached is not None:
            return cached
        try:
            resp = await http.adirect("GET", LISTING_URL, headers={"User-Agent": USER_AGENT},
                                      timeout=LISTING_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            rows = await aparse(len(resp.content), parse_listing, resp.text)
        except Exception as exc:  # noqa: BLE001 (failure -> None is the adapter contract)
            _listing_failed(exc)
            return None
        listing = _new_listing(rows, resp.status_code)
        if listing is not None:
            await anyio.to_thread.run_sync(
                functools.partial(cache.set, self._LISTING_KEY, listing, ttl=LISTING_TTL), limiter=portal.IO_LANE)
        return listing

    def _index(self, listing: dict) -> tuple[list[list[str]], dict[str, list[int]], dict]:
        """The token -> row-number postings and the BM25 corpus statistics of one listing, built
        once per fetched listing."""
        stamp = f"{listing.get('fetched')}:{len(listing.get('rows') or [])}"
        with self._lock:
            if self._index_for != stamp:
                rows = listing.get("rows") or []
                postings: dict[str, list[int]] = {}
                for i, row in enumerate(rows):
                    for tok in {t for text, _w in _fields(row) for t in relevance.tokenize(text)}:
                        postings.setdefault(tok, []).append(i)
                stats = relevance.corpus_stats([_fields(r) for r in rows])
                self._rows, self._postings, self._stats, self._index_for = rows, postings, stats, stamp
            return self._rows, self._postings, self._stats

    def _rank(self, listing: dict, query: str, limit: Optional[int]) -> list[list[str]]:
        """Listing rows best first (``limit`` None: every row that matches a query term)."""
        rows, postings, stats = self._index(listing)
        if not relevance.query_terms(query):
            return rows[:limit]  # browse: the listing's own order, newest first
        query = fold_number(query)
        terms = relevance.query_terms(query)
        # Only rows holding a query term can score above 0; scored against the whole listing's
        # statistics, each gets the score it would get if all 28k rows were scored.
        cand = sorted({i for t in terms for i in postings.get(t, ())})
        scores = relevance.field_scores([_fields(rows[i]) for i in cand], query, corpus=stats)
        ranked = [(s, k) for k, s in enumerate(scores) if s > 0]
        # Highest score first; on a tie the listing's order (newest first) decides.
        ranked.sort(key=lambda sk: (-sk[0], cand[sk[1]]))
        return [rows[cand[k]] for _s, k in ranked[:limit]]

    async def _arecord(self, pid: str) -> Optional[dict]:
        """Async twin of ``_record`` (same key, same deadline rule, same parse)."""
        key = _record_key(pid)
        cached = await cache.aget_once(key)
        if cached is not None:
            return cached
        if _no_time_for_record():
            return None
        url = _record_url(pid)
        try:
            resp = await http.adirect("GET", url, headers={"User-Agent": USER_AGENT},
                                      timeout=RECORD_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            _record_failed(pid, url, exc)
            return None
        rec = await aparse(len(resp.content), parse_record, resp.text)
        if rec is not None:
            await anyio.to_thread.run_sync(functools.partial(cache.set, key, rec, ttl=RECORD_TTL), limiter=portal.IO_LANE)
        return rec

    # ---------------------------------------------------------------- documents
    @staticmethod
    def _to_doc(pid: str, title: str, authors: str, rec: Optional[dict]) -> Document:
        meta = {"paper_id": pid, "year": pid[:4], "mirror_pdf_url": f"{MIRROR}/{pid}.pdf",
                "full_text_note": ("eprint.iacr.org refuses programs the PDF; read ONE named paper "
                                   "from the third-party mirror and check its version against the "
                                   "official abstract page")}
        content = ""
        date = None
        tags = ["paper", "cryptography"]
        if rec:
            title = rec.get("title") or title
            if rec.get("authors"):
                authors = ", ".join(rec["authors"])
            content = rec.get("abstract") or ""
            date = to_utc(rec.get("submitted"))
            for k in ("category", "submitted", "revised", "license"):
                if rec.get(k):
                    meta[k] = rec[k]
            if rec.get("category"):
                tags.append(rec["category"])
        else:
            meta["record"] = "not fetched (title and authors from the listing only)"
        return Document(source="iacr_eprint", source_id=pid, url=f"{BASE}/{pid}", title=title,
                           content=content, author=authors or None, date=date, tags=tags,
                           metadata=meta)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native twin of ``search``: the GETs are coroutines; ranking (and the one-off index build
        over the whole listing) runs in a worker process (portal.run_cpu)."""
        listing = await self._alisting()
        if not listing or limit <= 0:
            return []
        ranked = await portal.run_cpu(_worker_rank, listing, query or "")
        docs = []
        for pid, title, authors in ranked:
            rec = await self._arecord(pid)
            if rec and rec.get("withdrawn"):
                continue
            docs.append(self._to_doc(pid, title, authors, rec))
            if len(docs) >= limit:
                break
        return docs

    async def afetch_url(self, url: str) -> Optional[Document]:
        """Claim an abstract page ``https://eprint.iacr.org/<year>/<number>`` and answer it from
        OAI-PMH (abstract, category, dates, licence). PDFs and other pages are not claimed."""
        p = urlparse(url or "")
        if (p.hostname or "").lower() != "eprint.iacr.org":
            return None
        m = _PATH_RE.match(p.path or "")
        if not m:
            return None
        pid = f"{m.group(1)}/{m.group(2)}"
        rec = await self._arecord(pid)
        if rec is None or rec.get("withdrawn"):
            return None
        return self._to_doc(pid, rec.get("title") or "", "", rec)

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        """Both paths a search uses, each with its smallest request: a HEAD of the listing (the
        6.6 MB body is not downloaded) and one GetRecord answer that must parse to a record."""
        try:
            head = await http.adirect("HEAD", LISTING_URL, headers={"User-Agent": USER_AGENT},
                               timeout=RECORD_TIMEOUT, follow_redirects=True)
            if head.status_code != 200:
                return False, f"listing HTTP {head.status_code}"
            url = f"{OAI_URL}?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai:eprint.iacr.org:2024/996"
            resp = await http.adirect("GET", url, headers={"User-Agent": USER_AGENT},
                               timeout=RECORD_TIMEOUT, follow_redirects=True)
            if resp.status_code != 200:
                return False, f"OAI GetRecord HTTP {resp.status_code}"
            rec = await aparse(len(resp.text), parse_record, resp.text)  # here or process lane
            if rec is None:
                return False, "OAI GetRecord answered without a record"
            return True, f"listing HTTP 200; OAI GetRecord 2024/996 -> {rec['title'][:40]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


_ranker: Optional[IACREprintAdapter] = None


def _worker_rank(listing: dict, query: str) -> list[list[str]]:
    """``asearch``'s ranking, run in a worker of the process lane: the worker keeps its own adapter so
    the index over the listing is built once per worker and listing, as ``_index`` does in-process."""
    global _ranker
    if _ranker is None:
        _ranker = IACREprintAdapter()
    return _ranker._rank(listing, query, None)


from omniseek.core.fetcher import register_adapter  # noqa: E402

register_adapter(IACREprintAdapter())
