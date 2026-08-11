"""LMArena (ex-LMSys) blog — Chatbot Arena Elo methodology + LLM eval deep-dives.

Was an RSS row in ``rss_bundles.json`` until 2026-07-25, when the site was found to publish no
feed at all any more: ``arena.ai/blog/rss/`` (and every candidate: lmarena.ai/blog/rss/,
lmarena.ai/rss.xml, blog.lmarena.ai/rss/, news.lmarena.ai/rss/, lmsys.org/rss.xml) answers with the
SPA's HTML shell, zero ``<item>``, and the blog page declares no ``<link rel=alternate>`` feed
anywhere. The RSS adapter was correctly refusing those bodies as "not a feed", so the source had
been dark for weeks.

The CONTENT is still perfectly reachable: the blog is server-rendered, with 13 post cards present
in the static HTML (verified 2026-07-25). So the source moves from the RSS table to the declarative
HTML path (``extract_schema`` + ``fetch_html``): CSS selectors as DATA, no hand-written parsing and
no code execution. The wrapping ``<a>`` carries the permalink while the inner ``<article>`` carries
the title and the summary, hence the ``a:has(article)`` item selector.

No live capability is lost in the move: the old row's ``url_pattern`` fetch_url claim resolved
against cached FEED documents, and the feed had stopped producing any.
"""

from __future__ import annotations

from penumbra.core.sources.scrape._base import BaseScrapeAdapter


class LmsysArenaAdapter(BaseScrapeAdapter):
    name = "lmsys_arena"
    description = (
        "LMArena (原 LMSys) 官方博客 — Chatbot Arena Elo 方法论 + LLM 评测深度文章 + red-teaming "
        "报告 (LLM 评测方法论的第一手来源). 站点已不再提供 RSS, 故直接抓其服务端渲染的博客列表."
    )
    kind = "stream"
    domains = ["eval"]
    modes = ["RECALL", "MONITOR"]

    search_url = "https://lmarena.ai/blog"   # no {query} placeholder: fetch the index, then BM25 it
    base_url = "https://lmarena.ai"          # post hrefs are root-relative (/blog/<slug>)
    fetch_html = True
    cache_ttl = 7200                         # 2h, as the retired RSS row used
    rank = True                              # the index is small, so filter it by the agent's query

    # Verified live 2026-07-25: 13/13 cards yield BOTH a permalink and a title.
    extract_schema = {
        "item_selector": "a:has(article)",   # the <a> wraps the card, so href lives on the item
        "fields": {
            "url": {"attr": "href"},
            "title": {"selector": "h3"},
            "content": {"selector": "p"},
        },
    }
