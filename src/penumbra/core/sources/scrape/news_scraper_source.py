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

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("scrape_sites.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
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


def _get(url: str) -> Optional[str]:
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape GET failed %s: %s", url, exc)
    return None


def _render(url: str) -> Optional[str]:
    """Fetch a JS-RENDERED page via the shared CDP Chrome — for SPAs whose content
    is absent from the initial HTML (NodeFlair-style runtime rendering). Lazy CDP
    import so non-render sites never load playwright/CDP. Read-only navigation of
    public pages; the account-bearing sessions in the profile are untouched."""
    try:
        from penumbra.core.sources.walled._cdp import cdp_call
    except Exception as exc:  # noqa: BLE001
        logger.warning("CDP unavailable for render of %s: %s", url, exc)
        return None

    def _nav(page):
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(2800)  # let client-side rendering populate the DOM
        return page.content()

    try:
        return cdp_call(_nav, initial_url=url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CDP render failed %s: %s", url, exc)
        return None


class _ScrapeSite:
    """An index-page scraper configured from a data row (one named source)."""

    needs_credentials = False
    kind = "stream"

    def __init__(self, name: str, description: str, sites: list, cache_ttl: int = 10800,
                 url_pattern: Optional[str] = None, explicit_only=False) -> None:
        self.name = name
        self.description = description
        self.sites = sites  # list of {"url": str, "path_contains"?: str}
        self.cache_ttl = cache_ttl
        self.url_pattern = url_pattern
        self.explicit_only = explicit_only  # row-declared: True / reason string / absent

    def _extract(self, html: str, base: str, path_contains: Optional[str]) -> list[dict]:
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

    def _items(self) -> list[dict]:
        key = cache.make_key(self.name, "items", len(self.sites))
        cached = cache.get(key)
        if cached is not None:
            return cached
        items: list[dict] = []
        for s in self.sites:
            html = _render(s["url"]) if s.get("render") else _get(s["url"])
            if html:
                items.extend(self._extract(html, s["url"], s.get("path_contains")))
        if items:
            cache.set(key, items, ttl=self.cache_ttl)
        return items

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = [
            Document(
                source=self.name, source_id=it["url"], url=it["url"], title=it["title"],
                content="(scraped index item — click URL for full content)",
                tags=[self.name], metadata={"raw": jsonsafe(it)},
            )
            for it in self._items()
        ]
        q = (query or "").strip()
        if q:
            return keyword_score_filter(docs, q)[:limit]
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        if not self.url_pattern:
            return None
        host = urlparse(url).hostname or ""
        if not re.search(self.url_pattern, host):
            return None
        html = _get(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        title_el = soup.find(["h1", "h2"]) or soup.title
        title = title_el.get_text(strip=True) if title_el else "(untitled)"
        main = soup.find("main") or soup.find("article") or soup.body
        body = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True)).strip() if main else ""
        return Document(
            source=self.name, source_id=url, url=url, title=title, content=body or "(no content)",
            tags=[self.name], metadata={"raw": jsonsafe({"url": url, "title": title, "body": body})},
        )

    def health_check(self) -> tuple[bool, str]:
        for s in self.sites:
            html = _render(s["url"]) if s.get("render") else _get(s["url"])
            if html and self._extract(html, s["url"], s.get("path_contains")):
                return True, f"OK (scrape; {len(self.sites)} site(s), first yields items)"
        return False, f"0 items from {len(self.sites)} site(s) (JS-rendered? selector drift?)"


def _register_row(row: dict) -> None:
    from penumbra.core.fetcher import register_adapter
    register_adapter(_ScrapeSite(
        name=row["name"], description=row["description"], sites=row["sites"],
        cache_ttl=row.get("cache_ttl", 10800), url_pattern=row.get("url_pattern"),
        explicit_only=row.get("explicit_only", False),
    ))


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
        from penumbra.core.curator import apply as _apply
        from penumbra.core.curator import apply_live as _apply_live
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
