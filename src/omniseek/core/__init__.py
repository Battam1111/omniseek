"""OmniSeek Eye — multi-source information retrieval.

OmniSeek gives OmniSeek the ability to query 25 curated information sources
(academic APIs, social platforms, blogs, forums) through a unified
interface. It is one capability of OmniSeek, not the whole of OmniSeek.
"""

from omniseek.core.fetcher import fetch_one, search_many, list_sources, health_check

__all__ = ["fetch_one", "search_many", "list_sources", "health_check"]
