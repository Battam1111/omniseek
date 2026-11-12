"""sshrc_awards — Canadian SSHRC research grants, the computational-linguistics / NLP / digital-
humanities slice (STRUCTURE). The second of Canada's three federal research councils (NSERC sciences,
SSHRC humanities + social, CIHR health).

SSHRC funds the humanities + social sciences, where the NLP-relevant work lives as computational
linguistics, language technology, digital humanities, corpus/text studies, and computational social
science — a slice NSERC (sciences/engineering) does NOT cover. Open data is bulk per-fiscal-year
Payments CSV (FY2024 ~6.9MB, no query API). Telos: a CA-bound NLP researcher's funding map, the
humanities-adjacent half.

Acquisition (OmniSeek's bulk-file pattern, shared via _bulk_funding):
  * The Payments CSV URL is resolved from CKAN ``package_show`` each refresh (the per-year filename is
    inconsistent across years, so never hardcode it; fall back to the verified FY2024 URL if the
    catalog call fails).
  * Fetch the static annual CSV at most monthly (cache_ttl 30d), keep only is_ai_relevant rows
    (~comp-ling/NLP), cache those docs query-independent; a query BM25-filters the cached subset.

Header verified live 2026-06-22 (24 cols, utf-8-sig; latest = "2024 Payments"). explicit_only: a
named-query lookup like nsf_awards / cihr_grants.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional


from omniseek.core import diag, http
from omniseek.core.normalize import Document
from omniseek.core.sources.api._bulk_funding import UA, BulkFundingBase, is_ai_relevant, year_of

logger = logging.getLogger(__name__)

_PKG = "b4e2b302-9bc6-4b33-b880-6496f8cef0f1"
_PKG_SHOW = f"https://open.canada.ca/data/en/api/3/action/package_show?id={_PKG}"
_DATASET_URL = f"https://open.canada.ca/data/en/dataset/{_PKG}"
_FALLBACK_CSV = "https://www.sshrc-crsh.gc.ca/opendata/SSHRC_FY2024_Expenditures.csv"


class SSHRCAwardsAdapter(BulkFundingBase):
    name = "sshrc_awards"
    description = (
        "加拿大 SSHRC 人文社科经费：计算语言学/NLP/数字人文 切片 (加拿大三大联邦研究局之二: NSERC 理工、"
        "SSHRC 人文社科、CIHR 健康). NLP 在人文社科这边以 计算语言学/语言技术/数字人文/语料文本/计算社科 "
        "形式存在, 是 NSERC(理工) 不覆盖的一块. 开放数据为逐年 Payments bulk CSV (FY2024 ~6.9MB, 无查询 API). "
        "逐笔奖助: 获奖人 + 机构 + 金额(CAD) + program + 学科/方向 + 关键词. 仅收 AI/ML/NLP 相关切片. 命名钻取 (omniseek_search 单源 raw)."
    )
    explicit_only = "SSHRC 加拿大人文社科经费 comp-ling/NLP 切片 (bulk CSV, 命名钻取 (omniseek_search 单源 raw)); 月级刷新"
    domains = ["funding"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    _version = "FY2024"

    def _row_to_doc(self, row: dict) -> Optional[Document]:
        name = (row.get("Name-Nom") or "").strip()
        title = (row.get("Title-Titre") or "").strip()
        if not (name or title):
            return None
        inst = (row.get("Institution") or "").strip()
        province = (row.get("Province_EN") or "").strip()
        amount = (row.get("Amount-Montant") or "").strip()
        program = (row.get("Program") or "").strip()
        discipline = (row.get("SSHRC_Discipline_EN") or "").strip()
        area = (row.get("SSHRC_Area_of_Research") or "").strip()
        keywords = (row.get("Keywords-Mots-clés") or "").strip()
        role = (row.get("Role-Rôle") or "").strip()
        year = (row.get("Fiscal_Year-Exercice_financier") or "").strip()
        cle = (row.get("cle") or "").strip()
        parts = [f"SSHRC 经费. 获奖人: {name} ({inst}{', ' + province if province else ''}). "
                 f"Program: {program}." + (f" 角色: {role}." if role else "")]
        if discipline or area:
            parts.append(f"学科: {discipline}. 方向: {area}.")
        if amount:
            parts.append(f"金额: CAD {amount}.")
        if keywords:
            parts.append(f"关键词: {keywords}.")
        return Document(
            source=self.name,
            source_id=f"sshrc:{self._version}:{cle or (name + title)[:48]}",
            url=_DATASET_URL,
            title=(title or f"{program} — {name}")[:140],
            content=" ".join(parts),
            author=name or None,
            tags=["funding", "canada", "sshrc"],
            metadata={"recipient": name, "institution": inst, "province": province,
                      "amount_cad": amount, "program": program, "discipline": discipline,
                      "area": area, "role": role, "fiscal_year": year,
                      "keywords": keywords, "file_id": cle},
        )

    # ── NATIVE ASYNC TWIN ──────────────────────────────────────────────
    # A PURE ADDITION mirroring search / _subset_docs (BulkFundingBase) + _build_subset_docs /
    # _resolve_csv_url line-for-line, changing ONLY the egress + the disk-cache round-trip to be
    # non-blocking; every parse / map / filter is byte-identical and stays ON the loop. Adding asearch
    # makes this adapter AsyncSearchCapable, so the fetcher awaits it directly (no held pool thread for
    # the dominant network wait) instead of pushing the sync .search onto the shared thread pool. The
    # sync search() / _subset_docs (base) are untouched and stay the legacy-runner path.
    async def _aresolve_csv_url(self) -> str:
        """Async replacement of the deleted sync ``_resolve_csv_url`` (11-04, this is the only one now): the CKAN package_show GET goes async (http.aget_json); the
        resource pick (filter / sort / fallback) is pure CPU, byte-identical, on the loop."""
        data = await http.aget_json(_PKG_SHOW)
        if isinstance(data, dict):
            res = (data.get("result") or {}).get("resources") or []
            pays = [r for r in res if "payment" in (r.get("name") or "").lower()
                    and (r.get("format") or "").upper() == "CSV" and r.get("url")]
            pays.sort(key=lambda r: year_of(r.get("name")), reverse=True)
            if pays:
                return pays[0]["url"]
        return _FALLBACK_CSV

    async def _abuild_subset_docs(self) -> list[Document]:
        """Async replacement of the deleted sync ``_build_subset_docs`` (11-04, this is the only one now) The RAW httpx.get -> http.aget (the shared async leaf): the
        FY2024 CSV is ~6.9MB, well under http's 30MB cap, so routing through it gains the pooled client +
        SSRF guard + cache_only for free; http.aget already logs + diag.notes a network / non-2xx failure
        (label "http.get"), so a None here is the same failure -> [] contract. The decode is kept EXACT as
        r.content.decode("utf-8-sig") (NOT aget_text: the BOM strip must stay explicit, else the first
        column header would keep its BOM), inside a try so a corrupt body -> [] like the sync path. The
        csv parse + row mapping are pure CPU, byte-identical, and stay ON the loop."""
        url = await self._aresolve_csv_url()
        resp = await http.aget(url, headers={"User-Agent": UA}, timeout=85, follow_redirects=True)
        if resp is None:
            logger.warning("sshrc_awards: CSV fetch failed or unavailable (%s)", url)
            return []
        try:
            text = resp.content.decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001 — a corrupt body -> [] (the contract), mirrors the sync try
            logger.warning("sshrc_awards: CSV decode failed (%s): %s", url, exc)
            diag.note("sshrc_awards.csv", url=url, status=resp.status_code, exc=exc)
            return []
        docs: list[Document] = []
        for row in csv.DictReader(io.StringIO(text)):
            if is_ai_relevant(row.get("Title-Titre"), row.get("Keywords-Mots-clés"),
                              row.get("SSHRC_Discipline_EN"), row.get("SSHRC_Area_of_Research"),
                              row.get("CRDC_Field_of_Research")):
                d = self._row_to_doc(row)
                if d is not None:
                    docs.append(d)
        logger.info("sshrc_awards: built %d comp-ling/NLP docs from FY2024", len(docs))
        return docs

    async def ahealth_check(self) -> tuple[bool, str]:
        data = await http.aget_json(_PKG_SHOW)
        if isinstance(data, dict) and (data.get("result") or {}).get("resources"):
            return True, "OK (open.canada package_show resolves)"
        return False, "package_show did not resolve resources"


from omniseek.core.fetcher import register_adapter  # noqa: E402

register_adapter(SSHRCAwardsAdapter())
