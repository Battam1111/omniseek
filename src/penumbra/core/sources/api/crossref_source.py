"""Crossref — DOI registration agency + scholarly works metadata.

Crossref maintains the DOI metadata for the majority of formally-published
scholarly works (~150M records). Complement to:
- arxiv (preprints)
- openalex (open scholarly graph)
- semantic_scholar (citation graph + TLDRs)

Each adapter brings different lens. Crossref strength: definitive DOI
resolution, formal publisher metadata, full bibliographic records.

API docs: https://api.crossref.org/swagger-ui/index.html
- /works?query=...&rows=N — full-text search
- /works/{doi} — fetch by DOI

Polite-pool: include `mailto:` in User-Agent for fast lane.

Note: arXiv DOIs (10.48550/arXiv.XXXX.YYYYY) are NOT all indexed in
Crossref — for preprints prefer the `arxiv` adapter.

Migrated to ``BaseAPIAdapter`` (template method): the cache/search/registration
boilerplate now lives in the base; the two hooks below (``_raw_fetch`` =
``/works?query=`` GET, ``_to_document`` = the verbatim item→doc mapping) carry
every Crossref-specific fact. ``rank_locally=False`` keeps Crossref's
server-relevance order byte-identical to the hand-written form (the old code
never ran a local scorer). ``fetch_url`` (DOI/api lookup) and ``health_check``
stay overridden verbatim — they hit by-DOI / probe endpoints the base can't
express generically.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import auth
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

CROSSREF_BASE = "https://api.crossref.org"
TIMEOUT = 20
# Polite-pool contact is the DEPLOYER's, host-injected via auth.contact_email(), never in-tree.
USER_AGENT = f"polaris-eye/0.1 (mailto:{auth.contact_email()}; automated retrieval)"


class CrossrefAdapter(BaseAPIAdapter):
    name = "crossref"
    needs_credentials = False
    description = (
        "Crossref — DOI registration agency, ~150M formally-published works "
        "(complement to arxiv/openalex/semantic_scholar)"
    )

    # Crossref already returns server-relevance order; preserve it verbatim
    # (the hand-written search ran no local scorer).
    rank_locally = False
    cache_ttl = 3600
    url_host = "doi.org"

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        """GET /works?query= and return message.items ([] on any failure)."""
        try:
            resp = httpx.get(
                f"{CROSSREF_BASE}/works",
                params={"query": query, "rows": min(limit, 25)},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Crossref search failed: %s", exc)
            return []

        return ((data.get("message") or {}).get("items")) or []

    def _to_document(self, raw) -> Optional[PolarisDocument]:
        return self._item_to_document(raw)

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        host = (urlparse(url).hostname or "").lower()
        # Crossref handles DOI URLs (doi.org/...) and direct api.crossref.org/works/DOI
        doi: Optional[str] = None
        if "doi.org" in host:
            # https://doi.org/10.xxxx/yyyy → DOI is everything after /
            doi = urlparse(url).path.lstrip("/")
        elif "api.crossref.org" in host:
            path = urlparse(url).path.strip("/")
            if path.startswith("works/"):
                doi = path[len("works/"):]
        if not doi:
            return None

        try:
            resp = httpx.get(
                f"{CROSSREF_BASE}/works/{doi}",
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            item = (resp.json() or {}).get("message")
            if not isinstance(item, dict):
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Crossref fetch_url failed: %s", exc)
            return None
        return self._item_to_document(item)

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{CROSSREF_BASE}/works",
                params={"query": "test", "rows": 1},
                headers={"User-Agent": USER_AGENT},
                timeout=8,
            )
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _item_to_document(item: dict) -> PolarisDocument:
        doi = item.get("DOI") or ""
        url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")

        # Title is a list
        titles = item.get("title") or []
        title = titles[0] if titles else "(no title)"

        # Authors
        authors_field = item.get("author") or []
        author_names: list[str] = []
        for a in authors_field:
            if not isinstance(a, dict):
                continue
            given = a.get("given") or ""
            family = a.get("family") or ""
            full = f"{given} {family}".strip()
            if full:
                author_names.append(full)
        author_str = ", ".join(author_names[:5]) if author_names else None
        if author_str and len(author_names) > 5:
            author_str += f" et al. ({len(author_names)} authors)"

        # Container (venue / journal)
        containers = item.get("container-title") or []
        venue = containers[0] if containers else None

        # Date from issued or published-online
        date = None
        for date_field in ("issued", "published-online", "published-print", "published"):
            d = item.get(date_field) or {}
            parts = d.get("date-parts") if isinstance(d, dict) else None
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                try:
                    y = int(parts[0][0])
                    m = int(parts[0][1]) if len(parts[0]) > 1 else 1
                    dd = int(parts[0][2]) if len(parts[0]) > 2 else 1
                    date = datetime(y, m, dd, tzinfo=timezone.utc)
                    break
                except (ValueError, TypeError, IndexError):
                    continue

        # Abstract (often missing or as JATS XML)
        abstract = item.get("abstract") or ""
        if abstract:
            # Strip JATS XML tags for readability
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip()
        if not abstract:
            abstract = f"{venue or 'Crossref-indexed work'} • {item.get('type', 'work')}"

        return PolarisDocument(
            source="crossref",
            source_id=doi or url,
            url=url,
            title=title,
            content=abstract,
            author=author_str,
            date=date,
            signals=mk_signal("citations", item.get("is-referenced-by-count"),
                              kind="citation", by="crossref/is-referenced-by-count"),
            tags=item.get("subject") or [],
            metadata={
                "doi": doi,
                "type": item.get("type"),
                "venue": venue,
                "publisher": item.get("publisher"),
                "cited_by_count": item.get("is-referenced-by-count"),
                "references_count": item.get("references-count"),
                "raw": jsonsafe(item),  # Crossref work's original API dict
            },
        )
