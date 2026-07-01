"""HK 5 大学 CS/CSE/COMP dept news HTML scrape — a regional academic signal.

P10 实施：HK 大学系统普遍不暴露 RSS，但 dept-level news 页面是结构化 HTML 列表，
可 BeautifulSoup 解析。覆盖 HK CS 院系动态 + 学术 peer 网络的核心信号源。

站级现状 (2026-06-29 实测复核)：5 站异构且半数已衰变。HKUST CSE 仍是结构化静态
HTML、可稳定抓到当前 dated 新闻;HKU CS 的 /news/ 冻结在 2018 存档(站本身不更新);
CUHK CSE / CityU CS / PolyU COMP 已改 JS/卡片渲染,news 不在静态 anchor 文本里
(CDP render 亦超时/结构异常,非选择器可救)。提取器据此改为「card-aware + 必须有近期
日期」:一条 recency 闸同时滤掉无日期的导航与陈旧存档(如 HKU 2018),只放当前真新闻 —
宁可某站返 0,也绝不把旧闻/导航当新闻吐出。站若日后恢复静态新闻会被自动重新收录。

应用场景：
- 跟踪 HK CS 院系动态 / 同行 / 导师
- HK AI 学术圈生态（与 Sensetime/SCMP 等已覆盖的"外部"视角互补）
- 学术招聘信号（教职 / 博后 hiring announcements）
"""

from __future__ import annotations

import datetime
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from penumbra.core import cache
from penumbra.core.normalize import PolarisDocument, jsonsafe

logger = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

# (display_name, url, host_filter_re)
UNIS = [
    ("HKU CS", "https://www.cs.hku.hk/news/", r"cs\.hku\.hk"),
    ("HKUST CSE", "https://cse.hkust.edu.hk/news/", r"cse\.hkust\.edu\.hk"),
    ("CUHK CSE", "https://www.cse.cuhk.edu.hk/news/", r"cse\.cuhk\.edu\.hk"),
    ("CityU CS", "https://www.cs.cityu.edu.hk/news/", r"cs\.cityu\.edu\.hk"),
    ("PolyU COMP", "https://www.polyu.edu.hk/comp/news-and-events/news/", r"polyu\.edu\.hk"),
]

MIN_TITLE_LEN = 18
MAX_TITLE_LEN = 220

# A real news item carries a RECENT date. This single gate keeps the source honest across
# these decaying sites: it drops nav/people links (no date) AND stale archives (HKU CS /news/
# froze at 2018) in one rule, instead of emitting either as if it were current news.
RECENCY_DAYS = 550  # ~18 months of dept news

# Section / nav paths that are NOT news even when their link text is headline-length.
_NAV_PATH = re.compile(
    r"/(people|staff|study|about|admission|ug|pg|mphil|phd|academics|visit|privacy|"
    r"contact|programme|program|curriculum|prospective|alumni|index|home|login|search|"
    r"sitemap|faculty-profiles?)\b",
    re.I,
)

_MONTHS: dict[str, int] = {}
for _i, _m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1):
    _MONTHS[_m.lower()] = _i
    _MONTHS[_m[:3].lower()] = _i


def _month_num(s: str) -> Optional[int]:
    return _MONTHS.get(s.lower()) or _MONTHS.get(s[:3].lower())


def _parse_news_date(text: str) -> Optional[datetime.date]:
    """First plausible publication date in a card's text. Handles ISO (2026-06-24),
    '24 June 2026', and 'June 24, 2026' — the shapes the HK dept pages actually use."""
    m = re.search(r"\b(20[12]\d)[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20[12]\d)\b", text)
    if m and _month_num(m.group(2)):
        try:
            return datetime.date(int(m.group(3)), _month_num(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20[12]\d)\b", text)
    if m and _month_num(m.group(1)):
        try:
            return datetime.date(int(m.group(3)), _month_num(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def _fetch_html(url: str) -> Optional[str]:
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,zh;q=0.8"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("HK uni HTML fetch failed for %s: %s", url, exc)
        return None


def _extract_news_items(html: str, base_url: str, uni_name: str) -> list[dict]:
    """Extract RECENT, DATED news items from a dept page (heterogeneous HTML).

    Card-aware (title from an inner heading when the anchor wraps label + date + title),
    same-domain only, nav-path filtered, and gated on a parseable recent date. The date
    gate is what keeps the source honest across these decaying sites: no-date -> nav chrome,
    stale-date -> frozen archive; both are dropped rather than emitted as current. Newest first.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    for noise in soup.select("nav, footer, header, .menu, .navigation, #nav, #footer, #header, script, style"):
        noise.decompose()

    items: list[dict] = []
    seen_urls: set[str] = set()
    base_host = urlparse(base_url).hostname or ""
    base_path = urlparse(base_url).path.rstrip("/")
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=120)  # tolerate near-future event dates
    floor = today - datetime.timedelta(days=RECENCY_DAYS)

    for a in soup.find_all("a", href=True):
        heading = a.find(["h1", "h2", "h3", "h4", "h5"])
        text = heading.get_text(" ", strip=True) if heading else a.get_text(" ", strip=True)
        if not text or len(text) < MIN_TITLE_LEN or len(text) > MAX_TITLE_LEN:
            continue
        if any(skip in text.lower() for skip in ("read more", "click here", "more...", "view more")):
            continue
        href = a["href"].strip()
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        host = parsed.hostname or ""
        if not (host == base_host or host.endswith(base_host.split(".", 1)[-1])):
            continue
        if parsed.path.rstrip("/") == base_path:  # self / section-index link, not an article
            continue
        if _NAV_PATH.search(parsed.path):  # people / study / about — not news
            continue
        if full_url in seen_urls:
            continue

        # Tightest-scope date: try the anchor, then walk up a few ancestors, taking the
        # FIRST (smallest) node that carries a parseable date — per-card when cards are
        # individually dated, falling back to the section's date otherwise.
        pub: Optional[datetime.date] = None
        scope_text = text
        node = a
        for _ in range(4):
            txt = node.get_text(" ", strip=True)
            pub = _parse_news_date(txt)
            if pub:
                scope_text = txt
                break
            node = node.parent
            if node is None:
                break
        if not pub or pub < floor or pub > horizon:
            continue

        seen_urls.add(full_url)
        snippet = re.sub(r"\s+", " ", scope_text.replace(text, "", 1)).strip()[:500]
        items.append({
            "title": text,
            "url": full_url,
            "snippet": snippet,
            "date_str": pub.isoformat(),
            "_date": pub,
            "uni": uni_name,
        })

    items.sort(key=lambda it: it["_date"], reverse=True)
    for it in items:
        it.pop("_date", None)
    return items


class HKUniversitiesAdapter:
    name = "hk_universities"
    needs_credentials = False
    description = (
        "HK 5 大学 CS/CSE/COMP dept news — HKU CS / HKUST CSE / CUHK CSE / "
        "CityU CS / PolyU COMP (HTML scrape，HK CS 院系动态信号)"
    )

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        key = cache.make_key("hk_universities", "search", query, limit)
        cached_data = cache.get_docs(key)
        if cached_data is not None:
            return cached_data

        # Root cause confirmed: this IS a serial fan-out. The loop fetches each of
        # the 5 dept pages over the network one after another (the dominant cost —
        # CUHK ~175KB / CityU ~133KB / PolyU ~137KB are large, slow downloads), and
        # each is followed by a full-page BeautifulSoup parse. The 5 unis are
        # independent hosts with no shared rate limit, so fetch+extract per uni runs
        # in parallel. Each worker keeps the same fetch-then-extract-with-try/except
        # shape, so a single uni's failure stays isolated. copy_context() is captured
        # HERE on the search thread (where the cache `fresh` contextvar is set) and one
        # private copy goes to each worker via ctx.run — never copied inside the worker.
        # The query filter, [:limit] slice, doc build, and 1h cache below are untouched.
        def _fetch_uni(entry: tuple) -> list[dict]:
            uni_name, url, _host_re = entry
            html = _fetch_html(url)
            if not html:
                return []
            try:
                return _extract_news_items(html, url, uni_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("HK uni extract failed for %s: %s", uni_name, exc)
                return []

        contexts = [copy_context() for _ in UNIS]
        with ThreadPoolExecutor(max_workers=min(len(UNIS), 5)) as ex:
            per_uni = list(ex.map(lambda ctx, e: ctx.run(_fetch_uni, e),
                                  contexts, UNIS))
        # Round-robin interleave so EVERY uni surfaces within `limit` — not the first uni
        # (HKU CS, whose cs.hku.hk/news/ page is itself stale, linking 2018 items) hogging all
        # the slots and starving PolyU / HKUST / CUHK / CityU current news. (2026-06-29 fix:
        # an empty-query fetch returned only HKU-CS-2018 because the per-uni lists were
        # concatenated HKU-first and then sliced [:limit].)
        from itertools import zip_longest
        all_items: list[dict] = [it for tier in zip_longest(*per_uni) for it in tier if it is not None]

        # Filter by query
        if query:
            q_lower = query.lower()
            filtered = []
            for item in all_items:
                blob = (item["title"] + " " + item["snippet"]).lower()
                if q_lower in blob:
                    filtered.append(item)
            all_items = filtered

        docs: list[PolarisDocument] = []
        for item in all_items[:limit]:
            docs.append(self._item_to_document(item))

        cache.set_docs(key, docs, ttl=3600)
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        host = (urlparse(url).hostname or "").lower()
        if not any(re.search(host_re, host) for _, _, host_re in UNIS):
            return None
        # Find matching uni; scrape the news index and look for matching link
        for uni_name, index_url, host_re in UNIS:
            if not re.search(host_re, host):
                continue
            html = _fetch_html(index_url)
            if not html:
                continue
            items = _extract_news_items(html, index_url, uni_name)
            for item in items:
                if item["url"] == url:
                    return self._item_to_document(item)
        return None

    def health_check(self) -> tuple[bool, str]:
        # Honest health = how many unis actually YIELD dated news, not just return HTML.
        # (A page can 200 + be large yet yield nothing: a JS-rendered list or a stale archive.)
        yields: list[str] = []
        loaded = 0
        for uni_name, url, _ in UNIS:
            html = _fetch_html(url)
            if not (html and len(html) > 500):
                continue
            loaded += 1
            if _extract_news_items(html, url, uni_name):
                yields.append(uni_name)
        if not yields:
            return False, f"{loaded}/{len(UNIS)} pages load but 0 yield dated news (JS/stale)"
        return True, f"OK ({len(yields)}/{len(UNIS)} unis yield news: {', '.join(yields)})"

    @staticmethod
    def _item_to_document(item: dict) -> PolarisDocument:
        return PolarisDocument(
            source="hk_universities",
            source_id=item["url"],
            url=item["url"],
            title=f"[{item['uni']}] {item['title']}",
            content=item["snippet"] or f"{item['uni']} — {item['title']}",
            author=item["uni"],
            tags=[item["uni"].lower().replace(" ", "_"), "hk-academia"],
            metadata={
                "uni": item["uni"],
                "date_str": item["date_str"],
                "raw": jsonsafe(item),
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(HKUniversitiesAdapter())
