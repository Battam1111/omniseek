"""DBLP — CS bibliography database (free, no auth).

DBLP indexes ~7M CS publications + ~3M authors with rich metadata
(venue, year, DOI, BibTeX, co-authorship graph). For ML/AI PhDs it's
the canonical lens onto venue + author trajectory.

API docs: https://dblp.org/faq/13501473.html
Endpoints used:
- /search/publ/api?q=...&format=json&h=N — publication search
- /search/author/api?q=...&format=json&h=N — author search

Response shape (publ):
{ "result": { "hits": { "hit": [
    { "info": { "title", "authors", "venue", "year", "doi", "url", "type" } }
] } } }

DBLP query language supports: phrase quotes, prefix wildcard *, +-NOT operators.

Migrated to ``BaseAPIAdapter`` (template method): the base owns the
cache-check / map / cache-set / auto-register mechanism. The two source-specific
facts live in the hooks — ``_raw_fetch`` (the publ-search call + the retry-on-500
with a sanitized query) and ``_to_document`` (the per-field raw→doc mapping). DBLP
sorts server-side, so ``rank_locally = False`` keeps the server order verbatim
(byte-identical to the hand-written form). ``fetch_url`` + ``health_check`` are
overridden because they do real API I/O the base defaults can't supply.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core.normalize import PolarisDocument, jsonsafe
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

DBLP_BASE = "https://dblp.org"
TIMEOUT = 20
USER_AGENT = "polaris-eye/0.1 (automated retrieval)"


class DBLPAdapter(BaseAPIAdapter):
    name = "dblp"
    needs_credentials = False
    description = (
        "DBLP — CS bibliography database (~7M publications + 3M authors, "
        "venue + year + DOI metadata; canonical CS publication lens)"
    )

    # Cache identity must stay ("dblp", "publ_search", query, limit) — the
    # hand-written form used this middle part, so preserve it exactly.
    search_label = "publ_search"
    cache_ttl = 3600
    # DBLP sorts server-side (server relevance); keep its order verbatim.
    rank_locally = False
    url_host = "dblp.org"

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        """Return the publ-search hit list (verbatim port of the old retry logic).

        DBLP intermittently returns 500 on certain query strings. Retry once with a
        sanitized version (alphanumeric + space only) on first failure; return [] if
        the sanitized query is empty or the retry also fails (the base maps [] → no
        docs, no cache write)."""
        try:
            data = self._publ_search(query, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DBLP first attempt failed (%s); retrying with sanitized query", exc)
            sanitized = " ".join(part for part in query.split() if part.isalnum())
            if not sanitized:
                return []
            try:
                data = self._publ_search(sanitized, limit)
            except Exception as exc2:  # noqa: BLE001
                logger.warning("DBLP retry also failed: %s", exc2)
                return []

        return (((data.get("result") or {}).get("hits") or {}).get("hit")) or []

    def _to_document(self, raw) -> PolarisDocument:
        """One publ-search hit → PolarisDocument (delegates to the verbatim mapper)."""
        return self._publ_to_document(raw)

    # --------------------------------------------------------------- API call
    def _publ_search(self, query: str, limit: int) -> dict:
        resp = httpx.get(
            f"{DBLP_BASE}/search/publ/api",
            params={"q": query, "format": "json", "h": min(limit, 50)},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        host = (urlparse(url).hostname or "").lower()
        if "dblp.org" not in host:
            return None
        # DBLP publication URLs look like dblp.org/rec/<key>.html
        # We'll fall back to querying by the URL itself as a publ-search hit.
        try:
            data = self._publ_search(url, 1)
            hits = (((data.get("result") or {}).get("hits") or {}).get("hit")) or []
            if hits:
                return self._publ_to_document(hits[0])
        except Exception as exc:  # noqa: BLE001
            logger.debug("DBLP fetch_url failed: %s", exc)
        return None

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{DBLP_BASE}/search/author/api",
                params={"q": "Yoshua Bengio", "format": "json", "h": 1},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=10,
            )
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ field mapping
    @staticmethod
    def _publ_to_document(hit: dict) -> PolarisDocument:
        info = hit.get("info") or {}
        publ_id = hit.get("@id") or info.get("key") or ""
        title = info.get("title") or "(no title)"
        url = info.get("url") or info.get("ee") or info.get("doi") or f"https://dblp.org/rec/{publ_id}.html"
        if isinstance(url, list):
            url = url[0]
        doi = info.get("doi")

        # Authors come as either string or dict or list-of-dicts
        authors_field = info.get("authors") or {}
        author_list: list[str] = []
        if isinstance(authors_field, dict):
            au = authors_field.get("author")
            if isinstance(au, list):
                for a in au:
                    if isinstance(a, dict):
                        author_list.append(a.get("text", ""))
                    elif isinstance(a, str):
                        author_list.append(a)
            elif isinstance(au, dict):
                author_list.append(au.get("text", ""))
            elif isinstance(au, str):
                author_list.append(au)
        author_str = ", ".join(filter(None, author_list[:6])) or None

        venue = info.get("venue") or info.get("journal") or info.get("booktitle")
        year = info.get("year")
        date = None
        if year:
            try:
                date = datetime(int(year), 1, 1, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        pub_type = info.get("type")  # e.g., "Conference and Workshop Papers", "Journal Articles"

        return PolarisDocument(
            source="dblp",
            source_id=str(publ_id),
            url=url,
            title=title,
            content=f"{venue or '(no venue)'} • {year or '?'} • {pub_type or '?'}",
            author=author_str,
            date=date,
            tags=[pub_type] if pub_type else [],
            metadata={
                "publ_id": publ_id,
                "venue": venue,
                "year": year,
                "doi": doi,
                "type": pub_type,
                "raw": jsonsafe(hit),
            },
        )
