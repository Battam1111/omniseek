"""Stack Overflow — programming Q&A (parallel to academia_se via SE API).

Stack Overflow is the canonical programming Q&A site. For ML/AI PhDs it's
the primary destination for: pytorch/JAX/numpy bug-fixes, GPU/CUDA issues,
data preprocessing patterns, training/eval implementation questions.

Uses the same Stack Exchange v2.3 public API as academia_se, just with
site=stackoverflow. All SE adapters share the URL pattern and quota
(10000/day unauth), so heavy users may want to pace queries.

Now a thin subclass over the shared SE machinery in ``_stackexchange.py``: the cache-checked
``search`` skeleton + auto-registration come from BaseScrapeAdapter; the SE-API I/O + the
question→doc / answer→doc map live in ``_stackexchange`` (shared by all six SE sources). This
adapter just declares its site/facets and wires the hooks.

Each returned question yields its own doc PLUS its top-3 votes-ranked answer docs (source_id
"{qid}a{aid}", title "A: <title>", metadata.is_accepted) — the accepted answer is the actual gold,
which the old question-body-only mapping silently dropped. rank stays default-False so the
endpoint's own ``sort=relevance`` order is preserved.
"""

from __future__ import annotations

from typing import Any, Optional

from omniseek.core import _stackexchange
from omniseek.core.normalize import Document
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

SITE = "stackoverflow"
SITE_HOST = "stackoverflow.com"


class StackOverflowAdapter(BaseScrapeAdapter):
    name = "stackoverflow"
    needs_credentials = False
    description = (
        "Stack Overflow — programming Q&A (parallel to academia_se; "
        "primary destination for pytorch/CUDA/JAX/data-preprocessing implementation issues)"
    )
    cache_ttl = 900
    kind = "lookup"
    domains = ["community", "code"]
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
        # Shared single-flight probe (see _stackexchange.health): all SE sources share the
        # api.stackexchange.com keyless per-IP quota, so they delegate to ONE 60s-cached probe
        # instead of each firing its own live /questions GET on every health sweep.
        return _stackexchange.health()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
