"""微信公众号 (WeChat Official Accounts) adapter.

Two-layer architecture:
- **Layer A (this file's primary path)**: single-URL fetch via direct HTTP.
  `https://mp.weixin.qq.com/s/<id>` URLs are publicly accessible without
  login. Penumbra can fetch any specific article a user passes in.

- **Layer B (search/discovery via wewe-rss)**: planned as a separate path
  inside this same adapter. wewe-rss runs as a launchd service on the Mac;
  it uses an operator-supplied 微信读书 account to poll subscribed 公众号 and
  expose them as RSS. This adapter's `search()` reads those RSS feeds.
  See docs/wewe-rss-setup.md for setup (after Layer B is deployed).

For Penumbra use cases, Layer A covers the 80% case (user reads 微信 daily,
shares interesting article URLs). Layer B adds proactive monitoring.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_md

from penumbra.core import auth, cache
from penumbra.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

# A modern desktop browser UA — mp.weixin.qq.com sometimes returns mobile
# layout for "WeChat client" UA, so we explicitly identify as a desktop browser.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 25

# Layer B (search/discovery) — uses wechat2rss.xlab.app's free aggregator for
# the popular AI accounts. wewe-rss self-host is supported but optional (only
# needed for accounts wechat2rss doesn't index).
#
# wechat2rss is a community-run service that polls listed accounts and exposes
# RSS for free. Has covered all major AI/research accounts since 2023.
# (Source: https://wechat2rss.xlab.app/list/list)
DEFAULT_WECHAT2RSS_FEEDS = [
    # 高价值 AI/ML 学术号（wechat2rss 已收录，零维护）
    ("PaperWeekly", "https://wechat2rss.xlab.app/feed/3be891c2f4e526629ab055a297cc2cd6c1f0a563.xml"),
    ("机器之心", "https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml"),
    ("量子位", "https://wechat2rss.xlab.app/feed/7131b577c61365cb47e81000738c10d872685908.xml"),
    ("新智元", "https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml"),
    # AI寒武纪：wechat2rss 未收录，已通过 wewe-rss 自部署订阅（见 credentials/wechat.json）
]

# Optional: Layer B alternative (self-hosted wewe-rss). Only needed for accounts
# wechat2rss doesn't index (e.g., 学术志, 科研圈). See docs/wewe-rss-self-host.md.
auth.write_template(
    "wechat",
    {
        "_comment": "OPTIONAL. wechat2rss feeds for 3 default accounts are hardcoded. Add more accounts here either by adding to wechat2rss_extra_feeds (URLs from wechat2rss.xlab.app/list/list) OR by setting up wewe-rss locally.",
        "wechat2rss_extra_feeds": [
            # Example: ["学术写作公众号名", "https://wechat2rss.xlab.app/feed/<hash>.xml"]
        ],
        "wewerss_base_url": "",
        "wewerss_auth_code": "",
        "wewerss_subscribed_feed_ids": [],
    },
)


class WechatAdapter:
    name = "wechat"
    needs_credentials = False  # Layer A needs no creds; Layer B is optional
    explicit_only = "walled(微信公众号);命名 penumbra_fetch 才调,不进广搜"
    description = "微信公众号 — single-URL fetch (mp.weixin.qq.com/s/<id>); discovery via wewe-rss (Layer B)"

    # ─────────────────────────────────────────────────────────────────
    # Layer A: single-URL fetch (works immediately, no extra setup)
    # ─────────────────────────────────────────────────────────────────

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        # Accept mp.weixin.qq.com (canonical) and mp.weixin.qq.com.cn (rare)
        if "mp.weixin.qq.com" not in host:
            return None

        key = cache.make_key("wechat", "fetch", url)
        cached = cache.get(key)
        if cached is not None:
            return Document.model_validate(cached)

        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"},
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WeChat fetch failed for %s: %s", url, exc)
            return None

        # WeChat sometimes returns "环境异常" verification page
        html = resp.text
        if "环境异常" in html or "verify_msg" in html:
            logger.warning("WeChat returned anti-bot verification page for %s", url)
            return None

        doc = self._parse_article(url, html)
        if doc:
            cache.set(key, doc.model_dump(mode="json"), ttl=7 * 86400)
        return doc

    @staticmethod
    def _parse_article(url: str, html: str) -> Optional[Document]:
        soup = BeautifulSoup(html, "lxml")

        # ── Title ──
        # Modern WeChat articles use #activity-name or meta og:title
        title_el = soup.select_one("#activity-name, h1.rich_media_title, h2#activity-name")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            title = og.get("content", "").strip() if og else "(no title)"

        # ── Author (公众号 name + author name) ──
        # .rich_media_meta_text holds metadata; .rich_media_meta_link is author
        account_el = soup.select_one("#js_name, .rich_media_meta_link[href*='profile']")
        account = account_el.get_text(strip=True) if account_el else None

        author_el = soup.select_one("#meta_content .rich_media_meta_text, .rich_media_meta_nickname")
        author_name = author_el.get_text(strip=True) if author_el else None

        # Compose author string: prefer account; fall back to author_name
        author = account or author_name

        # ── Publish date ──
        date = None
        date_el = soup.select_one("#publish_time, em.rich_media_meta_text[id='publish_time']")
        if date_el:
            try:
                date = datetime.fromisoformat(date_el.get_text(strip=True).replace("/", "-"))
            except (ValueError, TypeError):
                pass
        if not date:
            # WeChat also embeds publish time in a JS variable; try meta
            pub_meta = soup.select_one('meta[property="article:published_time"]')
            if pub_meta and pub_meta.get("content"):
                try:
                    date = datetime.fromisoformat(pub_meta["content"].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

        # ── Body ──
        # Article body is in #js_content (the canonical container)
        body_el = soup.select_one("#js_content, .rich_media_content")
        if body_el is None:
            return None

        # Clean up: remove script/style/img placeholders
        for unwanted in body_el.select("script, style, .qr_code_pc_outer, .reward_area"):
            unwanted.decompose()

        # Replace lazy-load image src
        for img in body_el.select("img[data-src]"):
            real_src = img.get("data-src")
            if real_src:
                img["src"] = real_src

        try:
            body_md = html_to_md(str(body_el), heading_style="ATX").strip()
        except Exception:  # noqa: BLE001
            body_md = body_el.get_text("\n", strip=True)

        # ── Source ID from URL ──
        # URL: https://mp.weixin.qq.com/s/<id> or with __biz=...&mid=...&idx=...
        parsed = urlparse(url)
        source_id = parsed.path.split("/")[-1] or url

        return Document(
            source="wechat",
            source_id=source_id,
            url=url,
            title=title,
            content=body_md or "(no body extracted)",
            author=author,
            date=date,
            metadata={
                "account_name": account,
                "author_name": author_name,
                "raw": jsonsafe(str(body_el)),
            },
        )

    # ─────────────────────────────────────────────────────────────────
    # Layer B: discovery via wewe-rss (stub until setup is complete)
    # ─────────────────────────────────────────────────────────────────

    def _all_feeds(self) -> list[tuple[str, str]]:
        """Return [(account_name, feed_url), ...] from defaults + user config."""
        feeds = list(DEFAULT_WECHAT2RSS_FEEDS)
        creds = auth.load("wechat") or {}
        # User-added wechat2rss feeds
        for entry in creds.get("wechat2rss_extra_feeds") or []:
            if isinstance(entry, list) and len(entry) == 2:
                feeds.append((str(entry[0]), str(entry[1])))
        # wewe-rss self-hosted feeds (if configured)
        wewerss_base = creds.get("wewerss_base_url")
        for fid in creds.get("wewerss_subscribed_feed_ids") or []:
            if wewerss_base:
                feeds.append((f"wewerss:{fid}", f"{wewerss_base.rstrip('/')}/feeds/{fid}.atom"))
        return feeds

    def search(self, query: str, limit: int = 10) -> list[Document]:
        """Search across WeChat 公众号 RSS feeds.

        Sources are (in order): wechat2rss.xlab.app feeds for the popular
        AI accounts (hardcoded defaults), plus any user-added feeds in
        ~/.penumbra/credentials/wechat.json (wechat2rss extras OR wewe-rss
        self-hosted).
        """
        key = cache.make_key("wechat", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        import feedparser
        from penumbra.core.sources.scrape._rss import entry_to_document, _DictAsObj

        query_terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 1]

        all_entries: list[tuple[str, str, dict]] = []
        for account_name, feed_url in self._all_feeds():
            try:
                resp = httpx.get(
                    feed_url,
                    timeout=20,
                    headers={
                        "User-Agent": DEFAULT_UA,
                        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                    },
                    follow_redirects=True,
                )
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                # For wewe-rss feeds, the placeholder account_name is "wewerss:<id>".
                # The real 公众号 name is in <feed><title>...</title></feed>.
                # Pull it so docs display "AI寒武纪" instead of "wewerss:MP_WXS_xxx".
                if account_name.startswith("wewerss:"):
                    real_title = getattr(parsed.feed, "title", "") if parsed.feed else ""
                    if real_title:
                        account_name = real_title
                for e in parsed.entries:
                    all_entries.append((account_name, feed_url, dict(e)))
            except Exception as exc:  # noqa: BLE001
                logger.debug("WeChat feed %s (%s) failed: %s", account_name, feed_url, exc)

        # Rank by query-term occurrence in title + summary
        scored: list[tuple[int, str, str, dict]] = []
        for account_name, feed_url, entry in all_entries:
            text = ((entry.get("title") or "") + " " + (entry.get("summary") or "")).lower()
            score = sum(text.count(t) for t in query_terms) if query_terms else 1
            if query_terms and score == 0:
                continue
            scored.append((score, account_name, feed_url, entry))
        scored.sort(key=lambda x: x[0], reverse=True)

        docs: list[Document] = []
        for score, account_name, feed_url, entry in scored[:limit]:
            doc = entry_to_document(_DictAsObj(entry), "wechat", feed_url)
            if doc:
                # Override author with the account name (more useful than RSS author field)
                doc.author = account_name
                docs.append(doc)

        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=900)
        return docs

    def health_check(self) -> tuple[bool, str]:
        # Layer A is always healthy if the network is up — probe a known URL pattern
        # by HEAD-fetching the WeChat homepage
        try:
            resp = httpx.get("https://mp.weixin.qq.com/", headers={"User-Agent": DEFAULT_UA}, timeout=8)
            layer_a = resp.status_code in (200, 302)
        except Exception:  # noqa: BLE001
            layer_a = False

        # Layer B = the wechat2rss 公众号 feeds (4 hardcoded defaults incl 新智元, +
        # any user-added wechat2rss/wewe-rss). It's ALWAYS active (no config needed) —
        # probe the first feed for liveness. (Earlier this only checked the OPTIONAL
        # self-hosted wewe-rss, so it wrongly reported "Layer B not configured".)
        feeds = self._all_feeds()
        layer_b_ok = False
        if feeds:
            try:
                r = httpx.get(feeds[0][1], headers={"User-Agent": DEFAULT_UA}, timeout=8,
                              follow_redirects=True)
                layer_b_ok = r.status_code == 200
            except Exception:  # noqa: BLE001
                layer_b_ok = False

        if layer_a and layer_b_ok:
            return True, f"OK (Layer A + Layer B: {len(feeds)} 公众号 feeds via wechat2rss)"
        if layer_a:
            return True, f"OK (Layer A; Layer B {len(feeds)} feeds unreachable)"
        return False, "WeChat unreachable"


from penumbra.core.fetcher import register_adapter

register_adapter(WechatAdapter())
