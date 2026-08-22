"""Shared base for non-RSS HTML/JSON scrape adapters (the regular many).

`_rss.py::RSSAdapterBase` is the golden template-method base for feed sources;
this is its sibling for the *other* scrape shape — sources that hit a search
endpoint (a public API like Academia Stack Exchange, or a query-able HTML/JSON
search page) and parse the response. Roughly a dozen scrape adapters share the
identical ritual:

    def search(self, query, limit):
        key = cache.make_key(name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None: return cached
        raw = <fetch the query-specific URL/API>
        if raw is None: return []
        docs = [<parse each item> ...]
        cache.set_docs(key, docs, ttl)
        return docs

This base owns that mechanism (cache check / build / atomic set_docs / optional
shared-BM25 rank / self-registration); a concrete adapter declares a few class
attributes and fills TWO hooks:

    _raw_fetch(query, limit) -> Any | None     # the network call → parsed payload
    _to_documents(raw, query, limit) -> list[Document]   # payload → docs

It is a CONVENIENCE, never a mandate. The Protocol (duck-typed
``fetcher.SourceAdapter``) stays the contract; a source with bespoke needs
(multi-host fan-out, signed requests, GBK search encoding, anti-bot headers)
keeps writing search/fetch_url/health_check by hand. This base only serves the
regular, single-endpoint majority.

Design rules it obeys (the moat):
  * It carries NO judgment — ranking is the ONE shared scorer
    (``relevance.doc_scores``: title 3x + content 1x), the same engine RSS,
    search-ranking, and ``keyword_score_filter`` use, so a base-using source can
    never drift. Ranking is OPT-IN per source (``rank`` class attr) precisely so
    a source whose endpoint already returns server-relevance order (e.g. an API
    with ``sort=relevance``) stays byte-identical to its hand-written form.
  * ``explicit_only`` self-declaration, atomic cache writes (via cache.set_docs),
    and the failure→empty contract (``_raw_fetch`` returns None → ``search`` →
    ``[]``) all pass straight through.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Optional

import anyio

from omniseek.core import cache, diag, relevance
from omniseek.core.normalize import Document, schema_extract

logger = logging.getLogger(__name__)
_SCRAPE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")
# A browser-shaped Accept is load-bearing, NOT cosmetic: some WAFs (immigratemanitoba.com's
# WordPress front, verified 2026-07-25) answer 415 Unsupported Media Type to a request that carries
# a browser UA but NO Accept header, so mpnp_draws silently returned 0 rows for months. Sending what
# a real browser sends fixes the whole class. Scoped to the HTML-page path ONLY (never http.get_json)
# so no JSON API can content-negotiate its way into serving us HTML.
_SCRAPE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class BaseScrapeAdapter:
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
        _raw_fetch(query, limit) -> Any | None
            Do the network call and return a parsed payload (dict / list / str /
            BeautifulSoup — whatever ``_to_documents`` wants). Return ``None`` on
            ANY failure: the base turns that into ``[]`` (the adapter contract).
            Default uses ``http.get_json(search_url)`` when ``search_url`` is set.
        _to_documents(raw, query, limit) -> list[Document]
            Convert the payload into Documents. Slice to ``limit`` here if the
            payload can over-return (the base does NOT re-slice, to preserve each
            source's exact "take first N items" semantics).
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
    # ``{limit}``) placeholder and the default ``_raw_fetch`` will GET+parse it.
    search_url: Optional[str] = None
    # Declarative HTML extraction (force-multiplier: a source becomes DATA, not code). Set
    # ``extract_schema`` (see normalize.schema_extract: {item_selector, fields}) + ``fetch_html=True``
    # (default _raw_fetch returns the page text instead of get_json) + ``base_url`` (relative-URL
    # join) and the default _to_documents builds docs with ZERO per-source parsing. Default off ⇒
    # every existing hand-written scrape adapter is byte-identical.
    extract_schema: Optional[dict] = None
    fetch_html: bool = False
    base_url: str = ""

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

    # ── hooks ──────────────────────────────────────────────────────────────
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Network call → parsed payload (None on failure). Default: GET+JSON of
        ``search_url`` (URL-encoding the query). Override for anything else
        (POST, HTML, params dict, multi-step). Returning None ⇒ search ⇒ []."""
        if not self.search_url:
            raise NotImplementedError(
                f"{type(self).__name__} must set `search_url` or override `_raw_fetch`"
            )
        from urllib.parse import quote
        url = self.search_url.format(query=quote(query), limit=limit)
        if self.fetch_html:  # declarative-HTML source: fetch the page text for schema_extract
            import httpx
            try:
                r = httpx.get(url, timeout=20, follow_redirects=True,
                              headers={"User-Agent": _SCRAPE_UA, "Accept": _SCRAPE_ACCEPT})
                r.raise_for_status()
                return r.text
            except Exception as exc:  # noqa: BLE001 — failure → None → [] (the contract)
                logger.warning("%s: html fetch failed: %s", self.name, exc)
                return None
        from omniseek.core import http
        return http.get_json(url)

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
    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key(self.name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        raw = self._raw_fetch(query, limit)
        if raw is None:
            return []  # failure → empty, the adapter contract (don't cache a miss)

        try:
            docs = self._to_documents(raw, query, limit) or []
        except Exception as exc:  # noqa: BLE001 — a malformed payload degrades to []
            logger.warning("%s: _to_documents failed: %s", self.name, exc)
            return []

        if self.rank and docs:
            docs = self._rank(docs, query)

        cache.set_docs(key, docs, ttl=self.cache_ttl)
        return docs

    async def _asearch_via(self, query, limit, afetch, abuild):
        """Async twin of `search`'s mechanism (cache round-trip + opt-in rank) for a subclass whose
        egress has gone NATIVE async. `afetch()` -> raw|None (async, off-loop NETWORK). `abuild(raw)`
        -> list[Document]; POLYMORPHIC: if it returns an awaitable it is awaited (an async
        ASSEMBLY twin that egresses per-record, e.g. Stack Exchange fetching answers), else it is used
        directly (a PURE-CPU `_to_documents` that only parses/maps -- the common single-call case, run on
        the loop). This lets every A-tier source pass its sync `_to_documents` with a one-line `asearch`.
        This is a `_`-prefixed HELPER, NOT `asearch`, so the base is NOT flagged AsyncSearchCapable (only
        a subclass that DEFINES `asearch` -> calls this is). Off-loop: cache get/set (disk IO). On loop:
        `_rank` (shared BM25, pure CPU). BEHAVIOR-IDENTICAL to `search` given identical egress."""
        key = cache.make_key(self.name, "search", query, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)   # disk read OFF loop
        if cached is not None:
            return cached
        raw = await afetch()
        if raw is None:
            return []                                                  # failure -> [], NOT cached (mirror)
        try:
            built = abuild(raw)                                        # sync _to_documents OR async twin
            docs = (await built if inspect.isawaitable(built) else built) or []
        except Exception as exc:  # noqa: BLE001 -- malformed payload -> [] (mirror search)
            logger.warning("%s: async _to_documents failed: %s", self.name, exc)
            return []
        if self.rank and docs:
            docs = self._rank(docs, query)                             # pure CPU, on loop
        await anyio.to_thread.run_sync(                                # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=self.cache_ttl))
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

    def fetch_url(self, url: str) -> Optional[Document]:
        """Default: this source does not claim arbitrary URLs. Override to claim a
        host + build a doc from a single page (the academia_se / pypi pattern)."""
        return None

    def health_check(self) -> tuple[bool, str]:
        """Default probe: a cheap ``_raw_fetch`` with a trivial query proves the
        endpoint answers. Override for a lighter/different liveness signal (e.g. an
        API ``/info`` endpoint or a quota readout)."""
        diag.enable()
        try:
            raw = self._raw_fetch("test", 1)
        except Exception as exc:  # noqa: BLE001
            diag.drain()
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        captures = diag.drain()
        if raw is None:
            return False, diag.failure_reason(
                captures,
                fallback="no payload without an observed egress failure",
            )
        return True, "OK"
