"""Source adapters for the OmniSeek eye.

Each adapter implements the SourceAdapter protocol defined in fetcher.py.
Adapters are organized by access mechanism:
- api/: official APIs (Reddit, Semantic Scholar, arXiv, Bluesky, OpenReview)
- scrape/: web scraping (Zhihu, Bilibili, LessWrong, etc.)
- walled/: walled gardens needing special handling (Xiaohongshu, WeChat)
"""
