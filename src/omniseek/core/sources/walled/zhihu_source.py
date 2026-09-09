"""知乎 adapter — Playwright via CDP to the persistent logged-in Chrome.

Why CDP and not direct HTTP scraping:
- 知乎 returns 403 on unauthenticated direct fetch
- API endpoints are signed (x-zse-93 / x-zse-96 headers) — reverse-engineering is fragile
- The persistent Chrome already has a logged-in session (the operator logs in once via VNC)
- CDP lets OmniSeek drive that browser to perform searches as a real user

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

from omniseek.core import cache
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.walled._base import EMPTY_TTL
from omniseek.core.sources.walled._cdp import cdp_call, cdp_health, content_with_media, images_from_page

logger = logging.getLogger(__name__)

# WHERE THE AUTHOR AND DATE ACTUALLY LIVE ON THE SEARCH PAGE (dumped from the live logged-in
# Chrome on 2026-08-30, 38 cards):
#
#     author  ->  .RichContent-inner > b        e.g. <b>Woo Tzins</b>
#     date    ->  .SearchItem-time              e.g. <span class="... SearchItem-time">07-03</span>
#
# and, measured on the same dump, .AuthorInfo-name / .UserLink-link / .AuthorInfo /
# .ContentItem-meta / .ContentItem-time ALL match ZERO nodes here. The adapter was written
# against an older DOM and had been silently returning author=None ever since; `date` was never
# read at all, so callers were reverse-engineering publication dates out of 知乎's numeric ids.
#
# The <b> is a real structural handle, which is why it is preferred over any text pattern:
# 知乎 renders the name in bold at the head of the excerpt, so get_text() folds it into the
# excerpt as a "作者名：正文" prefix. Reading the tag gives the name exactly; reading the text
# only lets you guess where the name ends. The text rule below stays as a last resort, and it is
# deliberately conservative because an excerpt is arbitrary prose: a run like "结论：" would sail
# through any purely syntactic test, so the fallback must not be trusted the way the tag is.
_AUTHOR_PREFIX = re.compile(r"^([^：:，。！？；、（）()]{1,24})[：:]\s*")

# Non-name words that DO show up before a colon at the head of Chinese technical prose. Without
# this, "结论：强化学习……" yields the author "结论".
_NOT_AUTHOR = {
    "结论", "注", "注意", "例", "例如", "问", "答", "总结", "背景", "方法", "实验",
    "摘要", "前言", "引言", "定义", "题目", "问题", "答案", "提示", "警告", "更新",
    "第一步", "第二步", "第三步", "原文", "译文", "来源", "参考", "声明", "免责声明",
}


def _split_author_prefix(excerpt: str) -> tuple[Optional[str], str]:
    """LAST-RESORT author read: peel a leading '作者名：' off an excerpt.

    Only used when the <b> tag is absent. Returns (author or None, excerpt without the prefix);
    the strip matters because leaving the prefix in makes every excerpt read as though the
    author's name were the article's first word.
    """
    m = _AUTHOR_PREFIX.match(excerpt or "")
    if not m:
        return None, excerpt
    name = m.group(1).strip()
    if not name or name in _NOT_AUTHOR:
        return None, excerpt
    return name, excerpt[m.end():]


def _card_author(card, excerpt: str) -> tuple[Optional[str], str]:
    """Author for one search card, structure first.

    Returns (author or None, excerpt with the name prefix removed).
    """
    b = card.select_one(".RichContent-inner b, .CopyrightRichText-richText b, .RichText b")
    if b:
        name = b.get_text(strip=True)
        if name and len(name) <= 40:
            # get_text() already folded the <b> into the excerpt; drop it plus its separator.
            rest = re.sub(r"^" + re.escape(name) + r"\s*[：:]?\s*", "", excerpt)
            return name, rest
    return _split_author_prefix(excerpt)


def _card_date(card, today=None) -> Optional[str]:
    """Publication date for one search card.

    知乎 abbreviates recent dates to MM-DD and only spells out the year for older items, so a
    bare '07-03' has to be resolved against today: a month-day already past this year is this
    year, one still ahead of us belongs to last year. This is an INFERENCE for the abbreviated
    form and can be off by a year right at the boundary; the four-digit form is exact.
    """
    txt = _first_text(card, ".SearchItem-time, .ContentItem-time")
    if not txt:
        return None
    m = re.search(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})", txt)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"\s*(\d{1,2})[-月/](\d{1,2})\s*日?\s*", txt)
    if m:
        today = today or datetime.now()
        mo, day = int(m.group(1)), int(m.group(2))
        year = today.year if (mo, day) <= (today.month, today.day) else today.year - 1
        return "%d-%02d-%02d" % (year, mo, day)
    return None


def _first_text(soup, selectors: str) -> Optional[str]:
    """First selector that yields NON-EMPTY text.

    select_one stops at the first structural match, which on 知乎 article pages is an empty
    .AuthorInfo-name shell (measured 2026-08-30: fetch_url returned author='' , not None).
    An empty string is worse than None because it reads as 'looked and found nothing'.
    """
    for sel in [s.strip() for s in selectors.split(",")]:
        for el in soup.select(sel):
            txt = el.get_text(strip=True)
            if txt:
                return txt
    return None


def _article_date(soup) -> Optional[str]:
    """Publication date for an article/answer page.

    Nothing in this adapter ever set `date`, so every 知乎 doc came back dateless and callers
    had to reverse it out of the numeric id. Reads the standard metadata first (meta tags and
    JSON-LD are far more stable than 知乎's React class names), then falls back to the visible
    timestamp. Returns an ISO-ish string or None; never raises.
    """
    for sel, attr in (
        ('meta[itemprop="datePublished"]', "content"),
        ('meta[itemprop="dateCreated"]', "content"),
        ('meta[property="article:published_time"]', "content"),
        ('meta[itemprop="dateModified"]', "content"),
    ):
        el = soup.select_one(sel)
        if el and el.get(attr):
            return el[attr].strip()

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        m = re.search(r'"date(?:Published|Created|Modified)"\s*:\s*"([^"]+)"', raw)
        if m:
            return m.group(1)

    txt = _first_text(soup, ".ContentItem-time, .ContentItem-time span, .Post-Time")
    if txt:
        m = re.search(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})", txt)
        if m:
            return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    return None


class ZhihuAdapter:
    name = "zhihu"
    needs_credentials = False  # Login happens once via VNC; we just use the session
    explicit_only = "shared CDP Chrome (precious logged-in session)"
    fetch_url_class = "fulltext"
    fetch_url_hosts = ("zhihu.com",)
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
                    from omniseek.core import diag
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

        # Author. The old code took select_one's first structural match, which on an article page
        # is an EMPTY .AuthorInfo-name shell, so this returned '' (measured 2026-08-30). An empty
        # string is the worst outcome: it reads as 'we looked and the page has no author'.
        # _first_text skips empty matches; the meta/JSON-LD tail is there because 知乎 emits
        # standard article metadata that outlives its React class names.
        author = _first_text(soup, ".AuthorInfo-name, .AuthorInfo .UserLink-link, .UserLink-link, .AuthorInfo-content .UserLink")
        if not author:
            meta_author = soup.select_one('meta[itemprop="name"], meta[name="author"]')
            if meta_author and meta_author.get("content"):
                author = meta_author["content"].strip()
        if not author:
            for script in soup.select('script[type="application/ld+json"]'):
                m_a = re.search(r'"author"\s*:\s*(?:\{[^}]*?"name"\s*:\s*"([^"]+)"|"([^"]+)")',
                                script.string or script.get_text() or "")
                if m_a:
                    author = (m_a.group(1) or m_a.group(2)).strip()
                    break

        date = _article_date(soup)

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
            date=date,
            media=images,
        )

    def health_check(self) -> tuple[bool, str]:
        cdp_ok, cdp_msg = cdp_health(ensure=True)
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

        # Author and date. The legacy .AuthorInfo-name / .UserLink-link selectors stay as the
        # first probe because they are the CORRECT source whenever 知乎 renders them (a future
        # DOM change then takes over silently), but they match nothing on today's search page,
        # so in practice the <b> tag inside the excerpt is what fires. See the module header for
        # the live DOM dump this is based on.
        author = _first_text(card, ".AuthorInfo-name, .UserLink-link")
        if not author:
            author, excerpt = _card_author(card, excerpt)
        date = _card_date(card)

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
            date=date,
            signals=mk_signal('votes', score, kind='engagement', by='zhihu/score'),
            metadata={"raw": jsonsafe(str(card))},
        )


from omniseek.core.fetcher import register_adapter

register_adapter(ZhihuAdapter())
