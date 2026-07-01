"""Shared RSS helpers for blog / newsletter style adapters.

Most of our Tier 2 sources publish content through standard RSS/Atom
feeds. Rather than reimplement the same scraping logic 5 times, this
module provides a base class that handles fetching, parsing, HTML→Markdown
conversion, basic relevance filtering, and caching.

Concrete adapters subclass `RSSAdapterBase` and just declare their
feeds + an optional content-extraction tweak.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import feedparser
from markdownify import markdownify as html_to_md

from penumbra.core import cache, http, relevance
from penumbra.core.normalize import PolarisDocument, jsonsafe

logger = logging.getLogger(__name__)

# Reasonable browser-like UA to avoid some basic 403s on RSS endpoints
DEFAULT_UA = "Mozilla/5.0 (compatible; PolarisEye/0.1; +https://github.com/cyj/polaris)"
FETCH_TIMEOUT = 15


def fetch_feed(url: str, *, guard_ip: bool = False,
               impersonate: bool = False) -> Optional[feedparser.FeedParserDict]:
    """Fetch and parse an RSS/Atom feed. Returns None on failure.

    impersonate (opt-in per row; default OFF so the 143 in-tree sources are byte-identical): the
    feed host walls plain httpx by its TLS/JA3 fingerprint (PerimeterX/HUMAN, Cloudflare-TLS) but
    lets a real browser through, so fetch it through ``http.get_impersonated`` (curl_cffi Chrome
    TLS) instead of ``http.get``, then parse. A SEPARATE fetch tier under the heavy CDP browser.

    Routes through the shared pooled ``http`` egress (one process-wide keep-alive client +
    the 30MB cap + the failure->None contract + follow-redirects) instead of a bare per-call
    httpx.get, so RSS shares the connection pool with the other open-API sources (one egress,
    less entropy). Same UA (http.USER_AGENT == the old DEFAULT_UA) + same Accept header.

    guard_ip (curator overlay-origin feeds ONLY; default OFF for the 143 in-tree sources, so their
    behavior is byte-identical): before fetching, resolve the feed host and reject it if ANY resolved
    IP is private/loopback/link-local/reserved (reusing the curator probe's SSRF validation + the
    deployment fake-IP-proxy allowance). An agent-auto-admitted feed (apply_live, never human-vetted)
    thus cannot become a blind SSRF on its recurring poll if its host later DNS-rebinds to an internal
    address. Lazy import keeps the sources layer free of an import-time curator dependency, and a
    guard failure fails CLOSED (skip the feed). Residual: a sub-second rebind race between this
    resolve and http.get's own connect; full IP-pinning of the rss egress is the documented follow-up."""
    if guard_ip:
        host = urlparse(url).hostname or ""
        try:
            from penumbra.core.curator.probe import _resolve_safe_ip
            _ip, _fam, blocked = _resolve_safe_ip(host)
            if blocked:
                logger.warning("guarded RSS feed %s skipped: host resolves to a blocked IP (%s)",
                               url, blocked)
                return None
        except Exception as exc:  # noqa: BLE001: a guard failure must fail CLOSED (skip the feed)
            logger.warning("guarded RSS feed %s skipped: IP guard unavailable (%s)", url, exc)
            return None
    accept = {"Accept": "application/rss+xml, application/xml, text/xml, */*"}
    if impersonate:  # TLS/JA3-walled feed: fetch via curl_cffi (Chrome handshake), then parse
        content = http.get_impersonated(url, timeout=FETCH_TIMEOUT, headers=accept)
        if content is None:
            return None
        try:
            return feedparser.parse(content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RSS parse failed for %s: %s", url, exc)
            return None
    resp = http.get(url, timeout=FETCH_TIMEOUT, headers=accept)
    if resp is None:
        return None
    try:
        return feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RSS parse failed for %s: %s", url, exc)
        return None


def _rss_media(entry: dict) -> list[str]:
    """Image/video URLs from an RSS entry (media_content / thumbnail / image enclosures)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(u) -> None:
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)

    for mc in (entry.get("media_content") or []):
        if isinstance(mc, dict):
            _add(mc.get("url"))
    for mt in (entry.get("media_thumbnail") or []):
        if isinstance(mt, dict):
            _add(mt.get("url"))
    for enc in (entry.get("enclosures") or entry.get("links") or []):
        if isinstance(enc, dict) and "image" in (enc.get("type") or ""):
            _add(enc.get("href") or enc.get("url"))
    return out


def entry_to_document(entry, source_name: str, feed_url: str,
                      feeds: Optional[list] = None) -> Optional[PolarisDocument]:
    """Convert a feedparser entry to a PolarisDocument."""
    title = (entry.get("title") or "(untitled)").strip()
    link = entry.get("link") or ""

    # Content: prefer full content, fall back to summary/description
    body_html = ""
    if entry.get("content"):
        body_html = entry.content[0].get("value", "") if entry.content else ""
    if not body_html:
        body_html = entry.get("summary") or entry.get("description") or ""

    try:
        body_md = html_to_md(body_html, heading_style="ATX").strip() if body_html else ""
    except Exception:  # noqa: BLE001 — markdownify can be picky on weird HTML
        body_md = re.sub(r"<[^>]+>", "", body_html).strip()

    # Date
    date = None
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                date = datetime(*struct[:6], tzinfo=timezone.utc)
                break
            except (ValueError, TypeError):
                pass

    # Author
    author = entry.get("author")
    if not author and entry.get("authors"):
        author = ", ".join(a.get("name", "") for a in entry.authors if a.get("name"))

    # Tags
    tags: list[str] = []
    if entry.get("tags"):
        tags = [t.get("term", "") for t in entry.tags if t.get("term")]

    # Source ID: prefer guid/id, fallback to link
    source_id = entry.get("id") or entry.get("guid") or link

    if not link:
        return None

    # Lossless escape hatch + media — the data-layer guarantee (Phase 2b).
    raw_dict = entry._d if isinstance(entry, _DictAsObj) else dict(entry)

    return PolarisDocument(
        source=source_name,
        source_id=source_id,
        url=link,
        title=title,
        content=body_md or "(no content)",
        author=author or None,
        date=date,
        tags=tags,
        media=_rss_media(raw_dict),
        metadata={"feed_url": feed_url, "raw": jsonsafe(raw_dict),
                  **({"feeds": feeds} if feeds and len(feeds) > 1 else {})},
    )


class RSSAdapterBase:
    """Base class for RSS/Atom-based source adapters.

    Subclass and set class-level attributes:
        name: str — source identifier
        description: str — human-readable description
        feeds: list[str] — RSS feed URLs to aggregate
        url_pattern: str | None — optional regex; if set, fetch_url uses it
                                  to decide whether this adapter claims a URL
        cache_ttl: int — cache duration in seconds (default 1800 = 30 min)
    """

    name: str = ""
    description: str = ""
    needs_credentials = False
    kind = "stream"
    feeds: list[str] = []
    url_pattern: Optional[str] = None
    cache_ttl: int = 1800
    guard_ip: bool = False  # curator overlay-origin feeds set True -> per-fetch SSRF IP-revalidation
    tls_impersonate: bool = False  # opt-in: fetch feeds via curl_cffi (Chrome TLS) for JA3-walled hosts

    def _fetch_all_docs(self) -> list:
        """Fetch all feeds, return combined+deduped PolarisDocuments (cached).

        HTML→Markdown conversion (entry_to_document) runs ONCE here at fetch time and the
        CONVERTED docs are cached — so a cache hit (every search within the TTL) does zero
        markdownify and zero HTML re-tokenization. (Previously the raw entries were cached and
        every search re-converted + re-scored the raw HTML — the bulk of the per-broad CPU;
        the SE-style scrape sources already cached docs, RSS now matches.)"""
        key = cache.make_key(self.name, "all_docs")
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        # Fetch feeds in PARALLEL: a bundle can hold ~20 feeds, and sequential fetching let a few
        # slow/dead feeds SUM past the search/health deadline (the substack_matrix >25s timeout).
        # All-parallel makes the total ≈ the slowest single feed (per-feed FETCH_TIMEOUT), not the
        # sum; a dead feed just drops out (degrade), never blocking the whole bundle.
        with ThreadPoolExecutor(max_workers=min(len(self.feeds) or 1, 24)) as ex:
            results = list(ex.map(lambda u: (u, fetch_feed(
                u, guard_ip=self.guard_ip, impersonate=self.tls_impersonate)), self.feeds))

        # Aggregate + DEDUP across feeds. A bundle can cross-list the SAME item in several feeds
        # (e.g. Nature Careers lists one job in EVERY per-countrycode RSS), which without this
        # returned N identical copies. Collapse by canonical id (guid/id, else link sans tracking
        # query), keep the first occurrence, and record every feed the item appeared in.
        by_key: dict[str, dict] = {}
        order: list[str] = []
        for url, parsed in results:
            if parsed is None:
                continue
            for entry in parsed.entries:  # plain dicts so the cache stays JSON-serializable
                ed = dict(entry)
                link = ed.get("link") or ""
                dkey = ed.get("id") or ed.get("guid") or (link.split("?")[0] or None)
                if not dkey:
                    dkey = f"_nokey_{len(order)}"  # no stable id: never collapse with others
                existing = by_key.get(dkey)
                if existing is not None:
                    feeds = existing.setdefault("_feeds", [existing["_feed_url"]])
                    if url not in feeds:
                        feeds.append(url)
                    continue
                by_key[dkey] = {"_feed_url": url, "_entry": ed}
                order.append(dkey)
        # Convert ONCE here (markdownify happens at fetch time, not on every read).
        docs: list[PolarisDocument] = []
        for k in order:
            e = by_key[k]
            doc = entry_to_document(_DictAsObj(e["_entry"]), self.name,
                                    e["_feed_url"], e.get("_feeds"))
            if doc:
                docs.append(doc)
        cache.set_docs(key, docs, ttl=self.cache_ttl)
        return docs

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        docs_all = self._fetch_all_docs()  # already-converted PolarisDocuments (cached)
        if not docs_all:
            return []

        # Score the converted docs via the shared BM25 engine (relevance.doc_scores: title
        # 3x + content 1x — the SAME scorer search-ranking + keyword_score_filter use, so RSS
        # can't drift). Scoring clean markdown (not raw HTML) is cheaper AND removes the
        # tag-token noise that used to distort doclen. A term-less query keeps the feed order.
        if not relevance.query_terms(query):
            return docs_all[:limit]

        scores = relevance.doc_scores(docs_all, query)
        scored = [(s, d) for s, d in zip(scores, docs_all) if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:limit]]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        if not self.url_pattern:
            return None
        host = urlparse(url).hostname or ""
        if not re.search(self.url_pattern, host):
            return None
        # Scan the (already-converted) cached docs for a matching link.
        for doc in self._fetch_all_docs():
            if doc.url == url:
                return doc
        return None

    def health_check(self) -> tuple[bool, str]:
        if not self.feeds:
            return False, "no feeds configured"
        # Parallel probe (same reason as _fetch_all_entries): total ≈ slowest feed, not the sum,
        # so a couple of slow/dead feeds can't time out the whole-bundle health probe.
        with ThreadPoolExecutor(max_workers=min(len(self.feeds), 24)) as ex:
            parsed_list = list(ex.map(lambda u: fetch_feed(
                u, guard_ip=self.guard_ip, impersonate=self.tls_impersonate), self.feeds))
        ok = sum(1 for p in parsed_list if p and getattr(p, "entries", None))
        n = len(self.feeds)
        if ok == 0:
            return False, f"all {n} feeds failed"
        if ok < n:
            return True, f"{ok}/{n} feeds OK (degraded; {n - ok} slow/dead)"
        return True, f"OK ({ok} feeds)"


class _DictAsObj:
    """Wrap a dict to look like a feedparser entry (attr access)."""

    def __init__(self, d: dict):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getattr__(self, key):
        return self._d.get(key)
