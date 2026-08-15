"""Data Science Stack Exchange — applied ML / data-science Q&A via the Stack Exchange API.

datascience.stackexchange.com is the peer-curated Q&A site for APPLIED machine learning and data
science: model architecture choices, feature engineering, training/evaluation gotchas, handling
imbalanced data, NLP/CV pipelines, the practical "how do I make this model work" questions. Sits
between Stack Overflow (pure programming) and Cross Validated (statistical theory).

Same keyless Stack Exchange v2.3 public API as the other SE sources (site=datascience), sharing the
10000/day-per-IP quota and the shared answer-fetching machinery in ``_stackexchange.py``. Each
returned question ships its own doc PLUS its top-3 votes-ranked answer docs (the gold).
"""

from __future__ import annotations

from typing import Any, Optional

from omniseek.core import _stackexchange
from omniseek.core.normalize import Document
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

SITE = "datascience"
SITE_HOST = "datascience.stackexchange.com"


class DataScienceSEAdapter(BaseScrapeAdapter):
    name = "datascience_se"
    needs_credentials = False
    description = (
        "Data Science Stack Exchange — applied ML/data-science Q&A (architecture choices, "
        "feature engineering, training/eval gotchas, imbalanced data, NLP/CV pipelines)"
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
