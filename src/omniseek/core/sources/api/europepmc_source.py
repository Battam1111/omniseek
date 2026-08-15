"""Europe PMC — keyless biomedical literature search + open-access full text.

Europe PMC (https://europepmc.org) is EMBL-EBI's index of the biomedical and life
sciences literature: ~40M abstracts plus a large open-access full-text corpus
(PMC articles, Agricola, preprint servers, patents, theses). Its public REST API
is fully keyless. OmniSeek's biomedical STRUCTURE source — and, via the
fullTextXML endpoint, the eye's SECOND keyless full-text spine alongside CORE
(core.ac.uk needs a registered key; Europe PMC's OA full text needs none).

Access via the public REST API (no auth, no key):
  GET https://www.ebi.ac.uk/europepmc/webservices/rest/search
      ?query=<q>&format=json&resultType=core&pageSize=<limit>
  -> {"resultList": {"result": [{id, source, pmid, pmcid, doi, title,
        authorString, journalTitle, pubYear, firstPublicationDate,
        isOpenAccess('Y'/'N'), inEPMC, hasPDF, citedByCount,
        abstractText, fullTextUrlList, grantsList, pubTypeList}, ...]},
      "nextCursorMark": ...}
  resultType=core is required for abstractText (the lite/idlist types omit it).

Open-access FULL TEXT fan-out (the non-redundant core value): for an article that
is open access AND lives in EPMC with a PMC id, the JATS body is fetched from
  GET https://www.ebi.ac.uk/europepmc/webservices/rest/<source>/<pmcid>/fullTextXML
and the article body text (JATS tags stripped) replaces the abstract in content.
This is bounded — only the first _MAX_FULLTEXT eligible results per search pull the
body — so a broad search does not turn into N slow secondary requests against the
EBI host. Every fan-out is guarded: any failure falls back to the abstract, so a
full-text miss never drops a result.

A thin BaseScrapeAdapter subclass: the cache check / atomic set_docs /
self-registration ritual lives in the base; this declares its facets and fills the
two hooks. rank stays default-False — the search endpoint returns its own relevance
order. explicit_only: biomedical-leaning, so it is a NAMED drill (point it at a
clinical / life-sciences question) rather than broad-fan-out fodder that would
pull PubMed-flavoured hits into a general ML retrieval.

Recon trail: field names from the live Europe PMC REST docs + a probe of the
search + fullTextXML endpoints (the Claude sandbox DNS-blackholes www.ebi.ac.uk;
the eye host live-verifies post-deploy).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Full-text JATS XML for an OA article in EPMC: /<pmcid>/fullTextXML (the pmcid already
# carries its "PMC" prefix; the older /<source>/<pmcid>/ form 404s, live-confirmed 2026-06-21).
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
# EPMC abstract page (the canonical human URL when there is no DOI): /article/<source>/<id>
ARTICLE_URL = "https://europepmc.org/article/{source}/{id}"

# Bound the OA full-text fan-out per search: only the first N eligible (OA + in EPMC
# + pmcid) results pull the JATS body, so a broad search stays one search request +
# a few small follow-ups, not N slow ones against the shared EBI host.
_MAX_FULLTEXT = 3
# Cap the inlined full-text body so one large article cannot dominate the payload;
# the agent can omniseek_read the article for the whole thing. (The true length is
# implicit in the content itself; this is a defensive ceiling, not a truncation flag.)
_BODY_CAP = 20000


class EuropePMCAdapter(BaseScrapeAdapter):
    name = "europepmc"
    needs_credentials = False
    description = ("Europe PMC — keyless biomedical / life-sciences literature "
                   "(abstracts + citations + open-access JATS full text); name it to "
                   "drill a clinical / biomedical question. The eye's SECOND keyless "
                   "full-text spine (CORE needs a key, this does not). STRUCTURE, "
                   "keyless www.ebi.ac.uk.")
    cache_ttl = 3600  # literature metadata changes slowly
    kind = "lookup"
    domains = ["papers"]
    modes = ["STRUCTURE"]
    explicit_only = ("europepmc: biomedical-leaning literature — a NAMED drill (point it at a "
                     "clinical / life-sciences question), not broad-fan-out fodder that would "
                     "pull PubMed-flavoured hits into general ML retrieval")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # resultType=core is REQUIRED for abstractText; pageSize is the per-page count.
        return http.get_json(
            SEARCH_URL,
            params={
                "query": query,
                "format": "json",
                "resultType": "core",
                "pageSize": max(1, int(limit)),
            },
            timeout=20,
        )

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # Async twin of _raw_fetch: byte-faithful mirror, only http.get_json → await http.aget_json.
        return await http.aget_json(
            SEARCH_URL,
            params={
                "query": query,
                "format": "json",
                "resultType": "core",
                "pageSize": max(1, int(limit)),
            },
            timeout=20,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        results = ((raw.get("resultList") or {}).get("result")) or []
        docs: list[Document] = []
        fulltext_budget = _MAX_FULLTEXT
        for result in results[:limit]:
            doc = self._result_to_doc(result)
            if doc is None:
                continue
            # Bounded OA full-text fan-out: only the first few eligible results pull
            # the JATS body, and only when there is budget left. A failure inside
            # _maybe_fulltext is swallowed there (doc keeps its abstract).
            if fulltext_budget > 0 and self._fulltext_eligible(result):
                if self._maybe_fulltext(doc, result):
                    fulltext_budget -= 1
            docs.append(doc)
        return docs

    async def _ato_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        """Async twin of _to_documents (TWO-LAYER: the per-record OA full-text enrichment
        egresses). Line-for-line mirror of _to_documents, but the bounded fan-out awaits its
        async twin _amaybe_fulltext (http.get_text → await http.aget_text). SAME _MAX_FULLTEXT
        budget, SAME eligibility gate, SAME order (sequential awaits ⇒ byte-identical doc list),
        SAME per-record skip-on-fail (a miss keeps the abstract), SAME pure-CPU _result_to_doc /
        _strip_jats on the loop."""
        if not isinstance(raw, dict):
            return []
        results = ((raw.get("resultList") or {}).get("result")) or []
        docs: list[Document] = []
        fulltext_budget = _MAX_FULLTEXT
        for result in results[:limit]:
            doc = self._result_to_doc(result)
            if doc is None:
                continue
            # Bounded OA full-text fan-out: only the first few eligible results pull
            # the JATS body, and only when there is budget left. A failure inside
            # _amaybe_fulltext is swallowed there (doc keeps its abstract).
            if fulltext_budget > 0 and self._fulltext_eligible(result):
                if await self._amaybe_fulltext(doc, result):
                    fulltext_budget -= 1
            docs.append(doc)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip;
        egress via _araw_fetch; mapping via the ASYNC _ato_documents (the enrichment layer egresses,
        so _asearch_via awaits the async abuild — byte-identical to search's two-layer flow)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._ato_documents(raw, query, limit))

    # ---------------------------------------------------------------- doc builder
    def _result_to_doc(self, result: Any) -> Optional[Document]:
        """One search result -> a Document (or None to drop a junk row).

        Pure: builds the doc from the search payload alone (no network). The OA
        full-text body is grafted on separately by _maybe_fulltext."""
        if not isinstance(result, dict):
            return None
        rid = result.get("id")
        src = result.get("source")
        doi = (result.get("doi") or "").strip()
        # A usable stable id is required: prefer the DOI, else source:id. With neither
        # there is nothing to identify or link the record by, so drop it.
        if doi:
            source_id = doi
        elif rid and src:
            source_id = f"{src}:{rid}"
        else:
            return None

        title = (result.get("title") or "").strip()
        if not title:
            return None  # an untitled row is unusable in a result list

        # URL: prefer the DOI resolver; else the EPMC abstract page from source+id.
        if doi:
            url = f"https://doi.org/{doi}"
        elif src and rid:
            url = ARTICLE_URL.format(source=src, id=rid)
        else:
            return None

        content = (result.get("abstractText") or "").strip() or title
        author = (result.get("authorString") or "").strip() or None
        date = self._parse_date(result.get("firstPublicationDate"), result.get("pubYear"))

        return Document(
            source=self.name,
            source_id=source_id,
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            signals=mk_signal(
                "cited_by", result.get("citedByCount"),
                kind="citation", by="europepmc/citedByCount",
            ),
            tags=self._pub_types(result),
            metadata={
                "pmid": result.get("pmid"),
                "pmcid": result.get("pmcid"),
                "doi": doi or None,
                "is_open_access": result.get("isOpenAccess"),
                "has_pdf": result.get("hasPDF"),
                "in_epmc": result.get("inEPMC"),
                "journal": self._journal_title(result),
                "full_text_urls": self._full_text_urls(result),
                "raw": jsonsafe(result),
            },
        )

    # ---------------------------------------------------------- OA full-text graft
    @staticmethod
    def _fulltext_eligible(result: Any) -> bool:
        """True iff this result is open access, lives inside EPMC, and has a PMC id —
        the only shape the fullTextXML endpoint can serve. Pure, no network."""
        if not isinstance(result, dict):
            return False
        return (
            (result.get("isOpenAccess") or "").upper() == "Y"
            and (result.get("inEPMC") or "").upper() == "Y"
            and bool(result.get("pmcid"))
            and bool(result.get("source"))
        )

    def _maybe_fulltext(self, doc: Document, result: dict) -> bool:
        """Fetch the JATS full text for an eligible OA article and graft the stripped
        body into ``doc.content``. Returns True iff a non-empty body was grafted (so the
        caller can spend its fan-out budget on real hits only). Any failure -> False and
        ``doc`` keeps its abstract: a full-text miss never costs us the result."""
        src = result.get("source")
        pmcid = result.get("pmcid")
        if not src or not pmcid:
            return False
        xml = http.get_text(
            FULLTEXT_URL.format(pmcid=pmcid), timeout=20,
        )
        body = self._strip_jats(xml)
        if not body:
            return False
        doc.content = body[:_BODY_CAP]
        doc.metadata["full_text"] = "europepmc/fullTextXML"
        return True

    async def _amaybe_fulltext(self, doc: Document, result: dict) -> bool:
        """Async twin of _maybe_fulltext: byte-faithful mirror, only http.get_text → await
        http.aget_text. SAME eligibility guard, SAME _strip_jats (pure CPU, on loop), SAME
        _BODY_CAP, SAME failure → False fallback (doc keeps its abstract)."""
        src = result.get("source")
        pmcid = result.get("pmcid")
        if not src or not pmcid:
            return False
        xml = await http.aget_text(
            FULLTEXT_URL.format(pmcid=pmcid), timeout=20,
        )
        body = self._strip_jats(xml)
        if not body:
            return False
        doc.content = body[:_BODY_CAP]
        doc.metadata["full_text"] = "europepmc/fullTextXML"
        return True

    @staticmethod
    def _strip_jats(xml: Optional[str]) -> str:
        """Reduce a JATS fullTextXML document to plain body text.

        Drops the <front> (title page / abstract / author block — already captured in
        the doc fields), removes reference/table/figure-graphic noise, strips all
        remaining tags, unescapes the few entities JATS uses, and collapses
        whitespace. Best-effort and total: returns '' on empty / non-text / no body."""
        if not isinstance(xml, str) or not xml.strip():
            return ""
        text = xml
        # Prefer the <body>…</body> region when present (skips front-matter + back-matter).
        m = re.search(r"<body[\s>].*?</body>", text, flags=re.DOTALL | re.IGNORECASE)
        if m:
            text = m.group(0)
        # Strip whole noisy blocks (refs / tables / inline math / float wrappers) before
        # the blanket tag removal so their inner text does not pollute the body.
        for block in ("ref-list", "table-wrap", "table", "tex-math", "mml:math", "fig"):
            text = re.sub(rf"<{block}[\s>].*?</{block}>", " ", text,
                          flags=re.DOTALL | re.IGNORECASE)
        # Drop XML comments / processing instructions, then every remaining tag.
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        # Unescape the handful of entities JATS emits (order matters: &amp; last).
        for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                        ("&apos;", "'"), ("&#x2018;", "'"), ("&#x2019;", "'"),
                        ("&nbsp;", " "), ("&amp;", "&")):
            text = text.replace(ent, ch)
        # Collapse all runs of whitespace to single spaces.
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # ------------------------------------------------------------------- helpers
    @staticmethod
    def _journal_title(result: dict) -> Optional[str]:
        """Journal name: the flat journalTitle field, else journalInfo.journal.title."""
        jt = (result.get("journalTitle") or "").strip()
        if jt:
            return jt
        ji = result.get("journalInfo")
        if isinstance(ji, dict):
            j = ji.get("journal")
            if isinstance(j, dict):
                title = (j.get("title") or "").strip()
                if title:
                    return title
        return None

    @staticmethod
    def _pub_types(result: dict) -> list[str]:
        """Publication types as tags. EPMC core nests them at pubTypeList.pubType
        (a list of strings). Tolerant of the field being absent / a bare string."""
        ptl = result.get("pubTypeList")
        if isinstance(ptl, dict):
            pts = ptl.get("pubType")
            if isinstance(pts, list):
                return [str(p) for p in pts if p]
            if isinstance(pts, str) and pts:
                return [pts]
        return []

    @staticmethod
    def _full_text_urls(result: dict) -> list[str]:
        """The OA full-text URLs EPMC lists (PDF / HTML mirrors), for metadata so the
        agent can drill the body even when the inline fan-out was not spent on this row.
        Shape: fullTextUrlList.fullTextUrl[].url. Empty list when absent."""
        ftl = result.get("fullTextUrlList")
        if not isinstance(ftl, dict):
            return []
        items = ftl.get("fullTextUrl")
        if not isinstance(items, list):
            return []
        urls: list[str] = []
        for it in items:
            if isinstance(it, dict):
                u = (it.get("url") or "").strip()
                if u:
                    urls.append(u)
        return urls

    @staticmethod
    def _parse_date(first_pub: Any, pub_year: Any) -> Optional[datetime]:
        """firstPublicationDate is 'YYYY-MM-DD' (sometimes 'YYYY-MM' / 'YYYY'); fall back
        to a bare pubYear. None on anything unparseable (never raises)."""
        if isinstance(first_pub, str) and first_pub.strip():
            for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    return datetime.strptime(first_pub.strip(), fmt)
                except ValueError:
                    continue
        if pub_year is not None:
            try:
                return datetime(int(str(pub_year).strip()[:4]), 1, 1)
            except (ValueError, TypeError):
                return None
        return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
