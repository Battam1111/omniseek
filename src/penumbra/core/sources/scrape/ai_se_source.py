"""Artificial Intelligence Stack Exchange — AI concepts Q&A via the Stack Exchange API.

ai.stackexchange.com is the peer-curated Q&A site for ARTIFICIAL INTELLIGENCE concepts: neural
network architectures, reinforcement learning, search/planning, the theory and intuition behind AI
techniques (why attention works, what a policy gradient is, how a transformer differs from an RNN).
Conceptual and explanatory, distinct from datascience's applied-pipeline focus.

Same keyless Stack Exchange v2.3 public API as the other SE sources (site=ai), sharing the
10000/day-per-IP quota and the shared answer-fetching machinery in ``_stackexchange.py``. Each
returned question ships its own doc PLUS its top-3 votes-ranked answer docs (the gold).
"""

from __future__ import annotations

from typing import Any, Optional

from penumbra.core import _stackexchange
from penumbra.core.normalize import Document
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

SITE = "ai"
SITE_HOST = "ai.stackexchange.com"


class AIStackExchangeAdapter(BaseScrapeAdapter):
    name = "ai_se"
    needs_credentials = False
    description = (
        "Artificial Intelligence Stack Exchange — AI-concepts Q&A (NN architectures, RL, "
        "search/planning, the theory + intuition behind AI techniques); conceptual, not applied pipelines"
    )
    cache_ttl = 900
    kind = "lookup"
    domains = ["community", "methodology"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return _stackexchange.search(query, limit, SITE)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        return _stackexchange.build_documents(raw, limit, self.name, SITE, SITE_HOST)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BaseScrapeAdapter.search -> AsyncSearchCapable (the S4a fan-out awaits this
        directly; the SE-API search + per-question answer GETs cost COROUTINES, not held pool threads).
        Shares the base async cache round-trip; egress via `_stackexchange` async twins (same breaker/cap
        as sync). BEHAVIOR-IDENTICAL to `search`: same shared mappers, same cache key."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: _stackexchange.asearch(query, limit, SITE),
            abuild=lambda raw: _stackexchange.abuild_documents(raw, limit, self.name, SITE, SITE_HOST))

    def fetch_url(self, url: str) -> Optional[Document]:
        return _stackexchange.fetch_question_document(url, self.name, SITE, SITE_HOST)

    def health_check(self) -> tuple[bool, str]:
        # Shared single-flight probe — all SE sources share the keyless per-IP quota.
        return _stackexchange.health()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
