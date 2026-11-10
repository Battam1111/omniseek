"""Shared base for non-RSS HTML/JSON scrape adapters (the regular many).

`_rss.py::RSSAdapterBase` is the golden template-method base for feed sources;
this is its sibling for the *other* scrape shape — sources that hit a search
endpoint (a public API like Academia Stack Exchange, or a query-able HTML/JSON
search page) and parse the response. Roughly a dozen scrape adapters share the
identical ritual:

    async def asearch(self, query, limit):
        key = cache.make_key(name, "search", query, limit)
        cached = await <the cache, off the loop>
        if cached is not None: return cached
        raw = await <fetch the query-specific URL/API>
        if raw is None: return []
        docs = [<parse each item> ...]
        <cache.set_docs(key, docs, ttl), off the loop>
        return docs

This base owns that mechanism (cache check / build / atomic set_docs / optional
shared-BM25 rank / self-registration); a concrete adapter declares a few class
attributes and fills TWO hooks:

    async _araw_fetch(query, limit) -> Any | None   # the network call → parsed payload
    _to_documents(raw, query, limit) -> list[Document]   # payload → docs

The search is written once, async (``asearch`` here); the sync ``search`` for scripts is
``sources._base.SourceBase``'s, generated from it. A source whose fetch can only block writes
``def _raw_fetch`` instead of ``_araw_fetch`` and the base runs it on a worker thread.

It is a CONVENIENCE, never a mandate. The Protocol (duck-typed
``fetcher.SourceAdapter``) stays the contract; a source with bespoke needs
(multi-host fan-out, signed requests, GBK search encoding, anti-bot headers)
keeps writing asearch/afetch_url/ahealth_check by hand. This base only serves the
regular, single-endpoint majority.

Design rules it obeys (the moat):
  * It carries NO judgment — ranking is the ONE shared scorer
    (``relevance.doc_scores``: title 3x + content 1x), the same engine RSS,
    search-ranking, and ``keyword_score_filter`` use, so a base-using source can
    never drift. Ranking is OPT-IN per source (``rank`` class attr) precisely so
    a source whose endpoint already returns server-relevance order (e.g. an API
    with ``sort=relevance``) stays byte-identical to its hand-written form.
  * ``explicit_only`` self-declaration, atomic cache writes (via cache.set_docs),
    and the failure→empty contract (the fetch hook returns None → ``asearch`` →
    ``[]``) all pass straight through.
"""

from __future__ import annotations

import functools
import importlib
import logging
from typing import Any, Optional

import anyio

from omniseek.core import cache, diag, fetcher, portal, relevance
from omniseek.core.normalize import Document, schema_extract
from omniseek.core.sources._base import SourceBase, ablocking, aparse

logger = logging.getLogger(__name__)
_SCRAPE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")
# A browser-shaped Accept is load-bearing, NOT cosmetic: some WAFs (immigratemanitoba.com's
# WordPress front, verified 2026-07-25) answer 415 Unsupported Media Type to a request that carries
# a browser UA but NO Accept header, so mpnp_draws silently returned 0 rows for months. Sending what
# a real browser sends fixes the whole class. Scoped to the HTML-page path ONLY (never http.get_json)
# so no JSON API can content-negotiate its way into serving us HTML.
_SCRAPE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def _adapter_to_documents(module: str, name: str, raw: Any, query: str, limit: int) -> list[Document]:
    """``<source name>._to_documents(raw, query, limit)`` in a worker of the process lane: importing the
    source's module registers it in the worker's own registry, so the source is found by name and
    nothing but its module, its name and the raw payload is pickled."""
    importlib.import_module(module)
    adapter = fetcher.get_adapter(name)
    if adapter is None:
        raise LookupError(f"source {name!r} is not registered by {module} in the process-lane worker")
    return adapter._to_documents(raw, query, limit)


def _answer_size(raw: Any, counted: int) -> int:
    """The answer's size for ``aparse``: the bytes the fetch step read through ``omniseek.core.http``; a
    fetch that read none that way (its own client) but hands back text or bytes counts their length."""
    if counted:
        return counted
    return len(raw) if isinstance(raw, (str, bytes, bytearray)) else 0


class BaseScrapeAdapter(SourceBase):
    """Template-method base for query-endpoint HTML/JSON scrape adapters.

    Subclass and set the class attributes, then implement the two hooks.

    Class attributes:
        name: str                — source identifier (required)
        description: str         — human-readable, the agent's domain router (required)
        needs_credentials: bool  — default False
        explicit_only            — False, True, or a reason string (excluded from broad
                                   fan-out; passed straight through to the fetcher)
        kind / domains / regions — optional routing facets (mirrors RSS / config rows)
        cache_ttl: int           — per-search cache duration in seconds (default 900)
        rank: bool               — re-rank the built docs with the shared BM25 scorer
                                   (default False: keep the endpoint's own order, so a
                                   server-relevance source stays byte-identical to its
                                   hand-written form; set True to score locally like RSS)

    Hooks (override):
        async _araw_fetch(query, limit) -> Any | None
            Do the network call and return a parsed payload (dict / list / str /
            BeautifulSoup — whatever ``_to_documents`` wants). Return ``None`` on
            ANY failure: the base turns that into ``[]`` (the adapter contract).
            Default uses ``http.aget_json(search_url)`` when ``search_url`` is set.
            A fetch that can only block: ``def _raw_fetch(query, limit)`` instead
            (run, with ``_to_documents``, on a worker thread).
        async _ato_documents(raw, query, limit) -> list[Document]   (optional)
            Use instead of ``_to_documents`` when building the documents needs more
            requests (per-record egress).
        _to_documents(raw, query, limit) -> list[Document]
            Convert the payload into Documents. Slice to ``limit`` here if the
            payload can over-return (the base does NOT re-slice, to preserve each
            source's exact "take first N items" semantics).
            The async path runs it through ``sources._base.aparse`` (ASYNC-CORE-DESIGN.md
            D14): on the loop for a small answer, in a worker process for one over
            ``PARSE_INLINE_MAX_BYTES`` (the bytes the fetch hook read). The worker finds
            this source by ``name`` in its own registry, so the payload must pickle
            (plain data, not a parser object) and the method must not lean on state
            set at runtime.
    """

    # ── identity / contract ────────────────────────────────────────────────
    name: str = ""
    description: str = ""
    needs_credentials: bool = False
    explicit_only = False
    cache_ttl: int = 900

    # ── behavior knobs ─────────────────────────────────────────────────────
    # OPT-IN local ranking. Default False so a base-migrated source whose endpoint
    # already returns relevance order is byte-for-byte identical to its hand form;
    # set True to score with the shared engine (the RSS default behavior).
    rank: bool = False
    # Optional convenience for the common "GET a templated search URL → JSON" case:
    # set ``search_url`` to a format string with a ``{query}`` (and optionally
    # ``{limit}``) placeholder and the default ``_araw_fetch`` will GET+parse it.
    search_url: Optional[str] = None
    # Declarative HTML extraction (force-multiplier: a source becomes DATA, not code). Set
    # ``extract_schema`` (see normalize.schema_extract: {item_selector, fields}) + ``fetch_html=True``
    # (default _araw_fetch returns the page text instead of the JSON) + ``base_url`` (relative-URL
    # join) and the default _to_documents builds docs with ZERO per-source parsing. Default off ⇒
    # every existing hand-written scrape adapter is byte-identical.
    extract_schema: Optional[dict] = None
    fetch_html: bool = False
    base_url: str = ""
    # QUERY-INDEPENDENT FETCH (EYE-GATE-FIX A, 2026-10-20). A source whose network fetch is the same
    # whatever the query (v2ex's node listings: the query only scores the merged topics locally) sets
    # this True. Its cache key then holds only what it fetches (``_fetch_identity``: the URLs and
    # parameters), never the query or the limit, so one fetch serves every query until the cache runs
    # out; the fetch hook / ``_to_documents`` get an empty query and must not slice to ``limit``;
    # ``rank`` scores the cached documents per query. Before, v2ex keyed by query and re-fetched the
    # same six listings for every new query: 375 of the 1,516 gate refusals measured over 4.2 days.
    query_independent: bool = False

    # --------------------------------------------------------- auto-registration
    def __init_subclass__(cls, *, register: bool = True, **kwargs) -> None:
        """Register a fresh instance of every concrete subclass on definition (same
        boilerplate-killer as BaseAPIAdapter — defining the class in an auto-imported
        ``*_source.py`` is enough, no module-tail register ceremony). ``register=False``
        or an empty ``name`` opts out (an abstract layer, not a deployable source)."""
        super().__init_subclass__(**kwargs)
        if not register or not getattr(cls, "name", ""):
            return
        from omniseek.core.fetcher import register_adapter  # local: avoid package-init cycle
        register_adapter(cls())

    def _search_target(self, query: str, limit: int) -> str:
        """``search_url`` filled with the URL-encoded query and the limit (the default hooks' URL)."""
        if not self.search_url:
            raise NotImplementedError(
                f"{type(self).__name__} must set `search_url` or override `_araw_fetch`"
            )
        from urllib.parse import quote
        return self.search_url.format(query=quote(query), limit=limit)

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Network call -> parsed payload (None on failure). Default: GET of ``search_url`` (the query
        URL-encoded), its page text when ``fetch_html`` (for ``extract_schema``), else its JSON. Override
        for anything else (POST, params, several steps). A source whose fetch can only BLOCK (a library
        with no async client) defines a plain ``def _raw_fetch(query, limit)`` instead and no
        ``_araw_fetch``: the base then runs that hook and the source's ``_to_documents`` on a worker
        thread (``_afetch`` / ``_abuild``)."""
        url = self._search_target(query, limit)
        from omniseek.core import http
        if self.fetch_html:
            try:
                r = await http.adirect("GET", url, timeout=20, follow_redirects=True,
                                       headers={"User-Agent": _SCRAPE_UA, "Accept": _SCRAPE_ACCEPT})
                r.raise_for_status()
                return r.text
            except Exception as exc:  # noqa: BLE001 — failure → None → [] (the contract)
                logger.warning("%s: html fetch failed: %s", self.name, exc)
                return None
        return await http.aget_json(url)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        """Parsed payload → Documents. Override, OR set ``extract_schema`` for the
        declarative path (no per-source code)."""
        if self.extract_schema is not None:
            return self._schema_to_documents(raw, query, limit)
        raise NotImplementedError(
            f"{type(self).__name__} must implement `_to_documents` (or set `extract_schema`)"
        )

    def _schema_to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        """Build docs from ``extract_schema`` (CSS-driven). Field-name convention →
        Document: title / url / content (or summary) / author / id; every other extracted
        field lands in metadata. A row with neither title nor url is dropped."""
        html = raw if isinstance(raw, str) else (raw[0] if isinstance(raw, tuple) else str(raw))
        from urllib.parse import urljoin
        docs: list[Document] = []
        for r in schema_extract(html, self.extract_schema)[:limit]:
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            if not (title or url):
                continue
            docs.append(Document(
                source=self.name,
                source_id=str(r.get("id") or url or title),
                url=urljoin(self.base_url, url) if url else (self.base_url or ""),
                title=title or "(untitled)",
                content=(r.get("content") or r.get("summary") or title or "").strip(),
                author=(r.get("author") or None),
                metadata={k: v for k, v in r.items()
                          if k not in ("title", "url", "content", "summary", "author", "id") and v},
            ))
        return docs

    # ── mechanism (the base owns this; subclasses rarely touch it) ──────────
    def _fetch_identity(self) -> tuple:
        """What a query-independent fetch asks for (its URLs and parameters): the whole cache key of
        such a source besides its name. Override with the endpoint list; the default is ``search_url``."""
        return (self.search_url or "",)

    def _fetch_ttl(self) -> float:
        """The cache time of a query-independent fetch: ``cache_ttl``, never shorter than the longest
        Crawl-delay of the hosts it asks (a cache that runs out first only buys a refused request)."""
        from omniseek.core import upstreams
        urls = [p for p in self._fetch_identity() if isinstance(p, str) and "://" in p]
        return upstreams.cache_ttl_for(self.cache_ttl, urls)

    def _query_independent_key(self) -> str:
        return cache.make_key(self.name, "fetch", *self._fetch_identity())

    def _scored(self, docs: list[Document], query: str) -> list[Document]:
        return self._rank(docs, query) if self.rank and docs else docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """The search, written once: ``_afetch`` then ``_abuild`` through ``_asearch_via``. ``search``
        (the sync entry for scripts) is SourceBase's, generated from this."""
        return await self._asearch_via(query, limit)

    def _blocking(self) -> bool:
        """The source's fetch can only block: it defines ``_raw_fetch`` and does not override
        ``_araw_fetch`` (an overridden ``_araw_fetch`` always wins, so a leftover sync hook is dead code,
        never the one that runs)."""
        return hasattr(self, "_raw_fetch") and type(self)._araw_fetch is BaseScrapeAdapter._araw_fetch

    async def _afetch(self, query: str, limit: int) -> tuple[Optional[Any], int]:
        """The fetch hook and the bytes it read (``http.count_bytes``): a blocking ``_raw_fetch`` on a
        worker thread (see ``_blocking``), else ``_araw_fetch`` on the loop."""
        from omniseek.core import http
        with http.count_bytes() as got:
            if self._blocking():
                raw = await ablocking(self._raw_fetch, query, limit)
            else:
                raw = await self._araw_fetch(query, limit)
        return raw, _answer_size(raw, got[0])

    async def _aparse(self, raw: Any, query: str, limit: int, size: int) -> list[Document]:
        """``_to_documents`` through ``aparse``: here on the loop for a small answer, in the process
        lane for a large one (the worker finds this source by ``name``, see ``_adapter_to_documents``)."""
        return await aparse(size, self._to_documents, raw, query, limit,
                            lane=functools.partial(_adapter_to_documents, type(self).__module__, self.name))

    async def _abuild(self, raw: Any, query: str, limit: int, size: int) -> list[Document]:
        """The build hook: ``_ato_documents`` when the source assembles with further requests (Stack
        Exchange fetching answers), else ``_to_documents`` through ``_aparse`` (on the worker thread for
        a source with a blocking ``_raw_fetch``, where its whole search ran before)."""
        if self._blocking():
            return await ablocking(self._to_documents, raw, query, limit)
        ato = getattr(self, "_ato_documents", None)
        if ato is not None:
            return await ato(raw, query, limit)
        return await self._aparse(raw, query, limit, size)

    async def _asearch_via(self, query: str, limit: int) -> list[Document]:
        """The mechanism (cache round-trip + opt-in rank): ``_afetch`` (off-loop NETWORK) then
        ``_abuild``. A query-independent source fetches with an empty query and parses through
        ``_aparse`` likewise. Off-loop: cache get/set (disk IO) and the parse of a large answer. On loop:
        `_rank` (shared BM25, pure CPU)."""
        if self.query_independent:
            key = self._query_independent_key()
            cached = await cache.aget_docs(key)                        # disk read OFF loop
            if cached is None:
                raw, size = await self._afetch("", limit)
                if raw is None:
                    return []
                try:
                    cached = await self._aparse(raw, "", limit, size) or []
                except Exception as exc:  # noqa: BLE001 -- malformed payload -> [] (mirror search)
                    logger.warning("%s: async _to_documents failed: %s", self.name, exc)
                    return []
                await anyio.to_thread.run_sync(
                    functools.partial(cache.set_docs, key, cached, ttl=self._fetch_ttl()), limiter=portal.IO_LANE)
            return self._scored(cached, query)                         # pure CPU, on loop
        key = cache.make_key(self.name, "search", query, limit)
        cached = await cache.aget_docs(key)                            # disk read OFF loop
        if cached is not None:
            return cached
        raw, size = await self._afetch(query, limit)
        if raw is None:
            return []                                                  # failure -> [], NOT cached (mirror)
        try:
            docs = await self._abuild(raw, query, limit, size) or []
        except Exception as exc:  # noqa: BLE001 -- malformed payload -> [] (mirror search)
            logger.warning("%s: async _to_documents failed: %s", self.name, exc)
            return []
        if self.rank and docs:
            docs = self._rank(docs, query)                             # pure CPU, on loop
        await anyio.to_thread.run_sync(                                # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=self.cache_ttl), limiter=portal.IO_LANE)
        return docs

    @staticmethod
    def _rank(docs: list[Document], query: str) -> list[Document]:
        """Score via the shared BM25 engine (title 3x + content 1x) — the SAME scorer
        as RSS / search-ranking / keyword_score_filter, so a base source can't drift.
        A term-less query keeps the incoming order; otherwise best-first, matches only."""
        if not relevance.query_terms(query):
            return docs
        scores = relevance.doc_scores(docs, query)
        scored = [(s, d) for s, d in zip(scores, docs) if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored]

    # ── a query-independent page inside a query-dependent source (EYE-GATE-FIX A) ──────────────
    def _url_key(self, url: str, params: Optional[dict]) -> str:
        return cache.make_key(self.name, "url", url, *sorted(f"{k}={v}" for k, v in (params or {}).items()))

    async def _aurl_json(self, url: str, params: Optional[dict] = None, *, timeout: int = 15) -> Optional[Any]:
        """GET+JSON of ``url`` cached by the URL and its parameters (not by any query), one fetch per key
        at a time, for a page that is the same whatever the query (data.gov.sg's dataset listing). None
        on failure (not cached)."""
        from omniseek.core import http, upstreams
        key = self._url_key(url, params)
        hit = await cache.aget_once(key)
        if hit is not None:
            return hit
        raw = await http.aget_json(url, params=params, timeout=timeout)
        if raw is not None:
            await anyio.to_thread.run_sync(functools.partial(
                cache.set, key, raw, ttl=upstreams.cache_ttl_for(self.cache_ttl, [url])), limiter=portal.IO_LANE)
        return raw

    async def afetch_url(self, url: str) -> Optional[Document]:
        """Default: this source does not claim arbitrary URLs. Override to claim a
        host + build a doc from a single page (the academia_se / pypi pattern). The
        override is ``async def afetch_url``, the source's only URL reader (there is no
        sync ``fetch_url``; see ``fetcher.SourceAdapter.afetch_url``)."""
        return None

    async def _ahealth_fetch(self, query: str, *, fallback: str) -> tuple[Any, str]:
        """``(raw, why)``: one fetch (``_afetch(query, 1)``, the source's own fetch hook) under an armed
        failure capture. When ``raw`` is None, ``why`` is the last egress failure the shared helpers
        noted (``HTTP 504: <body>``, or the exception), else ``fallback``; an exception from the fetch
        propagates."""
        diag.enable()
        try:
            raw, _size = await self._afetch(query, 1)
        finally:
            captures = diag.drain()
        return raw, ("" if raw is not None else diag.failure_reason(captures, fallback=fallback))

    async def ahealth_check(self) -> tuple[bool, str]:
        """Default probe: a cheap fetch with a trivial query proves the
        endpoint answers. Override for a lighter/different liveness signal (e.g. an
        API ``/info`` endpoint or a quota readout)."""
        try:
            raw, why = await self._ahealth_fetch("test", fallback="no payload without an observed egress failure")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if raw is None:
            return False, why
        return True, "OK"
