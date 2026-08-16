"""Shared helpers for OmniSeek's bulk-file FUNDING sources (the NSERC pattern, reused by SSHRC/CIHR).

A funding council that publishes its awards ONLY as a bulk per-fiscal-year file (CSV/XLSX, no query
API) gets this treatment: fetch the (static, annual) file at most monthly, keep ONLY the AI/ML/NLP
telos slice, cache the subset docs query-independent, and BM25-filter per query (zero network). A
non-AI grant is intentionally out of scope — a CS/AI researcher's lens, not the whole council.

This module name starts with '_' and does NOT end in '_source', so the omniseek.server walk skips it;
a ``*_source.py`` subclass imports it (the same convention as ``api/_base.py``).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from omniseek.core import cache
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")
CACHE_TTL = 2592000  # 30 days: an annual static snapshot → refresh ~monthly (catches a new FY)

# AI/ML/NLP relevance terms — the telos slice across councils (SSHRC humanities comp-ling, CIHR
# health-AI). Matched against a grant's title / abstract / keywords / discipline / category. (NSERC
# keeps its own sciences-tuned list in nserc_awards_source; this one is broadened for humanities +
# health, hence 'computational social' / 'digital humanities'.)
AI_TERMS = (
    "natural language", "machine learning", "deep learning", "neural network",
    "artificial intelligence", "computational lingu", "language model",
    "computer vision", "speech recognition", "reinforcement learning",
    "information retrieval", "data mining", "text mining", "language technolog",
    "large language model", " nlp ", "computational social", "digital humanities",
)


def is_ai_relevant(*texts: Optional[str]) -> bool:
    """Does any of the given text fields mention an AI/ML/NLP term (the telos slice)?"""
    blob = " " + " ".join(t or "" for t in texts).lower() + " "
    return any(t in blob for t in AI_TERMS)


def year_of(name: Optional[str]) -> int:
    """First 4-digit year in a resource name (for picking the latest fiscal-year file). 0 if none."""
    m = re.search(r"(\d{4})", name or "")
    return int(m.group(1)) if m else 0


class BulkFundingBase:
    """Cache + search over a query-independent AI/ML/NLP doc subset built from a bulk funding file.

    Subclass: set name / description / explicit_only / facets + ``_version`` (a cache-key part; bump
    when targeting a new fiscal year) and implement ``_build_subset_docs() -> list[Document]``
    (fetch the bulk file, keep is_ai_relevant rows, build docs). The base owns the 30-day subset cache
    + the per-query BM25 filter + the fetch_url no-op. Registration stays a module-tail
    ``register_adapter(...)`` in the subclass file (this is a duck-typed Protocol adapter, not a
    BaseAPIAdapter — there is no __init_subclass__ auto-register here)."""

    needs_credentials = False
    kind = "lookup"
    cache_ttl = CACHE_TTL
    _version = ""

    def _build_subset_docs(self) -> list[Document]:
        raise NotImplementedError("subclass must implement _build_subset_docs")

    def _subset_docs(self) -> list[Document]:
        key = cache.make_key(self.name, "subset", self._version)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        docs = self._build_subset_docs()
        if docs:
            cache.set_docs(key, docs, ttl=self.cache_ttl)
        return docs

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = self._subset_docs()
        if not docs:
            return []
        q = (query or "").strip()
        return docs[:limit] if not q else keyword_score_filter(docs, q)[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None
