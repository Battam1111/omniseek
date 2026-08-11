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

from penumbra.core import diag, http
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

DBLP_BASE = "https://dblp.org"
TIMEOUT = 20
USER_AGENT = "penumbra/0.1 (automated retrieval)"


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
                st = getattr(getattr(exc2, "response", None), "status_code", None)
                diag.note("dblp.search", url=f"{DBLP_BASE}/search/publ/api", status=st, exc=exc2)
                return []

        return (((data.get("result") or {}).get("hits") or {}).get("hit")) or []

    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async twin of ``_raw_fetch`` (verbatim port of the retry logic). Mirrors the sync
        control flow, only keying the sanitized-query retry off ``_apubl_search`` returning
        ``None`` (``http.aget_json``'s failure→None contract) exactly where the sync path keys off
        a raised exception: the SAME failure set (non-2xx / timeout / connection / bad JSON) fires
        the retry, and a reached-but-empty 200 does NOT (``data`` is a dict, not None). Each failed
        attempt already emits an ``http.get`` diag capture (with status + body) via the shared leaf;
        the ``dblp.search`` note is kept as the source-labeled breadcrumb on the double failure,
        mirroring the sync call site."""
        data = await self._apubl_search(query, limit)
        if data is None:
            logger.warning("DBLP first attempt failed; retrying with sanitized query")
            sanitized = " ".join(part for part in query.split() if part.isalnum())
            if not sanitized:
                return []
            data = await self._apubl_search(sanitized, limit)
            if data is None:
                logger.warning("DBLP retry also failed")
                diag.note("dblp.search", url=f"{DBLP_BASE}/search/publ/api",
                          body="both attempts returned None (see the http.get capture for status/body)")
                return []

        return (((data.get("result") or {}).get("hits") or {}).get("hit")) or []

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` → AsyncSearchCapable (the async fan-out awaits this
        directly, so DBLP's network wait costs a coroutine, not a held pool thread). Shares the
        base async cache round-trip ``_aapi_search`` (SAME cache key ``(name, "publ_search", query,
        limit)``, per-record ``_to_document``, ``rank_locally=False`` so DBLP's server order is kept
        verbatim, cache-only-if-docs); egress via the native-async ``_araw_fetch``; per-record
        mapping via the SAME pure-CPU ``_to_document`` — BEHAVIOR-IDENTICAL to ``search`` given
        identical egress."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    def _to_document(self, raw) -> Document:
        """One publ-search hit → Document (delegates to the verbatim mapper)."""
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

    async def _apubl_search(self, query: str, limit: int) -> Optional[dict]:
        """Async twin of ``_publ_search``: byte-faithful mirror (same endpoint / params / headers /
        timeout), egress swapped from the raw ``httpx.get`` for the shared async leaf
        ``http.aget_json``. Returns the parsed JSON dict, or ``None`` on any failure INSTEAD of
        raising (aget_json's failure→None contract) — the retry in ``_araw_fetch`` keys off that
        None where the sync retry keys off the exception. Routing through the leaf earns the shared
        async pool + SSRF guard + 30MB cap; NOTE it also honours cache_only (returns None with no
        live HTTP), so in cache-only mode the async path correctly does no network, a benign,
        intended divergence from the raw sync egress (which never had that guard)."""
        return await http.aget_json(
            f"{DBLP_BASE}/search/publ/api",
            params={"q": query, "format": "json", "h": min(limit, 50)},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=TIMEOUT,
        )

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
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
    def _publ_to_document(hit: dict) -> Document:
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

        return Document(
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
