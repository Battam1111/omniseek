"""Government open-data — config-driven dataset search across national/regional portals.

A single keyless adapter that fans out over several government open-data portals and
merges the hits into one dataset feed. It fills OmniSeek's gap on OFFICIAL government
statistics + open datasets (income/wage tables, labour-force series, census/economic
indicators) for Singapore, Hong Kong, and Canada, where the canonical numbers live on
the portal rather than in scholarship.

Two portal shapes are handled (the config row says which):

  * CKAN ``package_search`` (the common open-data stack):
        GET {portal}/api/3/action/package_search?q=<q>&rows=N
        -> {"result": {"results": [{title, notes, organization{title}, name, resources[]}]}}
    Used by data.gov.hk (``/en-data/api/3/...``) and open.canada.ca (``/data/api/3/...``).
    ``name`` is the slug for the dataset page; ``notes`` is the description; each resource
    carries a ``format`` (CSV/XLSX/JSON/...).

  * data.gov.sg v2 (NOT CKAN: the SG portal migrated off classic CKAN, the old
    ``/api/action/package_search`` now 404s and the v2 ``/datasets`` endpoint ignores all
    query params, returning the same unfiltered page regardless). So SG is handled as a
    client-side filter: page through the keyless listing
        GET https://api-production.data.gov.sg/v2/public/api/datasets?page=N
        -> {"data": {"datasets": [{datasetId, name, description, format, managedByAgencyName}], "pages": M}}
    and keep the records whose name/description contain the query terms. Bounded to a few
    pages so a broad query stays cheap (the portal has ~4.4k datasets / ~440 pages).

Emits one dataset doc per hit: title, content = the description + a one-line note on the
resource formats, url = the human dataset page, metadata = {portal, organization}.

Thin subclass over BaseScrapeAdapter: the cache check / atomic set_docs / self-registration
ritual lives in the base; this adapter overrides ``_raw_fetch`` (the multi-portal fan-out)
and ``_to_documents`` (the per-portal record -> doc map). ``rank`` is set True because
merging several portals' independent server orders has no single meaningful order, so we
re-score the merged set with the shared BM25 engine (the same scorer RSS / ranked-search
use) for a coherent best-first result.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, Optional

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

TIMEOUT = 15
# SG client-side filter is bounded: scan at most this many listing pages (10 datasets/page),
# so even a zero-hit broad query costs a fixed handful of GETs rather than ~440 pages.
SG_MAX_PAGES = 6

# Portal config. Each row declares how to fetch + how to build a dataset page URL.
#   kind="ckan"  -> CKAN package_search; page_url = page_base + record["name"] (the slug)
#   kind="sg_v2" -> data.gov.sg v2 client-side filter; page_url = page_base + datasetId + "/view"
PORTALS: list[dict[str, Any]] = [
    {
        "id": "sg",
        "region": "sg",
        "label": "data.gov.sg",
        "kind": "sg_v2",
        "list_url": "https://api-production.data.gov.sg/v2/public/api/datasets",
        "page_base": "https://data.gov.sg/datasets/",
    },
    {
        "id": "hk",
        "region": "hk",
        "label": "data.gov.hk",
        "kind": "ckan",
        "search_url": "https://data.gov.hk/en-data/api/3/action/package_search",
        "page_base": "https://data.gov.hk/en-data/dataset/",
    },
    {
        "id": "ca",
        "region": "ca",
        "label": "open.canada.ca",
        "kind": "ckan",
        "search_url": "https://open.canada.ca/data/api/3/action/package_search",
        "page_base": "https://open.canada.ca/data/en/dataset/",
    },
]


class GovOpenDataAdapter(BaseScrapeAdapter):
    name = "gov_open_data"
    needs_credentials = False
    description = "Government open-data datasets (income/labour/census/economic stats) across data.gov.sg, data.gov.hk, open.canada.ca (keyless)"
    cache_ttl = 900
    kind = "lookup"
    domains = ["data", "compensation"]
    modes = ["STRUCTURE"]
    regions = ["sg", "hk", "ca"]
    # Merged multi-portal set has no single meaningful server order: re-score with the shared engine.
    rank = True

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Fan out over every portal CONCURRENTLY; collect (portal, record) pairs. One portal failing
        (None / exception) is skipped, not fatal: the others still answer. Returns None
        only if EVERY portal failed (so the base degrades to [] honestly).

        PARALLEL since 2026-07-25, which is what this docstring always claimed. It was a sequential
        loop, so this source's wall clock was the SUM of every portal: measured 21.3s over 8 requests,
        which blew the broad-search deadline on 62% of searches (1231 of 1986 recorded) and left its
        contribution to the ranked output at exactly ZERO. Yet no portal is actually slow: each answers
        in ~1-3s (open.canada.ca measured 1.7 / 3.8 / 1.7s). Summing them was the entire problem, so
        the wall clock is now the SLOWEST portal instead, not the total.

        Each task runs under a COPIED contextvars Context (the house pattern, cf. researcher_watch):
        the per-request cache flags (fresh / cache_only) ride contextvars, so a bare thread would
        silently drop them and read stale cache on a fresh search. Result ORDER is unchanged, since
        ex.map preserves input order and the zip below re-pairs against PORTALS."""
        pairs: list[tuple[dict, dict]] = []
        any_ok = False

        def _one(portal: dict) -> Optional[list[dict]]:
            try:
                return self._fetch_portal(portal, query, limit)
            except Exception as exc:  # noqa: BLE001 — a single portal hiccup is non-fatal
                logger.warning("%s: portal %s failed: %s", self.name, portal["id"], exc)
                return None

        contexts = [copy_context() for _ in PORTALS]
        with ThreadPoolExecutor(max_workers=min(len(PORTALS), 8)) as ex:
            per_portal = list(ex.map(lambda ctx, p: ctx.run(_one, p), contexts, PORTALS))
        for portal, records in zip(PORTALS, per_portal):
            if records is None:
                continue
            any_ok = True
            for rec in records:
                pairs.append((portal, rec))
        if not any_ok:
            return None  # every portal failed -> base turns this into []
        return pairs

    def _fetch_portal(self, portal: dict, query: str, limit: int) -> Optional[list[dict]]:
        """Fetch one portal's matching records (already sliced to ``limit``). None on failure."""
        kind = portal["kind"]
        if kind == "ckan":
            raw = http.get_json(
                portal["search_url"],
                params={"q": query, "rows": limit},
                timeout=TIMEOUT,
            )
            if not isinstance(raw, dict):
                return None
            results = ((raw.get("result") or {}).get("results")) or []
            return [r for r in results if isinstance(r, dict)][:limit]
        if kind == "sg_v2":
            return self._fetch_sg(portal, query, limit)
        logger.warning("%s: unknown portal kind %r", self.name, kind)
        return None

    def _fetch_sg(self, portal: dict, query: str, limit: int) -> Optional[list[dict]]:
        """data.gov.sg has no server-side search: page the listing and filter locally by
        query terms over name + description. Bounded by SG_MAX_PAGES; stops once ``limit``
        hits are found or the page count is exhausted."""
        terms = [t for t in query.lower().split() if t]
        hits: list[dict] = []
        saw_any_page = False
        page = 1
        while page <= SG_MAX_PAGES:
            raw = http.get_json(portal["list_url"], params={"page": page}, timeout=TIMEOUT)
            if not isinstance(raw, dict):
                break
            data = raw.get("data") or {}
            datasets = data.get("datasets") or []
            if not datasets:
                break
            saw_any_page = True
            for ds in datasets:
                if not isinstance(ds, dict):
                    continue
                if _sg_matches(ds, terms):
                    hits.append(ds)
                    if len(hits) >= limit:
                        return hits
            total_pages = data.get("pages")
            if isinstance(total_pages, int) and page >= total_pages:
                break
            page += 1
        if not saw_any_page:
            return None  # could not read even one page -> a real failure for this portal
        return hits[:limit]

    # ── native-async egress twins (byte-faithful mirror of the sync fan-out) ─
    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: byte-faithful mirror of the multi-portal fan-out. The ONLY
        change down the whole chain is the shared-http egress fn (http.get_json -> await
        http.aget_json) inside _afetch_portal / _afetch_sg; same control flow, same per-portal
        skip-on-failure, same all-portals-failed -> None contract."""
        pairs: list[tuple[dict, dict]] = []
        any_ok = False
        for portal in PORTALS:
            try:
                records = await self._afetch_portal(portal, query, limit)
            except Exception as exc:  # noqa: BLE001 — a single portal hiccup is non-fatal
                logger.warning("%s: portal %s failed: %s", self.name, portal["id"], exc)
                continue
            if records is None:
                continue
            any_ok = True
            for rec in records:
                pairs.append((portal, rec))
        if not any_ok:
            return None  # every portal failed -> base turns this into []
        return pairs

    async def _afetch_portal(self, portal: dict, query: str, limit: int) -> Optional[list[dict]]:
        """Async twin of _fetch_portal: same URL / params / timeout, egress swapped to http.aget_json."""
        kind = portal["kind"]
        if kind == "ckan":
            raw = await http.aget_json(
                portal["search_url"],
                params={"q": query, "rows": limit},
                timeout=TIMEOUT,
            )
            if not isinstance(raw, dict):
                return None
            results = ((raw.get("result") or {}).get("results")) or []
            return [r for r in results if isinstance(r, dict)][:limit]
        if kind == "sg_v2":
            return await self._afetch_sg(portal, query, limit)
        logger.warning("%s: unknown portal kind %r", self.name, kind)
        return None

    async def _afetch_sg(self, portal: dict, query: str, limit: int) -> Optional[list[dict]]:
        """Async twin of _fetch_sg: same SG_MAX_PAGES bound / local AND-filter / stop conditions,
        egress swapped to http.aget_json."""
        terms = [t for t in query.lower().split() if t]
        hits: list[dict] = []
        saw_any_page = False
        page = 1
        while page <= SG_MAX_PAGES:
            raw = await http.aget_json(portal["list_url"], params={"page": page}, timeout=TIMEOUT)
            if not isinstance(raw, dict):
                break
            data = raw.get("data") or {}
            datasets = data.get("datasets") or []
            if not datasets:
                break
            saw_any_page = True
            for ds in datasets:
                if not isinstance(ds, dict):
                    continue
                if _sg_matches(ds, terms):
                    hits.append(ds)
                    if len(hits) >= limit:
                        return hits
            total_pages = data.get("pages")
            if isinstance(total_pages, int) and page >= total_pages:
                break
            page += 1
        if not saw_any_page:
            return None  # could not read even one page -> a real failure for this portal
        return hits[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip;
        egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents (byte-identical to search)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        """Merged (portal, record) pairs -> dataset docs. The base re-ranks (rank=True) and
        OmniSeek's ranked search re-scores across sources, so we do NOT slice to ``limit`` here:
        each portal already capped its own contribution at ``limit``."""
        if not isinstance(raw, list):
            return []
        docs: list[Document] = []
        for item in raw:
            try:
                portal, rec = item
            except (TypeError, ValueError):
                continue
            doc = self._record_to_doc(portal, rec)
            if doc is not None:
                docs.append(doc)
        return docs

    def _record_to_doc(self, portal: dict, rec: dict) -> Optional[Document]:
        if portal["kind"] == "ckan":
            return self._ckan_to_doc(portal, rec)
        if portal["kind"] == "sg_v2":
            return self._sg_to_doc(portal, rec)
        return None

    def _ckan_to_doc(self, portal: dict, rec: dict) -> Optional[Document]:
        title = (rec.get("title") or rec.get("name") or "").strip()
        if not title:
            return None
        slug = rec.get("name") or ""
        url = f"{portal['page_base']}{slug}" if slug else portal["page_base"].rstrip("/")

        org_raw = rec.get("organization") or {}
        organization = (org_raw.get("title") or org_raw.get("name")) if isinstance(org_raw, dict) else None

        notes = (rec.get("notes") or "").strip()
        resources = rec.get("resources") or []
        formats = _ckan_formats(resources)

        content = _compose_content(notes, formats, len(resources))

        tags = [t.get("name") for t in (rec.get("tags") or [])
                if isinstance(t, dict) and t.get("name")]

        metadata: dict[str, Any] = {
            "portal": portal["label"],
            "region": portal["region"],
            "organization": organization,
            "resource_formats": formats,
            "num_resources": len(resources),
            "license": rec.get("license_title"),
            "raw": jsonsafe(rec),
        }
        # A dataset has no engagement count; carry the resource tally as a "other"-kind fact.
        signals = mk_signal(
            "resources", len(resources), kind="other", by=f"{portal['id']}/num_resources",
            unit="resources",
        )

        return Document(
            source=self.name,
            source_id=f"{portal['id']}:{slug or title}",
            url=url,
            title=title,
            content=content,
            author=organization or None,
            signals=signals,
            tags=tags,
            metadata=metadata,
        )

    def _sg_to_doc(self, portal: dict, rec: dict) -> Optional[Document]:
        title = (rec.get("name") or "").strip()
        if not title:
            return None
        ds_id = rec.get("datasetId") or ""
        url = f"{portal['page_base']}{ds_id}/view" if ds_id else portal["page_base"].rstrip("/")

        organization = (rec.get("managedByAgencyName") or "").strip() or None
        notes = (rec.get("description") or "").strip()
        fmt = rec.get("format")
        formats = [fmt] if isinstance(fmt, str) and fmt else []

        content = _compose_content(notes, formats, len(formats))

        metadata: dict[str, Any] = {
            "portal": portal["label"],
            "region": portal["region"],
            "organization": organization,
            "resource_formats": formats,
            "dataset_id": ds_id,
            "raw": jsonsafe(rec),
        }
        signals = mk_signal(
            "resources", len(formats), kind="other", by=f"{portal['id']}/format",
            unit="resources",
        )

        return Document(
            source=self.name,
            source_id=f"{portal['id']}:{ds_id or title}",
            url=url,
            title=title,
            content=content,
            author=organization,
            signals=signals,
            tags=[],
            metadata=metadata,
        )


# ── helpers ─────────────────────────────────────────────────────────────────

def _sg_matches(ds: dict, terms: list[str]) -> bool:
    """A SG record matches when EVERY query term appears in its name or description
    (an AND match, mirroring a search engine's default). A term-less query matches all."""
    if not terms:
        return True
    hay = ((ds.get("name") or "") + " " + (ds.get("description") or "")).lower()
    return all(t in hay for t in terms)


def _ckan_formats(resources: list) -> list[str]:
    """Distinct upper-cased resource formats (CSV/XLSX/JSON/...), order-preserving."""
    seen: list[str] = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        fmt = (r.get("format") or "").strip().upper()
        if fmt and fmt not in seen:
            seen.append(fmt)
    return seen


def _compose_content(notes: str, formats: list[str], num_resources: int) -> str:
    """description (if any) + a one-line note on the downloadable resources/formats."""
    lines: list[str] = []
    if notes:
        lines.append(notes)
    if formats:
        lines.append(
            f"Resources: {num_resources} file(s); formats: {', '.join(formats)}."
        )
    elif num_resources:
        lines.append(f"Resources: {num_resources} file(s).")
    if not lines:
        lines.append("Government open-data dataset. See the dataset page for resources.")
    return "\n\n".join(lines)


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
