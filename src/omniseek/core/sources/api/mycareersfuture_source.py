"""MyCareersFuture — Singapore's government job board (open API, salary-mandated).

Phase 4 P15 (2026-05-30). MyCareersFuture (run by Workforce Singapore) is the
canonical SG job board: every posting carries a mandated salary range + the
hiring company's UEN, and the search API is fully public (no auth, no proxy).
This complements `overseas_ai_jobs` (which only covers a fixed set of overseas
AI labs): MCF surfaces the FULL Singapore market (local firms, MNCs' SG offices,
gov labs), filtered by free-text query.

API (verified 2026-05-30):
  POST https://api.mycareersfuture.gov.sg/v2/search?limit=N&page=0
  body: {"search": "<query>", "sortBy": ["new_posting_date"], "limit": N, "page": 0}
  → {"total": int, "results": [ {uuid, title, salary{minimum,maximum,type},
       address, postedCompany{name}, hiringCompany, positionLevels,
       employmentTypes, skills[], metadata{newPostingDate, ...}} ]}
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Optional

import anyio
import httpx

from omniseek.core import cache, diag, http
from omniseek.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

API = "https://api.mycareersfuture.gov.sg/v2/search"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
CACHE_TTL = 3600  # 1h
DEFAULT_QUERY = "machine learning"


class MyCareersFutureAdapter:
    name = "mycareersfuture"
    needs_credentials = False
    description = (
        "MyCareersFuture — 新加坡政府求职板 (开放 API, **强制薪资范围** + 公司 UEN). "
        "SG 全境岗位 (本地/MNC/政府), 自由文本查询; 新加坡求职主板"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        q = (query or "").strip() or DEFAULT_QUERY
        key = cache.make_key("mycareersfuture", "search", q, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        n = min(max(limit, 5), 30)
        body = {
            "sessionId": "",
            "search": q,
            "sortBy": ["new_posting_date"],
            "limit": n,
            "page": 0,
        }
        try:
            r = httpx.post(f"{API}?limit={n}&page=0", json=body, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            results = r.json().get("results", []) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("MyCareersFuture search failed: %s", exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("mycareersfuture.search", url=API, status=st, exc=exc)
            return []

        docs: list[Document] = []
        for j in results:
            doc = self._to_doc(j)
            if doc:
                docs.append(doc)
            if len(docs) >= limit:
                break
        cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): mirrors ``search`` line-for-line so the fetcher's
        native async dispatch (``AsyncSearchCapable``) awaits it DIRECTLY, spending a coroutine on the
        MCF POST wait instead of a held pool thread. Three changes only vs ``search``:
          - the disk CACHE read/write go OFF the loop (anyio.to_thread.run_sync: get_docs / set_docs do
            file IO), keyed IDENTICALLY (same ``q``-normalized key) so async and sync share the cache;
          - the raw ``httpx.post`` + ``.json()`` swaps to the shared async leaf ``await http.apost_json``
            (a standard JSON POST: it gives the shared pool + SSRF guard + cache_only + 30MB cap for
            free, and emits its own failure diag.note under the ``http.post`` label). The source's own
            try/except + ``diag.note("mycareersfuture.search", ...)`` therefore collapse into
            ``apost_json`` returning None on any egress/parse failure; a non-dict body is treated the
            same. In either failure case we return [] WITHOUT caching — exactly as ``search``'s
            except-branch ``return []`` did (never pinning a transient miss);
          - the PURE-CPU result→doc mapping (``_to_doc``, the limit cap) stays ON the loop,
            byte-identical to ``search`` (no drift)."""
        q = (query or "").strip() or DEFAULT_QUERY
        key = cache.make_key("mycareersfuture", "search", q, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached

        n = min(max(limit, 5), 30)
        body = {
            "sessionId": "",
            "search": q,
            "sortBy": ["new_posting_date"],
            "limit": n,
            "page": 0,
        }
        data = await http.apost_json(f"{API}?limit={n}&page=0", json=body, headers=HEADERS, timeout=TIMEOUT)
        if not isinstance(data, dict):
            # egress/parse failure (None) or non-object body → honest empty, don't cache the miss
            # (mirrors search's except-branch return []; apost_json already logged + diag.note'd it).
            return []
        results = data.get("results", []) or []

        docs: list[Document] = []
        for j in results:
            doc = self._to_doc(j)
            if doc:
                docs.append(doc)
            if len(docs) >= limit:
                break
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=CACHE_TTL))
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            r = httpx.post(f"{API}?limit=1&page=0",
                           json={"search": "engineer", "limit": 1, "page": 0},
                           headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            return True, f"OK (total {r.json().get('total', '?')} for 'engineer')"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _to_doc(j: dict) -> Optional[Document]:
        uuid = j.get("uuid") or ""
        title = (j.get("title") or "").strip()
        if not uuid or not title:
            return None
        url = f"https://www.mycareersfuture.gov.sg/job/{uuid}"

        company = ((j.get("postedCompany") or {}) or (j.get("hiringCompany") or {})).get("name") or ""

        sal = j.get("salary") or {}
        lo, hi = sal.get("minimum"), sal.get("maximum")
        stype = ((sal.get("type") or {}).get("salaryType")) or "Monthly"
        salary_str = f"SGD {lo:,}–{hi:,} / {stype}" if lo and hi else ""

        levels = ", ".join(p.get("position", "") for p in (j.get("positionLevels") or []) if p.get("position"))
        emp = ", ".join(e.get("employmentType", "") for e in (j.get("employmentTypes") or []) if e.get("employmentType"))
        skills = [s.get("skill", "") for s in (j.get("skills") or []) if s.get("skill")][:10]

        meta = j.get("metadata") or {}
        posted = meta.get("newPostingDate") or meta.get("originalPostingDate")
        date = None
        if posted:
            try:
                date = datetime.fromisoformat(str(posted)).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        parts = []
        if salary_str:
            parts.append(f"💰 {salary_str}")
        if levels:
            parts.append(f"级别: {levels}")
        if emp:
            parts.append(emp)
        content = "  ·  ".join(parts)
        if skills:
            content += f"\n技能: {', '.join(skills)}"

        return Document(
            source="mycareersfuture",
            source_id=uuid,
            url=url,
            title=f"[{company}] {title}" if company else title,
            content=content or "(SG job)",
            author=company or "MyCareersFuture",
            date=date,
            signals=mk_signal('salary', (hi or lo), kind='compensation',
                              by='mycareersfuture/salary', unit='SGD/month'),
            tags=["sg-job", "singapore"] + ([f"emp:{emp}"] if emp else []),
            metadata={
                "company": company,
                "salary_min": lo,
                "salary_max": hi,
                "salary_type": stype,
                "levels": levels,
                "employment": emp,
                "skills": skills,
                "uuid": uuid,
                "views": (meta.get("totalNumberJobApplication")),
                "raw": jsonsafe(j),
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(MyCareersFutureAdapter())
