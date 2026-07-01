"""datagovsg_nonresident_pass_types — Singapore non-resident population by pass type (STRUCTURE).

data.gov.sg's DataStore exposes the official breakdown of Singapore's non-resident population by
pass type (Work Permit / S Pass / Employment Pass / dependants / students ...) as a per-year series.
A login-free, key-free CKAN DataStore API: GET datastore_search?resource_id=... -> result.records[],
each record a WIDE row (DataSeries + one column per year, values are percentage shares).

Razor (STRUCTURE): the official composition of Singapore's foreign-pass population is a first-hand
government statistic web search can't cleanly return as a parseable time-series. It is decision-context
for any Singapore immigration question (which pass realistically applies, how the EP/SP share has
shifted). explicit_only: a named statistical lookup, not the broad sweep.

To cover another data.gov.sg dataset (Total Foreign Workforce, etc.), add a sibling source with a
different ``_RID`` — the mechanism is identical.
"""

from __future__ import annotations

from typing import Optional

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument
from penumbra.core.sources.api._base import BaseAPIAdapter

_RID = "d_94fd56bdb981f0f966cb487d8247bf1a"  # "Non-Resident Population by Pass Type" (% shares)
_ENDPOINT = "https://data.gov.sg/api/action/datastore_search"


def _is_year(k: str) -> bool:
    k = (k or "").strip()
    return len(k) == 4 and k.isdigit()


class DataGovSgPassesAdapter(BaseAPIAdapter):
    name = "datagovsg_nonresident_pass_types"
    description = (
        "新加坡非居民人口按准证类型占比 (data.gov.sg DataStore 官方统计) — Work Permit / S Pass / "
        "Employment Pass / 家属准证 / 学生准证 等占非居民人口的逐年百分比序列. SG 移民/外籍劳动力结构"
        "一手官方数据 (网搜给不出可解析的时间序列). 新加坡移民问题的决策语境: 准证构成 = decision-context. "
        "命名 eye_fetch."
    )
    explicit_only = "data.gov.sg 准证类型占比 (单数据集, 统计 lookup); 命名 eye_fetch"
    cache_ttl = 86400  # daily: an annual statistic
    rank_locally = True  # small dataset; filter by DataSeries name against the query
    url_host = "data.gov.sg"
    health_probe_url = f"{_ENDPOINT}?resource_id={_RID}&limit=1"
    kind = "lookup"
    domains = ["immigration"]
    regions = ["sg"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> list:
        # Small dataset (a handful of pass-type rows): fetch all, let the base rank/filter by name.
        url = f"{_ENDPOINT}?resource_id={_RID}&limit=100"
        data = http.get_json(url)
        if not isinstance(data, dict):
            return []
        return ((data.get("result") or {}).get("records")) or []

    def _to_document(self, raw) -> Optional[PolarisDocument]:
        if not isinstance(raw, dict):
            return None
        series = (raw.get("DataSeries") or raw.get("dataseries") or "").strip()
        if not series:
            return None
        years = {k.strip(): v for k, v in raw.items() if _is_year(k)}
        pairs = sorted(years.items(), key=lambda kv: kv[0], reverse=True)
        body = "; ".join(f"{y}: {v}" for y, v in pairs
                         if v not in (None, "", "na", "NA", "-"))
        return PolarisDocument(
            source=self.name,
            source_id=f"datagovsg_npass:{series}",
            url=f"https://data.gov.sg/datasets/{_RID}/view",
            title=f"新加坡非居民准证占比 · {series}",
            content=f"{series} (占非居民人口百分比, 逐年): {body or '(无数据)'}",
            tags=["singapore", "immigration", "stats"],
            metadata={"data_series": series, "by_year": years, "_id": raw.get("_id")},
        )
