"""Cross Validated — statistics / ML / data-analysis Q&A via the Stack Exchange API.

stats.stackexchange.com (a.k.a. "Cross Validated") is the famous, peer-curated Q&A site for
statistics, machine learning, data analysis, data mining, and data visualization — the canonical
English destination for methodology questions (bias/variance, model selection, hypothesis tests,
the why behind an estimator), where the votes-ranked accepted answer is often a small essay by a
named statistician.

Same keyless Stack Exchange v2.3 public API as the other SE sources (site=stats), sharing the
10000/day-per-IP quota and the shared answer-fetching machinery in ``_stackexchange.py``. Each
returned question ships its own doc PLUS its top-3 votes-ranked answer docs (the gold).
"""

from __future__ import annotations

from typing import Any, Optional

from penumbra.core import _stackexchange
from penumbra.core.normalize import PolarisDocument
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

SITE = "stats"
SITE_HOST = "stats.stackexchange.com"


class CrossValidatedAdapter(BaseScrapeAdapter):
    name = "crossvalidated"
    needs_credentials = False
    description = (
        "Cross Validated (stats.stackexchange) — statistics/ML/data-analysis Q&A; "
        "the canonical English methodology site (model selection, bias-variance, estimators, tests)"
    )
    cache_ttl = 900
    kind = "lookup"
    domains = ["community", "methodology"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return _stackexchange.search(query, limit, SITE)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        return _stackexchange.build_documents(raw, limit, self.name, SITE, SITE_HOST)

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        return _stackexchange.fetch_question_document(url, self.name, SITE, SITE_HOST)

    def health_check(self) -> tuple[bool, str]:
        # Shared single-flight probe — all SE sources share the keyless per-IP quota.
        return _stackexchange.health()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
