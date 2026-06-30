"""Penumbra — multi-source information retrieval.

Penumbra gives Penumbra the ability to query 25 curated information sources
(academic APIs, social platforms, blogs, forums) through a unified
interface. It is one capability of Penumbra, not the whole of Penumbra.
"""

from penumbra.core.fetcher import fetch_one, search_many, list_sources, health_check

__all__ = ["fetch_one", "search_many", "list_sources", "health_check"]
