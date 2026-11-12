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
import functools
import logging
import re
import time
import weakref
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import feedparser
from markdownify import markdownify as html_to_md

from omniseek.core import cache, diag, http, portal, relevance, upstreams
from omniseek.core.normalize import Document, html_fragment_text, jsonsafe, strip_base64_images
from omniseek.core.sources._base import SourceBase, aparse

logger = logging.getLogger(__name__)

# Reasonable browser-like UA to avoid some basic 403s on RSS endpoints
DEFAULT_UA = "Mozilla/5.0 (compatible; OmniSeek/0.1)"
FETCH_TIMEOUT = 15

# BOUNDED FEED FAN-OUT (OmniSeek redo item 4, 2026-10-10). Each RSS source used to start every one of
# its feeds at once, and a cold broad sweep runs dozens of RSS sources together: one measured cold
# query sent 2,210 feed requests into the shared async pool (128 connections). The pool sat full with
# 50 to 75 requests queued for seconds, the loop spent the sweep doing TLS handshakes for blog hosts,
# and the API sources the caller actually waits for (arxiv, crossref, alphaxiv) queued behind them
# and missed the broad deadline. So at most _FEED_CONCURRENCY feed fetches are in flight per event
# loop, leaving the other half of the pool to every non-feed source. The feeds still all get
# fetched: a sweep that cuts an RSS source at the deadline lets it keep warming its cache detached.
# 64 = half the shared pool (http._aget_client max_connections 128), provisional until measured
# against other values on a cold sweep. Per loop because asyncio primitives are loop-bound (the
# service runs one loop; a script that calls search through portal.call gets its own one).
_FEED_CONCURRENCY = 64
_feed_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())


def _feed_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _feed_sems.get(loop)
    if sem is None:
        sem = _feed_sems[loop] = asyncio.Semaphore(_FEED_CONCURRENCY)
    return sem


def _ip_guard_refuses(url: str) -> bool:
    """True when a guard_ip feed must be skipped: its host resolves to a blocked IP, or the guard
    itself is unavailable (fails CLOSED). Blocking (a DNS resolve): run on a worker thread."""
    host = urlparse(url).hostname or ""
    try:
        from omniseek.core.curator.probe import _resolve_safe_ip
        _ip, _fam, blocked = _resolve_safe_ip(host)
        if blocked:
            logger.warning("guarded RSS feed %s skipped: host resolves to a blocked IP (%s)",
                           url, blocked)
            return True
    except Exception as exc:  # noqa: BLE001: a guard failure must fail CLOSED (skip the feed)
        logger.warning("guarded RSS feed %s skipped: IP guard unavailable (%s)", url, exc)
        return True
    return False


def _dead_hosts(dead: list, captures: list) -> str:
    """The dead feeds' hosts, each with the HTTP status its last failure capture recorded, if any
    ("www.higheredjobs.com: HTTP 504"); an exception without a status leaves the bare host."""
    status: dict[str, int] = {}
    for rec in captures or ():
        st, url = rec.get("status"), rec.get("url")
        if isinstance(st, int) and url:
            status[urlparse(str(url)).hostname or str(url)] = st
    hosts = sorted({urlparse(u).hostname or u for u in dead})
    return ", ".join(f"{h}: HTTP {status[h]}" if h in status else h for h in hosts)


def _parse_quiet(content, url: str, status: Optional[int] = None):
    """Parse feed bytes; REFUSE a body that is not a feed at all (challenge/error page with 200):
    (parsed or None, the event to report or None).

    feedparser turns ANY HTML page into a complaint-free zero-entry result, so a Cloudflare
    interstitial or maintenance page served with HTTP 200 used to read as "feed is empty": silent,
    and invisible to an armed diag capture. A real feed always carries a detected ``version``
    (rss20/atom10/...), and a genuinely empty feed keeps its version too, so version-less +
    entry-less = not a feed: None plus a "not_feed" event (egress evidence). Feeds WITH entries pass
    untouched however messy (bozo tolerated). A worker of the process lane runs this; the event goes
    back to be reported (``_report``) in the caller's process, where the log and an armed diag
    capture are."""
    try:
        parsed = feedparser.parse(content)
    except Exception as exc:  # noqa: BLE001
        return None, ("parse_failed", url, str(exc))
    if not parsed.entries and not (parsed.get("version") or "").strip():
        try:
            snippet = content if isinstance(content, str) else bytes(content[:400]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            snippet = ""
        return None, ("not_feed", url, status, snippet[:400] or None)
    return parsed, None


def _report(event: tuple) -> None:
    if event[0] == "parse_failed":
        logger.warning("RSS parse failed for %s: %s", event[1], event[2])
        return
    _kind, url, status, snippet = event
    logger.warning("RSS body is not a feed (challenge/error page?) for %s", url)
    diag.note("rss.fetch_feed", url=url, status=status, body=snippet)


def _parse_bundle(name: str, feeds: list, raws: list) -> tuple[list[Document], bool, list]:
    """The async bundle's whole CPU part, in ONE ``aparse`` call (a worker of the process lane for a long
    parse, here for a short one): parse every fetched feed
    (``raws``: (feed url, body or None, status)), then aggregate + dedup + convert exactly as the sync
    path does. Returns (docs, all_failed, events to report in the caller's process)."""
    results, events = [], []
    for url, content, status in raws:
        parsed = None
        if content is not None:
            parsed, event = _parse_quiet(content, url, status)
            if event is not None:
                events.append(event)
        results.append((url, parsed))
    docs, all_failed = _aggregate(name, feeds, results)
    return docs, all_failed, events


def _live_feeds(raws: list) -> tuple[list[str], list]:
    """The health check's CPU part, in ONE ``aparse`` call: which fetched feeds (``raws``: (feed url,
    body or None, status)) parse with entries, and the events to report in the caller's process."""
    live, events = [], []
    for url, content, status in raws:
        if content is None:
            continue
        parsed, event = _parse_quiet(content, url, status)
        if event is not None:
            events.append(event)
        if parsed and getattr(parsed, "entries", None):
            live.append(url)
    return live, events


def _pair_scores(pairs: list, query: str) -> list[float]:
    """relevance.doc_scores over (title, content) pairs: what it reads of a document, all a worker of
    the process lane is sent (pickling whole documents would cost the loop more than the scoring)."""
    return relevance.doc_scores([_TitleContent(t, c) for t, c in pairs], query)


class _TitleContent:
    __slots__ = ("title", "content")

    def __init__(self, title, content):
        self.title, self.content = title, content


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
        body_md = html_fragment_text(body_html)

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
        content=body_md or "",
        author=author or None,
        date=date,
        tags=tags,
        media=_rss_media(raw_dict),
        metadata={"feed_url": feed_url, "raw": jsonsafe(raw_dict),
                  **({"feeds": feeds} if feeds and len(feeds) > 1 else {})},
    )


def _aggregate(name: str, bundle_feeds: list, results) -> tuple[list[Document], bool]:
    """Aggregate + dedup across feeds, convert entries once, and compute the all-failed flag.

    Pure CPU (dedup + markdownify), no IO. `results` is a list of (feed_url, parsed_feed_or_None)
    tuples. Returns (docs, all_failed). This body was moved VERBATIM out of _fetch_all_docs so the
    sync path stays byte-identical; the async twin runs it in a worker process (_parse_bundle)."""
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
        doc = entry_to_document(_DictAsObj(e["_entry"]), name,
                                e["_feed_url"], e.get("_feeds"))
        if doc:
            docs.append(doc)
    # An ALL-FEEDS-FAILED empty is cached only briefly: caching it for the full TTL made a
    # later armed drill return the cached empty with ZERO egress (captures: [], a misleading
    # "parser?" note) for hours while the real wall sat upstream. 300s still bounds the
    # re-hammering of dead feeds; a partial or genuine empty keeps the normal TTL.
    all_failed = bool(bundle_feeds) and all(parsed is None for _, parsed in results)
    return docs, all_failed


class RSSAdapterBase(SourceBase):
    """Base class for RSS/Atom-based source adapters.

    Subclass and set class-level attributes:
        name: str — source identifier
        description: str — human-readable description
        feeds: list[str] — RSS feed URLs to aggregate
        url_pattern: str | None — optional regex; if set, fetch_url uses it
                                  to decide whether this adapter claims a URL
        cache_ttl: int — cache duration in seconds (default 1800 = 30 min)

    The async path parses through ``sources._base.aparse``: the feeds are
    fetched as coroutines, then the whole bundle's feedparser + dedup + markdownify is ONE parse
    (``_parse_bundle``: a worker process when it would hold the loop past the budget), and BM25 runs in
    the PROCESS lane (``_pair_scores``, ``portal.run_cpu``). A subclass that adds its own
    CPU work on the async path sends it there the same way (fellowships' ``_pick``), never to a thread.
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

    def _bundle_ttl(self) -> float:
        """``cache_ttl``, never shorter than the longest Crawl-delay among the feed hosts: a bundle
        refreshed sooner than such a host may be asked again only buys a refused request (statmodeling,
        Crawl-delay 3600 s against the 1800 s cache: 68 of 85 refused)."""
        from omniseek.core import upstreams
        return upstreams.cache_ttl_for(self.cache_ttl, self.feeds)

    async def _afetch_feed(self, url: str):
        """Fetch one RSS/Atom feed, egress only: (body, HTTP status or None), or None on failure, the
        parse being _afetch_all_docs's. Through the shared ``http`` egress (one process-wide
        keep-alive client + the 30MB cap + the failure->None contract + follow-redirects), no thread
        held during the network wait.

        impersonate (opt-in per row; default OFF so the 143 in-tree sources are byte-identical): the
        feed host walls plain httpx by its TLS/JA3 fingerprint (PerimeterX/HUMAN, Cloudflare-TLS) but
        lets a real browser through, so fetch it through ``http.aget_impersonated`` (curl_cffi Chrome
        TLS) instead of ``http.aget``, then parse. A SEPARATE fetch tier under the heavy CDP browser.

        RSS shares the connection pool with the other open-API sources (one egress, less entropy):
        same UA (http.USER_AGENT == the old DEFAULT_UA) + the Accept header below.

        guard_ip (curator overlay-origin feeds ONLY; default OFF for the 143 in-tree sources, so their
        behavior is byte-identical): before fetching, resolve the feed host and reject it if ANY resolved
        IP is private/loopback/link-local/reserved (reusing the curator probe's SSRF validation + the
        deployment fake-IP-proxy allowance). An agent-auto-admitted feed (apply_live, never human-vetted)
        thus cannot become a blind SSRF on its recurring poll if its host later DNS-rebinds to an internal
        address. Lazy import keeps the sources layer free of an import-time curator dependency, and a
        guard failure fails CLOSED (skip the feed). Residual: a sub-second rebind race between this
        resolve and http.aget's own connect; full IP-pinning of the rss egress is the documented follow-up."""
        accept = {"Accept": "application/rss+xml, application/xml, text/xml, */*"}
        async with _feed_slot():
            if self.guard_ip and await anyio.to_thread.run_sync(_ip_guard_refuses, url,
                                                                limiter=portal.IO_LANE):
                return None
            if self.tls_impersonate:  # TLS/JA3-walled feed: curl_cffi (Chrome handshake), then parse
                content = await http.aget_impersonated(url, timeout=FETCH_TIMEOUT, headers=accept)
                return None if content is None else (content, None)
            resp = await http.aget(url, timeout=FETCH_TIMEOUT, headers=accept)
        if resp is None:
            return None
        return resp.content, resp.status_code

    async def _afetch_all_docs(self) -> list[Document]:
        """All feeds, combined + deduped Documents (cached): off-loop cache round-trip, feeds fetched
        as concurrent coroutines, and the heavy aggregate+convert in the process lane. HTML->Markdown
        conversion runs ONCE here at fetch time and the CONVERTED docs are cached, so a cache hit (every
        search within the TTL) does zero markdownify and zero HTML re-tokenization. A dead feed just
        drops out (degrade), never blocking the whole bundle."""
        key = cache.make_key(self.name, "all_docs")
        cached = await cache.aget_docs(key)  # disk read OFF loop; one fetch per bundle at a time
        if cached is not None:
            return cached

        # Fetch all feeds CONCURRENTLY. Every coroutine runs on the one loop thread, so NO contextvars
        # copy is needed: an armed diag capture propagates
        # naturally and every feed's note lands in the caller's trace list. No thread is held during
        # the network wait (a guard_ip feed's DNS check alone runs off-loop).
        async def _one(u):
            raw = await self._afetch_feed(u)
            return (u, None, None) if raw is None else (u, raw[0], raw[1])
        raws = await asyncio.gather(*[_one(u) for u in self.feeds]) if self.feeds else []

        # The bundle's CPU (feedparser over every feed, dedup, markdownify) is pure Python: ONE parse
        # through aparse, the process lane for a long one, never a thread of this process.
        docs, all_failed, events = await aparse(sum(len(r[1]) for r in raws if r[1]), _parse_bundle,
                                                self.name, list(self.feeds), list(raws))
        for event in events:
            _report(event)
        await anyio.to_thread.run_sync(functools.partial(  # disk write OFF loop
            cache.set_docs, key, docs,
            ttl=(min(300, self.cache_ttl) if all_failed else self._bundle_ttl())), limiter=portal.IO_LANE)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """The bundle (feeds fetched as concurrent coroutines, the parse in the process lane), then the
        shared BM25 (``relevance``: title 3x + content 1x, the SAME scorer search-ranking and
        ``keyword_score_filter`` use, so RSS can't drift), also in the process lane. A term-less query
        keeps the feed order. A subclass that customizes the search overrides this and reaches the
        bundle through ``super().asearch`` (fellowships)."""
        docs_all = await self._afetch_all_docs()
        if not docs_all:
            return []
        if not relevance.query_terms(query):
            return docs_all[:limit]
        # BM25 over the whole bundle is pure CPU: the process lane (same reason as the parse).
        scores = await portal.run_cpu(_pair_scores, [(d.title, d.content) for d in docs_all], query)
        scored = [(s, d) for s, d in zip(scores, docs_all) if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:limit]]

    async def afetch_url(self, url: str) -> Optional[Document]:
        if not self.url_pattern:
            return None
        host = urlparse(url).hostname or ""
        if not re.search(self.url_pattern, host):
            return None
        # Scan the (already-converted) cached docs for a matching link: the bundle the search reads.
        for doc in await self._afetch_all_docs():
            if doc.url == url:
                return doc
        return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        if not self.feeds:
            return False, "no feeds configured"
        # Every feed at once as coroutines on the loop (as _afetch_all_docs): total ≈ slowest feed, not
        # the sum, so a couple of slow/dead feeds can't time out the whole-bundle health probe. They
        # run in the health check's own context, so its ledger (_probe) sees a feed's 429 and an
        # all-rate-limited bundle reads "not verified", not "down". An armed failure capture (diag): a
        # dead feed is then named with what its host answered ("HTTP 504"), not only by its host
        # (2026-10-04; the curl tier notes it too).
        diag.enable()
        try:
            async def _one(u):
                raw = await self._afetch_feed(u)
                return (u, None, None) if raw is None else (u, raw[0], raw[1])
            raws = await asyncio.gather(*[_one(u) for u in self.feeds])
            live, events = await aparse(sum(len(r[1]) for r in raws if r[1]), _live_feeds, list(raws))
            for event in events:
                _report(event)
        finally:
            captures = diag.drain()
        # A feed whose request the host's declared gate held back (a Crawl-delay host the hourly ingest
        # had just asked: statmodeling's 3600 s) was not asked this run: it is not dead on that alone.
        # What the host last answered anyone in this process says what is known; a last answer that
        # was an error counts it dead, and its record carries the address (the exit-node sweep refetches
        # it); otherwise it is only named as held. Before 2026-11-09 every held feed read dead, so the
        # bundle went "degraded" with no address to refetch, and a real 403 seen only by the ingest
        # never reached the check.
        held_urls = {str(rec.get("url")) for rec in captures
                     if "request not sent" in str(rec.get("body") or "")}
        held: list[str] = []
        for u in self.feeds:
            if u in live or diag.strip_secrets(u) not in held_urls:
                continue
            last = upstreams.last_answer(urlparse(u).hostname or "")
            if last is None or last[0] < 400 or last[0] == 429:  # a rate limit is not a dead feed
                held.append(u)
                continue
            age = max(0, int(time.time() - last[1]))
            rec = {"helper": "rss.last_answer", "url": u, "status": last[0],
                   "body": f"last answer HTTP {last[0]} {age}s ago; this check's own request was held by the host's gate"}
            diag.note(rec["helper"], url=u, status=last[0], body=rec["body"])
            captures.append(rec)
        dead = [u for u in self.feeds if u not in live and u not in held]
        n = len(self.feeds)
        ok = n - len(dead) - len(held)
        held_note = (f"; held by the host's gate, not asked this run: {', '.join(sorted({urlparse(u).hostname or u for u in held}))}"
                     if held else "")
        if ok == 0 and not dead:
            return None, f"not verified: all {n} feeds held by their hosts' gates{held_note}"
        if ok == 0:
            # Name them here too. The degraded branch below already does, and a bundle that is
            # fully dead is exactly when the reader most needs to know WHICH hosts to go look at:
            # "all 2 feeds failed" sent the 2026-09-09 sweep hunting through our own adapter for
            # what turned out to be one unreachable host and one 403.
            return False, f"all {n - len(held)} feeds failed ({_dead_hosts(dead, captures)}){held_note}"
        if dead:
            # NAME the dead feeds: a bundle that silently loses a member (sg_immigration lost 1 of 2)
            # otherwise reads healthy forever. The "degraded" marker is what the watchdog keys on to
            # Bark a full->degraded transition, so member rot surfaces before ALL feeds die.
            return True, f"{ok}/{n} feeds OK (degraded; dead: {_dead_hosts(dead, captures)}){held_note}"
        return True, f"OK ({ok} feeds{held_note})"


class _DictAsObj:
    """Wrap a dict to look like a feedparser entry (attr access)."""

    def __init__(self, d: dict):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getattr__(self, key):
        return self._d.get(key)
