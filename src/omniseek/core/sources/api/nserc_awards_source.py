"""nserc_awards — Canadian NSERC research grant awards, the CS / AI / ML / NLP slice (STRUCTURE).

NSERC (Natural Sciences and Engineering Research Council) is Canada's main science-funding body. Its
open data is published ONLY as bulk per-fiscal-year CSV files (no query API, no DataStore) — FY2024
Expenditures is ~56MB / ~62k award rows. This is OmniSeek's FIRST Canadian funding source (it had US
NSF/NIH but no CA funding): for a PhD heading to a Canadian postdoc/faculty track, knowing which
Canadian labs/PIs hold NSERC grants in NLP/ML — amounts, institutions, programs, keywords — is direct
career-targeting intel web search can't return as structured records.

Razor (STRUCTURE): the per-award record (recipient, institution, amount, program, subject, keywords)
beats web search's prose. Telos: Canada-bound NLP researcher; the CS/AI grant landscape = where the
funded work + hireable labs are.

DESIGN — OmniSeek's bulk-file pattern (reusable for SSHRC/CIHR later):
  * The 56MB CSV is a STATIC annual snapshot, so we fetch it at most monthly (cache_ttl 30d) and only
    on a cache miss. We never re-fetch per query.
  * On refresh we keep ONLY the telos slice — rows whose ResearchSubjectEN is Computer Science OR whose
    title/keywords/summary/field mention an AI/ML/NLP term (~3-4k of 62k rows) — and cache THOSE docs
    (query-independent). A query then BM25-filters the cached subset (zero network). So a non-CS NSERC
    grant is intentionally out of scope (this is a CS/AI researcher's lens, not the whole council).
  * The fetch bypasses http.get's 30MB cap (direct httpx); the 56MB lives in memory only transiently
    during the monthly refresh.

explicit_only: a named-query lookup (like nsf_awards / nih_reporter), kept out of the broad sweep.
Header verified live 2026-06-22 (39 cols, utf-8-sig, Range-capable; total 56,525,716 bytes).
"""

from __future__ import annotations

import csv
import functools
import io
import logging
import threading
from typing import Optional

import anyio
import httpx

from omniseek.core import cache
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")
_AI_TERMS = (
    "natural language", "machine learning", "deep learning", "neural network",
    "artificial intelligence", "computational linguistic", "language model",
    "computer vision", "speech recognition", "reinforcement learning",
    "information retrieval", "data mining", "text mining", " nlp ",
)

# Module-level async client for the bulk CSV pull (S4b native-async twin). The 56MB body is LARGER
# than http.aget*'s 30MB MAX_BYTES cap, so this source (like its sync path) canNOT route through the
# shared async leaf; it keeps its OWN async client — lazy, double-checked-lock like
# _openalex._aget_client — with the SAME follow_redirects / User-Agent / timeout as the sync
# httpx.get egress it mirrors. Only asearch awaits it; the sync search is byte-identical.
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()  # construction is sync (no await); double-check like http._aget_client


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = httpx.AsyncClient(
                    headers={"User-Agent": _UA},
                    timeout=240,
                    follow_redirects=True,
                )
    return _aclient


class NSERCAwardsAdapter:
    name = "nserc_awards"
    description = (
        "加拿大 NSERC 科研经费 — 计算机/AI/ML/NLP 切片 (眼首个加拿大经费源, 此前只有美国 NSF/NIH). "
        "NSERC 是加拿大主科学资助局, 开放数据仅以逐年 bulk CSV 发布 (无查询 API, FY2024 ~56MB/~6 万行). "
        "逐笔奖助: 获奖人 + 机构 + 金额 (CAD) + program + 学科 + 关键词. 博士赴加找实验室/PI/资助方向的"
        "一手结构 (网搜给不出). 仅收 CS 学科 + AI/ML/NLP 关键词的子集 (~3-4k 行, telos 视角, 非全 NSERC). "
        "命名钻取 (omniseek_search 单源 raw)."
    )
    needs_credentials = False
    explicit_only = "NSERC 加拿大经费 CS/AI 切片 (bulk CSV, 命名钻取 (omniseek_search 单源 raw)); 月级刷新"
    kind = "lookup"
    domains = ["funding"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    url_host = "nserc-crsng.gc.ca"

    _YEAR = 2024
    _CSV_URL = f"https://www.nserc-crsng.gc.ca/opendata/NSERC_FY{_YEAR}_Expenditures.csv"
    _DATASET_URL = "https://open.canada.ca/data/en/dataset/c1b0f627-8c29-427c-ab73-33968ad9176e"
    _CACHE_TTL = 2592000  # 30 days: an annual static snapshot, so refresh ~monthly (catches a new FY)

    # ── telos filter ────────────────────────────────────────────────────────
    def _is_cs_ai(self, row: dict) -> bool:
        if "computer" in (row.get("ResearchSubjectEN") or "").lower():
            return True
        blob = " ".join((row.get(k) or "") for k in (
            "ApplicationTitle", "Keywords", "ApplicationSummary",
            "FieldOfResearchListNamesEN", "ResearchSubjectEN")).lower()
        return any(t in blob for t in _AI_TERMS)

    def _row_to_doc(self, row: dict) -> Optional[Document]:
        name = (row.get("Name-Nom") or "").strip()
        title = (row.get("ApplicationTitle") or "").strip()
        if not (name or title):
            return None
        inst = (row.get("Institution-Établissement") or "").strip()
        province = (row.get("ProvinceEN") or "").strip()
        program = (row.get("ProgramNameEN") or "").strip()
        subject = (row.get("ResearchSubjectEN") or "").strip()
        field = (row.get("FieldOfResearchListNamesEN") or "").strip()
        amount = (row.get("AwardAmount") or "").strip()
        keywords = (row.get("Keywords") or "").strip()
        summary = (row.get("ApplicationSummary") or "").strip()
        year = (row.get("FiscalYear-Exercice financier") or "").strip()
        app_id = (row.get("ApplicationID") or "").strip()
        parts = [f"NSERC 经费. 获奖人: {name} ({inst}{', ' + province if province else ''}). Program: {program}."]
        if subject or field:
            parts.append(f"学科: {subject}. 领域: {field}.")
        if amount:
            parts.append(f"金额: CAD {amount}.")
        if keywords:
            parts.append(f"关键词: {keywords}.")
        if summary:
            parts.append(summary)
        return Document(
            source=self.name,
            source_id=f"nserc:{self._YEAR}:{app_id or (name + title)[:48]}",
            url=self._DATASET_URL,
            title=(title or f"{program} — {name}")[:140],
            content=" ".join(p for p in parts if p),
            author=name or None,
            tags=["funding", "canada", "nserc"],
            metadata={"recipient": name, "institution": inst, "province": province,
                      "amount_cad": amount, "program": program, "subject": subject,
                      "field": field, "fiscal_year": year, "keywords": keywords,
                      "application_id": app_id},
        )

    # ── bulk fetch + subset cache (query-independent) ───────────────────────
    def _subset_docs(self) -> list[Document]:
        key = cache.make_key(self.name, "subset", self._YEAR)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        docs = self._fetch_filter_build()
        if docs:
            cache.set_docs(key, docs, ttl=self._CACHE_TTL)
        return docs

    def _fetch_filter_build(self) -> list[Document]:
        try:
            r = httpx.get(self._CSV_URL, headers={"User-Agent": _UA},
                          timeout=240, follow_redirects=True)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001 — failure → [] (the contract); don't cache a miss
            logger.warning("nserc_awards: CSV fetch failed: %s", exc)
            return []
        docs: list[Document] = []
        for row in csv.DictReader(io.StringIO(text)):
            if self._is_cs_ai(row):
                d = self._row_to_doc(row)
                if d is not None:
                    docs.append(d)
        logger.info("nserc_awards: built %d CS/AI docs from FY%d (of the full CSV)", len(docs), self._YEAR)
        return docs

    # ── native-async twins (S4b) ────────────────────────────────────────────
    async def _afetch_filter_build(self) -> list[Document]:
        """Async egress twin of ``_fetch_filter_build``: the bulk CSV pull goes async (its OWN
        module-level AsyncClient, since 56MB > http.aget*'s 30MB cap, mirroring the sync httpx.get's
        headers / timeout / follow_redirects); the utf-8-sig decode + csv parse + telos filter + doc
        build stay ON the loop (pure CPU, byte-identical to the sync path — rule: parse stays on the
        loop). This whole path runs only on a monthly cache MISS, so the on-loop parse is rare."""
        try:
            r = await _aget_client().get(self._CSV_URL)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001 — failure → [] (the contract); don't cache a miss
            logger.warning("nserc_awards: CSV fetch failed: %s", exc)
            return []
        docs: list[Document] = []
        for row in csv.DictReader(io.StringIO(text)):
            if self._is_cs_ai(row):
                d = self._row_to_doc(row)
                if d is not None:
                    docs.append(d)
        logger.info("nserc_awards: built %d CS/AI docs from FY%d (of the full CSV)", len(docs), self._YEAR)
        return docs

    async def _asubset_docs(self) -> list[Document]:
        """Async twin of ``_subset_docs``: the disk cache read + write go OFF the loop
        (anyio.to_thread.run_sync; the SAME cache key as ``_subset_docs``); the fetch+filter+build
        uses the async egress twin. Behavior-identical to ``_subset_docs``."""
        key = cache.make_key(self.name, "subset", self._YEAR)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached
        docs = await self._afetch_filter_build()
        if docs:
            await anyio.to_thread.run_sync(  # disk write OFF loop
                functools.partial(cache.set_docs, key, docs, ttl=self._CACHE_TTL))
        return docs

    # ── Protocol surface ────────────────────────────────────────────────────
    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = self._subset_docs()
        if not docs:
            return []
        q = (query or "").strip()
        if not q:
            return docs[:limit]
        return keyword_score_filter(docs, q)[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): mirrors it line-for-line — the query-independent
        cached subset, then the ONE shared BM25 scorer. The subset cache round-trip + the bulk CSV
        egress go async in ``_asubset_docs``; the term filter is pure CPU on the loop. Being
        AsyncSearchCapable routes this source to the fetcher's native async dispatch branch."""
        docs = await self._asubset_docs()
        if not docs:
            return []
        q = (query or "").strip()
        if not q:
            return docs[:limit]
        return keyword_score_filter(docs, q)[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        # Cheap: a Range request for the header proves the file is live without the 56MB pull.
        try:
            r = httpx.get(self._CSV_URL, headers={"User-Agent": _UA, "Range": "bytes=0-2000"},
                          timeout=20, follow_redirects=True)
            ok = r.status_code in (200, 206) and "ApplicationID" in r.text
            return ok, f"HTTP {r.status_code}" + ("" if ok else " (header missing)")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"


from omniseek.core.fetcher import register_adapter  # noqa: E402

register_adapter(NSERCAwardsAdapter())
