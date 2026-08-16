"""cordis_eu — EU research grants under Horizon Europe (2021-2027), the AI / ML / NLP slice (STRUCTURE).
OmniSeek's FIRST European funding source (it had US NSF/NIH + Canada NSERC/SSHRC/CIHR, but no EU).

CORDIS is the European Commission's research-results service. Its Horizon Europe project data is
published ONLY as a bulk monthly zip (CSV/JSON, no query API) at cordis.europa.eu/data — the CSV zip
is ~35MB and unpacks to project.csv (~22.5k projects) + organization.csv (participants, incl. the
coordinator) + euroSciVoc/topics/legalBasis. For a PhD eyeing a European postdoc/ERC track, knowing
which EU labs hold Horizon/ERC grants in NLP/ML — amounts (EUR), coordinator institution + country,
call, funding scheme, objective — is direct career-targeting intel web search can't return structured.

Razor (STRUCTURE): the per-project record (title, objective, keywords, EC contribution, coordinator
institution + country, ERC/collaborative scheme) beats web search's prose. Telos: an EU-bound NLP
researcher; the Horizon/ERC AI grant landscape = where the funded work + hireable labs are.

DESIGN — OmniSeek's bulk-file pattern (shared via _bulk_funding, as NSERC/SSHRC/CIHR):
  * The CSV zip URL is STABLE (unlike the Canadian per-year filenames) and refreshed monthly in place,
    so we fetch it at most monthly (cache_ttl 30d) and only on a cache miss. Never re-fetch per query.
  * On refresh we read project.csv, keep ONLY the telos slice — rows whose title/objective/keywords
    mention an AI/ML/NLP term (~3.1k of 22.5k) — then join organization.csv for each kept project's
    coordinator (name/country/city), and cache THOSE docs query-independent. A query then BM25-filters
    the cached subset (zero network). A non-AI Horizon project is intentionally out of scope.
  * The ~35MB zip bypasses http.get's 30MB cap (direct httpx); it lives in memory only transiently
    during the monthly refresh. Parse (project + coordinator join) is a few seconds once in memory.

Only Horizon Europe (2021-2027, the ACTIVE programme) is ingested; the closed H2020 (2014-2020) is a
separate same-shape zip that could be a sibling later. explicit_only: a named-query lookup like
nsf_awards / nserc_awards. Structure verified live 2026-07-10 (project.csv 21 cols, ';'-delimited,
utf-8-sig; totalCost is EUR with a comma decimal, e.g. '2073781,25'; coordinator lives in
organization.csv role=='coordinator'; project page = cordis.europa.eu/project/id/{id}).
"""

from __future__ import annotations

import csv
import functools
import io
import logging
import threading
import zipfile
from datetime import datetime
from typing import Optional

import anyio
import httpx

from omniseek.core import cache, diag
from omniseek.core.normalize import Document, keyword_score_filter
from omniseek.core.sources.api._bulk_funding import UA, BulkFundingBase, is_ai_relevant

logger = logging.getLogger(__name__)

_CSV_ZIP = "https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip"
_PROJECT_URL = "https://cordis.europa.eu/project/id/{pid}"
_DATA_URL = "https://cordis.europa.eu/data"

# Module-level async client for the native-async twin (S4b). The ~35MB CSV zip EXCEEDS http.aget*'s
# 30MB cap (why the sync path uses a raw httpx.get, not the shared leaf), so its async twin needs its
# own httpx.AsyncClient. Built lazily under a double-checked lock (like _openalex._aget_client);
# follow_redirects=True mirrors the sync httpx.get. Touched only on the monthly cache-miss refresh.
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()  # construction is sync (no await); double-check like _openalex


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = httpx.AsyncClient(follow_redirects=True)
    return _aclient


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


class CordisEuAdapter(BulkFundingBase):
    name = "cordis_eu"
    description = (
        "欧盟 CORDIS 科研经费 — Horizon Europe (2021-2027) AI/ML/NLP 切片 (眼首个欧盟经费源, 此前有美国 "
        "NSF/NIH + 加拿大 NSERC/SSHRC/CIHR, 无欧盟). CORDIS 是欧委会科研成果服务, Horizon Europe 项目数据"
        "仅以逐月 bulk zip 发布 (CSV/JSON, 无查询 API; CSV zip ~35MB, project.csv ~2.25 万项目 + "
        "organization.csv 参与机构含协调方). 逐项目: 标题 + 目标摘要 + 关键词 + EC 出资(EUR) + 协调机构 + "
        "国别 + call + 资助方案 (ERC/协作). 博士赴欧找实验室/ERC 方向的一手结构 (网搜给不出). 仅收 AI/ML/NLP "
        "切片 (~3.1k, telos 视角, 非全 Horizon). 仅活跃的 Horizon Europe (已闭的 H2020 可另建). 命名钻取 (omniseek_search 单源 raw)."
    )
    explicit_only = "CORDIS 欧盟 Horizon Europe 经费 AI/ML/NLP 切片 (bulk CSV zip, 命名钻取 (omniseek_search 单源 raw)); 月级刷新"
    domains = ["funding"]
    regions = ["eu"]
    modes = ["STRUCTURE"]
    url_host = "cordis.europa.eu"
    _version = "horizon-2021-2027"

    # ── bulk fetch + coordinator join + subset build ────────────────────────
    def _build_subset_docs(self) -> list[Document]:
        try:
            # 85s < the 90s fetch_one deadline (the NSERC/SSHRC/CIHR precedent): generous for a ~35MB
            # zip. Raise the OUTER deadline, not this, if a larger download is ever needed.
            r = httpx.get(_CSV_ZIP, headers={"User-Agent": UA}, timeout=85, follow_redirects=True)
            r.raise_for_status()
            content = r.content
        except Exception as exc:  # noqa: BLE001 — failure → [] (the contract); don't cache a miss
            logger.warning("cordis_eu: zip fetch failed: %s", exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("cordis_eu.zip", url=_CSV_ZIP, status=st, exc=exc)
            return []
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            proj_text = zf.read("project.csv").decode("utf-8-sig", errors="replace")
            org_text = zf.read("organization.csv").decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cordis_eu: zip parse failed: %s", exc)
            return []
        projects: dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(proj_text), delimiter=";"):
            if is_ai_relevant(row.get("title"), row.get("objective"), row.get("keywords")):
                pid = (row.get("id") or "").strip()
                if pid:
                    projects[pid] = row
        # Join the coordinator org (first coordinator row) for the kept projects only.
        coords: dict[str, dict] = {}
        for orow in csv.DictReader(io.StringIO(org_text), delimiter=";"):
            if (orow.get("role") or "") == "coordinator":
                pid = (orow.get("projectID") or "").strip()
                if pid in projects and pid not in coords:
                    coords[pid] = orow
        docs: list[Document] = []
        for pid, proj in projects.items():
            d = self._row_to_doc(proj, coords.get(pid))
            if d is not None:
                docs.append(d)
        logger.info("cordis_eu: built %d AI/ML/NLP docs from Horizon Europe (of %d projects)",
                    len(docs), len(projects))
        return docs

    # ── native-async twins (S4b): asearch -> _asubset_docs -> _abuild_subset_docs ───────────────
    # Byte-faithful mirror of BulkFundingBase.search / _subset_docs + this source's _build_subset_docs
    # (the base owns search/_subset_docs; this file owns the bulk build, so both twins live here).
    # Changing ONLY: the disk cache round-trip goes OFF the loop (anyio.to_thread, SAME cache key) and
    # the raw ~35MB httpx.get goes async via the module AsyncClient. The BM25 filter + zip/csv parse +
    # coordinator join stay pure CPU ON the loop, byte-identical. Defining asearch flags the adapter
    # AsyncSearchCapable, routing it to the fetcher's native-async dispatch branch.
    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BulkFundingBase.search."""
        docs = await self._asubset_docs()
        if not docs:
            return []
        q = (query or "").strip()
        return docs[:limit] if not q else keyword_score_filter(docs, q)[:limit]

    async def _asubset_docs(self) -> list[Document]:
        """Native-async twin of BulkFundingBase._subset_docs: cache round-trip OFF the loop (SAME key),
        async build on a miss. The fresh / cache_only contextvars propagate into the worker thread."""
        key = cache.make_key(self.name, "subset", self._version)  # SAME key as _subset_docs
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached
        docs = await self._abuild_subset_docs()
        if docs:
            await anyio.to_thread.run_sync(  # disk write OFF loop
                functools.partial(cache.set_docs, key, docs, ttl=self.cache_ttl))
        return docs

    async def _abuild_subset_docs(self) -> list[Document]:
        """Native-async twin of _build_subset_docs. ONLY the raw ~35MB httpx.get -> await
        _aget_client().get (same UA / timeout / follow_redirects); the zip parse + coordinator join +
        row->doc mapping below are pure CPU held ON the loop, byte-identical to _build_subset_docs (a
        monthly cache-miss event; the conversion pattern keeps csv/parse on the loop). KEEP IN SYNC
        with _build_subset_docs if its parse/join ever changes."""
        try:
            r = await _aget_client().get(
                _CSV_ZIP, headers={"User-Agent": UA}, timeout=85, follow_redirects=True)
            r.raise_for_status()
            content = r.content
        except Exception as exc:  # noqa: BLE001 — failure → [] (the contract); don't cache a miss
            logger.warning("cordis_eu: zip fetch failed: %s", exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("cordis_eu.zip", url=_CSV_ZIP, status=st, exc=exc)
            return []
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            proj_text = zf.read("project.csv").decode("utf-8-sig", errors="replace")
            org_text = zf.read("organization.csv").decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cordis_eu: zip parse failed: %s", exc)
            return []
        projects: dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(proj_text), delimiter=";"):
            if is_ai_relevant(row.get("title"), row.get("objective"), row.get("keywords")):
                pid = (row.get("id") or "").strip()
                if pid:
                    projects[pid] = row
        # Join the coordinator org (first coordinator row) for the kept projects only.
        coords: dict[str, dict] = {}
        for orow in csv.DictReader(io.StringIO(org_text), delimiter=";"):
            if (orow.get("role") or "") == "coordinator":
                pid = (orow.get("projectID") or "").strip()
                if pid in projects and pid not in coords:
                    coords[pid] = orow
        docs: list[Document] = []
        for pid, proj in projects.items():
            d = self._row_to_doc(proj, coords.get(pid))
            if d is not None:
                docs.append(d)
        logger.info("cordis_eu: built %d AI/ML/NLP docs from Horizon Europe (of %d projects)",
                    len(docs), len(projects))
        return docs

    @staticmethod
    def _row_to_doc(proj: dict, coord: Optional[dict]) -> Optional[Document]:
        pid = (proj.get("id") or "").strip()
        title = (proj.get("title") or "").strip()
        if not (pid or title):
            return None
        acronym = (proj.get("acronym") or "").strip()
        status = (proj.get("status") or "").strip()
        objective = (proj.get("objective") or "").strip()
        keywords = (proj.get("keywords") or "").strip()
        total_cost = (proj.get("totalCost") or "").strip()          # EUR, comma decimal
        ec_max = (proj.get("ecMaxContribution") or "").strip()      # EUR, comma decimal
        scheme = (proj.get("fundingScheme") or "").strip()
        master_call = (proj.get("masterCall") or "").strip()
        start_date = (proj.get("startDate") or "").strip()
        end_date = (proj.get("endDate") or "").strip()
        grant_doi = (proj.get("grantDoi") or "").strip()
        coord_name = (coord.get("name") or "").strip() if coord else ""
        coord_country = (coord.get("country") or "").strip() if coord else ""
        coord_city = (coord.get("city") or "").strip() if coord else ""
        loc = coord_country + (", " + coord_city if coord_city else "")
        parts = [f"CORDIS EU 经费 (Horizon Europe). 项目: {acronym + ' — ' if acronym else ''}{title}."]
        if coord_name:
            parts.append(f"协调机构: {coord_name}{' (' + loc + ')' if loc else ''}.")
        parts.append(f"资助方案: {scheme}. Call: {master_call}. 状态: {status}.")
        if ec_max or total_cost:
            parts.append(f"EC 出资: EUR {ec_max}. 总成本: EUR {total_cost}.")
        if keywords:
            parts.append(f"关键词: {keywords}.")
        if objective:
            parts.append(objective)
        tags = ["funding", "eu", "horizon-europe"]
        if master_call.upper().startswith("ERC") or "ERC" in scheme.upper():
            tags.append("erc")
        return Document(
            source="cordis_eu",
            source_id=f"cordis:{pid}",
            url=_PROJECT_URL.format(pid=pid) if pid else _DATA_URL,
            title=(title or acronym)[:140],
            content=" ".join(p for p in parts if p),
            author=None,  # CORDIS project rows carry a coordinator ORG, not a PI person
            date=_parse_date(start_date),
            tags=tags,
            metadata={"project_id": pid, "acronym": acronym, "status": status,
                      "funding_scheme": scheme, "master_call": master_call,
                      "ec_contribution_eur": ec_max, "total_cost_eur": total_cost,
                      "start_date": start_date, "end_date": end_date, "grant_doi": grant_doi,
                      "coordinator": coord_name, "coordinator_country": coord_country,
                      "coordinator_city": coord_city, "keywords": keywords},
        )

    def health_check(self) -> tuple[bool, str]:
        # Cheap: a 2-byte Range proves the zip is live (PK magic) without the ~35MB pull.
        try:
            r = httpx.get(_CSV_ZIP, headers={"User-Agent": UA, "Range": "bytes=0-1"},
                          timeout=20, follow_redirects=True)
            ok = r.status_code in (200, 206) and r.content[:2] == b"PK"
            return ok, f"HTTP {r.status_code}" + ("" if ok else " (not a zip / unreachable)")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"


from omniseek.core.fetcher import register_adapter  # noqa: E402

register_adapter(CordisEuAdapter())
