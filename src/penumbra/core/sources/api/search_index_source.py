"""Search-index sources — reach WALLED venues (Blind / Glassdoor / X / 脉脉 / LinkedIn)
via a ``site:``-scoped web search, NOT by scraping the venue itself.

Each row in ``search_index_sites.json`` becomes one adapter. All are ``explicit_only``:
every call fires an external search-engine query, so they stay OUT of the broad
fan-out — name them in ``sources=[...]`` (or penumbra_fetch). The result is the engine's
indexed title + URL + snippet → Document (snippet is the content; the venue
itself is login/JS-walled, so the snippet is the reachable signal).

ToS posture: we read the search engine, never the walled site with our UA — this is
the whole point (sidesteps the target's robots.txt / login wall / anti-bot).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.api._search_backend import backend_ping, search_web

logger = logging.getLogger(__name__)
_DATA = Path(__file__).with_name("search_index_sites.json")

# Index-snapshot tombstones: when a venue migrates URLs (teamblind did in 2026),
# the engine's crawler captures the redirect/removed-page SHELL, and that shell
# text becomes the snippet — i.e. the doc's entire content. Such docs are pure
# noise (the real thread text was never indexed at that URL). Match VERIFIED
# shell phrasings only (don't guess patterns), and only when the shell text IS
# the whole snippet (length guard) — a real post merely *mentioning* a moved
# page stays. Extend the pattern as new venues' shells are actually observed.
_TOMBSTONE_RE = re.compile(
    r"this page has moved|page not found|automatic redirect", re.IGNORECASE)

# Generic site-description boilerplate some engines return as the snippet for a DEEP page
# they could not index (e.g. Brave serves zhihu's homepage meta for gated /question/ pages).
# That snippet is the doc's whole content → pure noise, drop it. Junk regardless of length, so
# this is checked BEFORE the tombstone length guard. Verified openings only (extend as seen).
_BOILERPLATE_PREFIXES = (
    "知乎，让每一次点击",
    "知乎，中文互联网高质量",
)


def _is_tombstone(content: str) -> bool:
    c = (content or "").strip()
    if c.startswith(_BOILERPLATE_PREFIXES):  # generic meta-description, no real page content
        return True
    return len(c) < 120 and bool(_TOMBSTONE_RE.search(c))


class _SearchVenue:
    """A walled venue reached via site:-scoped web search (configured from a data row)."""

    needs_credentials = False
    kind = "proxy"

    def __init__(self, name: str, description: str, site: str, extra: str = "",
                 url_filter: str = "", explicit_only=False, domains=None, regions=None) -> None:
        self.name = name
        self.description = description
        self.site = site
        self.extra = extra  # optional default keywords appended to every query
        self.url_filter = re.compile(url_filter) if url_filter else None  # keep only matching URLs (drop nav/login shells)
        self.explicit_only = explicit_only  # row-declared: True / reason string / absent
        self.domains = domains or []
        self.regions = regions or []

    def search(self, query: str, limit: int = 10) -> list[Document]:
        from penumbra.core import cache  # local import: avoid import cycle
        q = f"site:{self.site} {(query or '').strip()}".strip()
        if self.extra:
            q = f"{q} {self.extra}"
        ck = cache.make_key("search_index", self.name, q, limit)
        cached = cache.get_docs(ck)  # honors the fresh-flag (returns None when fresh=True)
        if cached is not None:
            return cached
        # Always over-fetch: the engine bills per QUERY (not per result), and both
        # filters below drop docs — url_filter nav/login shells (e.g. xiaohongshu
        # pro./job. pages) and tombstone shells (moved/removed-page snapshots).
        n = min(max(limit * 3, limit + 5), 20)
        docs: list[Document] = []
        for r in search_web(q, n=n):
            url = r.get("url")
            if not url:
                continue
            if self.url_filter and not self.url_filter.search(url):
                continue
            if _is_tombstone(r.get("snippet") or ""):
                logger.info("%s: dropped tombstone shell %s", self.name, url)
                continue
            docs.append(Document(
                source=self.name,
                source_id=url,
                url=url,
                title=r.get("title") or "(untitled)",
                content=r.get("snippet") or "(snippet only — open the URL for the full thread; venue may be login-walled)",
                tags=[self.name, "search-index"],
                metadata={"site": self.site, "via": "search-index", "raw": jsonsafe(r)},
            ))
            if len(docs) >= limit:
                break
        # Cache results AND empties — an uncached empty re-burns the search backend on
        # every retry (the #1 Brave-quota drain). Empties get a shorter TTL so a
        # transiently-empty venue recovers sooner; walled content is slow-moving.
        cache.set_docs(ck, docs, ttl=1800 if docs else 600)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        # Search-only: the venue itself is login/JS-walled, so we index via search, not direct fetch.
        return None

    def health_check(self) -> tuple[bool, str]:
        return backend_ping()


def _load() -> None:
    from penumbra.core.fetcher import register_adapter
    for row in json.loads(_DATA.read_text(encoding="utf-8")):
        register_adapter(_SearchVenue(
            name=row["name"], description=row["description"], site=row["site"],
            extra=row.get("extra", ""), url_filter=row.get("url_filter", ""),
            explicit_only=row.get("explicit_only", False),
            domains=row.get("domains"), regions=row.get("regions"),
        ))


_load()
