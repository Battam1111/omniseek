"""CORE (core.ac.uk): the world's largest aggregator of open-access research,
and OmniSeek's only source of EXTRACTED FULL-TEXT paper bodies.

Why this exists, against the existing paper adapters:
- arxiv / semantic_scholar / openalex / crossref return METADATA only
  (title + abstract + citations + venue). They never carry the paper body.
- CORE harvests the open-access PDFs themselves and extracts the text, so a
  work returned here carries ``fullText`` (the whole document as plain text)
  plus a working OA download URL. That body is content OmniSeek gets from NO
  other source: the differentiator this adapter adds.

API (https://core.ac.uk/documentation/api ; v3):
- GET /v3/search/works/  (note the TRAILING SLASH; no-slash 301s to a
  Cloudflare interstitial). params: q, limit, offset, sort=relevance.
- Each result work carries: title, abstract, fullText, doi, yearPublished,
  publishedDate, authors[].name, journals[].title, publisher, downloadUrl,
  sourceFulltextUrls[], fieldOfStudy[], documentType, id.
- ``q`` may include ``_exists_:fullText`` to restrict to body-bearing works.

Auth: FREE REGISTERED key REQUIRED (needs_credentials=True). The free
no-registration tier was probed live (this host + a US egress, within the
documented 5-req/10s pace) and returned 429 / 500 every time: it is not usable
for an automated client. A free registered key (https://core.ac.uk/services/api,
no cost) raises the per-key limit to usable. The key lives only on the host at
~/.omniseek/credentials/core.json -> {"api_key": "..."} and is sent as
``Authorization: Bearer <key>``. With no per-call COST, CORE is NOT explicit_only:
it joins the papers broad fan-out alongside openalex / s2 / crossref.

Built on BaseAPIAdapter (the two-hook template): ``_raw_fetch`` does the keyed
GET, ``_to_document`` carries every CORE-specific fact. ``rank_locally=False``
keeps CORE's server-side relevance order verbatim. The shared ``http`` helper is
deliberately bypassed in ``_raw_fetch`` because it sends no auth header (keyed /
walled adapters keep bespoke headers; see http.py's module docstring).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from omniseek.core import auth
from omniseek.core._guard import GateBusy, bounded_async_slot, bounded_slot
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

CORE_SEARCH = "https://api.core.ac.uk/v3/search/works/"  # trailing slash matters
CORE_WORK = "https://api.core.ac.uk/v3/works"            # /{coreId} by-id lookup
TIMEOUT = 30
# Full body can be large; cap the inline preview so a single result does not blow
# the tool payload. The TRUE length is stamped in metadata so the agent knows to
# omniseek_read the PDF (downloadUrl) for the whole document.
_BODY_PREVIEW_CAP = 12000

# Drop a credential template on first import (free registered key, see module docstring).
auth.write_template(
    "core",
    {"_comment": "FREE registered key from https://core.ac.uk/services/api (no cost). "
                 "The anonymous no-key tier is rate-walled and not usable.",
     "api_key": ""},
)

# Global in-flight cap on CORE egress: api.core.ac.uk runs on ONE shared registered-key quota (free
# tier ~5 req/10s). CORE bypasses the shared http helper (keyed source, direct httpx), so it had no
# concurrency ceiling; under the broad-fan-out / multi-agent burst that storms the one quota. This
# module-global semaphore (held only around the egress, in _core_get) caps concurrent requests so a
# burst paces through instead of cascading into 429s, mirroring _s2 / _openalex / reddit.
_CORE_MAX_INFLIGHT = 3
_core_sema = threading.BoundedSemaphore(_CORE_MAX_INFLIGHT)


def _core_get(url: str, **kwargs):
    """Single CORE egress chokepoint: all httpx.get to api.core.ac.uk pass through here so the global
    in-flight cap (_core_sema) bounds concurrent requests to the shared key-quota host."""
    # Reuse the existing wire timeout as the queue budget. CORE may return large full-text pages, so
    # the queue is allowed the same patience as the request but can never wait forever.
    max_wait = float(kwargs.get("timeout", TIMEOUT))
    with bounded_slot(
        _core_sema,
        max_wait,
        lambda waited: GateBusy(f"CORE gate busy after {waited:.1f}s"),
    ):
        return httpx.get(url, **kwargs)


# ── ASYNC EGRESS TWIN (S4) ───────────────────────────────────────────────────────────────────────
# CORE's search path goes native async. A DEDICATED module-level AsyncClient, NOT http.aget*, because
# CORE's full-text search response (a page of whole document bodies — the fullText differentiator this
# adapter exists for) can exceed http.aget*'s 30MB stream cap, which aborts + returns None -> [] on
# exactly the large full-text responses CORE is reached for. So mirror the sync raw-httpx egress
# (uncapped) instead. The fixed api.core.ac.uk host means no SSRF surface, same as the sync path (which
# also uses raw httpx with no guard). Lazy + double-checked-lock like _openalex._aget_client; per-call
# follow_redirects / headers / timeout ride in _acore_get's **kwargs, identical to the sync _core_get.
_acore_client_obj: Optional[httpx.AsyncClient] = None
_acore_client_lock = threading.Lock()  # construction is sync (no await); double-check like _get_client


def _acore_client() -> httpx.AsyncClient:
    """Lazily build (once) the module-level pooled async client for CORE egress. Double-checked lock so
    the first concurrent async callers create exactly one. Uncapped (no MAX_BYTES) to match the sync
    raw-httpx egress: a full-text page is CORE's high-value payload and must not be truncated."""
    global _acore_client_obj
    if _acore_client_obj is None:
        with _acore_client_lock:
            if _acore_client_obj is None:
                _acore_client_obj = httpx.AsyncClient(
                    timeout=TIMEOUT,
                    limits=httpx.Limits(max_keepalive_connections=4, max_connections=8,
                                        keepalive_expiry=30.0),
                )
    return _acore_client_obj


async def _acore_get(url: str, **kwargs):
    """Async twin of ``_core_get``: the SAME single CORE egress chokepoint, so the SHARED ``_core_sema``
    in-flight cap bounds sync + async requests TOGETHER against the one shared-key host (the reddit
    ``_arctic_sema`` precedent — ONE ``threading.BoundedSemaphore``, never a second ``asyncio`` one, so
    the async migration can never double the CORE burst). ``bounded_async_slot`` acquires it OFF the
    loop, bounds the queue wait by the existing request timeout, and keeps acquire/release paired under
    cancellation. Egress uses the module-level uncapped AsyncClient (see ``_acore_client``)."""
    max_wait = float(kwargs.get("timeout", TIMEOUT))
    async with bounded_async_slot(
        _core_sema,
        max_wait,
        lambda waited: GateBusy(f"CORE gate busy after {waited:.1f}s"),
    ):
        return await _acore_client().get(url, **kwargs)


class CoreAdapter(BaseAPIAdapter):
    name = "core"
    needs_credentials = True
    description = (
        "CORE (core.ac.uk): largest open-access research aggregator; the ONLY eye "
        "paper source that returns the EXTRACTED FULL-TEXT body (work.fullText) + a "
        "working OA PDF url, where arxiv/openalex/semantic_scholar/crossref give only "
        "metadata. Reach for it to read a paper's actual text, not just its abstract."
    )

    # CORE sorts server-side by relevance; preserve that order verbatim.
    rank_locally = False
    cache_ttl = 3600
    url_host = "core.ac.uk"

    @staticmethod
    def _key() -> Optional[str]:
        return (auth.load("core") or {}).get("api_key")

    @staticmethod
    def _headers(key: str) -> dict:
        return {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "omniseek/0.1 (+https://github.com/cyj/omniseek)",
        }

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        """GET /v3/search/works/ (keyed) and return the ``results`` list ([] on any failure).

        Bypasses the shared http helper on purpose: this is a keyed source and the
        shared GET sends no Authorization header. No-key -> [] (the adapter degrades
        to empty, surfaced to the agent as 'needs_credentials')."""
        key = self._key()
        if not key:
            logger.warning("core: no api_key configured (~/.omniseek/credentials/core.json)")
            return []
        try:
            resp = _core_get(
                CORE_SEARCH,
                params={"q": query, "limit": min(limit, 25), "offset": 0,
                        "sort": "relevance"},
                headers=self._headers(key),
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001; failure -> empty is the adapter contract
            logger.warning("CORE search failed: %s", exc)
            return []
        results = data.get("results") if isinstance(data, dict) else None
        return results or []

    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async twin of ``_raw_fetch`` (S4): the keyed GET /v3/search/works/ goes native async so the
        live async omniseek_search awaits it directly instead of parking a pool thread on the CORE round-trip.
        BYTE-FAITHFUL mirror of ``_raw_fetch`` — same no-key gate + warn, same endpoint / params / auth
        headers / timeout / follow_redirects, same ``results`` extraction, same failure -> [] contract.
        ONLY the egress swaps: the sync ``_core_get`` (raw httpx.get under the shared _core_sema) -> the
        async ``_acore_get`` (await the module-level AsyncClient under the SAME shared _core_sema). The
        response ``.raise_for_status()`` / ``.json()`` run on the already-read body (pure CPU, on loop)."""
        key = self._key()
        if not key:
            logger.warning("core: no api_key configured (~/.omniseek/credentials/core.json)")
            return []
        try:
            resp = await _acore_get(
                CORE_SEARCH,
                params={"q": query, "limit": min(limit, 25), "offset": 0,
                        "sort": "relevance"},
                headers=self._headers(key),
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001; failure -> empty is the adapter contract
            logger.warning("CORE search failed: %s", exc)
            return []
        results = data.get("results") if isinstance(data, dict) else None
        return results or []

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> makes CORE AsyncSearchCapable (routed to the fetcher's
        native async dispatch). Shares the base async cache round-trip (``_aapi_search``: SAME cache key
        ``(name, search_label, query, limit)`` off-loop, per-record ``_to_document``, ``rank_locally=False``
        so CORE's server-side relevance order is preserved verbatim, cache-only-if-docs); egress via the
        native-async ``_araw_fetch``; per-record mapping via the SAME pure-CPU ``_to_document`` /
        ``_work_to_document`` (byte-identical to ``search``). SAME cache as ``search`` (async + sync share it)."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    def _to_document(self, raw) -> Optional[Document]:
        return self._work_to_document(raw)

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        """Claim core.ac.uk/works/{id} (by-id lookup) and doi.org/{doi} URLs."""
        host = (urlparse(url).hostname or "").lower()
        core_id: Optional[str] = None
        if "core.ac.uk" in host:
            # https://core.ac.uk/works/12345  ->  12345
            parts = [p for p in urlparse(url).path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "works":
                core_id = parts[1]
        if not core_id:
            return None
        key = self._key()
        if not key:
            return None
        try:
            resp = _core_get(f"{CORE_WORK}/{core_id}", headers=self._headers(key),
                             timeout=TIMEOUT, follow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            work = resp.json()
            if not isinstance(work, dict):
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("CORE fetch_url failed: %s", exc)
            return None
        return self._work_to_document(work)

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[bool, str]:
        key = self._key()
        if not key:
            return False, "no api_key (~/.omniseek/credentials/core.json)"
        try:
            resp = _core_get(CORE_SEARCH, params={"q": "test", "limit": 1},
                             headers=self._headers(key), timeout=10,
                             follow_redirects=True)
            # 429 = alive but throttling (key valid, just paced); treat as healthy-ish.
            ok = resp.status_code in (200, 429)
            return ok, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # ----------------------------------------------------------- doc assembly
    @staticmethod
    def _work_to_document(work: dict) -> Optional[Document]:
        if not isinstance(work, dict):
            return None

        core_id = str(work.get("id") or "")
        doi = (work.get("doi") or "").strip()
        # URL preference: canonical DOI, else the CORE work page.
        if doi:
            url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        elif core_id:
            url = f"https://core.ac.uk/works/{core_id}"
        else:
            url = (work.get("downloadUrl") or "").strip()
        if not url:
            return None

        title = (work.get("title") or "(untitled)").strip()

        # Authors: list of {"name": ...}
        author_names: list[str] = []
        for a in (work.get("authors") or []):
            if isinstance(a, dict) and a.get("name"):
                author_names.append(str(a["name"]).strip())
        author_str = ", ".join(author_names[:5]) if author_names else None
        if author_str and len(author_names) > 5:
            author_str += f" et al. ({len(author_names)} authors)"

        # Date: publishedDate (ISO) else depositedDate else yearPublished.
        date = None
        for f in ("publishedDate", "depositedDate"):
            v = work.get(f)
            if v:
                try:
                    date = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                    break
                except (ValueError, TypeError):
                    date = None
        if date is None and work.get("yearPublished"):
            try:
                date = datetime(int(work["yearPublished"]), 1, 1, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                date = None

        # THE DIFFERENTIATOR: the extracted full-text body. Preview-capped for the
        # tool payload; the TRUE length is stamped so the agent knows there is more
        # (omniseek_read the PDF for the whole document). Abstract is the fallback.
        full_text = (work.get("fullText") or "").strip()
        abstract = (work.get("abstract") or "").strip()
        full_text_chars = len(full_text)
        if full_text:
            content = full_text[:_BODY_PREVIEW_CAP]
            if full_text_chars > _BODY_PREVIEW_CAP:
                content += (f"\n\n[... full text continues: {full_text_chars} chars total; "
                            f"open the PDF (metadata.download_url) for the whole document ...]")
        elif abstract:
            content = abstract
        else:
            content = "(no full text or abstract available)"

        # OA PDF / download urls -> metadata + media (a fetch/read path can reach them).
        download_url = (work.get("downloadUrl") or "").strip() or None
        source_pdfs = [u for u in (work.get("sourceFulltextUrls") or []) if u]
        media = [u for u in [download_url, *source_pdfs] if u]

        journals = [j.get("title") for j in (work.get("journals") or [])
                    if isinstance(j, dict) and j.get("title")]
        venue = journals[0] if journals else None

        return Document(
            source="core",
            source_id=core_id or doi or url,
            url=url,
            title=title,
            content=content,
            author=author_str,
            date=date,
            signals=mk_signal("citations", work.get("citationCount"),
                              kind="citation", by="core/citationCount"),
            tags=list(work.get("fieldOfStudy") or []) if isinstance(work.get("fieldOfStudy"), list)
                 else ([work["fieldOfStudy"]] if work.get("fieldOfStudy") else []),
            media=media,
            metadata={
                "core_id": core_id or None,
                "doi": doi or None,
                "has_full_text": bool(full_text),
                "full_text_chars": full_text_chars,
                "abstract": abstract or None,
                "download_url": download_url,        # CORE-hosted OA PDF
                "source_fulltext_urls": source_pdfs,  # origin OA PDF(s)
                "venue": venue,
                "publisher": work.get("publisher"),
                "year": work.get("yearPublished"),
                "document_type": work.get("documentType"),
                "cited_by_count": work.get("citationCount"),
                "raw": jsonsafe(work),  # CORE work original dict (lossless escape hatch)
            },
        )
