"""arXiv adapter — queries the arXiv Atom API directly through the shared http helpers.

arXiv has a free public API (``export.arxiv.org/api/query``) with no auth. We hit it via
``eye.http`` (shared UA / timeout / 30MB size-cap / streaming) and parse the Atom feed with
``feedparser`` — the SAME parser the official ``arxiv`` package uses internally, so the
field mapping is well-understood.

**2026-06-04 (P29):** migrated OFF the ``arxiv`` library. That library fetches via
``urllib`` with NO request timeout (it blocked all the way to our outer ``fetch_one``
deadline under arXiv's throttling) and uses its own dedicated User-Agent that arXiv
rate-limits independently. Routing through ``http.py`` gives a real per-request timeout,
our own UA, and the body-size cap — and degrades to ``[]`` on failure instead of blocking.

Native query syntax passes straight through: the API's ``search_query`` forwards the
string verbatim, so field prefixes + booleans work as-is — ``cat:cs.LG``, ``au:bengio``,
``ti:transformer``, ``abs:diffusion``, ``ti:llm AND cat:cs.CL``.

**B2 migration:** now rides ``BaseAPIAdapter`` (template-method base). The two hooks
``_raw_fetch`` / ``_to_document`` carry the verbatim arXiv I/O + field mapping; the base
supplies the cache-checked ``search`` skeleton + auto-registration. ``rank_locally=False``
preserves the server's ``sortBy=relevance`` order byte-for-byte (no local re-rank, exactly
as the hand-written form did). ``fetch_url`` (id_list by-id lookup) and ``health_check``
(treats HTTP 429 as alive) are overridden because they differ from the base defaults.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import feedparser
import httpx

from penumbra.core import http
from penumbra.core._guard import BackendGuard
from penumbra.core.normalize import Document
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

_API = "https://export.arxiv.org/api/query"

# arXiv load-guard: the shared BackendGuard (the concurrency cap + rate pacer + circuit breaker that
# _s2 / _openalex / _github already ride). arXiv rate-limits the export host aggressively (this adapter
# migrated off the arxiv lib for exactly that). The OLD sema bounded CONCURRENCY but NOT req/s, so 4
# concurrent workers could still burst past arXiv's ~3/s politeness line into a 429, and a throttled/dead
# host made every request eat the full timeout. The guard adds a min-interval RATE pacer (spaces request
# STARTS so a fan-out can't spike the rate), a breaker (consecutive failures -> fail fast + serve cache
# instead of hammering), and backlog fail-fast. _MAX_INFLIGHT stays 4 (concurrency unchanged);
# _MIN_INTERVAL_S ~= 3/s balances politeness vs the broad fan-out (too wide would get arXiv deadline-
# dropped in the ~95-source sweep). SHARED sync<->async so the async path cannot double the concurrency
# OR the rate. Mirrors _s2's _call (breaker -> pace -> sema -> record ok/fail).
_ARXIV_MAX_INFLIGHT = 4
_ARXIV_MIN_INTERVAL_S = 0.35     # ~3 req/s across ALL callers + threads (arXiv's politeness rate)
_ARXIV_PACE_MAX_WAIT_S = 12.0    # a caller waiting > this on the rate gate fails fast (< the 15s deadline)
_ARXIV_ACQUIRE_MAX_WAIT_S = 20.0  # a caller waiting > this for a CONCURRENCY permit sheds load (degrade to None)
_guard = BackendGuard("arxiv", _ARXIV_MAX_INFLIGHT, break_after=5, break_for_s=120.0,
                      min_interval_s=_ARXIV_MIN_INTERVAL_S, log=logger)


class _ArxivBusy(RuntimeError):
    """Internal signal: the rate-gate backlog is pathological (> _ARXIV_PACE_MAX_WAIT_S), so shed load +
    degrade to None (the caller returns []) instead of stacking an unbounded gate wait."""


def _arxiv_get_text(url: str, **kwargs):
    """Single arXiv egress chokepoint (sync): pass through the shared guard so the throttled arXiv host
    sees bounded concurrency (sema) + a bounded rate (pace) + a breaker. Returns None on breaker-open or
    a pathological rate-gate backlog (callers degrade to []); a falsy http result feeds the breaker as a
    failure so a sustained outage opens the circuit and fails fast."""
    if _guard.is_open():
        return None  # breaker open: skip the throttled host — no request, no wait, caller degrades to []
    try:
        _guard.pace(on_backlog=lambda w: _ArxivBusy() if w > _ARXIV_PACE_MAX_WAIT_S else None)
        with _guard.slot(_ARXIV_ACQUIRE_MAX_WAIT_S, lambda w: _ArxivBusy()):  # bounded: degrade, don't hang
            r = http.get_text(url, **kwargs)
    except _ArxivBusy:
        return None
    _guard.record_ok() if r else _guard.record_fail()
    return r


async def _arxiv_aget_text(url: str, **kwargs):
    """Async twin of _arxiv_get_text: SAME guard (breaker -> pace -> sema -> record), but the two blocking
    waits go OFF the loop — the rate-gate wait via ``await anyio.sleep`` (reserve_pace_slot is loop-safe
    arithmetic under a brief lock), the sema acquire via to_thread (a `with _guard.sema:` on the loop would
    freeze it). The guard is SHARED sync<->async so the async migration cannot double the concurrency OR
    the rate. Mirrors _s2's async pace path."""
    if _guard.is_open():
        return None
    try:
        wait = _guard.reserve_pace_slot(
            on_backlog=lambda w: _ArxivBusy() if w > _ARXIV_PACE_MAX_WAIT_S else None)
    except _ArxivBusy:
        return None
    if wait > 0:
        await anyio.sleep(wait)                           # rate gate, OFF-loop wait
    try:
        # shared cap, OFF-loop + SHIELDED + BOUNDED: a cancel can't take the permit then skip the
        # release (the leak that drained the pool); a saturated pool sheds load like the rate gate.
        async with _guard.aslot(_ARXIV_ACQUIRE_MAX_WAIT_S, lambda w: _ArxivBusy()):
            r = await http.aget_text(url, **kwargs)
    except _ArxivBusy:
        return None
    _guard.record_ok() if r else _guard.record_fail()
    return r


class ArxivAdapter(BaseAPIAdapter):
    name = "arxiv"
    needs_credentials = False
    description = "arXiv preprints — 3M+ papers across physics, math, CS, biology"

    # arXiv's API returns relevance-sorted results (sortBy=relevance); keep that
    # server order verbatim — no local re-rank — exactly as the hand form did.
    rank_locally = False
    cache_ttl = 3600
    url_host = "arxiv.org"

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        xml = _arxiv_get_text(_API, params={
            "search_query": query,
            "max_results": max(1, min(limit, 100)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        if not xml:
            return []  # network failure / timeout / oversize → empty (do NOT cache)
        feed = feedparser.parse(xml)
        return feed.entries

    def _to_document(self, raw) -> Document:
        return self._entry_to_document(raw)

    # ------------------------------------------------------------- async twins
    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async twin of _raw_fetch: BYTE-FAITHFUL mirror — same URL, same params (search_query,
        the max(1,min(limit,100)) clamp, sortBy=relevance, sortOrder=descending), same not-xml -> []
        contract, same feedparser.parse. ONLY the shared-http egress is swapped for its async twin:
        _arxiv_get_text -> await _arxiv_aget_text (both ride the SAME shared _guard: sema + pace + breaker)."""
        xml = await _arxiv_aget_text(_API, params={
            "search_query": query,
            "max_results": max(1, min(limit, 100)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        if not xml:
            return []  # network failure / timeout / oversize → empty (do NOT cache)
        feed = feedparser.parse(xml)
        return feed.entries

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip
        (_aapi_search); egress via _araw_fetch; mapping via the SAME pure-CPU _to_document, so it is
        behavior-identical to search (same cache key, per-record skip, rank_locally=False verbatim
        server order, cache-only-if-docs). The arXiv guard (_guard: sema + pace + breaker) stays shared with sync."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        # arXiv URLs: arxiv.org/abs/XXXX.YYYYY or arxiv.org/pdf/XXXX.YYYYY
        host = urlparse(url).hostname or ""
        if "arxiv.org" not in host:
            return None
        # /pdf/ URLs are full-text PDFs → defer to the pdf adapter (it extracts the WHOLE paper);
        # arxiv here only serves the abstract+metadata for /abs/. Skipping /pdf/ avoids shadowing it.
        if "/pdf/" in (urlparse(url).path or "").lower():
            return None
        arxiv_id = urlparse(url).path.rstrip("/").split("/")[-1].replace(".pdf", "")
        if not arxiv_id:
            return None
        xml = _arxiv_get_text(_API, params={"id_list": arxiv_id, "max_results": 1})
        if not xml:
            return None
        feed = feedparser.parse(xml)
        if not feed.entries:
            return None
        return self._entry_to_document(feed.entries[0])

    def health_check(self) -> tuple[bool, str]:
        # Light DIRECT probe with its OWN short timeout. A 429 means the API is UP and
        # merely throttling us → report healthy (search falls back to cache / degrades).
        try:
            with _guard.sema:  # count the health probe against the same global arXiv in-flight cap
                resp = httpx.get(
                    _API,
                    params={"search_query": "all:machine learning", "max_results": 1},
                    timeout=15,
                )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if resp.status_code == 200:
            return True, "OK"
        if resp.status_code == 429:
            return True, "OK (HTTP 429 — API alive, rate-limiting us)"
        return False, f"HTTP {resp.status_code}"

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _dt(parsed) -> Optional[datetime]:
        """feedparser ``*_parsed`` struct_time (UTC) → tz-aware datetime."""
        if not parsed:
            return None
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _entry_to_document(e) -> Document:
        entry_id = e.get("id", "") or ""
        arxiv_id = entry_id.rsplit("/", 1)[-1]
        authors = [a.get("name", "").strip() for a in (e.get("authors") or []) if a.get("name")]
        cats = [t.get("term") for t in (e.get("tags") or []) if t.get("term")]
        primary = (e.get("arxiv_primary_category") or {}).get("term")
        pdf_url = next(
            (l.get("href") for l in (e.get("links") or [])
             if l.get("title") == "pdf" or l.get("type") == "application/pdf"),
            None,
        )
        title = (e.get("title") or "").strip().replace("\n", " ")
        summary = (e.get("summary") or "").strip()
        return Document(
            source="arxiv",
            source_id=arxiv_id,
            url=entry_id,
            title=title or "(untitled)",
            content=summary,  # full abstract — no truncation
            author=", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "") or None,
            date=ArxivAdapter._dt(e.get("published_parsed")),
            tags=cats,
            metadata={
                "pdf_url": pdf_url,
                "doi": e.get("arxiv_doi"),
                "journal_ref": e.get("arxiv_journal_ref"),
                "primary_category": primary,
                "comment": e.get("arxiv_comment"),
                "updated": ArxivAdapter._dt(e.get("updated_parsed")).isoformat()
                if e.get("updated_parsed") else None,
                "all_authors": authors,
            },
        )
