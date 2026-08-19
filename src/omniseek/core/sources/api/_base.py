"""Template-method base class for plain HTTP/JSON API source adapters.

Most of our Tier-1 open-API sources (arXiv, DBLP, OpenAlex, Crossref, Hacker
News, …) share the SAME skeleton: build a cache key → check the cache → early
return on a hit → hit the API → map each raw record to a Document →
(optionally) lexically rank → cache → return. The per-source code that actually
differs is tiny: *how to fetch the raw records* and *how to turn one raw record
into a Document*. Everything else is mechanism repeated ~38 times.

``BaseAPIAdapter`` factors that mechanism out, mirroring ``scrape/_rss.py``'s
``RSSAdapterBase``: a subclass only implements two hooks and declares a handful
of class attributes; the base supplies ``search`` / ``fetch_url`` /
``health_check`` and auto-registers the adapter on class creation.

Two hooks (the ONLY required overrides)::

    def _raw_fetch(self, query: str, limit: int) -> list:      # API-specific I/O
    def _to_document(self, raw) -> Optional[Document]:   # one record → doc

Class attributes (declare on the subclass)::

    name              str   — source id (SourceAdapter.name)
    description       str   — human-readable router blurb (SourceAdapter.description)
    needs_credentials bool  — default False
    explicit_only     bool|str — moat self-declaration; default False (broad-search ok)
    cache_ttl         int   — search-result TTL in seconds (default 3600)
    search_label      str   — middle part of the cache key (default "search"); set
                              to match an existing source's key exactly on migration
    rank_locally      bool  — default True: base lexically ranks results via the
                              shared ``keyword_score_filter`` (the funnel guard).
                              Set False for sources the API ALREADY ranks
                              server-side (arXiv/DBLP/OpenAlex sort= relevance) so
                              the base preserves the server order verbatim.
    url_host          str   — substring matched against a URL's hostname so
                              fetch_url can claim it (e.g. "arxiv.org"); None →
                              fetch_url returns None unless the subclass overrides.
    health_probe_url  str   — URL hit by the default health_check; None → the
                              subclass must override health_check.
    kind/domains/regions/facets — optional routing facets (list_sources decoration).

The base intentionally holds NO business judgment: it never re-sorts by a source
metric, never decides relevance beyond the shared BM25 scorer, never special-cases
a query. "代码要笨,agent 才聪明" — the base is pure plumbing; the subclass's two
hooks carry every source-specific fact.

This module's filename does NOT end in ``_source``, so the auto-discovery walk in
``omniseek.server`` skips it — it stays inert until a ``*_source.py`` subclass
imports it. Subclassing alone registers the adapter (``__init_subclass__`` calls
``register_adapter``); there is no file-tail registration ceremony.

opt-in, never mandatory: this base is a CONVENIENCE for the boilerplate-shaped
sources. A source with bespoke needs (signing, anti-bot headers, a non-standard
cache shape, multi-endpoint fan-out) stays a plain duck-typed adapter — the
``SourceAdapter`` Protocol is the only real contract, and that back door is always
open.
"""

from __future__ import annotations

import functools
import logging
from typing import Optional
from urllib.parse import urlparse

import anyio

from omniseek.core import cache, http
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)


class BaseAPIAdapter:
    """Base class for plain HTTP/JSON API source adapters (template method).

    Subclass, set the class attributes, implement ``_raw_fetch`` + ``_to_document``.
    The base provides ``search`` / ``fetch_url`` / ``health_check`` and registers
    the instance automatically when the subclass is defined.
    """

    # --- SourceAdapter Protocol surface (subclass overrides name/description) ---
    name: str = ""
    description: str = ""
    needs_credentials: bool = False

    # --- moat self-declaration (kept on the adapter, read by fetcher) ----------
    explicit_only: "bool | str" = False

    # --- caching + ranking knobs ----------------------------------------------
    cache_ttl: int = 3600
    search_label: str = "search"
    rank_locally: bool = True

    # --- routing helpers -------------------------------------------------------
    url_host: Optional[str] = None
    health_probe_url: Optional[str] = None

    # --- optional list_sources facets (adapter's own declaration wins) ---------
    # (declared as None so they are inert unless a subclass sets them; fetcher
    #  reads them via getattr and falls back to facets.json)

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        """Fetch raw records for ``query`` from the source's API.

        Return a list of opaque per-record objects (dicts, feedparser entries,
        anything) — the base passes each to ``_to_document``. Return ``[]`` on any
        network failure / timeout (the "failure → empty, do not cache" contract);
        do NOT raise for an ordinary empty result. The base never inspects the
        items, so their shape is entirely the subclass's business.
        """
        raise NotImplementedError("subclass must implement _raw_fetch")

    def _to_document(self, raw) -> Optional[Document]:
        """Map ONE raw record to a Document (or None to drop it).

        Called once per item returned by ``_raw_fetch``. Returning None silently
        skips a malformed record (the base logs it at debug). Any exception raised
        here is caught + skipped per-record, so one bad record can't lose the rest.
        """
        raise NotImplementedError("subclass must implement _to_document")

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 10) -> list[Document]:
        """Cache-checked search: make_key → get_docs → early return → _raw_fetch →
        map → (rank) → set_docs.

        Cache identity is ``(name, search_label, query, limit)`` — set
        ``search_label`` to match an existing source's key on migration. On a hit
        the cached, already-mapped Documents are returned directly (zero
        re-parse, zero re-score). Empty results are NOT cached (a transient
        failure must not pin an empty answer for the whole TTL).
        """
        key = cache.make_key(self.name, self.search_label, query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        raw_items = self._raw_fetch(query, limit) or []
        docs: list[Document] = []
        for raw in raw_items[:limit]:
            try:
                doc = self._to_document(raw)
            except Exception as exc:  # noqa: BLE001 — one bad record can't sink the rest
                logger.debug("%s: skipping malformed record: %s", self.name, exc)
                continue
            if doc is not None:
                docs.append(doc)

        # Lexical funnel guard (shared BM25 scorer, so this can never drift from
        # search-ranking / filtering elsewhere). A term-less query returns docs in
        # source order. Sources the API already ranks server-side opt out
        # (rank_locally=False) so the base preserves the server's order verbatim.
        if self.rank_locally:
            docs = keyword_score_filter(docs, query)

        if docs:
            cache.set_docs(key, docs, ttl=self.cache_ttl)
        return docs

    async def _aapi_search(self, query, limit, araw_fetch):
        """Async twin of ``search``'s mechanism (cache round-trip + per-record map + opt-in rank) for a
        subclass whose egress has gone NATIVE async. ``araw_fetch()`` -> the raw records list (async,
        off-loop NETWORK). This is a ``_``-prefixed HELPER, NOT ``asearch``, so the base is NOT flagged
        AsyncSearchCapable; only a subclass that DEFINES ``asearch`` -> calls this is. Off-loop: cache
        get/set (disk IO). On loop: per-record ``_to_document`` (pure CPU) + ``keyword_score_filter``
        (BM25). BEHAVIOR-IDENTICAL to ``search`` given identical egress: same cache key (``search_label``),
        same per-record skip-on-error, same ``rank_locally``, same cache-only-if-docs policy."""
        key = cache.make_key(self.name, self.search_label, query, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)   # disk read OFF loop
        if cached is not None:
            return cached
        raw_items = (await araw_fetch()) or []
        docs: list[Document] = []
        for raw in raw_items[:limit]:
            try:
                doc = self._to_document(raw)
            except Exception as exc:  # noqa: BLE001 — one bad record can't sink the rest
                logger.debug("%s: skipping malformed record: %s", self.name, exc)
                continue
            if doc is not None:
                docs.append(doc)
        if self.rank_locally:
            docs = keyword_score_filter(docs, query)
        if docs:
            await anyio.to_thread.run_sync(   # disk write OFF loop
                functools.partial(cache.set_docs, key, docs, ttl=self.cache_ttl))
        return docs

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        """Default claim-by-host: if ``url_host`` is a substring of the URL's
        hostname, scan this source's already-cached search docs for an exact URL
        match. Returns None when the URL isn't ours or isn't in cache.

        This is a deliberately cheap default — it never issues a network request
        of its own (a by-id endpoint differs per API). A source that can fetch a
        single record by URL/id (arXiv ``id_list=``, OpenAlex ``/works/{id}``)
        SHOULD override this with that direct lookup; the base just guarantees a
        sane no-op for sources that don't.
        """
        if not self.url_host:
            return None
        host = (urlparse(url).hostname or "").lower()
        if self.url_host.lower() not in host:
            return None
        # No own cache index of every URL ever seen → best-effort scan of the
        # adapter's cached search docs is intentionally omitted (would require a
        # query). Subclasses that need real URL fetch override this.
        return None

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[Optional[bool], str]:
        """Default probe: GET ``health_probe_url`` through the shared http client
        and report on the status. ``http.get`` returns None on any failure (incl.
        oversize / timeout), which maps to an unhealthy result. Subclasses with a
        smarter probe (e.g. treating HTTP 429 as "alive but throttling") override
        this.
        """
        if not self.health_probe_url:
            # None, not False. A missing probe URL is OUR configuration gap, not evidence about the
            # upstream: reporting False published "this source is down" for something we never
            # asked. None is the third state, "not measured", which the watchdog now leaves out of
            # the consecutive-fail counter instead of quarantining the source over it.
            return None, "our adapter configuration is missing health_probe_url"
        resp = http.get(self.health_probe_url, timeout=10)
        if resp is None:
            return False, "request failed (timeout / network / oversize)"
        ok = resp.status_code == 200
        return ok, f"HTTP {resp.status_code}"

    # --------------------------------------------------------- auto-registration
    def __init_subclass__(cls, *, register: bool = True, **kwargs) -> None:
        """Register a fresh instance of every concrete subclass on definition.

        This is the boilerplate killer: the module-tail
        ``register_adapter(FooAdapter())`` ceremony repeated in ~38 files goes
        away — defining the class in a ``*_source.py`` module (which the server
        auto-imports) is enough to register it.

        Pass ``register=False`` in the class header to opt out (an intermediate
        base layer, or a subclass that wants to register a custom instance
        itself). A subclass with an empty ``name`` is skipped too (it's clearly an
        abstract layer, not a deployable source).
        """
        super().__init_subclass__(**kwargs)
        if not register or not getattr(cls, "name", ""):
            return
        # Local import: avoid a package-init import cycle (fetcher → normalize,
        # and source modules import this base before fetcher is fully wired).
        from omniseek.core.fetcher import register_adapter

        register_adapter(cls())
