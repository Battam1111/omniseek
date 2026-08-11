"""oecd_ai_policy — the OECD.AI Policy Navigator, the world's living registry of national AI-policy
initiatives (STRUCTURE / MONITOR). The single largest official catalog of government AI policies:
2364 initiatives across 80+ jurisdictions and intergovernmental organisations, each with a jurisdiction,
an instrument type, a category, a binding status, start/end years, and a link to the primary document.

The oecd.ai/en/dashboards/policy-initiatives dashboard is powered by a keyless JSON API:

    GET https://api.oecdai.org/policy-initiatives?page=N

Response: ``{data:[...], currentPage, lastPage, total, perPage}``. ``perPage`` is fixed server-side at
20 (page-size params are ignored), the only working knob is ``page``, and the endpoint has NO free-text
search: the dashboard's own search resolves text to ontology ``conceptUris`` first, so there is no
single ``{query}`` GET a declarative row could use. The list is sorted id-descending = newest-added
first (page 1 = the most recently added / updated initiatives, page 119 = the 2025 launch import).

Full-corpus snapshotting is INFEASIBLE inside the eye's ~90s fetch deadline: each page is ~86KB (the
long ``overview`` HTML) at ~3s, so 119 pages would be ~350s. So this adapter takes the NEWEST slice:
it paginates the first ``_PAGES`` pages (the recently added / updated initiatives across jurisdictions),
caches those docs query-independent for 30 days, and BM25-filters the cached subset per query (zero
network). This is a MONITOR lens on what is entering the navigator, not the full historical registry.

Thin subclass over BulkFundingBase (the bulk-snapshot + per-query-BM25 base): the cache + BM25 + the
fetch_url no-op live in the base; this adapter declares facets + ``_version`` and fills
``_build_subset_docs`` (paginate the newest pages, map each initiative to a policy Document).

Verified live 2026-07-10: total=2364, lastPage=119, keyless, charset utf-8. Some older ``overview``
fields carry source-side mojibake, so this adapter builds ``content`` from the clean ``description`` +
structured facet fields, never the raw ``overview`` HTML. explicit_only: a named-query lookup (bounded
newest slice, slow cold fetch), like the funding drills.
"""

from __future__ import annotations

import functools
import logging
import time
from datetime import datetime
from typing import Any, Optional

import anyio

from penumbra.core import cache, diag, http
from penumbra.core.normalize import Document, keyword_score_filter
from penumbra.core.sources.api._bulk_funding import BulkFundingBase

logger = logging.getLogger(__name__)

_API = "https://api.oecdai.org/policy-initiatives"
_DETAIL = "https://oecd.ai/en/dashboards/policy-initiatives/"  # + slug
_PAGES = 15          # newest ~300 initiatives; ~45s cold fetch, safely under the 90s deadline
_ELAPSED_CAP = 60.0  # stop paginating early if the cumulative fetch runs long (network variance guard)


class OecdAiPolicyAdapter(BulkFundingBase):
    name = "oecd_ai_policy"
    description = (
        "OECD.AI Policy Navigator — 全球官方 AI 政策倡议活册 (STRUCTURE/MONITOR). 全世界最大的政府 AI 政策目录: "
        "2364 项倡议, 覆盖 80+ 法域与政府间组织, 每项带 法域/工具类型/类别/约束力状态/起止年份 + 原始政策文件链接. "
        "端点无全文检索且 perPage 固定 20 (119 页 × ~3s 无法在抓取窗口内快照全量), 故本源取 最新切片: 抓最新 "
        "~300 项 (按加入时间倒序 = 最近新增/更新的倡议, 跨法域), 缓存 30 天, 逐查询 BM25 过滤. "
        "这是对 谁在进入导航册 的 MONITOR 视角, 非全史注册表. 命名钻取 (penumbra_search 单源 raw)."
    )
    explicit_only = (
        "OECD.AI 政策倡议 最新切片 (最新 ~300 项, 冷启动抓取较慢约 45s; 无全文检索故取最新而非全量); "
        "命名钻取 (penumbra_search 单源 raw); 月级刷新"
    )
    domains = ["policy"]
    regions = ["global"]
    modes = ["MONITOR", "STRUCTURE"]
    _version = "v1-newest15"

    def _build_subset_docs(self) -> list[Document]:
        docs: list[Document] = []
        seen: set[int] = set()
        started = time.monotonic()
        for page in range(1, _PAGES + 1):
            if time.monotonic() - started > _ELAPSED_CAP:
                logger.info("oecd_ai_policy: elapsed cap hit at page %d, stopping", page)
                break
            payload = http.get_json(_API, params={"page": page}, timeout=15)
            if not isinstance(payload, dict):
                diag.note("oecd_ai_policy.page", url=_API, status=None, body=f"page={page}")
                break  # keep the pages already gathered; do not cache a full miss (base skips empty)
            rows = payload.get("data") or []
            if not rows:
                break
            for row in rows:
                d = self._row_to_doc(row)
                if d is not None and row.get("id") not in seen:
                    seen.add(row.get("id"))
                    docs.append(d)
            if page >= (payload.get("lastPage") or _PAGES):
                break
        logger.info("oecd_ai_policy: built %d initiative docs from newest %d pages", len(docs), _PAGES)
        return docs

    async def _abuild_subset_docs(self) -> list[Document]:
        """Async egress twin of ``_build_subset_docs`` (S4b): byte-for-byte the same paginate / dedup /
        map loop, the ONLY change being ``http.get_json`` -> ``await http.aget_json`` (the async network
        wait stays ON the loop via epoll, no held pool thread; the SSRF getaddrinfo in the async leaf is
        moved off-loop by S4b Part 1). The elapsed-cap clock, the pure-CPU ``_row_to_doc`` map, the
        seen-set dedup and the ``lastPage`` break all stay ON the loop, identical to the sync path."""
        docs: list[Document] = []
        seen: set[int] = set()
        started = time.monotonic()
        for page in range(1, _PAGES + 1):
            if time.monotonic() - started > _ELAPSED_CAP:
                logger.info("oecd_ai_policy: elapsed cap hit at page %d, stopping", page)
                break
            payload = await http.aget_json(_API, params={"page": page}, timeout=15)
            if not isinstance(payload, dict):
                diag.note("oecd_ai_policy.page", url=_API, status=None, body=f"page={page}")
                break  # keep the pages already gathered; do not cache a full miss (base skips empty)
            rows = payload.get("data") or []
            if not rows:
                break
            for row in rows:
                d = self._row_to_doc(row)
                if d is not None and row.get("id") not in seen:
                    seen.add(row.get("id"))
                    docs.append(d)
            if page >= (payload.get("lastPage") or _PAGES):
                break
        logger.info("oecd_ai_policy: built %d initiative docs from newest %d pages", len(docs), _PAGES)
        return docs

    async def _asubset_docs(self) -> list[Document]:
        """Async twin of ``BulkFundingBase._subset_docs``: the SAME 30-day subset cache key
        (``cache.make_key(self.name, "subset", self._version)``, so the async and sync paths share the
        warmed snapshot) with the disk read + write pushed OFF the loop via ``anyio.to_thread.run_sync``
        and the paginated egress swapped to the async ``_abuild_subset_docs``. The ``if docs:`` write
        gate (never cache an empty/failed build) mirrors the sync ``_subset_docs`` exactly."""
        key = cache.make_key(self.name, "subset", self._version)  # SAME key as _subset_docs
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached
        docs = await self._abuild_subset_docs()  # async paginated egress
        if docs:
            await anyio.to_thread.run_sync(  # disk write OFF loop
                functools.partial(cache.set_docs, key, docs, ttl=self.cache_ttl))
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``BulkFundingBase.search`` (S4b): serve/build the query-independent
        subset off the loop, then run the pure-CPU BM25 filter ON the loop. BEHAVIOR-IDENTICAL to the
        sync ``search`` (same subset cache, same ``keyword_score_filter`` rank, same term-less
        passthrough, same ``limit`` truncation). Exposing ``asearch`` makes this adapter
        AsyncSearchCapable, routing it to the fetcher's native async dispatch branch."""
        docs = await self._asubset_docs()
        if not docs:
            return []
        q = (query or "").strip()
        return docs[:limit] if not q else keyword_score_filter(docs, q)[:limit]

    def _row_to_doc(self, row: dict) -> Optional[Document]:
        rid = row.get("id")
        title = (row.get("englishName") or row.get("originalName") or "").strip()
        if rid is None or not title:
            return None
        slug = (row.get("slug") or "").strip()
        url = _DETAIL + slug if slug else _DETAIL
        country = ((row.get("gaiinCountry") or {}) or {}).get("name")
        country_code = ((row.get("gaiinCountry") or {}) or {}).get("code")
        igo = ((row.get("intergovernmentalOrganisation") or {}) or {}).get("name")
        jurisdiction = country or igo or "International"
        itype = ((row.get("initiativeType") or {}) or {}).get("name")
        category = row.get("category")
        status = row.get("status")
        binding = row.get("extentBinding")
        start_year = row.get("startYear")
        end_year = row.get("endYear")
        website = (row.get("website") or "").strip()
        principles = [p.get("name") for p in (row.get("principles") or []) if p.get("name")]
        sectors = [s.get("name") for s in (row.get("targetSectors") or []) if s.get("name")]

        # content = the clean description + a structured facet line (never the mojibake-prone overview).
        parts = [(row.get("description") or "").strip()]
        facet = [f"法域: {jurisdiction}."]
        if itype:
            facet.append(f"工具类型: {itype}.")
        if category:
            facet.append(f"类别: {category}.")
        if status:
            facet.append(f"状态: {status}.")
        if binding:
            facet.append(f"约束力: {binding}.")
        if start_year:
            facet.append(f"起始年份: {start_year}" + (f", 结束: {end_year}." if end_year else "."))
        parts.append(" ".join(facet))
        if website:
            parts.append(f"原始政策文件: {website}")

        return Document(
            source=self.name,
            source_id=f"oecd_ai_policy:{rid}",
            url=url,
            title=f"{title} ({jurisdiction})"[:160],
            content="\n".join(p for p in parts if p),
            date=self._parse_date(row.get("updatedAt") or row.get("createdAt")),
            tags=[t for t in ["policy", "ai-policy", country_code, "oecd"] if t],
            metadata={
                "jurisdiction": jurisdiction, "country": country, "country_code": country_code,
                "intergovernmental_org": igo, "initiative_type": itype, "category": category,
                "status": status, "extent_binding": binding, "start_year": start_year,
                "end_year": end_year, "principles": principles, "target_sectors": sectors,
                "official_url": website, "slug": slug, "initiative_id": rid,
            },
        )

    @staticmethod
    def _parse_date(value: Any) -> Optional[datetime]:
        """OECD ``updatedAt`` / ``createdAt`` is ISO-8601 with a Z suffix (e.g. 2026-07-03T16:28:47.000Z)."""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None

    def health_check(self) -> tuple[bool, str]:
        payload = http.get_json(_API, params={"page": 1}, timeout=15)
        if isinstance(payload, dict) and payload.get("data"):
            return True, f"OK (total={payload.get('total')}, lastPage={payload.get('lastPage')})"
        return False, "policy-initiatives page 1 did not return data"


from penumbra.core.fetcher import register_adapter  # noqa: E402

register_adapter(OecdAiPolicyAdapter())
