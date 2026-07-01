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
    _to_documents(raw, query, limit) -> list[PolarisDocument]   # payload → docs

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

import logging
from typing import Any, Optional

from penumbra.core import cache, relevance
from penumbra.core.normalize import PolarisDocument, schema_extract

logger = logging.getLogger(__name__)
_SCRAPE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")


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
        _to_documents(raw, query, limit) -> list[PolarisDocument]
            Convert the payload into PolarisDocuments. Slice to ``limit`` here if the
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
        from penumbra.core.fetcher import register_adapter  # local: avoid package-init cycle
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
                              headers={"User-Agent": _SCRAPE_UA})
                r.raise_for_status()
                return r.text
            except Exception as exc:  # noqa: BLE001 — failure → None → [] (the contract)
                logger.warning("%s: html fetch failed: %s", self.name, exc)
                return None
        from penumbra.core import http
        return http.get_json(url)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        """Parsed payload → PolarisDocuments. Override, OR set ``extract_schema`` for the
        declarative path (no per-source code)."""
        if self.extract_schema is not None:
            return self._schema_to_documents(raw, query, limit)
        raise NotImplementedError(
            f"{type(self).__name__} must implement `_to_documents` (or set `extract_schema`)"
        )

    def _schema_to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        """Build docs from ``extract_schema`` (CSS-driven). Field-name convention →
        PolarisDocument: title / url / content (or summary) / author / id; every other extracted
        field lands in metadata. A row with neither title nor url is dropped."""
        html = raw if isinstance(raw, str) else (raw[0] if isinstance(raw, tuple) else str(raw))
        from urllib.parse import urljoin
        docs: list[PolarisDocument] = []
        for r in schema_extract(html, self.extract_schema)[:limit]:
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            if not (title or url):
                continue
            docs.append(PolarisDocument(
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
    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
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

    @staticmethod
    def _rank(docs: list[PolarisDocument], query: str) -> list[PolarisDocument]:
        """Score via the shared BM25 engine (title 3x + content 1x) — the SAME scorer
        as RSS / search-ranking / keyword_score_filter, so a base source can't drift.
        A term-less query keeps the incoming order; otherwise best-first, matches only."""
        if not relevance.query_terms(query):
            return docs
        scores = relevance.doc_scores(docs, query)
        scored = [(s, d) for s, d in zip(scores, docs) if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        """Default: this source does not claim arbitrary URLs. Override to claim a
        host + build a doc from a single page (the academia_se / pypi pattern)."""
        return None

    def health_check(self) -> tuple[bool, str]:
        """Default probe: a cheap ``_raw_fetch`` with a trivial query proves the
        endpoint answers. Override for a lighter/different liveness signal (e.g. an
        API ``/info`` endpoint or a quota readout)."""
        try:
            raw = self._raw_fetch("test", 1)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if raw is None:
            return False, "fetch returned nothing"
        return True, "OK"
