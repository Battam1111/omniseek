"""Shared RSS helpers for blog / newsletter style adapters.

Most of our Tier 2 sources publish content through standard RSS/Atom
feeds. Rather than reimplement the same scraping logic 5 times, this
module provides a base class that handles fetching, parsing, HTML→Markdown
conversion, basic relevance filtering, and caching.

Concrete adapters subclass `RSSAdapterBase` and just declare their
feeds + an optional content-extraction tweak.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import feedparser
from markdownify import markdownify as html_to_md

from omniseek.core import cache, diag, http, relevance
from omniseek.core.normalize import Document, jsonsafe, strip_base64_images

logger = logging.getLogger(__name__)

# Reasonable browser-like UA to avoid some basic 403s on RSS endpoints
DEFAULT_UA = "Mozilla/5.0 (compatible; OmniSeek/0.1; +https://github.com/cyj/omniseek)"
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
            from omniseek.core.curator.probe import _resolve_safe_ip
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
        return _parse_or_refuse(content, url)
    resp = http.get(url, timeout=FETCH_TIMEOUT, headers=accept)
    if resp is None:
        return None
    return _parse_or_refuse(resp.content, url, status=resp.status_code)


def _parse_or_refuse(content, url: str,
                     status: Optional[int] = None) -> Optional[feedparser.FeedParserDict]:
    """Parse feed bytes; REFUSE a body that is not a feed at all (challenge/error page with 200).

    feedparser turns ANY HTML page into a complaint-free zero-entry result, so a Cloudflare
    interstitial or maintenance page served with HTTP 200 used to read as "feed is empty":
    silent, and invisible to an armed diag capture. A real feed always carries a detected
    ``version`` (rss20/atom10/...), and a genuinely empty feed keeps its version too, so
    version-less + entry-less = not a feed: note it as egress evidence and fail to None
    (the adapter contract). Feeds WITH entries pass untouched however messy (bozo tolerated)."""
    try:
        parsed = feedparser.parse(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RSS parse failed for %s: %s", url, exc)
        return None
    if not parsed.entries and not (parsed.get("version") or "").strip():
        try:
            snippet = content if isinstance(content, str) else bytes(content[:400]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            snippet = ""
        logger.warning("RSS body is not a feed (challenge/error page?) for %s", url)
        diag.note("rss.fetch_feed", url=url, status=status, body=snippet[:400] or None)
        return None
    return parsed


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
                      feeds: Optional[list] = None) -> Optional[Document]:
    """Convert a feedparser entry to a Document."""
    title = (entry.get("title") or "(untitled)").strip()
    link = entry.get("link") or ""

    # Content: prefer full content, fall back to summary/description
    body_html = ""
    if entry.get("content"):
        body_html = entry.content[0].get("value", "") if entry.content else ""
    if not body_html:
        body_html = entry.get("summary") or entry.get("description") or ""

    try:
        body_md = strip_base64_images(html_to_md(body_html, heading_style="ATX").strip()) if body_html else ""
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

    return Document(
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
        """Fetch all feeds, return combined+deduped Documents (cached).

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
        # Each task runs in a COPY of the caller's contextvars context: an armed diag capture
        # (a drill's contextvar) does not cross ThreadPoolExecutor on its own, so worker-thread
        # failure notes used to vanish and a drill on an all-dead bundle reported captures: []
        # (the 2026-07-09 sg_immigration misdiagnosis). One copy per task (a single Context
        # cannot be entered concurrently); every copy shares the SAME trace list object, so
        # worker notes land in the caller's drain().
        tasks = [(u, contextvars.copy_context()) for u in self.feeds]
        with ThreadPoolExecutor(max_workers=min(len(self.feeds) or 1, 24)) as ex:
            results = list(ex.map(lambda t: (t[0], t[1].run(fetch_feed,
                t[0], guard_ip=self.guard_ip, impersonate=self.tls_impersonate)), tasks))

        # Aggregate + dedup + convert. Shared VERBATIM with the async twin (_afetch_all_docs) so the
        # sync and async paths can never drift; the sole caller-side difference is how `results` is
        # produced (thread pool here, concurrent coroutines there).
        docs, all_failed = self._aggregate_and_convert(results)
        cache.set_docs(key, docs, ttl=(min(300, self.cache_ttl) if all_failed else self.cache_ttl))
        return docs

    def _aggregate_and_convert(self, results) -> tuple[list[Document], bool]:
        """Aggregate + dedup across feeds, convert entries once, and compute the all-failed flag.

        Pure CPU (dedup + markdownify), no IO. `results` is a list of (feed_url, parsed_feed_or_None)
        tuples. Returns (docs, all_failed). This body was moved VERBATIM out of _fetch_all_docs so the
        sync path stays byte-identical; the async twin runs it OFF the loop for the same result."""
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
        docs: list[Document] = []
        for k in order:
            e = by_key[k]
            doc = entry_to_document(_DictAsObj(e["_entry"]), self.name,
                                    e["_feed_url"], e.get("_feeds"))
            if doc:
                docs.append(doc)
        # An ALL-FEEDS-FAILED empty is cached only briefly: caching it for the full TTL made a
        # later armed drill return the cached empty with ZERO egress (captures: [], a misleading
        # "parser?" note) for hours while the real wall sat upstream. 300s still bounds the
        # re-hammering of dead feeds; a partial or genuine empty keeps the normal TTL.
        all_failed = bool(self.feeds) and all(parsed is None for _, parsed in results)
        return docs, all_failed

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs_all = self._fetch_all_docs()  # already-converted Documents (cached)
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

    async def _afetch_feed(self, url: str):
        """Async twin of the per-feed fetch. A NORMAL feed goes native async (http.aget + the
        pure-CPU parse, no thread held during the network wait). An impersonate OR guard_ip feed
        runs the sync fetch_feed OFF the loop (curl_cffi has no async API; guard_ip does a blocking
        DNS resolve). Returns a parsed feed or None, the SAME contract as fetch_feed."""
        if self.guard_ip or self.tls_impersonate:
            return await anyio.to_thread.run_sync(
                functools.partial(fetch_feed, url, guard_ip=self.guard_ip,
                                  impersonate=self.tls_impersonate))
        accept = {"Accept": "application/rss+xml, application/xml, text/xml, */*"}
        resp = await http.aget(url, timeout=FETCH_TIMEOUT, headers=accept)
        if resp is None:
            return None
        return _parse_or_refuse(resp.content, url, status=resp.status_code)

    async def _afetch_all_docs(self) -> list[Document]:
        """Native-async twin of _fetch_all_docs: off-loop cache round-trip, feeds fetched as
        concurrent coroutines, and the heavy aggregate+convert off the loop. Shares
        _aggregate_and_convert + the cache TTL policy with the sync path so the two cannot drift."""
        key = cache.make_key(self.name, "all_docs")
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached

        # Fetch all feeds CONCURRENTLY. Every coroutine runs on the one loop thread, so (unlike the
        # sync ThreadPoolExecutor) NO contextvars copy is needed: an armed diag capture propagates
        # naturally and every feed's note lands in the caller's trace list. Native for normal feeds
        # (no thread held during the network wait); off-loop only for the impersonate/guard_ip few.
        async def _one(u):
            return (u, await self._afetch_feed(u))
        results = await asyncio.gather(*[_one(u) for u in self.feeds]) if self.feeds else []

        # Heavy pure CPU (dedup + markdownify at bundle scale) -> OFF the loop.
        docs, all_failed = await anyio.to_thread.run_sync(
            functools.partial(self._aggregate_and_convert, list(results)))
        await anyio.to_thread.run_sync(functools.partial(  # disk write OFF loop
            cache.set_docs, key, docs,
            ttl=(min(300, self.cache_ttl) if all_failed else self.cache_ttl)))
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. A plain RSSAdapterBase subclass (feeds
        only) inherits this and becomes async-capable: feeds fetched as concurrent coroutines, not a
        held thread pool, same shared BM25 as search -> byte-identical results given the same feeds.

        SAFETY GUARD (the load-bearing rule): a subclass that CUSTOMIZES search or _fetch_all_docs
        (e.g. fellowships keyword-gates the feed entries and appends a curated static shelf) inherits
        THIS asearch and is flagged AsyncSearchCapable, so the live async omniseek_search path would route
        here and SILENTLY DROP that customization. So when a subclass has overridden search or
        _fetch_all_docs, run its OWN sync search OFF the loop instead (correct + faithful; those few
        sources just do not go native async, which is fine). This makes every customizing subclass
        auto-safe with no per-subclass asearch override needed."""
        cls = type(self)
        if (cls.search is not RSSAdapterBase.search
                or cls._fetch_all_docs is not RSSAdapterBase._fetch_all_docs):
            return await anyio.to_thread.run_sync(functools.partial(self.search, query, limit))
        docs_all = await self._afetch_all_docs()
        if not docs_all:
            return []
        if not relevance.query_terms(query):
            return docs_all[:limit]
        scores = relevance.doc_scores(docs_all, query)
        scored = [(s, d) for s, d in zip(scores, docs_all) if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:limit]]

    def fetch_url(self, url: str) -> Optional[Document]:
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
            parsed_list = list(ex.map(lambda u: (u, fetch_feed(
                u, guard_ip=self.guard_ip, impersonate=self.tls_impersonate)), self.feeds))
        dead = [u for u, p in parsed_list if not (p and getattr(p, "entries", None))]
        n = len(self.feeds)
        ok = n - len(dead)
        if ok == 0:
            return False, f"all {n} feeds failed"
        if dead:
            # NAME the dead feeds: a bundle that silently loses a member (sg_immigration lost 1 of 2)
            # otherwise reads healthy forever. The "degraded" marker is what the watchdog keys on to
            # Bark a full->degraded transition, so member rot surfaces before ALL feeds die.
            deadhosts = ", ".join(sorted({urlparse(u).hostname or u for u in dead}))
            return True, f"{ok}/{n} feeds OK (degraded; dead: {deadhosts})"
        return True, f"OK ({ok} feeds)"


class _DictAsObj:
    """Wrap a dict to look like a feedparser entry (attr access)."""

    def __init__(self, d: dict):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getattr__(self, key):
        return self._d.get(key)
