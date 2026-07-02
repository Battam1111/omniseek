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

from penumbra.core import _openalex as oa
from penumbra.core import cache
from penumbra.core.normalize import Document, mk_signal

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


class OpenAlexAdapter:
    name = "openalex"
    backend = "openalex"  # shared by openalex_cn (subclass) / researcher_watch / 39 org_watch slices
    needs_credentials = False
    description = (
        "OpenAlex — open academic graph (250M+ scholarly works, "
        "institutions + concepts ontology; open alternative to Semantic Scholar)"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # Pull any allowlisted OpenAlex `key:value` filters out of the query and
        # route them to the API's `filter=` param. No recognized filter →
        # behaviour unchanged (plain `search=` as before).
        search_text, filter_str = _parse_filters(query or "")

        # CRITICAL: the resolved filter is part of the cache identity — otherwise
        # `from_publication_date:2024-01-01 x` and `...:2020-01-01 x` (same search
        # text) would collide. Empty filter_str keeps the key shape stable.
        key = cache.make_key("openalex", "search", search_text, filter_str or "", limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        params: dict = {
            "per-page": min(limit, 25),
            "sort": "relevance_score:desc",
        }
        # Only send `search` when there's actual free text — a filter-only query
        # (e.g. just `institutions.id:I... from_publication_date:...`) is valid on
        # its own, and an empty `search=` would needlessly skew relevance ranking.
        if search_text:
            params["search"] = search_text
        if filter_str:
            params["filter"] = filter_str

        try:
            data = oa.get_json("/works", params)
        except Exception as exc:  # noqa: BLE001 — incl. the shared circuit breaker
            logger.warning("OpenAlex search failed: %s", exc)
            return []

        results = data.get("results") or []
        docs: list[Document] = []
        for work in results[:limit]:
            try:
                docs.append(self._work_to_document(work))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed OpenAlex work: %s", exc)

        cache.set_docs(key, docs, ttl=3600)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "openalex.org" not in host:
            return None
        path = urlparse(url).path.strip("/")
        work_id = path.split("/")[-1]
        if not work_id.startswith("W"):
            return None
        try:
            work = oa.get_json(f"/works/{work_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAlex fetch_url failed: %s", exc)
            return None
        return self._work_to_document(work)

    def health_check(self) -> tuple[bool, str]:
        # Shared single-flight upstream probe (see _openalex.health): all OpenAlex-backed sources
        # delegate here so the health sweep makes ONE OpenAlex call, not one-per-source (which used
        # to burst the shared key into 429 and trip the breaker, degrading all 40 at once). The old
        # probe here also used a `search` call, needlessly spending the scarce 1k/day search budget.
        return oa.health()

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
            content=p["abstract"] or "(no abstract available)",
            author=author_str,
            date=p["date"],
            signals=mk_signal("citations", p["cited_by"],
                              kind="citation", by="openalex/cited_by"),
            tags=concepts,
            metadata={
                "openalex_id": p["work_id"],
                "doi": p["doi"],
                # The drill-in handle for penumbra_paper_enrich / penumbra_paper_recommend. source_id is an
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


from penumbra.core.fetcher import register_adapter

register_adapter(OpenAlexAdapter())
