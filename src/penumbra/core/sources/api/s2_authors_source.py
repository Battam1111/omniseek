"""Semantic Scholar Authors — researcher profiles + citation metrics via the keyless Graph API.

Resolves a researcher NAME to disambiguated author entities with the citation metrics ORCID
lacks (h-index / citation count / paper count). Penumbra's people-STRUCTURE reinforcement:
the people domain previously rested on ORCID alone (a keyless single point flagged
coverage_critical); this adds a SECOND, different-backend researcher-identity source, and pairs
with penumbra_resolve_identity (OpenAlex) for cross-backend author disambiguation.

Access via the public Graph API (no key needed; a free key only raises the rate limit):
  GET https://api.semanticscholar.org/graph/v1/author/search?query=<name>&fields=name,hIndex,paperCount,citationCount
  -> {"total": N, "offset": 0, "data": [{authorId, name, hIndex, paperCount, citationCount}, ...]}
The author page URL is CONSTRUCTED from authorId (no url field), so this is a thin coded adapter.
``affiliations`` is empty in the search projection (S2 populates it on /author/{id} detail), so we
ship the metrics-and-disambiguation layer; the agent can penumbra_read the author page for more.

backend="semantic_scholar": shares the S2 graph with the existing `semantic_scholar` paper source
(honest backend count: it is the same upstream, a different facet). explicit_only: a named
researcher drill, and the shared keyless pool is rate-limited (429 under load) so it must not ride
the broad fan-out. cache_ttl is long for the same reason.

Recon trail: brain note eye-free-api-probe-round2-2026-06-21 (live-probed keyless 200).
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/author/search"
AUTHOR_URL = "https://www.semanticscholar.org/author/{id}"
_FIELDS = "name,hIndex,paperCount,citationCount"


class S2AuthorsAdapter(BaseScrapeAdapter):
    name = "s2_authors"
    backend = "semantic_scholar"  # same S2 graph as the `semantic_scholar` paper source, different facet
    needs_credentials = False
    description = ("Semantic Scholar authors — resolve a researcher by NAME to citation metrics "
                   "(h-index / citation count / paper count) + disambiguated candidate entities; "
                   "name a researcher to rank who's who. STRUCTURE, keyless, people-lookup. Pairs "
                   "with orcid (self-asserted CV) and penumbra_resolve_identity (OpenAlex).")
    cache_ttl = 86400  # 24h: profiles change slowly + the shared keyless pool is rate-limited
    kind = "lookup"
    domains = ["people"]
    modes = ["STRUCTURE"]
    explicit_only = ("s2_authors: a named researcher drill (resolve a person + citation metrics); "
                     "not broad-fan-out fodder (the keyless S2 pool is rate-limited)")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return http.get_json(
            SEARCH_URL,
            params={"query": query, "fields": _FIELDS, "limit": max(1, min(int(limit), 20))},
            timeout=15,
        )

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: same URL, same params, same timeout; ONLY the shared-http
        egress swaps http.get_json -> await http.aget_json. Everything else verbatim."""
        return await http.aget_json(
            SEARCH_URL,
            params={"query": query, "fields": _FIELDS, "limit": max(1, min(int(limit), 20))},
            timeout=15,
        )

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BaseScrapeAdapter.search -> AsyncSearchCapable. Shares the base
        async cache round-trip; egress via _araw_fetch; mapping via the SAME pure-CPU
        _to_documents (byte-identical to search)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        data = raw.get("data") or []
        docs: list[Document] = []
        for author in data[:limit]:
            doc = self._author_to_doc(author)
            if doc is not None:
                docs.append(doc)
        return docs

    def _author_to_doc(self, author: Any) -> Optional[Document]:
        if not isinstance(author, dict):
            return None
        aid = author.get("authorId")
        name = author.get("name")
        if not aid or not name:
            return None  # no stable id / name -> no doc
        cites = author.get("citationCount")
        h = author.get("hIndex")
        papers = author.get("paperCount")
        bits = []
        if h is not None:
            bits.append(f"h-index {h}")
        if cites is not None:
            bits.append(f"{cites} citations")
        if papers is not None:
            bits.append(f"{papers} papers")
        content = name + ((" — " + ", ".join(bits)) if bits else "")
        return Document(
            source=self.name,
            source_id=str(aid),
            url=AUTHOR_URL.format(id=aid),
            title=name,
            content=content,
            author=name,
            date=None,
            signals=self._cite_signal(cites),
            tags=[],
            metadata={
                "h_index": h,
                "paper_count": papers,
                "citation_count": cites,
                "raw": jsonsafe(author),
            },
        )

    @staticmethod
    def _cite_signal(cites: Any) -> dict:
        """Citation count -> a citation-class signal (the researcher's impact scale).
        Absent/non-numeric -> {}."""
        if isinstance(cites, (int, float)) and not isinstance(cites, bool):
            return mk_signal("citations", cites, kind="citation", by="s2_authors/citationCount")
        return {}

    def health_check(self) -> tuple[bool, str]:
        """Liveness via a tiny author search. The shared keyless S2 pool 429s under load; a 429
        PROVES the endpoint is alive (it answered), so report healthy-but-throttled rather than
        down. http.get_json raise_for_status-es a 429 down to None (losing the status), so probe
        with a direct httpx call here that does NOT raise, and read the status itself."""
        try:
            r = httpx.get(SEARCH_URL, params={"query": "Bengio", "fields": "name", "limit": 1},
                          headers={"User-Agent": http.USER_AGENT}, timeout=15, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if r.status_code == 429:
            return True, "OK (S2 keyless pool throttled 429; endpoint alive)"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        try:
            return ("data" in r.json()), "OK"
        except Exception:  # noqa: BLE001
            return False, "no envelope"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
