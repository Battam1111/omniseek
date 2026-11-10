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

from omniseek.core import _stackexchange
from omniseek.core.normalize import Document
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

SITE = "ai"
SITE_HOST = "ai.stackexchange.com"


class AIStackExchangeAdapter(BaseScrapeAdapter):
    name = "ai_se"
    needs_credentials = False
    description = (
        "Artificial Intelligence Stack Exchange: AI-concepts Q&A (NN architectures, RL, "
        "search/planning, the theory + intuition behind AI techniques); conceptual, not applied pipelines"
    )
    cache_ttl = 900
    kind = "lookup"
    domains = ["community", "methodology"]
    modes = ["STRUCTURE"]

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return await _stackexchange.asearch(query, limit, SITE)

    async def _ato_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        # the SE-API search + per-question answer GETs cost coroutines, not held pool threads
        return await _stackexchange.abuild_documents(raw, limit, self.name, SITE, SITE_HOST)

    async def afetch_url(self, url: str) -> Optional[Document]:
        return await _stackexchange.afetch_question_document(url, self.name, SITE, SITE_HOST)

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # Shared single-flight probe — all SE sources share the keyless per-IP quota.
        return await _stackexchange.ahealth()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
