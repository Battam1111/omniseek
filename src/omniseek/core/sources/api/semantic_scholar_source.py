"""Semantic Scholar adapter — uses the official semanticscholar Python package.

Free public API. Without API key: shared 5000 req/5min pool.
With API key (S2_API_KEY env or credentials/semantic_scholar.json): 1 RPS guaranteed.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from omniseek.core import _s2, auth, cache
from omniseek.core import filters as _flt
from omniseek.core.normalize import Document, mk_signal, to_utc
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

# Query qualifiers routed to the S2 ``search_paper`` API params. Allowlisted (not
# a generic `word:word` grab) so prose colons never become filters. Absent → the
# call is identical to before. We deliberately support only single-token-value
# qualifiers — ``fields_of_study`` is omitted because its values contain spaces
# ("Computer Science") which can't be parsed unambiguously from a space-delimited
# query; use metadata["raw"] / the openalex adapter's concept filters for that.
#   year:2020   year:2020-2023   year:2020-   year:-2023
#   venue:NeurIPS                min_citations:50
_S2_YEAR_RE = re.compile(r"(?:^|\s)year:(\d{4}(?:-\d{0,4})?|-\d{4})", re.IGNORECASE)
_S2_VENUE_RE = re.compile(r"(?:^|\s)venue:([^\s,]+)", re.IGNORECASE)
_S2_MINCITE_RE = re.compile(r"(?:^|\s)min_citations?:(\d+)", re.IGNORECASE)


def _parse_s2_qualifiers(query: str) -> tuple[str, dict]:
    """Split allowlisted S2 qualifiers out of the query.

    Returns ``(clean_query, kwargs)`` where ``kwargs`` holds any of
    ``year`` / ``venue`` / ``min_citation_count`` to pass to ``search_paper``.
    Empty ``kwargs`` → behaviour unchanged. Matched qualifiers are stripped from
    the free-text query.
    """
    q = query or ""
    kwargs: dict = {}

    m = _S2_YEAR_RE.search(q)
    if m:
        kwargs["year"] = m.group(1)
        q = _S2_YEAR_RE.sub(" ", q)
    m = _S2_VENUE_RE.search(q)
    if m:
        kwargs["venue"] = [m.group(1)]
        q = _S2_VENUE_RE.sub(" ", q)
    m = _S2_MINCITE_RE.search(q)
    if m:
        kwargs["min_citation_count"] = int(m.group(1))
        q = _S2_MINCITE_RE.sub(" ", q)

    return q.strip(), kwargs

# Drop a template for the optional API key
auth.register_template(
    "semantic_scholar",
    {"_comment": "API key is OPTIONAL. Without it the shared rate limit is used.", "api_key": ""},
)


class SemanticScholarAdapter(SourceBase):
    name = "semantic_scholar"
    needs_credentials = False  # API key is optional, not required
    description = "Semantic Scholar: 225M+ papers, citation graphs, TLDR summaries"

    # Source spec (omniseek.core.filters). year and citations translate into the query qualifiers
    # _parse_s2_qualifiers reads; venue / fields_of_study / publication_types / open_access go to the
    # S2 search_paper parameters of the same names (read back in search via filters.upstream()).
    filters = {
        "year": {"type": "int", "ops": ["eq", "gte", "lte", "range"], "doc_key": "metadata.year",
                 "desc": "publication year (sent as year:A-B)"},
        "citations": {"type": "int", "ops": ["gte"],
                      "desc": "at least this many citations (sent as min_citation_count)"},
        "venue": {"type": "keyword", "ops": ["eq", "any"], "doc_key": "metadata.venue",
                  "desc": "venue name as S2 writes it, e.g. NeurIPS, ACL"},
        "fields_of_study": {"type": "keyword", "ops": ["eq", "any"],
                            "doc_key": "metadata.fields_of_study",
                            "values": ["Computer Science", "Medicine", "Chemistry", "Biology",
                                       "Materials Science", "Physics", "Geology", "Psychology", "Art",
                                       "History", "Geography", "Sociology", "Business",
                                       "Political Science", "Economics", "Philosophy", "Mathematics",
                                       "Engineering", "Environmental Science",
                                       "Agricultural and Food Sciences", "Education", "Law",
                                       "Linguistics"],
                            "desc": "S2 field of study (S2's own classification, s2FieldsOfStudy)"},
        "publication_types": {"type": "keyword", "ops": ["eq", "any"],
                              "values": ["Review", "JournalArticle", "CaseReport", "ClinicalTrial",
                                         "Conference", "Dataset", "Editorial", "LettersAndComments",
                                         "MetaAnalysis", "News", "Study", "Book", "BookSection"],
                              "desc": "S2 publication type"},
        "open_access": {"type": "bool", "ops": ["eq"],
                        "desc": "true = only papers with an open-access PDF"},
    }
    # omniseek_search(as_of=...) pushes "year <= as_of" upstream through this field: only a year upstream: the year is pushed, the exit cut then goes by day.
    as_of_field = "year"
    coverage = {"since": None, "until": None, "gaps": [],
                "note": "the S2 academic graph (225M+ papers); its oldest date is not recorded here"}
    license = ("Semantic Scholar API License Agreement (semanticscholar.org/product/api/license); "
               "abstracts keep their publishers' rights")
    samples = [{"query": "retrieval augmented generation", "filters": {"year": {"gte": 2023}}},
               {"query": "protein language model", "filters": {"fields_of_study": "Biology"}}]

    def translate_filters(self, query: str, clauses: list):
        """year / citations -> the query qualifiers; the rest -> search_paper parameters."""
        q = [(query or "").strip()]
        params: dict = {}
        for c in clauses:
            if c.field == "year":
                lo, hi = (c.value, c.value) if c.op == "eq" else (c.lo, c.hi)
                q.append(f"year:{lo}" if lo == hi else f"year:{lo or ''}-{hi or ''}")
            elif c.field == "citations":
                q.append(f"min_citations:{c.lo}")
            elif c.field in ("venue", "fields_of_study", "publication_types"):
                params[c.field] = [c.value] if c.op == "eq" else list(c.value)
            elif c.field == "open_access":
                if c.value:
                    params["open_access_pdf"] = True
                else:
                    raise _flt.Unsupported("open_access", "S2 can only require an open-access PDF")
            else:
                raise _flt.Unsupported(c.field)
        return " ".join(x for x in q if x), params

    # The S2 paper fields this adapter's document assembly reads. Passed verbatim to the
    # shared _s2.search_paper / _s2.get_paper wrappers.
    _FIELDS = [
        "paperId",
        "externalIds",
        "url",
        "title",
        "abstract",
        "authors",
        "year",
        "publicationDate",
        "citationCount",
        "influentialCitationCount",
        "tldr",
        "venue",
        "publicationVenue",
        "fieldsOfStudy",
        "s2FieldsOfStudy",
    ]

    @staticmethod
    def _plan(query: str, limit: int) -> tuple[str, dict, str]:
        """(clean query, search_paper parameters, cache key) of one search."""
        # Pull allowlisted qualifiers (year / venue / min_citations) out of the
        # query → S2 API params. None → unchanged.
        clean_query, qualifiers = _parse_s2_qualifiers(query or "")
        # A filtered call's search_paper parameters (translate_filters); {} on an unfiltered call.
        qualifiers.update(_flt.upstream())

        # CRITICAL: qualifiers are part of the cache identity — `year:2020 x` and
        # `year:2023 x` must not collide. Sorted repr keeps the key deterministic.
        qual_key = ",".join(f"{k}={qualifiers[k]}" for k in sorted(qualifiers))
        return clean_query, qualifiers, cache.make_key("s2", "search", clean_query, qual_key, limit)

    def _docs(self, results) -> list[Document]:
        docs: list[Document] = []
        for paper in results:
            try:
                docs.append(self._paper_to_document(paper))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed S2 paper: %s", exc)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native twin of ``search``: the library's async call through _s2.asearch_paper (same
        pacer, permit, breaker and page bound), same key, same documents, same caching."""
        clean_query, qualifiers, key = self._plan(query, limit)

        async def _aget() -> Optional[list[Document]]:
            results = await _s2.asearch_paper(clean_query, limit=limit, fields=self._FIELDS, **qualifiers)
            docs = self._docs(results)
            return docs

        return await self._acached(key, 3600, _aget)

    async def afetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "semanticscholar.org" not in host:
            return None
        # URL pattern: semanticscholar.org/paper/<paperId>
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "paper":
            return None
        paper_id = parts[1]
        # Route through the shared wrapper (pacer + concurrency cap + breaker); it degrades to
        # None on any failure / breaker-open, so no local try/except is needed. The wrapper awaits the
        # semanticscholar library's async client on the loop.
        paper = await _s2.aget_paper(paper_id, fields=self._FIELDS)
        if paper is None:
            return None
        try:
            return self._paper_to_document(paper)
        except Exception as exc:  # noqa: BLE001
            logger.warning("S2 fetch_url failed for %s: %s", url, exc)
            return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # Delegate to the ONE shared, single-flighted S2 probe (60s cache). Before this, this
        # adapter probed S2 directly in its own health_check; the all-source health sweep then
        # fired every S2-backed source's probe at once, bursting the shared key into a 429 storm
        # that tripped the breaker. await _s2.ahealth() is single-flighted so N concurrent callers cause
        # exactly ONE upstream call, and surfaces a recent-429 signal honestly.
        return await _s2.ahealth()

    @staticmethod
    def _paper_to_document(paper) -> Document:
        authors = []
        try:
            authors = [a.name for a in (paper.authors or []) if a.name]
        except Exception:  # noqa: BLE001
            pass
        author_str = ", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")

        # Compose content: TLDR if available + abstract
        content_parts = []
        tldr = getattr(paper, "tldr", None)
        if tldr and getattr(tldr, "text", None):
            content_parts.append(f"**TLDR:** {tldr.text}")
        if paper.abstract:
            content_parts.append(paper.abstract)
        content = "\n\n".join(content_parts)

        date = None
        if getattr(paper, "publicationDate", None):
            date = to_utc(str(paper.publicationDate))

        # The semanticscholar library keeps the source JSON on .raw_data — use it
        # directly for metadata["raw"] (already a plain, serializable dict).
        raw = getattr(paper, "raw_data", None)

        return Document(
            source="semantic_scholar",
            source_id=paper.paperId or "",
            url=paper.url or f"https://semanticscholar.org/paper/{paper.paperId}",
            title=(paper.title or "(untitled)").strip(),
            content=content,  # TLDR + full abstract — no truncation
            author=author_str or None,
            date=date,
            signals=mk_signal("citations", getattr(paper, "citationCount", None),
                              kind="citation", by="semantic_scholar/citationCount"),
            tags=list(getattr(paper, "fieldsOfStudy", None) or []),
            metadata={
                "venue": getattr(paper, "venue", None),
                "year": getattr(paper, "year", None),
                # The classification S2's fields_of_study search parameter filters on (probed
                # 2026-10-20: every hit of fields_of_study=Biology carries Biology here, while the
                # external fieldsOfStudy in ``tags`` often does not).
                "fields_of_study": _s2_fields(paper, raw),
                "influential_citation_count": getattr(
                    paper, "influentialCitationCount", None
                ),
                "external_ids": getattr(paper, "externalIds", None),
                "raw": raw if isinstance(raw, dict) else None,  # S2 paper original dict
            },
        )


def _s2_fields(paper, raw) -> list:
    """The distinct categories of a paper's s2FieldsOfStudy, in order; [] when absent."""
    items = getattr(paper, "s2FieldsOfStudy", None)
    if not isinstance(items, (list, tuple)) or not items:
        items = raw.get("s2FieldsOfStudy") if isinstance(raw, dict) else None
    out: list = []
    for it in items if isinstance(items, (list, tuple)) else []:
        cat = it.get("category") if isinstance(it, dict) else getattr(it, "category", None)
        if isinstance(cat, str) and cat and cat not in out:
            out.append(cat)
    return out

from omniseek.core.fetcher import register_adapter

register_adapter(SemanticScholarAdapter())
