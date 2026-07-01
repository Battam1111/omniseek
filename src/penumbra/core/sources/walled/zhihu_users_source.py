"""知乎 user-column follower — track specific researchers' posts via CDP.

Distinct from `zhihu` adapter (which does Zhihu-wide keyword search). This
adapter proactively pulls each followed researcher's column / 想法 / 文章
posts, then filters by query. Use case: track senior Chinese ML/NLP
researchers who publish on Zhihu rather than English venues.

Initially follows 张俊林 (新浪微博 AI 首席科学家, an S-tier Chinese-world
senior). The deployer extends the list via `~/.polaris/credentials/zhihu_users.json`:

    {
      "users": [
        {"handle": "zhang-jun-lin-76", "display_name": "张俊林"},
        {"handle": "another-handle", "display_name": "另一研究者"}
      ]
    }

Reuses existing CDP infrastructure (`cdp_page`) — the operator logs into 知乎
once via VNC, then this adapter drives that browser to fetch user pages.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from penumbra.core import auth, cache, relevance
from penumbra.core.normalize import PolarisDocument, jsonsafe
from penumbra.core.sources.walled._cdp import cdp_call, cdp_health

logger = logging.getLogger(__name__)

# Default followed users (the deployer can override via credential file)
DEFAULT_USERS = [
    {
        "handle": "zhang-jun-lin-76",
        "display_name": "张俊林",
        "bio": "新浪微博 AI 首席科学家 / 中科院软件所 PhD / NLP+搜索方向",
    },
]

# Drop credential template
auth.write_template(
    "zhihu_users",
    {
        "_comment": "Customize the list of Zhihu users to track. Default tracks 张俊林.",
        "users": [
            {"handle": "zhang-jun-lin-76", "display_name": "张俊林"},
        ],
    },
)


# A tracked researcher's recent posts don't depend on the search query, so cache them
# query-INDEPENDENTLY per handle: the expensive CDP nav is paid once per handle per TTL and
# reused by every query + the prewarmer (the watchlist generalization of the RSS "convert once"
# pattern; the old code re-navved on EVERY different query). POSTS_PER_HANDLE caps recent posts.
POSTS_PER_HANDLE = 25
POSTS_TTL = 3600  # 1h: a researcher posts a few times/day at most; the prewarmer keeps it hot


class ZhihuUsersAdapter:
    name = "zhihu_users"
    needs_credentials = False  # CDP login is one-time; this just configures user list
    explicit_only = "shared CDP Chrome (precious logged-in session)"
    description = (
        "知乎 followed researchers — 张俊林 等 senior 中文 NLP/ML 作者跟踪 "
        "(via CDP, configurable via ~/.polaris/credentials/zhihu_users.json)"
    )

    def _load_users(self) -> list[dict]:
        creds = auth.load("zhihu_users")
        if creds and isinstance(creds.get("users"), list):
            return creds["users"]
        return DEFAULT_USERS

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        users = self._load_users()
        if not users:
            return []

        # Query-keyed result cache (a cheap second layer over the per-handle post cache).
        key = cache.make_key("zhihu_users", "search", query, limit, len(users))
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        # Each user's posts are fetched + cached query-INDEPENDENTLY (see _fetch_user_posts), so
        # the expensive CDP nav is paid once per handle per TTL and EVERY query (+ the prewarmer)
        # reuses it — a different-query call is now an in-memory filter, not a re-nav.
        all_docs: list[PolarisDocument] = []
        for user in users:
            handle = user.get("handle")
            display_name = user.get("display_name", handle)
            if not handle:
                continue
            try:
                posts = self._fetch_user_posts(handle, display_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("zhihu_users fetch failed for %s: %s", handle, exc)
                continue
            all_docs.extend(posts)

        # Filter by query via the ONE shared scorer (CJK bigrams + ASCII boundaries): the old
        # re.findall(r"\w+") collapsed a CJK query into a single mega-token and then required
        # that whole run as one contiguous substring, so any multi-concept CJK query matched
        # nothing. filter_rank returns matches only (term-less query -> all posts, unchanged).
        all_docs = relevance.filter_rank(all_docs, query)

        # Sort by date desc (None last)
        def sort_key(d: PolarisDocument):
            return d.date or datetime.min
        all_docs.sort(key=sort_key, reverse=True)
        all_docs = all_docs[:limit]

        cache.set_docs(key, all_docs, ttl=1800)
        return all_docs

    def _fetch_user_posts(self, handle: str, display_name: str) -> list[PolarisDocument]:
        """The expensive CDP nav, cached query-INDEPENDENTLY per handle (a researcher's recent
        posts don't depend on the search query). Paid once per handle per POSTS_TTL and reused by
        every query + the prewarmer — instead of re-navving on each new query (the old behavior,
        where this fetch lived inside the query-keyed search and so re-ran per distinct query)."""
        pkey = cache.make_key("zhihu_users", "posts", handle)
        pcached = cache.get_docs(pkey)
        if pcached is not None:
            return pcached

        url = f"https://www.zhihu.com/people/{handle}/posts"

        def navigate_and_extract(page) -> str:
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            # SPA hydration — wait for the stable title hook. The list/card container
            # class names drift (2026-06: .List-item/.ContentItem no longer match), but
            # the article title lives in a stable `h2.ContentItem-title`.
            page.wait_for_timeout(2500)
            try:
                page.wait_for_selector("h2.ContentItem-title, .ProfileMain", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            # Zhihu lazy-loads on scroll — ONE nudge surfaces ~1 item; a few surface
            # the page (verified: 3 scrolls → 21 articles vs 1 without).
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 3000)")
                page.wait_for_timeout(1000)
            return page.content()

        try:
            html = cdp_call(navigate_and_extract, initial_url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CDP fetch failed for %s: %s", handle, exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        # Stable extraction: each article's title anchor (→ zhuanlan/p/<id>). Fall back
        # to any in-content /p/ link if the title class ever changes again.
        anchors = soup.select("h2.ContentItem-title a[href]")
        if not anchors:
            anchors = soup.select("a[href*='zhuanlan.zhihu.com/p/'], a[href*='/p/']")

        docs: list[PolarisDocument] = []
        seen_urls: set[str] = set()
        for a in anchors:
            try:
                doc = self._anchor_to_document(a, handle, display_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping zhihu user item: %s", exc)
                continue
            if doc and doc.url not in seen_urls:
                seen_urls.add(doc.url)
                docs.append(doc)
                if len(docs) >= POSTS_PER_HANDLE:
                    break
        cache.set_docs(pkey, docs, ttl=POSTS_TTL)
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        host = (urlparse(url).hostname or "").lower()
        if "zhihu.com" not in host:
            return None
        path = urlparse(url).path.strip("/")
        # zhihu.com/people/<handle>/posts → list page handled by search()
        # zhihu.com/p/<id> or zhuanlan.zhihu.com/p/<id> → individual post; defer to zhihu adapter
        if path.startswith("people/") and path.endswith("/posts"):
            handle = path.split("/")[1]
            posts = self._fetch_user_posts(handle, handle)
            if posts:
                return posts[0]
        return None

    def health_check(self) -> tuple[bool, str]:
        cdp_ok, cdp_msg = cdp_health()
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"
        users = self._load_users()
        if not users:
            return False, "no users configured"
        # Try fetching the first user
        first = users[0]
        try:
            posts = self._fetch_user_posts(
                first["handle"], first.get("display_name", first["handle"])
            )
            if not posts:
                return False, f"fetched 0 posts for {first.get('display_name', '?')} — login expired?"
            return True, f"OK ({len(users)} users configured; first probe returned {len(posts)} posts)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def _anchor_to_document(self, a, handle: str, display_name: str) -> Optional[PolarisDocument]:
        title = a.get_text(strip=True)
        href = a.get("href") or ""
        if not title or not href:
            return None
        if href.startswith("//"):
            url = "https:" + href
        elif href.startswith("/"):
            url = "https://www.zhihu.com" + href
        else:
            url = href

        # Excerpt + date best-effort from the enclosing card. Class names drift, so
        # match loosely and degrade gracefully — title + url are the guaranteed fields.
        card = a.find_parent(class_=re.compile(r"ContentItem|Card|List-item")) or a.parent
        excerpt = ""
        date = None
        if card is not None:
            ex = card.select_one(".RichText, .RichContent-inner, .Excerpt, [class*='excerpt']")
            if ex:
                excerpt = ex.get_text("\n", strip=True)
            time_el = card.select_one("time, [data-tooltip]")
            if time_el:
                tip = time_el.get("data-tooltip") or time_el.get_text(strip=True)
                m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", tip or "")
                if m:
                    try:
                        date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    except ValueError:
                        date = None

        m = re.search(r"/p/(\d+)", url)
        source_id = m.group(1) if m else url

        return PolarisDocument(
            source="zhihu_users",
            source_id=str(source_id),
            url=url,
            title=title,
            content=excerpt or "(click URL for full content)",
            author=display_name,
            date=date,
            tags=[f"zhihu-user:{handle}"],
            metadata={
                "user_handle": handle,
                "user_display_name": display_name,
                "raw": jsonsafe(str(card if card is not None else a)),
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(ZhihuUsersAdapter())
