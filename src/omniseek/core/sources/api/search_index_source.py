"""Search-index sources — reach WALLED venues (Blind / Glassdoor / X / 脉脉 / LinkedIn)
via a ``site:``-scoped web search, NOT by scraping the venue itself.

Each row in ``search_index_sites.json`` becomes one adapter. All are ``explicit_only``:
every call fires an external search-engine query, so they stay OUT of the broad
fan-out — name them in ``sources=[...]`` (or omniseek_search 单源钻取). The result is the engine's
indexed title + URL + snippet → Document (snippet is the content; the venue
itself is login/JS-walled, so the snippet is the reachable signal).

ToS posture: we read the search engine, never the walled site with our UA — this is
the whole point (sidesteps the target's robots.txt / login wall / anti-bot).
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import anyio

from omniseek.core.normalize import Document, jsonsafe
from omniseek.core.sources.api._search_backend import asearch_web, backend_ping, search_web

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


def _url_cache_key(source: str, url: str) -> str:
    """Stable key for replaying the search-index snapshot through ``omniseek_read``."""
    from omniseek.core import cache
    return cache.make_key("search_index", source, "url", url)


def _store_url_snapshots(source: str, docs: list[Document], ttl: int = 1800) -> None:
    """Persist the exact indexed snapshot for each returned URL.

    Search-index venues are intentionally snippet-only. Keeping the snapshot by URL lets a
    subsequent ``omniseek_read`` return that reachable evidence without accidentally driving the
    venue's logged-in browser adapter.
    """
    from omniseek.core import cache

    for doc in docs:
        cache.set(_url_cache_key(source, doc.url), doc.model_dump(mode="json"), ttl=ttl)


def _is_tombstone(content: str) -> bool:
    c = (content or "").strip()
    if c.startswith(_BOILERPLATE_PREFIXES):  # generic meta-description, no real page content
        return True
    return len(c) < 120 and bool(_TOMBSTONE_RE.search(c))


class _SearchVenue:
    """A walled venue reached via site:-scoped web search (configured from a data row)."""

    needs_credentials = False
    kind = "proxy"
    fetch_url_class = "search-index"

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
        self.fetch_url_hosts = (site.split("/", 1)[0],)

    def search(self, query: str, limit: int = 10) -> list[Document]:
        from omniseek.core import cache  # local import: avoid import cycle
        q = f"site:{self.site} {(query or '').strip()}".strip()
        if self.extra:
            q = f"{q} {self.extra}"
        ck = cache.make_key("search_index", self.name, q, limit)
        cached = cache.get_docs(ck)  # honors the fresh-flag (returns None when fresh=True)
        if cached is not None:
            # A URL filter is part of the source contract. If the contract was tightened after
            # this disk snapshot was written, a blind cache hit would keep returning obsolete
            # navigation URLs until TTL expiry. Treat that snapshot as stale and re-query once.
            if not self.url_filter or all(self.url_filter.search(doc.url) for doc in cached):
                return cached
            cached = None
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
                metadata={"site": self.site, "via": "search-index", "read_depth": "search-index-snippet",
                          "body_needs_read": True, "raw": jsonsafe(r)},
            ))
            if len(docs) >= limit:
                break
        # Cache results AND empties — an uncached empty re-burns the search backend on
        # every retry (the #1 Brave-quota drain). Empties get a shorter TTL so a
        # transiently-empty venue recovers sooner; walled content is slow-moving.
        cache.set_docs(ck, docs, ttl=1800 if docs else 600)
        if docs:
            _store_url_snapshots(self.name, docs)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> AsyncSearchCapable (routes to the fetcher's native
        dispatch branch). Mirrors ``search`` line-for-line, changing ONLY the blocking waits:
          - the disk CACHE read/write go OFF the loop (anyio.to_thread.run_sync, SAME cache key);
          - the egress ``search_web`` -> its async twin ``asearch_web`` (SAME Brave 1qps gate +
            cooldown breaker, shared with the sync path via one module-global ledger);
          - the query build + url_filter/tombstone parse + Document map are PURE CPU, kept ON
            the loop, byte-identical to ``search`` (no fan-out here — a single search_web call — so no
            gather). sync code untouched."""
        from omniseek.core import cache  # local import: avoid import cycle
        q = f"site:{self.site} {(query or '').strip()}".strip()
        if self.extra:
            q = f"{q} {self.extra}"
        ck = cache.make_key("search_index", self.name, q, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, ck)  # disk read OFF loop (honors fresh-flag)
        if cached is not None:
            if not self.url_filter or all(self.url_filter.search(doc.url) for doc in cached):
                return cached
            cached = None
        # Always over-fetch: the engine bills per QUERY (not per result), and both
        # filters below drop docs — url_filter nav/login shells (e.g. xiaohongshu
        # pro./job. pages) and tombstone shells (moved/removed-page snapshots).
        n = min(max(limit * 3, limit + 5), 20)
        docs: list[Document] = []
        for r in await asearch_web(q, n=n):  # async egress; parse below is pure CPU, on the loop
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
                metadata={"site": self.site, "via": "search-index", "read_depth": "search-index-snippet",
                          "body_needs_read": True, "raw": jsonsafe(r)},
            ))
            if len(docs) >= limit:
                break
        # Cache results AND empties — an uncached empty re-burns the search backend on
        # every retry (the #1 Brave-quota drain). Empties get a shorter TTL so a
        # transiently-empty venue recovers sooner; walled content is slow-moving.
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, ck, docs, ttl=1800 if docs else 600))
        if docs:
            await anyio.to_thread.run_sync(_store_url_snapshots, self.name, docs)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        # Search-only: replay the exact indexed snapshot when the URL came from this adapter.
        # Never fall through to an account/CDP adapter for this source: that would violate the
        # zero-device-risk contract and would turn a known snippet into an unrelated empty shell.
        parsed = urlparse(url)
        if parsed.hostname and self.site not in parsed.hostname:
            return None
        if self.url_filter and not self.url_filter.search(url):
            return None
        from omniseek.core import cache
        cached = cache.get(_url_cache_key(self.name, url))
        if cached is not None:
            return Document.model_validate(cached)
        return None

    def health_check(self) -> tuple[bool, str]:
        return backend_ping()


def _load() -> None:
    from omniseek.core.fetcher import register_adapter
    for row in json.loads(_DATA.read_text(encoding="utf-8")):
        register_adapter(_SearchVenue(
            name=row["name"], description=row["description"], site=row["site"],
            extra=row.get("extra", ""), url_filter=row.get("url_filter", ""),
            explicit_only=row.get("explicit_only", False),
            domains=row.get("domains"), regions=row.get("regions"),
        ))


_load()
