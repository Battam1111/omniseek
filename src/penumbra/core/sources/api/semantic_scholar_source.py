"""Semantic Scholar adapter — uses the official semanticscholar Python package.

Free public API. Without API key: shared 5000 req/5min pool.
With API key (S2_API_KEY env or credentials/semantic_scholar.json): 1 RPS guaranteed.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import _s2, auth, cache
from penumbra.core.normalize import Document, mk_signal

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
auth.write_template(
    "semantic_scholar",
    {"_comment": "API key is OPTIONAL. Without it the shared rate limit is used.", "api_key": ""},
)


class SemanticScholarAdapter:
    name = "semantic_scholar"
    needs_credentials = False  # API key is optional, not required
    description = "Semantic Scholar — 225M+ papers, citation graphs, TLDR summaries"

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
    ]

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # Pull allowlisted qualifiers (year / venue / min_citations) out of the
        # query → S2 API params. None → unchanged.
        clean_query, qualifiers = _parse_s2_qualifiers(query or "")

        # CRITICAL: qualifiers are part of the cache identity — `year:2020 x` and
        # `year:2023 x` must not collide. Sorted repr keeps the key deterministic.
        qual_key = ",".join(f"{k}={qualifiers[k]}" for k in sorted(qualifiers))
        key = cache.make_key("s2", "search", clean_query, qual_key, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        # Route through the shared _s2 wrapper: ONE pacer + concurrency cap + breaker for every
        # S2-backed capability. The wrapper is already HARD-BOUNDED to `limit`, honors the ~1 RPS
        # pace, and degrades to [] on error / breaker-open. No hand-rolled retry loop here: the
        # shared breaker IS the correct backoff (fail fast, do not sleep-and-multiply on a 429,
        # which only deepens the throttle and stacks latency across every caller). The qualifiers
        # (year / venue / min_citation_count) pass straight through to the S2 search API.
        results = _s2.search_paper(clean_query, limit=limit, fields=self._FIELDS, **qualifiers)

        docs: list[Document] = []
        for paper in results:
            try:
                docs.append(self._paper_to_document(paper))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed S2 paper: %s", exc)

        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=3600)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "semanticscholar.org" not in host:
            return None
        # URL pattern: semanticscholar.org/paper/<paperId>
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "paper":
            return None
        paper_id = parts[1]
        # Route through the shared wrapper (pacer + concurrency cap + breaker); it degrades to
        # None on any failure / breaker-open, so no local try/except is needed.
        paper = _s2.get_paper(paper_id, fields=self._FIELDS)
        if paper is None:
            return None
        try:
            return self._paper_to_document(paper)
        except Exception as exc:  # noqa: BLE001
            logger.warning("S2 fetch_url failed for %s: %s", url, exc)
            return None

    def health_check(self) -> tuple[bool, str]:
        # Delegate to the ONE shared, single-flighted S2 probe (60s cache). Before this, this
        # adapter probed S2 directly in its own health_check; the all-source health sweep then
        # fired every S2-backed source's probe at once, bursting the shared key into a 429 storm
        # that tripped the breaker. _s2.health() is single-flighted so N concurrent callers cause
        # exactly ONE upstream call, and surfaces a recent-429 signal honestly.
        return _s2.health()

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
        content = "\n\n".join(content_parts) or "(No abstract)"

        date = None
        if getattr(paper, "publicationDate", None):
            from datetime import datetime as _dt

            try:
                date = _dt.fromisoformat(str(paper.publicationDate))
            except (ValueError, TypeError):
                date = None

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
                "influential_citation_count": getattr(
                    paper, "influentialCitationCount", None
                ),
                "external_ids": getattr(paper, "externalIds", None),
                "raw": raw if isinstance(raw, dict) else None,  # S2 paper original dict
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(SemanticScholarAdapter())
