"""Transformer Circuits Thread — Anthropic interpretability research index.

transformer-circuits.pub is a chronological index of mechanistic
interpretability research from Anthropic's interpretability team. No
official RSS feed exists, so we scrape the index page directly.

Index structure (as of 2026-05):
- Reverse-chronological listing
- Article links arranged in date-grouped sections
- Each entry: title link + author attribution + brief description
- Updates roughly monthly (the "Circuits Updates" series + occasional
  big papers like the Mathematical Framework series, Toy Models of
  Superposition, etc.)

The site itself has no JS — pure static HTML, cheap to scrape.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from omniseek.core import cache, http
from omniseek.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

INDEX_URL = "https://transformer-circuits.pub/"
TIMEOUT = 15
USER_AGENT = "omniseek/0.1 (interpretability research aggregator)"

# Articles live under year-prefixed paths like /2026/march-update/index.html
# or /2024/scaling-monosemanticity/index.html. Skip nav links and external.
_ARTICLE_HREF_RE = re.compile(r"^/?\d{4}/[^/]+/(?:index\.html)?$|^/?\d{4}/[a-z-]+\.html$")
_DATE_FROM_URL_RE = re.compile(r"/(\d{4})/([a-z-]+|\d{1,2})")
_MONTH_NAME_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_from_url(href: str) -> Optional[datetime]:
    m = _DATE_FROM_URL_RE.search(href)
    if not m:
        return None
    year = int(m.group(1))
    second = m.group(2).lower()
    month = 1
    if second.isdigit():
        try:
            month = max(1, min(12, int(second)))
        except ValueError:
            month = 1
    else:
        # Look for any month name fragment in the slug (e.g., "march-update")
        for fragment in second.split("-"):
            if fragment in _MONTH_NAME_MAP:
                month = _MONTH_NAME_MAP[fragment]
                break
    try:
        return datetime(year, month, 1, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class TransformerCircuitsAdapter:
    name = "transformer_circuits"
    needs_credentials = False
    description = "Transformer Circuits Thread — Anthropic mechanistic interpretability research"

    def _fetch_index(self) -> list[dict]:
        key = cache.make_key("transformer_circuits", "index", "v1")
        cached = cache.get(key)
        if cached is not None:
            return cached

        html = http.get_text(INDEX_URL, timeout=TIMEOUT)
        if html is None:
            return []

        soup = BeautifulSoup(html, "lxml")
        entries: list[dict] = []
        seen_urls: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue
            # External links — skip
            if href.startswith("http") and "transformer-circuits.pub" not in href:
                continue
            # Match the article path pattern
            relative = href.replace("https://transformer-circuits.pub", "").lstrip("/")
            if not _ARTICLE_HREF_RE.match("/" + relative):
                continue

            full_url = urljoin(INDEX_URL, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            title = a.get_text(strip=True)
            if len(title) < 6:
                continue

            # Surrounding context
            parent = a.find_parent(["p", "li", "div", "section"])
            context = parent.get_text(" ", strip=True) if parent else title
            context = re.sub(r"\s+", " ", context)[:600]

            date = _parse_date_from_url(href)
            entries.append({
                "url": full_url,
                "title": title,
                "date": date.isoformat() if date else None,
                "context": context,
            })

        cache.set(key, entries, ttl=3600)
        return entries

    def search(self, query: str, limit: int = 10) -> list[Document]:
        entries = self._fetch_index()
        if not entries:
            return []

        query_terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 1]
        if not query_terms:
            scored = [(0, e) for e in entries[:limit]]
        else:
            scored = []
            for e in entries:
                blob = (e["title"] + " " + e["context"]).lower()
                title_score = sum(e["title"].lower().count(t) for t in query_terms) * 3
                body_score = sum(blob.count(t) for t in query_terms)
                score = title_score + body_score
                if score > 0:
                    scored.append((score, e))
            scored.sort(key=lambda x: x[0], reverse=True)

        docs: list[Document] = []
        for score, e in scored[:limit]:
            date = None
            if e.get("date"):
                try:
                    date = datetime.fromisoformat(e["date"])
                except ValueError:
                    pass
            doc = Document(
                source="transformer_circuits",
                source_id=e["url"],
                url=e["url"],
                title=e["title"],
                content=e["context"] or "(no context)",
                author="Anthropic Interpretability Team",
                date=date,
                tags=["mechanistic-interpretability"],
                metadata={"raw": jsonsafe(e)},
            )
            docs.append(doc)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "transformer-circuits.pub" not in host:
            return None
        for e in self._fetch_index():
            if e["url"] == url:
                date = None
                if e.get("date"):
                    try:
                        date = datetime.fromisoformat(e["date"])
                    except ValueError:
                        pass
                return Document(
                    source="transformer_circuits",
                    source_id=url,
                    url=url,
                    title=e["title"],
                    content=e["context"] or "(no context)",
                    author="Anthropic Interpretability Team",
                    date=date,
                    tags=["mechanistic-interpretability"],
                    metadata={"raw": jsonsafe(e)},
                )
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                INDEX_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
                follow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.text) > 1000:
                entries = self._fetch_index()
                return True, f"OK ({len(entries)} articles indexed)"
            return False, f"HTTP {resp.status_code}, len={len(resp.text)}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from omniseek.core.fetcher import register_adapter

register_adapter(TransformerCircuitsAdapter())
