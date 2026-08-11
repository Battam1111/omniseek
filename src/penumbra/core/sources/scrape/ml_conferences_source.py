"""NeurIPS / ICML / ICLR official blogs — awards, submission-policy changes, keynotes, newsletters.

Was three RSS rows in ``rss_bundles.json`` until 2026-07-25. The feeds did not die; they went behind
a bot wall. Measured, in order: an xml-shaped Accept (what the RSS fetcher sends) -> 415 for all
three; a browser-shaped Accept -> 200 but ~11.9KB of text/html with zero <item> and no <rss> root,
the same JS-interstitial shell mpnp serves. curl_cffi does not clear it either. So the source had
been dark for weeks while the content was still being published.

A real browser clears it: through CDP the blog index renders fully (blog.neurips.cc ~87KB, 10 post
cards) and the same ``article h2 a`` selector yields 10 posts on ALL THREE venues, each with a title
and a dated permalink (verified 2026-07-25). So the source is rebuilt on the CDP + declarative-HTML
path rather than retired: the coverage it holds (award announcements, the NeurIPS AI-generated-paper
policy, ICML registration caps, ICLR keynotes) is NOT what conference_deadlines carries, which is
deadline dates only. Retiring this would have silently dropped the announcements/policy layer.

Posture change, stated honestly: a CDP source is serialized, so this becomes ``explicit_only`` (named
drill) instead of riding the broad sweep. That is a smaller reach than the ORIGINAL feed row had, but
strictly larger than the dark source it replaces, and it is the only tier that reaches the content.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core.normalize import Document, schema_extract
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

# (venue, blog index url)
_VENUES = [
    ("NeurIPS", "https://blog.neurips.cc/"),
    ("ICML", "https://blog.icml.cc/"),
    ("ICLR", "https://blog.iclr.cc/"),
]

# All three run the same WordPress theme: each post card is an <article> whose <h2> holds the
# permalinked title. Verified live on all three venues (10 posts each).
_POST_SCHEMA = {"item_selector": "article h2 a",
                "fields": {"title": {}, "url": {"attr": "href"}}}

# WordPress permalinks carry the publication date: /2026/07/03/<slug>/
_DATE_IN_URL = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/")


def _date_from_url(url: str) -> Optional[datetime]:
    m = _DATE_IN_URL.search(url or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


class MlConferencesAdapter(BaseScrapeAdapter):
    name = "ml_conferences"
    description = (
        "NeurIPS / ICML / ICLR 官方博客 — 获奖公告 (Outstanding Papers / Test of Time / Awards)、"
        "投稿与评审政策变化 (如 NeurIPS 对 AI 生成论文的处理)、注册与容量公告、keynote 名单、"
        "newsletter. 与 conference_deadlines 互补: 那个只给截稿日期, 这个给公告与政策. "
        "站点已把 feed 挡在反爬后, 故经 CDP 真浏览器渲染抓取, 命名钻取 (penumbra_search 单源 raw)."
    )
    kind = "stream"
    domains = ["deadlines", "news"]
    modes = ["RECALL", "MONITOR"]
    explicit_only = "会议官方博客 (feed 被反爬挡住, 改经 CDP 真浏览器渲染, 串行较慢); 命名钻取"
    cache_ttl = 21600   # 6h: conference blogs post rarely, and every miss costs a CDP render
    rank = True         # BM25-filter the ~30 posts by the agent's query (venue / award / policy)

    def _raw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Render all three blog indexes through the shared CDP browser.

        Serial on purpose: CDP is a single shared browser, so a fan-out would only queue anyway.
        A venue that fails is skipped (never sinks the other two), matching the old bundle's
        per-feed degrade."""
        from penumbra.core.sources.walled._cdp import cdp_call

        def _nav(page):
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            try:  # the interstitial resolves itself into the blog; wait for a real post card
                page.wait_for_selector("article", timeout=15000)
            except Exception:  # noqa: BLE001 — no card: hand on what rendered, extraction yields []
                page.wait_for_timeout(5000)
            return page.content()

        out: list[tuple[str, str, str]] = []
        for venue, url in _VENUES:
            try:
                html = cdp_call(_nav, initial_url=url, timeout=75)
                if html:
                    out.append((venue, url, html))
            except Exception as exc:  # noqa: BLE001 — one venue down must not sink the rest
                logger.warning("%s: CDP fetch failed for %s: %s", self.name, venue, exc)
        return out or None

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        docs: list[Document] = []
        for venue, index_url, html in (raw or []):
            for row in schema_extract(html, _POST_SCHEMA):
                title = (row.get("title") or "").strip()
                url = (row.get("url") or "").strip()
                if not title or not url.startswith("http"):
                    continue
                date = _date_from_url(url)
                docs.append(Document(
                    source=self.name,
                    source_id=url,
                    url=url,
                    title=f"[{venue}] {title}",
                    content=f"{venue} 官方博客: {title}" + (f" ({date.date().isoformat()})" if date else ""),
                    author=venue,
                    date=date,
                    tags=["conference", venue.lower()],
                    metadata={"venue": venue, "index_url": index_url,
                              "date_str": date.date().isoformat() if date else None},
                ))
        # Newest first across venues (undated last), so a bare browse shows what just happened.
        docs.sort(key=lambda d: (d.date is not None, d.date or datetime.min), reverse=True)
        return docs
