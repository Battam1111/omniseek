"""OpenAlex — open academic graph (250M+ scholarly works, free, no auth).

Complement to existing arxiv + semantic_scholar adapters. OpenAlex covers a
broader corpus (250M works vs SS 225M), provides institutional + concepts
ontology, and is fully open-licensed. Use it to:
- Cross-reference papers found on arXiv with formal venue/citation graph
- Discover papers via OpenAlex Concepts (FOS / topic tree)
- Track institutional output (e.g., all papers from a lab)

Docs: https://docs.openalex.org
Key endpoints:
- /works?search=...              — full-text search
- /works/{W-id}                  — fetch by OpenAlex ID
- /works?filter=concept.id:...   — filter by concept (not exposed here)

Polite-pool: include `mailto:` in User-Agent for higher rate limit.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse


from omniseek.core import _openalex as oa
from omniseek.core import cache
from omniseek.core.normalize import Document, mk_signal
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

# OpenAlex structured filters the caller may embed in the query, e.g.
# `from_publication_date:2024-01-01 diffusion` or `institutions.id:I136199984 llm`.
# These map onto the API's ``filter=`` param (comma-joined). We use an ALLOWLIST
# (not a generic `word:word` grab) so ordinary text like "ratio:1" or a colon in
# prose is never mistaken for a filter. Add keys here as needs arise — anything
# not listed simply stays part of the free-text ``search`` (and the full work is
# always available under metadata["raw"] regardless).
_OPENALEX_FILTER_KEYS = (
    "from_publication_date",
    "to_publication_date",
    "publication_year",
    "institutions.id",
    "institutions.ror",
    "institutions.country_code",
    "authorships.institutions.id",
    "author.id",
    "concepts.id",
    "primary_topic.id",
    "type",
    "is_oa",
    "language",
)
# key must be one of the allowlist; value is a non-space, non-comma run.
_FILTER_RE = re.compile(
    r"(?:^|\s)(" + "|".join(re.escape(k) for k in _OPENALEX_FILTER_KEYS) + r"):([^\s,]+)",
    re.IGNORECASE,
)


def _parse_filters(query: str) -> tuple[str, Optional[str]]:
    """Split allowlisted OpenAlex ``key:value`` filters out of the query.

    Returns ``(search_text, filter_string)`` where ``filter_string`` is the
    comma-joined OpenAlex filter expression (or ``None`` when no recognized
    filter is present → behaviour unchanged). Recognized filters are stripped
    from the free-text search portion.
    """
    pairs: list[str] = []
    seen: set[str] = set()
    for m in _FILTER_RE.finditer(query or ""):
        key = m.group(1).lower()
        val = m.group(2)
        token = f"{key}:{val}"
        if token not in seen:
            seen.add(token)
            pairs.append(token)
    if not pairs:
        return (query or "").strip(), None
    search_text = _FILTER_RE.sub(" ", query).strip()
    return search_text, ",".join(pairs)


class OpenAlexAdapter(SourceBase):
    name = "openalex"
    backend = "openalex"  # shared by openalex_cn (subclass) / researcher_watch / 39 org_watch slices
    needs_credentials = False
    description = (
        "OpenAlex: open academic graph (250M+ scholarly works, "
        "institutions + concepts ontology; open alternative to Semantic Scholar)"
    )

    # Source spec (omniseek.core.filters). Every field translates into the allowlisted `key:value`
    # tokens _parse_filters already routes to the API's filter= param, so the cache key follows.
    filters = {
        "publication_date": {"type": "date", "ops": ["eq", "gte", "lte", "range"], "doc_key": "date",
                             "desc": "publication date (sent as from_publication_date / to_publication_date)"},
        "publication_year": {"type": "int", "ops": ["eq", "any"], "doc_key": "metadata.publication_year",
                             "desc": "publication year (sent as publication_year:, several joined with |)"},
        "type": {"type": "keyword", "ops": ["eq", "any"], "doc_key": "metadata.type",
                 "desc": "work type such as article, preprint, dissertation, book-chapter, dataset"},
        "is_oa": {"type": "bool", "ops": ["eq"], "doc_key": "metadata.is_oa",
                  "desc": "open access or not"},
        "language": {"type": "keyword", "ops": ["eq", "any"],
                     "desc": "ISO 639-1 language code of the work, e.g. en, zh"},
        "institution_country": {"type": "keyword", "ops": ["eq", "any"],
                                "desc": "ISO country code of an author institution, e.g. us, cn "
                                        "(sent as institutions.country_code)"},
        "institution_id": {"type": "keyword", "ops": ["eq", "any"],
                           "desc": "OpenAlex institution id, e.g. I136199984 (sent as institutions.id)"},
        "author_id": {"type": "keyword", "ops": ["eq", "any"],
                      "desc": "OpenAlex author id, e.g. A5023888391 (sent as author.id)"},
    }
    # omniseek_search(as_of=...) pushes "publication_date <= as_of" upstream through this field: the publication date (openalex_cn inherits it).
    as_of_field = "publication_date"
    coverage = {"since": None, "until": None, "gaps": [],
                "note": "the whole OpenAlex works graph (250M+ works); its oldest date is not recorded here"}
    license = "OpenAlex data is CC0 1.0"
    # Two years, so the sample also shows the any-of form (values joined with |).
    samples = [{"query": "graph neural networks", "filters": {"publication_year": [2022, 2023]}},
               {"query": "protein structure prediction", "filters": {"type": "article", "is_oa": True}}]

    _FILTER_TOKENS = {"publication_year": "publication_year", "type": "type", "language": "language",
                      "institution_country": "institutions.country_code",
                      "institution_id": "institutions.id", "author_id": "author.id"}

    def translate_filters(self, query: str, clauses: list) -> str:
        """Append the filters as OpenAlex `key:value` tokens (any-of joined with |); dates become
        from_publication_date / to_publication_date."""
        from omniseek.core.filters import Unsupported
        toks = []
        for c in clauses:
            if c.field == "publication_date":
                if c.lo:
                    toks.append(f"from_publication_date:{c.lo.isoformat()}")
                if c.hi:
                    toks.append(f"to_publication_date:{c.hi.isoformat()}")
            elif c.field == "is_oa":
                toks.append(f"is_oa:{'true' if c.value else 'false'}")
            elif c.field in self._FILTER_TOKENS:
                vals = [c.value] if c.op == "eq" else list(c.value)
                if any((" " in str(v)) or ("," in str(v)) for v in vals):
                    raise Unsupported(c.field, "a value with a space or comma cannot be sent")
                toks.append(f"{self._FILTER_TOKENS[c.field]}:{'|'.join(str(v) for v in vals)}")
            else:
                raise Unsupported(c.field)
        return " ".join([(query or "").strip(), *toks]).strip()

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, this is the only one now) (the OpenAlex sources go native async), so the live async
        omniseek_search path awaits it DIRECTLY instead of parking a pool thread on the ``/works`` call. It
        mirrors ``search`` step-for-step; only the BLOCKING work moves:
          • the disk cache read/write → ``anyio.to_thread.run_sync`` (get_docs / set_docs do file IO);
          • the single OpenAlex egress ``oa.get_json`` → ``await oa.aget_json`` (the byte-faithful async
            twin sharing the SAME breaker / rate pacer / concurrency cap / 2-lane budget — same path + params).
        There is NO per-item fan-out here (openalex is ONE ``/works`` call), so no asyncio.gather is
        needed. The pure-CPU parse/map (``_work_to_document`` → ``oa.parse_work``) stays ON the loop,
        byte-identical to ``search``. SAME cache KEY as ``search`` (async and sync share the cache).

        SUBCLASS GUARD (the load-bearing rule, same shape as ``RSSAdapterBase.asearch``): ``openalex_cn``
        SUBCLASSES this adapter and OVERRIDES ``search`` (it pins ``language:zh`` and re-stamps the doc
        source). ``AsyncSearchCapable`` is a runtime-checkable Protocol keyed on method PRESENCE, so a
        subclass that only inherits THIS ``asearch`` is still flagged async-capable — the live async path
        would route here and SILENTLY DROP the override. So when ``search`` is overridden, run the
        subclass's OWN sync ``search`` OFF the loop instead (correct + faithful; that one explicit_only
        facet just does not go native async, which is fine). This auto-protects every present/future
        subclass with no per-subclass ``asearch`` needed, resolving the hazard in this one file.
        """
        # Pull any allowlisted OpenAlex `key:value` filters out of the query and route them to the
        # API's `filter=` param. No recognized filter → behaviour unchanged (plain `search=` as before).
        search_text, filter_str = _parse_filters(query or "")

        # CRITICAL: the resolved filter is part of the cache identity (same as search) — otherwise
        # `from_publication_date:2024-01-01 x` and `...:2020-01-01 x` (same search text) would collide.
        # This is the SAME key as search() — async and sync share the cache.
        key = cache.make_key("openalex", "search", search_text, filter_str or "", limit)

        async def _aget() -> Optional[list[Document]]:
            params: dict = {
                "per-page": min(limit, 25),
                "sort": "relevance_score:desc",
            }
            # Only send `search` when there's actual free text — a filter-only query is valid on its own,
            # and an empty `search=` would needlessly skew relevance ranking.
            if search_text:
                params["search"] = search_text
            if filter_str:
                params["filter"] = filter_str

            try:
                data = await oa.aget_json("/works", params)  # native-async egress twin (same path + params)
            except Exception as exc:  # noqa: BLE001 — incl. the shared circuit breaker
                logger.warning("OpenAlex search failed: %s", exc)
                return None  # a failure: not cached

            results = data.get("results") or []
            docs: list[Document] = []
            for work in results[:limit]:
                try:
                    docs.append(self._work_to_document(work))  # pure CPU parse/map — stays ON the loop
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Skipping malformed OpenAlex work: %s", exc)

            return docs

        return await self._acached(key, 3600, _aget)

    async def afetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "openalex.org" not in host:
            return None
        path = urlparse(url).path.strip("/")
        work_id = path.split("/")[-1]
        if not work_id.startswith("W"):
            return None
        try:
            work = await oa.aget_json(f"/works/{work_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAlex fetch_url failed: %s", exc)
            return None
        return self._work_to_document(work)

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # Shared single-flight upstream probe (see _openalex.health): all OpenAlex-backed sources
        # delegate here so the health sweep makes ONE OpenAlex call, not one-per-source (which used
        # to burst the shared key into 429 and trip the breaker, degrading all 40 at once). The old
        # probe here also used a `search` call, needlessly spending the scarce 1k/day search budget.
        return await oa.ahealth()

    @staticmethod
    def _work_to_document(work: dict) -> Document:
        p = oa.parse_work(work)
        title = p["title"] or "(untitled)"
        url = p["url"] or f"https://openalex.org/{p['work_id'] or '?'}"

        author_names = p["authors"]
        author_str = ", ".join(author_names[:5]) if author_names else None
        if author_str and len(author_names) > 5:
            author_str += f" et al. ({len(author_names)} authors)"

        # Concepts (top-level topics) — an openalex-specific extra
        concepts = []
        for c in (work.get("concepts") or [])[:5]:
            if isinstance(c, dict) and c.get("display_name"):
                concepts.append(c["display_name"])

        return Document(
            source="openalex",
            source_id=p["work_id"] or "?",
            url=url,
            title=title,
            content=p["abstract"] or "",
            author=author_str,
            date=p["date"],
            signals=mk_signal("citations", p["cited_by"],
                              kind="citation", by="openalex/cited_by"),
            tags=concepts,
            metadata={
                "openalex_id": p["work_id"],
                "doi": p["doi"],
                # The drill-in handle for omniseek_paper_enrich / omniseek_paper_recommend. source_id is an
                # OpenAlex W-id those tools REJECT (enrich errors, recommend returned a silent n:0),
                # so surface the DOI here, named, where a chaining agent looks for "the id to pass".
                "paper_id": p["doi"] or None,
                "cited_by_count": p["cited_by"],
                "type": work.get("type"),
                "is_oa": (work.get("open_access") or {}).get("is_oa"),
                "venue": p["venue"],
                "publication_year": work.get("publication_year"),
                "raw": work,  # OpenAlex work original dict
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(OpenAlexAdapter())
