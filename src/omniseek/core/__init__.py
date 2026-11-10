"""OmniSeek — multi-source information retrieval.

OmniSeek gives an AI agent the ability to query 25 curated information sources
(academic APIs, social platforms, blogs, forums) through a unified
interface. It is one capability of OmniSeek, not the whole of OmniSeek.
"""

from omniseek.core.fetcher import afetch_one, aprobe_health, asearch_many, list_sources

__all__ = ["afetch_one", "aprobe_health", "asearch_many", "list_sources"]
