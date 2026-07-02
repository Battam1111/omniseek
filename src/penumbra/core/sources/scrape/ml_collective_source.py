"""ML Collective adapter — HTML scrape (mlcollective.org has no RSS).

mlcollective.org is a curated static site for the ML Collective non-profit (free ML research
mentorship). Its content streams are:
- /events/  : Open Collab Research Jams + conference socials, each a dated row
              ("Jun 17, 2026  Open Collab Research Jam #33" -> /events/research-jam-33/).
- /dlct/    : the Deep Learning: Classics and Trends reading-group talk list (links to /abs/<id>).
- /news/    : sporadic announcements.

Earlier this adapter dumped EVERY anchor on /news /wiki /events /projects /services as a
"document" with hardcoded placeholder content and no date, so it surfaced nav/menu junk
("our YouTube channel", "online", "MosaicML") and looked dead even though the site is current
(events run through 2026). This version extracts only REAL item links (a slug deeper than the
section index, or an /abs/ talk), parses each item's nearby date, fills real content, and sorts
newest-first — the same card-aware + date-anchored shape used across the HTML scrapers.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from penumbra.core import cache, http
from penumbra.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

BASE = "https://mlcollective.org"
INDEX_PATHS = ["/events/", "/news/", "/dlct/"]  # the dated content streams (not static nav pages)
UA = "Mozilla/5.0 (compatible; PenumbraEye/0.1; +automated retrieval)"

# Anchor text that is nav/footer/social, never a content item.
NAV_TEXTS = {
    "home", "about", "projects", "events", "services", "donate", "wiki", "news", "dlct",
    "twitter", "github", "discord", "linkedin", "youtube", "event calendar", "subscribe",
    "subscribe with your email", "read more", "back",
}
EXTERNAL_BAD_HOSTS = ("twitter.com", "x.com", "linkedin.com", "discord.com", "discord.gg")

_MONTHS: dict[str, int] = {}
for _i, _m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1):
    _MONTHS[_m.lower()] = _i
    _MONTHS[_m[:3].lower()] = _i


def _month_num(s: str) -> Optional[int]:
    return _MONTHS.get(s.lower()) or _MONTHS.get(s[:3].lower())


def _parse_date(text: str) -> Optional[datetime.date]:
    """First plausible date in a row's text: 'Jun 17, 2026' / '17 June 2026' / '2026-06-17'."""
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20[12]\d)\b", text)
    if m and _month_num(m.group(1)):
        try:
            return datetime.date(int(m.group(3)), _month_num(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20[12]\d)\b", text)
    if m and _month_num(m.group(2)):
        try:
            return datetime.date(int(m.group(3)), _month_num(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"\b(20[12]\d)[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


class MLCollectiveAdapter:
    name = "ml_collective"
    needs_credentials = False
    description = "ML Collective — free ML research mentorship + research jams + DLCT reading group"

    def _scrape_index(self, path: str) -> list[dict]:
        """Fetch an index page and return REAL item rows (title + url + parsed date)."""
        key = cache.make_key("ml_collective", "index2", path)
        cached = cache.get(key)
        if cached is not None:
            return cached

        html = http.get_text(urljoin(BASE, path), timeout=20)
        if html is None:
            return []

        soup = BeautifulSoup(html, "lxml")
        base_seg = path.rstrip("/")  # e.g. "/events"
        items: list[dict] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(" ", strip=True)
            if not text or len(text) < 8 or href.startswith(("#", "mailto:", "javascript:")):
                continue
            if text.lower() in NAV_TEXTS:
                continue
            host = urlparse(href).hostname or ""
            if any(bad in host for bad in EXTERNAL_BAD_HOSTS):
                continue
            full = urljoin(BASE + path, href)
            pu = urlparse(full)
            if "mlcollective.org" not in (pu.hostname or ""):
                continue
            p = pu.path.rstrip("/")
            # Real item = a slug deeper than the section index, or a /abs/ talk link.
            is_item = (p.startswith(base_seg + "/") and p != base_seg) or p.startswith("/abs/")
            if not is_item or full in seen:
                continue
            seen.add(full)
            parent = a.find_parent(["article", "div", "li", "tr", "section", "td", "p"])
            ctx = parent.get_text(" ", strip=True) if parent else text
            d = _parse_date(ctx)
            items.append({
                "url": full,
                "title": text[:160],
                "date": d.isoformat() if d else None,
                "source_path": path,
            })

        cache.set(key, items, ttl=6 * 3600)
        return items

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key("ml_collective", "search2", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        uniq: dict[str, dict] = {}
        for path in INDEX_PATHS:
            for it in self._scrape_index(path):
                uniq.setdefault(it["url"], it)
        items = list(uniq.values())

        terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 1]
        if terms:
            items = [it for it in items if any(t in it["title"].lower() for t in terms)]

        # Dated items newest-first; undated (e.g. DLCT talk list) after.
        items.sort(key=lambda it: it["date"] or "", reverse=True)

        docs: list[Document] = []
        for it in items[:limit]:
            is_talk = "/abs/" in it["url"]
            kind = "DLCT reading-group talk" if is_talk else f"ML Collective {it['source_path'].strip('/')}"
            when = f" ({it['date']})" if it["date"] else ""
            dt = None
            if it["date"]:
                y, m, d = (int(x) for x in it["date"].split("-"))
                dt = datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc)
            docs.append(Document(
                source="ml_collective",
                source_id=it["url"],
                url=it["url"],
                title=it["title"],
                content=f"{kind}{when}",
                date=dt,
                tags=["ml-collective", it["source_path"].strip("/")] + (["dlct-talk"] if is_talk else []),
                metadata={"index_path": it["source_path"], "date_str": it["date"], "raw": jsonsafe(it)},
            ))

        cache.set_docs(key, docs, ttl=900)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "mlcollective.org" not in host:
            return None
        html = http.get_text(url, timeout=20)
        if html is None:
            return None

        soup = BeautifulSoup(html, "lxml")
        title_el = soup.find(["h1", "h2"]) or soup.title
        title = title_el.get_text(strip=True) if title_el else "(untitled)"
        main = soup.find("main") or soup.find("article") or soup.body
        body = main.get_text("\n", strip=True) if main else ""
        body = re.sub(r"\n{3,}", "\n\n", body).strip()

        return Document(
            source="ml_collective",
            source_id=url,
            url=url,
            title=title,
            content=body,
            tags=["ml-collective"],
            metadata={"raw": jsonsafe({"url": url, "title": title, "body": body})},
        )

    def health_check(self) -> tuple[bool, str]:
        # Honest health = the /events/ stream actually yields real dated items, not just HTTP 200.
        try:
            resp = httpx.get(BASE, headers={"User-Agent": UA}, timeout=10, follow_redirects=True)
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        items = self._scrape_index("/events/")
        dated = [it for it in items if it["date"]]
        if not dated:
            return False, "site up but /events/ yields 0 dated items (selector drift?)"
        return True, f"OK ({len(dated)} dated events; newest {max(it['date'] for it in dated)})"


from penumbra.core.fetcher import register_adapter

register_adapter(MLCollectiveAdapter())
