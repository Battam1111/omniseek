"""知乎 adapter — Playwright via CDP to the persistent logged-in Chrome.

Why CDP and not direct HTTP scraping:
- 知乎 returns 403 on unauthenticated direct fetch
- API endpoints are signed (x-zse-93 / x-zse-96 headers) — reverse-engineering is fragile
- The persistent Chrome already has a logged-in session (the operator logs in once via VNC)
- CDP lets Penumbra drive that browser to perform searches as a real user

Search flow:
1. Connect to CDP Chrome (opens a new tab in the existing browser)
2. Navigate to https://www.zhihu.com/search?q=<query>&type=content
3. Wait for results to load
4. Parse the rendered HTML — 知乎's results are server-rendered + hydrated, but
   the SearchResult cards are stable selectors
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.walled._base import EMPTY_TTL
from penumbra.core.sources.walled._cdp import cdp_call, cdp_health, content_with_media, images_from_page

logger = logging.getLogger(__name__)


class ZhihuAdapter:
    name = "zhihu"
    needs_credentials = False  # Login happens once via VNC; we just use the session
    explicit_only = "shared CDP Chrome (precious logged-in session)"
    description = "知乎 — long-form PhD methodology discussions (via CDP Chrome session)"
    # fetch_url reads an answer/article page through the SHARED 9222 CDP pool; under the fetcher's
    # 30s default cap the adapter gets abandoned mid-flight (URL falls to the generic web fallback,
    # which hits 知乎's 安全验证 wall) while the orphaned cdp_call occupies the serial pool worker
    # up to its own 90s — the same wedge-under-load class as yipinsanfendi (observed 2026-07-08).
    # Budget must CONTAIN cdp_call's 90s default so CDP cleans up before the fetcher bound fires.
    fetch_timeout = 100.0

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key("zhihu", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        url = f"https://www.zhihu.com/search?q={quote(query)}&type=content"

        def _navigate(page) -> str:
            # Wait for an actual hydrated result TITLE, not the card shell or the .SearchMain
            # container. zhihu renders skeleton .List-item shells (and the container) BEFORE the
            # React app hydrates titles/excerpts, so waiting on the shell could return a
            # title-less half-rendered page (observed live: 89KB, cards=1, h2a=0 → 0 parseable
            # docs) while a fresh CLI process happened to win the race (179KB, 20 cards). Waiting
            # on the title link (.ContentItem-title a — exactly what _card_to_document reads)
            # blocks until at least one result is REALLY present; then a scroll + a generous
            # settle hydrates the rest. A genuine no-result / walled page never produces a title
            # → TimeoutError → the caller's except → uncached [] (retried next call).
            try:
                page.wait_for_selector(".ContentItem-title a, .SearchResult-Card h2 a", timeout=20000)
            except Exception:
                # No result title: genuine-empty OR logged-out (the same silent-false-empty class the
                # base sources self-heal). zhihu login is QR/SMS, so it CANNOT autofill-relogin like
                # yipinsanfendi; the best reactive move is to FAIL LOUD (a typed diagnostic) so a []
                # is never mis-read as 'nothing there', then propagate as before. Failure-path-only:
                # the success path below is byte-identical, so this cannot break a working search.
                pu = page.url or ""
                if ("/signin" in pu or "/login" in pu
                        or page.query_selector(".SignFlow, .Modal .SignContainer, .signFlow")):
                    from penumbra.core import diag
                    diag.note("zhihu.auth_expired", url=url, body=(
                        "AUTH_EXPIRED: zhihu shared-Chrome session logged out (login wall on search). "
                        "zhihu login is QR/SMS so it cannot autofill-relogin; needs a VNC re-login on "
                        "the mini (the 9222 Chrome). The session-warmer also Barks this. NOT "
                        "authoritative-empty."))
                raise
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(1500)
            return page.content()

        try:
            html = cdp_call(_navigate, initial_url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zhihu search failed: %s", exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(".SearchResult-Card, .List-item")

        docs: list[Document] = []
        seen_urls: set[str] = set()
        for card in cards:
            try:
                doc = self._card_to_document(card)
                if doc and doc.url not in seen_urls:
                    seen_urls.add(doc.url)
                    docs.append(doc)
                    if len(docs) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping Zhihu card: %s", exc)

        # Don't cache an empty result as authoritative for the full TTL: a transient blip
        # (slow hydration, a momentary wall) would otherwise blind this query for 15 min.
        # Real results → full TTL; empty → a short cooldown (spares the session a retry-storm,
        # self-heals on the next call). See EMPTY_TTL in _base.
        ttl = 900 if docs else EMPTY_TTL
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=ttl)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "zhihu.com" not in host:
            return None
        def _navigate(page):
            page.wait_for_selector("h1, .QuestionHeader-title, .Post-Title", timeout=20000)
            return page.content(), images_from_page(page)

        try:
            html, images = cdp_call(_navigate, initial_url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zhihu fetch_url failed: %s", exc)
            return None

        soup = BeautifulSoup(html, "lxml")

        # Title from h1 or question header
        title_el = soup.select_one("h1.QuestionHeader-title, h1.Post-Title, h1")
        title = title_el.get_text(strip=True) if title_el else "(no title)"

        # Body: for answers/posts, the main content is in .RichText or .Post-RichTextContainer
        body_el = soup.select_one(".Post-RichTextContainer, .RichContent .RichText, .Post-RichText")
        body = body_el.get_text("\n", strip=True) if body_el else ""

        # Author
        author_el = soup.select_one(".AuthorInfo-name, .UserLink-link")
        author = author_el.get_text(strip=True) if author_el else None

        # ID from URL
        m = re.search(r"/(question|answer|p|zhuanlan|column.*?/p)/(\d+)", url)
        source_id = m.group(2) if m else url

        return Document(
            source="zhihu",
            source_id=source_id,
            url=url,
            title=title,
            content=content_with_media(body, images) or "(no body extracted)",
            author=author,
            media=images,
        )

    def health_check(self) -> tuple[bool, str]:
        cdp_ok, cdp_msg = cdp_health()
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"
        try:
            page_url = cdp_call(lambda p: p.url, initial_url="https://www.zhihu.com/")
            if "/signin" in page_url or "/login" in page_url:
                return False, "CDP Chrome not logged into Zhihu — the operator needs to VNC + log in"
            return True, "OK (CDP + Zhihu session)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"

    def _card_to_document(self, card) -> Optional[Document]:
        # Title link
        title_link = card.select_one("h2 a, .ContentItem-title a")
        if not title_link:
            return None
        title = title_link.get_text(strip=True)
        href = title_link.get("href") or ""
        if href.startswith("//"):
            url = "https:" + href
        elif href.startswith("/"):
            url = "https://www.zhihu.com" + href
        else:
            url = href

        # Excerpt / answer preview. Select the BODY container directly: .Highlight is a
        # generic class zhihu also puts on the matched-query span INSIDE the title <a>, and a
        # CSS group selector returns the first match in DOCUMENT order, so listing .Highlight
        # made the 'excerpt' collapse to the title. Read with an empty separator (zhihu wraps
        # matched terms in inline <em>; '\n' as the separator inserted a newline at every one,
        # the title-with-embedded-newlines symptom), then drop the trailing 阅读全文 read-more.
        excerpt_el = card.select_one(".RichContent-inner, .CopyrightRichText-richText, .RichText")
        excerpt = excerpt_el.get_text("", strip=True) if excerpt_el else ""
        excerpt = re.sub(r"[​\s]*阅读全文[​\s]*$", "", excerpt)

        # Author
        author_el = card.select_one(".AuthorInfo-name, .UserLink-link")
        author = author_el.get_text(strip=True) if author_el else None

        # Vote count / votes
        score = None
        vote_el = card.select_one(".VoteButton, [aria-label*='赞同'], .ContentItem-actions")
        if vote_el:
            m = re.search(r"(\d+)", vote_el.get_text())
            if m:
                score = int(m.group(1))

        # Source ID from URL
        m = re.search(r"/(question|answer|p|zhuanlan/p)/(\d+)", url)
        source_id = m.group(2) if m else url

        return Document(
            source="zhihu",
            source_id=source_id,
            url=url,
            title=title,
            content=excerpt or "(click URL for full content)",
            author=author,
            signals=mk_signal('votes', score, kind='engagement', by='zhihu/score'),
            metadata={"raw": jsonsafe(str(card))},
        )


from penumbra.core.fetcher import register_adapter

register_adapter(ZhihuAdapter())
