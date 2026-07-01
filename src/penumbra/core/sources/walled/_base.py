"""Shared base for regular CDP (walled-garden) adapters.

The sibling of ``scrape/_base.py`` for the *walled* tier. Several CDP sources
(zhihu, yipinsanfendi, zhihu_users, …) drive the persistent logged-in Chrome
through the identical ritual:

    def search(self, query, limit):
        key = cache.make_key(name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None: return cached
        url = <search URL for query>
        try:
            raw = cdp_call(_flow, initial_url=url, cdp_url=...)
        except Exception:
            return []
        docs = <parse raw into PolarisDocuments>
        cache.set_docs(key, docs, ttl)
        return docs

    def health_check(self):
        ok, msg = cdp_health(cdp_url)
        if not ok: return False, ...
        <one probe cdp_call>

This base owns that mechanism — the cdp_call wrapping, the try/except→[]
degrade, caching, optional shared-BM25 ranking, the cdp_health probe, and
self-registration — so a concrete CDP source declares a couple of class
attributes and fills two hooks:

    _search_url(query) -> str              # the page to navigate to
    _flow(page) -> raw                     # interact with the page, return raw payload
    _to_documents(raw, query, limit) -> list[PolarisDocument]   # raw → docs

It is OPT-IN. The bespoke, account-rate-sensitive sources (xiaohongshu's 794-line
9223-isolated single-flight flow, mokahr's signed ATS requests) stay hand-written
— the duck-typed ``fetcher.SourceAdapter`` Protocol is always the real contract.
This base serves the regular shared-9222 CDP majority.

Moat preserved:
  * ``explicit_only`` is defaulted ON here (CDP = a precious shared logged-in
    session that must never enter the broad fan-out) but a subclass may override
    the reason string. It passes straight through to the fetcher.
  * The cdp tab-sweep / bounded-thread backstop / single-flight all live in
    ``_cdp.cdp_call`` — this base just calls it, so every defense is inherited.
  * Ranking, when enabled, is the ONE shared scorer (``relevance.doc_scores``);
    the base carries no judgment of its own.
  * Atomic cache writes via ``cache.set_docs``; failure (any cdp_call exception)
    → ``[]`` (the adapter contract).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from penumbra.core import cache, diag, relevance
from penumbra.core.normalize import PolarisDocument, is_blocked
from penumbra.core.sources.walled import _human
from penumbra.core.sources.walled._cdp import DEFAULT_CDP_URL, cdp_call, cdp_health

logger = logging.getLogger(__name__)

# A walled (CDP) search that comes back EMPTY must not be cached as authoritative for the full
# cache_ttl: a transient blip (slow hydration, a momentary wall, a 0-card render) would otherwise
# blind that query for the whole TTL — turning one flicker into minutes of false "no results".
# Real results cache at cache_ttl; an empty result caches for this short cooldown instead — long
# enough to spare the precious shared session a retry-storm, short enough to self-heal on the next
# call. Shared by the base AND the hand-written zhihu adapter so the policy lives in one place.
EMPTY_TTL = 60


class BaseCDPAdapter:
    """Template-method base for regular shared-Chrome CDP adapters.

    Class attributes:
        name: str                — source identifier (required)
        description: str         — human-readable, the agent's domain router (required)
        needs_credentials: bool  — default False (login happens once via VNC; we reuse it)
        explicit_only            — default a CDP reason string; override to taste. CDP is
                                   ALWAYS explicit-only (precious shared session); leaving
                                   this falsy would wrongly enter it into the broad fan-out.
        cdp_url: str             — which Chrome (default 9222 shared 大号; "http://127.0.0.1:9223"
                                   for an isolated 小号). Passed to cdp_call + cdp_health.
        human_fast: bool         — wrap ``_flow`` in ``_human.fast`` (shrunk, ban-cleared
                                   delays) when True. Default False = the 'safe' profile,
                                   byte-identical to every current CDP source.
        cdp_timeout: int         — per-call cdp_call timeout in seconds (default 90)
        cache_ttl: int           — per-search cache duration (default 900)
        rank: bool               — re-rank built docs with the shared BM25 scorer (default
                                   False: keep the page's DOM/result order, byte-identical
                                   to the hand-written sources)
        url_host: str            — substring gate for fetch_url ("zhihu.com"); if a URL's
                                   host doesn't contain it, fetch_url returns None. Empty
                                   ⇒ fetch_url is disabled (returns None) unless overridden.
        kind / domains / regions — optional routing facets.

    Hooks:
        _search_url(query) -> str
            The page ``_flow`` is navigated to. Must override.
        _flow(page) -> Any
            Interact with the loaded page (wait_for_selector, scroll, evaluate) and
            return a raw payload (HTML string, or a tuple incl. images_from_page(page)).
            Runs inside cdp_call's worker thread. Must override.
        _to_documents(raw, query, limit) -> list[PolarisDocument]
            Parse the raw payload into docs (slice to ``limit`` here). Must override.
        _fetch_flow(page) -> Any  /  _to_document(raw, url) -> Optional[PolarisDocument]
            Optional fetch_url hooks (mirror the search pair). If ``_to_document`` is
            left as the default no-op, fetch_url returns None.
    """

    # ── identity / contract ────────────────────────────────────────────────
    name: str = ""
    description: str = ""
    needs_credentials: bool = False
    # CDP is ALWAYS explicit-only — never let a base subclass forget and leak the
    # shared logged-in session into the broad fan-out. Override the *reason*, not the
    # truthiness, in a subclass.
    explicit_only = "shared CDP Chrome (precious logged-in session)"

    # ── CDP knobs ──────────────────────────────────────────────────────────
    cdp_url: str = DEFAULT_CDP_URL
    human_fast: bool = False
    cdp_timeout: int = 90
    cache_ttl: int = 900

    # ── behavior knobs ─────────────────────────────────────────────────────
    rank: bool = False
    url_host: str = ""

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
    def _search_url(self, query: str) -> str:
        """The page to navigate to for ``query``. Must override."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement `_search_url`"
        )

    def _flow(self, page) -> Any:
        """Interact with the loaded search page, return a raw payload. Must override.
        Runs inside cdp_call's worker thread (no asyncio loop)."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement `_flow`"
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        """Raw payload → PolarisDocuments (slice to ``limit``). Must override."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement `_to_documents`"
        )

    def _fetch_flow(self, page) -> Any:
        """Interact with a single-URL page for fetch_url. Default: full HTML.
        Override to also surface images (return ``page.content(), images_from_page(page)``)."""
        return page.content()

    def _to_document(self, raw: Any, url: str) -> Optional[PolarisDocument]:
        """Single-page payload → one PolarisDocument. Default no-op (fetch_url disabled).
        Override (with ``url_host`` set) to claim this source's URLs."""
        return None

    # ── mechanism ──────────────────────────────────────────────────────────
    def _run(self, callback, initial_url: Optional[str]) -> Any:
        """Run a page callback through cdp_call with this source's CDP knobs —
        wrapping in ``_human.fast`` iff ``human_fast`` is set."""
        cb = _human.fast(callback) if self.human_fast else callback
        return cdp_call(cb, initial_url=initial_url,
                        timeout=self.cdp_timeout, cdp_url=self.cdp_url)

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        key = cache.make_key(self.name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        url = self._search_url(query)
        try:
            raw = self._run(self._flow, url)
        except Exception as exc:  # noqa: BLE001 — CDP/network failure → empty (the contract)
            logger.warning("%s search failed: %s", self.name, exc)
            return []

        try:
            docs = self._to_documents(raw, query, limit) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: _to_documents failed: %s", self.name, exc)
            return []

        if self.rank and docs:
            docs = self._rank(docs, query)

        if not docs:  # distinguish a BLOCK (anti-bot / flood-control) from an authoritative empty
            html = raw[0] if isinstance(raw, tuple) else raw
            blocked, why = is_blocked(html if isinstance(html, str) else "")
            if blocked:
                diag.note(f"{self.name}.blocked", url=url, body=(
                    f"[] is a BLOCK, not 'no results': anti-bot / flood-control detected ({why}). "
                    f"NOT authoritative-empty — the paced/serialized retry self-heals; never read [] as 'nothing there'."))

        # Empty → short cooldown, not the full TTL (see EMPTY_TTL): a transient walled miss/block
        # must not blind this query for cache_ttl (and EMPTY_TTL spares the session a retry-storm).
        cache.set_docs(key, docs, ttl=self.cache_ttl if docs else EMPTY_TTL)
        return docs

    @staticmethod
    def _rank(docs: list[PolarisDocument], query: str) -> list[PolarisDocument]:
        """Score via the shared BM25 engine (title 3x + content 1x) — the one scorer
        RSS / search-ranking / keyword_score_filter share, so a base source can't drift.
        Term-less query keeps DOM order; else best-first, matches only."""
        if not relevance.query_terms(query):
            return docs
        scores = relevance.doc_scores(docs, query)
        scored = [(s, d) for s, d in zip(scores, docs) if s > 0.0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        """Claim ``url`` if its host contains ``url_host``, then drive the page via
        ``_fetch_flow`` and build a doc via ``_to_document``. Disabled (returns None)
        unless both ``url_host`` is set and ``_to_document`` is overridden."""
        if not self.url_host:
            return None
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if self.url_host not in host:
            return None
        try:
            raw = self._run(self._fetch_flow, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s fetch_url failed: %s", self.name, exc)
            return None
        try:
            return self._to_document(raw, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: _to_document failed: %s", self.name, exc)
            return None

    def health_check(self) -> tuple[bool, str]:
        """CDP connectivity + a light liveness probe.

        Default: prove the CDP Chrome is reachable (``cdp_health(cdp_url)``) and that
        navigating to ``_health_url`` (default = the search URL for a trivial query)
        returns a page. Override for a session-aware probe (e.g. zhihu checks the URL
        didn't bounce to /signin)."""
        ok, msg = cdp_health(self.cdp_url)
        if not ok:
            return False, f"CDP not reachable: {msg}"
        try:
            page_url = self._run(lambda p: p.url, self._health_url())
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if "/signin" in page_url or "/login" in page_url:
            return False, f"CDP Chrome not logged into {self.name}: the operator needs to VNC + log in"
        return True, f"OK (CDP + {self.name} session)"

    def _health_url(self) -> str:
        """Page the default health_check navigates to. Default: the search URL for a
        trivial query. Override to hit the site root or a cheaper liveness page."""
        return self._search_url("test")
