"""sshrc_awards — Canadian SSHRC research grants, the computational-linguistics / NLP / digital-
humanities slice (STRUCTURE). The second of Canada's three federal research councils (NSERC sciences,
SSHRC humanities + social, CIHR health).

SSHRC funds the humanities + social sciences, where the NLP-relevant work lives as computational
linguistics, language technology, digital humanities, corpus/text studies, and computational social
science — a slice NSERC (sciences/engineering) does NOT cover. Open data is bulk per-fiscal-year
Payments CSV (FY2024 ~6.9MB, no query API). Telos: a CA-bound NLP researcher's funding map, the
humanities-adjacent half.

Acquisition (the eye's bulk-file pattern, shared via _bulk_funding):
  * The Payments CSV URL is resolved from CKAN ``package_show`` each refresh (the per-year filename is
    inconsistent across years, so never hardcode it; fall back to the verified FY2024 URL if the
    catalog call fails).
  * Fetch the static annual CSV at most monthly (cache_ttl 30d), keep only is_ai_relevant rows
    (~comp-ling/NLP), cache those docs query-independent; a query BM25-filters the cached subset.

Header verified live 2026-06-22 (24 cols, utf-8-sig; latest = "2024 Payments"). explicit_only: a
named-query lookup like nsf_awards / nserc_awards.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

import httpx

from penumbra.core import http
from penumbra.core.normalize import Document
from penumbra.core.sources.api._bulk_funding import UA, BulkFundingBase, is_ai_relevant, year_of

logger = logging.getLogger(__name__)

_PKG = "b4e2b302-9bc6-4b33-b880-6496f8cef0f1"
_PKG_SHOW = f"https://open.canada.ca/data/en/api/3/action/package_show?id={_PKG}"
_DATASET_URL = f"https://open.canada.ca/data/en/dataset/{_PKG}"
_FALLBACK_CSV = "https://www.sshrc-crsh.gc.ca/opendata/SSHRC_FY2024_Expenditures.csv"


class SSHRCAwardsAdapter(BulkFundingBase):
    name = "sshrc_awards"
    description = (
        "加拿大 SSHRC 人文社科经费 — 计算语言学/NLP/数字人文 切片 (加拿大三大联邦研究局之二: NSERC 理工、"
        "SSHRC 人文社科、CIHR 健康). NLP 在人文社科这边以 计算语言学/语言技术/数字人文/语料文本/计算社科 "
        "形式存在, 是 NSERC(理工) 不覆盖的一块. 开放数据为逐年 Payments bulk CSV (FY2024 ~6.9MB, 无查询 API). "
        "逐笔奖助: 获奖人 + 机构 + 金额(CAD) + program + 学科/方向 + 关键词. 仅收 AI/ML/NLP 相关切片. 命名 penumbra_fetch."
    )
    explicit_only = "SSHRC 加拿大人文社科经费 comp-ling/NLP 切片 (bulk CSV, 命名 penumbra_fetch); 月级刷新"
    domains = ["funding"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    _version = "FY2024"

    def _resolve_csv_url(self) -> str:
        data = http.get_json(_PKG_SHOW)
        if isinstance(data, dict):
            res = (data.get("result") or {}).get("resources") or []
            pays = [r for r in res if "payment" in (r.get("name") or "").lower()
                    and (r.get("format") or "").upper() == "CSV" and r.get("url")]
            pays.sort(key=lambda r: year_of(r.get("name")), reverse=True)
            if pays:
                return pays[0]["url"]
        return _FALLBACK_CSV

    def _build_subset_docs(self) -> list[Document]:
        url = self._resolve_csv_url()
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, timeout=180, follow_redirects=True)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001 — failure → [] (the contract); don't cache a miss
            logger.warning("sshrc_awards: CSV fetch failed (%s): %s", url, exc)
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

    def health_check(self) -> tuple[bool, str]:
        data = http.get_json(_PKG_SHOW)
        if isinstance(data, dict) and (data.get("result") or {}).get("resources"):
            return True, "OK (open.canada package_show resolves)"
        return False, "package_show did not resolve resources"


from penumbra.core.fetcher import register_adapter  # noqa: E402

register_adapter(SSHRCAwardsAdapter())
