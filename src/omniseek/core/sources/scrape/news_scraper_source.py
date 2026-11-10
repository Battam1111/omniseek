"""Generic config-driven news/blog scraper — for high-value sources with NO RSS.

Some sources have no feed and no RSSHub route (RBC Borealis, SEA-LION, A*STAR,
Baseten, UK AISI, …). Rather than a bespoke file per site, this registers one
adapter per row in ``scrape_sites.json`` — each row aggregates a few index-page
URLs and extracts article links heuristically: anchors whose visible text is
headline-length (20–200 chars, not boilerplate nav) and whose href path looks
like an article, optionally narrowed by a per-site ``path_contains`` filter.

STATIC-HTML sites only. JS-runtime SPAs (content loaded by a runtime API, absent
from the initial HTML — e.g. NodeFlair/Apollo/ITIB/Amii/NTU/SenseTime) are NOT
covered here; they need a headless browser or per-site API and are tracked
separately. Adding a static site is a one-row JSON edit (like rss_bundles).
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import anyio
from bs4 import BeautifulSoup

from omniseek.core import cache, diag, http, portal, upstreams
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter
from omniseek.core.sources._base import SourceBase, ablocking, aparse
from omniseek.core.web_fallback import _extract_text

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("scrape_sites.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
_HEADERS = {"User-Agent": UA, "Accept": "text/html,*/*"}
TIMEOUT = 20
_ART = re.compile(r"/(news|blog|research|insights|article|stories|story|press|updates|publication|work|post|jobs?|details)[/-]", re.I)
_CJK = re.compile(r"[一-鿿]")
_LABELS = {"read more", "learn more", "view all", "see all", "find out more", "about us",
           "contact us", "home", "subscribe", "careers", "privacy policy", "terms of use",
           "media release", "press release", "insights", "news", "blog", "article", "event",
           "events", "story", "stories", "update", "updates", "publication", "publications",
           "research", "announcement", "announcements", "feature", "report", "more",
           "skip to content", "skip to main content", "updates & insights", "updates and insights",
           "all news", "all posts", "latest news", "view more", "show more"}
# A bare date segment (e.g. "May 23, 2026" / "2026-05-23" / "23 May 2026") — not a title.
_DATE_RE = re.compile(r"^[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}$|^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{1,2}\s+[A-Z][a-z]{2,8}\.?\s+\d{4}$")


def _title_from_anchor(a) -> str:
    """Best title for a card/anchor: prefer an inner heading, else the longest text
    segment that is not a date or a boilerplate label — handles cards that wrap
    label + date + title in one <a> (e.g. 'Media Release  May 23, 2026  Real Title')."""
    h = a.find(["h1", "h2", "h3", "h4", "h5"])
    if h:
        t = h.get_text(" ", strip=True)
        if len(t) >= 10:
            return t
    segs = [s.strip() for s in a.stripped_strings if s.strip()]
    cand = [s for s in segs if not _DATE_RE.match(s) and s.lower() not in _LABELS]
    if cand:
        return max(cand, key=len)
    return a.get_text(" ", strip=True)


def _get_failed(url: str, exc: BaseException) -> None:
    logger.warning("scrape GET failed %s: %s", url, exc)
    st = getattr(getattr(exc, "response", None), "status_code", None)
    diag.note("news_scraper.fetch", url=url, status=st, exc=exc)


async def _aget(url: str) -> Optional[str]:
    """One static page. http.adirect: the declared gates of every hop (the robots.txt Crawl-delay and
    any upstream gate, the one redirect rule), the declared User-Agent, the readings; a no-op gate for
    an undeclared host. UpstreamBusy lands in the except below: not sent."""
    try:
        r = await http.adirect("GET", url, headers=_HEADERS, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception as exc:  # noqa: BLE001
        _get_failed(url, exc)
    return None


def _render(url: str) -> Optional[str]:
    """Fetch a JS-RENDERED page via the shared CDP Chrome — for SPAs whose content
    is absent from the initial HTML (NodeFlair-style runtime rendering). Lazy CDP
    import so non-render sites never load playwright/CDP. Read-only navigation of
    public pages; the account-bearing sessions in the profile are untouched."""
    try:
        from omniseek.core.sources.walled._cdp import cdp_call
    except Exception as exc:  # noqa: BLE001
        logger.warning("CDP unavailable for render of %s: %s", url, exc)
        return None

    def _nav(page):
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(2800)  # let client-side rendering populate the DOM
        return page.content()

    try:
        # a render is a page load on that host: never a host that declares a User-Agent the browser
        # cannot send (DeclaredUserAgentRefused -> the except below); then the Chrome turn first,
        # within the time left (no host gate held while queueing, review F9); once it has the turn,
        # the host's declared gates tried once (no waiting while holding the turn, review P6), on a
        # lease as long as the render may take (a stuck render gives the host back, review P1).
        render_s = 90
        upstreams.check_browser(url)
        until = upstreams.budget_until(url, default=render_s)
        return cdp_call(_nav, initial_url=url, timeout=render_s, queue_until=until,
                        on_turn=lambda: upstreams.browser_turn(url, render_s))
    except Exception as exc:  # noqa: BLE001
        logger.warning("CDP render failed %s: %s", url, exc)
        return None


def _extract_links(html: str, base: str, path_contains: Optional[str]) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen: set[str] = set()
    base_path = urlparse(base).path.rstrip("/")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("#"):
            continue
        full = urljoin(base, href)
        fp = urlparse(full)
        if not _ART.search(fp.path):
            continue
        if fp.path.rstrip("/") == base_path:  # self / section-index link, not an article
            continue
        if path_contains and path_contains not in full:
            continue
        if full in seen:
            continue
        title = _title_from_anchor(a)
        # Headline-length floor: 20 chars for latin text; CJK packs far more
        # meaning per char, so 8 suffices (a 20-CJK-char floor drops most
        # real Chinese forum/news titles).
        floor = 8 if _CJK.search(title) else 20
        if not (floor <= len(title) <= 200) or title.lower() in _LABELS:
            continue
        seen.add(full)
        out.append({"title": title, "url": full})
    return out


def _extract_pages(sites: list, pages: list) -> list[dict]:
    """Every page of a row, in site order (pure CPU: run through aparse, the process lane for a long
    parse)."""
    items: list[dict] = []
    for s, html in zip(sites, pages):
        if html:
            items.extend(_extract_links(html, s["url"], s.get("path_contains")))
    return items


class _ScrapeSite(SourceBase):
    """An index-page scraper configured from a data row (one named source). Static pages come through
    ``_aget`` (http.adirect, no worker thread); a ``render`` site's page from the shared CDP Chrome, a
    blocking call, on a thread of the legacy lane (``ablocking``), whose turn takes the host's gates
    (``_render``). Its ``search`` is SourceBase's, generated from ``asearch``."""

    needs_credentials = False
    kind = "stream"

    def __init__(self, name: str, description: str, sites: list, cache_ttl: int = 10800,
                 url_pattern: Optional[str] = None, explicit_only=False) -> None:
        self.name = name
        self.description = description
        self.sites = sites  # list of {"url": str, "path_contains"?: str, "render"?: bool}
        self.cache_ttl = cache_ttl
        self.url_pattern = url_pattern
        self.explicit_only = explicit_only  # row-declared: True / reason string / absent

    @staticmethod
    def _dedup(items: list) -> list:
        """One row per URL. _extract dedups within ONE section, but a thread pinned or
        cross-posted in several sections came back once PER SECTION (measured on gter
        2026-08-16: the same /details/ thread three times in one drill). Applied on the
        cache-READ path too, so a pre-fix cached list is healed instead of served."""
        seen: set = set()
        out: list = []
        for it in items:
            u = it.get("url")
            if u in seen:
                continue
            seen.add(u)
            out.append(it)
        return out

    async def _afetch_items_once(self) -> list[dict]:
        """The static pages are fetched side by side (each host keeps its own gates), the render pages
        one after another on the shared Chrome, then all are read in site order. One after another, a
        row's round trips (scrape_singapore: five pages, two redirects) each waited for a turn of a busy
        loop inside a broad search, and the row ran past its deadline where the threaded version did
        not."""
        pages: list[Optional[str]] = [None] * len(self.sites)

        async def fetch(i: int, url: str) -> None:
            pages[i] = await _aget(url)

        async def render_all() -> None:
            for i, s in enumerate(self.sites):
                if s.get("render"):
                    pages[i] = await ablocking(_render, s["url"])

        async with anyio.create_task_group() as tg:
            for i, s in enumerate(self.sites):
                if not s.get("render"):
                    tg.start_soon(fetch, i, s["url"])
            if any(s.get("render") for s in self.sites):
                tg.start_soon(render_all)

        # The HTML parse is pure CPU: through aparse, the process lane for a long parse (ASYNC-CORE-DESIGN.md
        # D14). One hop for all the row's pages: each hop waits its turn in the lane, and a broad search fills it.
        return await aparse(sum(len(p) for p in pages if p), _extract_pages, self.sites, pages)

    def _items_key(self) -> str:
        return cache.make_key(self.name, "items", len(self.sites))

    async def _aitems(self) -> list[dict]:
        key = self._items_key()
        cached = await cache.aget_once(key)
        if cached is not None:
            return self._dedup(cached)
        items = await self._afetch_items_once()
        if not items and any(s.get("render") for s in self.sites):
            # A shared-Chrome render can come back empty on a cold first pass (the browser
            # is busy or still settling); the drill then reports an honest zero and the
            # caller walks away. Empties are never cached, so ONE immediate retry is this
            # call's only second chance; measured on gter 2026-08-16, where the cold drill
            # returned 0 and the same query against a warm cache returned real hits.
            items = await self._afetch_items_once()
        items = self._dedup(items)
        if items:
            await anyio.to_thread.run_sync(functools.partial(cache.set, key, items, ttl=self.cache_ttl), limiter=portal.IO_LANE)
        return items

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        return self._docs(await self._aitems(), query, limit)

    def _docs(self, items: list[dict], query: str, limit: int) -> list[Document]:
        docs = [
            Document(
                source=self.name, source_id=it["url"], url=it["url"], title=it["title"],
                content="",   # an index item has no body; the article is behind the url
                tags=[self.name], metadata={"raw": jsonsafe(it)},
            )
            for it in items
        ]
        q = (query or "").strip()
        if q:
            return keyword_score_filter(docs, q)[:limit]
        return docs[:limit]

    async def afetch_url(self, url: str) -> Optional[Document]:
        if not self.url_pattern:
            return None
        host = urlparse(url).hostname or ""
        if not re.search(self.url_pattern, host):
            return None
        html = await _aget(url)
        if not html:
            return None
        # OmniSeek's one whole-page extraction (chrome dropped, Markdown out), same as web_fallback;
        # pure CPU, through aparse (the process lane for a long parse).
        title, body = await aparse(len(html), _extract_text, html)
        title = title or "(untitled)"
        return Document(
            source=self.name, source_id=url, url=url, title=title, content=body or "",
            tags=[self.name], metadata={"raw": jsonsafe({"url": url, "title": title, "body": body})},
        )

    async def ahealth_check(self) -> tuple[bool, str]:
        """A render site's page comes from the shared CDP Chrome (blocking: a thread of the legacy lane),
        a static one through ``_aget``; the parse through ``aparse``."""
        for s in self.sites:
            html = await ablocking(_render, s["url"]) if s.get("render") else await _aget(s["url"])
            if html and await aparse(len(html), _extract_links, html, s["url"], s.get("path_contains")):
                return True, f"OK (scrape; {len(self.sites)} site(s), first yields items)"
        return False, f"0 items from {len(self.sites)} site(s) (JS-rendered? selector drift?)"


def site_adapter(row: dict) -> _ScrapeSite:
    """The adapter for one scrape_sites row."""
    return _ScrapeSite(
        name=row["name"], description=row["description"], sites=row["sites"],
        cache_ttl=row.get("cache_ttl", 10800), url_pattern=row.get("url_pattern"),
        explicit_only=row.get("explicit_only", False),
    )


def _register_row(row: dict) -> None:
    from omniseek.core.fetcher import register_adapter
    register_adapter(site_adapter(row))


def _load() -> None:
    """Base in-tree rows, THEN curator live-apply overlay rows (base wins; typed-validate + drop a
    bad overlay row). news_scraper is a _NEVER_AUTO_FAMILIES family so the one-tap lane never writes
    a row here; the loader is overlay-aware for symmetry + an operator-promoted reconcile path."""
    base = json.loads(_DATA.read_text(encoding="utf-8"))
    seen = set()
    for row in base:
        _register_row(row)
        seen.add(row["name"])
    try:
        from omniseek.core.curator import apply as _apply
        from omniseek.core.curator import apply_live as _apply_live
        for r in _apply_live.overlay_rows("news_scraper"):
            name = r.get("name")
            if name in seen:
                continue  # base wins
            problems = _apply.validate_row_typed("news_scraper", r)
            if problems:
                logger.warning("news_scraper overlay row %r dropped (invalid): %s", name, problems)
                continue
            _register_row(r)
            seen.add(name)
    except Exception as exc:  # noqa: BLE001, overlay best-effort; base must always load
        logger.warning("news_scraper overlay load skipped: %s", exc)


_load()
