"""Zenodo — open research repository (papers, datasets, software) via the keyless REST API.

Zenodo (CERN-operated, on InvenioRDM) is the catch-all open-access archive for the long
tail of scholarship that arXiv / OpenAlex / Crossref under-cover: deposited datasets,
software releases, posters, theses, preprints, and conference material — each with a
minted DOI. It fills Polaris-eye's gap on RESEARCH ARTIFACTS (the dataset/software half of
"papers"), not just the journal article.

Access via the public Zenodo REST API (no auth required for read search):

    GET https://zenodo.org/api/records?q=<query>&size=<limit>&sort=mostrecent

Response: ``{"hits": {"total": N, "hits": [<record>, ...]}}`` where each record carries
``id``, ``doi`` / ``doi_url``, ``links`` (``self_html`` is the canonical record page),
``stats`` (downloads/views), and ``metadata`` with ``title``, ``creators`` (list of
``{name, affiliation, orcid}``), ``description`` (HTML prose), ``publication_date``
(``YYYY-MM-DD``), ``resource_type`` (``{title, type, subtype}``), and ``keywords``.

Thin subclass over BaseScrapeAdapter: the cache check / atomic set_docs / self-registration
ritual lives in the base; this adapter only declares its facets and fills the two hooks.
The query path takes Zenodo's default RELEVANCE order (no explicit sort -- q-present defaults to
relevance) over a WIDE candidate pool (capped at Zenodo's max page size of 25; size>=30 -> HTTP
400), then re-ranks locally with the eye's shared BM25 scorer and caps to ``limit`` (server
relevance alone leaks off-topic keyword hits, e.g. matching "model" in an unrelated paper --
caught in a WebSearch head-to-head, 2026-06-17).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from markdownify import markdownify as html_to_md

from penumbra.core import http, relevance
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://zenodo.org/api/records"
# Pull a WIDE candidate pool, then re-rank with the eye's shared BM25 scorer + cap to limit
# (_to_documents). Zenodo CAPS page size at 25 (size>=30 -> HTTP 400), so 25 is the max pool.
_ZENODO_MAX_SIZE = 25
_CANDIDATE_POOL = 25


class ZenodoAdapter(BaseScrapeAdapter):
    name = "zenodo"
    needs_credentials = False
    description = "Zenodo — open research repository (papers, datasets, software, theses) with minted DOIs (keyless REST API)"
    cache_ttl = 900
    kind = "lookup"
    domains = ["papers", "datasets"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # NO explicit sort: Zenodo defaults to RELEVANCE order when q is present. Pull a WIDE pool
        # (capped at Zenodo's max page size of 25 -- size>=30 returns HTTP 400); _to_documents
        # re-ranks it with the eye's shared BM25 + caps to the caller's limit.
        return http.get_json(
            API_URL,
            params={"q": query, "size": min(max(limit, _CANDIDATE_POOL), _ZENODO_MAX_SIZE)},
            timeout=15,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        hits = (raw.get("hits") or {}).get("hits") or []
        docs: list[PolarisDocument] = []
        for rec in hits:  # the WIDE candidate pool (see _raw_fetch); re-ranked + capped below
            if not isinstance(rec, dict):
                continue
            doc = self._record_to_doc(rec)
            if doc is not None:
                docs.append(doc)
        # Zenodo's server relevance still leaks off-topic keyword hits; re-rank the wide pool with
        # the eye's shared BM25 scorer (title 3x + content 1x), keep only matches, cap to limit.
        # A term-less query keeps Zenodo's own order.
        if docs and relevance.query_terms(query):
            scores = relevance.doc_scores(docs, query)
            docs = [d for _s, d in sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
                    if _s > 0.0]
        return docs[:limit]

    def _record_to_doc(self, rec: dict) -> Optional[PolarisDocument]:
        meta = rec.get("metadata") or {}
        title = (meta.get("title") or "").strip()
        if not title:
            return None

        rec_id = str(rec.get("id") or rec.get("recid") or "")
        links = rec.get("links") or {}
        doi = rec.get("doi") or meta.get("doi")
        # Prefer the canonical record page; fall back to the DOI resolver, then the API self link.
        url = (
            links.get("self_html")
            or links.get("latest_html")
            or rec.get("doi_url")
            or (f"https://doi.org/{doi}" if doi else "")
            or links.get("self")
            or (f"https://zenodo.org/records/{rec_id}" if rec_id else "")
        )

        author = _format_authors(meta.get("creators") or [])
        date = _parse_date(meta.get("publication_date"))
        content = _description_to_md(meta.get("description"))

        # resource_type is a dict like {"title": "Dataset", "type": "dataset", "subtype": ...}
        rtype_raw = meta.get("resource_type") or {}
        rtype_label = (rtype_raw.get("title") if isinstance(rtype_raw, dict) else None) or (
            rtype_raw if isinstance(rtype_raw, str) else None
        )

        # keywords → tags (the resource-type label is a useful coarse tag too).
        kw = meta.get("keywords") or []
        tags = [str(k) for k in kw if isinstance(k, str)]
        if rtype_label:
            tags = [rtype_label, *tags]

        # views: a source-reported engagement count (None-safe via mk_signal).
        stats = rec.get("stats") or {}
        views = stats.get("views") if isinstance(stats, dict) else None
        signals = mk_signal("views", views, kind="engagement", by="zenodo/views")

        metadata: dict[str, Any] = {
            "resource_type": rtype_label,
            "resource_type_raw": jsonsafe(rtype_raw) if rtype_raw else None,
            "doi": doi,
            "zenodo_id": rec_id,
            "raw": jsonsafe(rec),
        }
        journal = meta.get("journal")
        if journal:
            metadata["journal"] = jsonsafe(journal)

        return PolarisDocument(
            source=self.name,
            source_id=rec_id or (doi or title),
            url=url,
            title=title,
            content=content,
            author=author or None,
            date=date,
            signals=signals,
            tags=tags,
            metadata=metadata,
        )


def _format_authors(creators: list) -> str:
    """Join creator names into a single author string (mirrors Crossref/DBLP author handling)."""
    names = []
    for c in creators:
        if isinstance(c, dict):
            nm = (c.get("name") or "").strip()
            if nm:
                names.append(nm)
        elif isinstance(c, str) and c.strip():
            names.append(c.strip())
    return ", ".join(names)


def _parse_date(s: Any) -> Optional[datetime]:
    """Zenodo publication_date is ISO ``YYYY-MM-DD`` (sometimes a year or year-month)."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _description_to_md(desc: Any) -> str:
    """Convert the HTML description to clean Markdown (same html_to_md the RSS base uses).
    Falls back to the raw string on the rare payload markdownify chokes on."""
    if not isinstance(desc, str) or not desc.strip():
        return ""
    try:
        return html_to_md(desc, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001 — markdownify can be picky on weird HTML
        return desc.strip()


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
