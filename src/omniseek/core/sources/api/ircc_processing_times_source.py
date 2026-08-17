"""IRCC processing times: Canada's official per-country / per-service table, STRUCTURED.

IRCC publishes the live processing-time estimate for every application type from
every country as JSON (the data that powers the official "Check processing
times" tool). Web search only narrates it and the tool itself is a form you must
click through country by country; this exposes it as a queryable lookup so
"study permit China" or "work permit India" returns the current estimate
directly. Mode: STRUCTURE (a per-route lookup table, refreshed monthly by IRCC).

Two payloads make one table:
  - data-ptime-en.json          : outside-Canada services keyed by application
                                  type then ISO-2 country code -> time string.
  - data-ptime-non-country-en.json : in-Canada services keyed by category then
                                  sub-service -> time string (+ a lastupdated).
  - data-country-name-en.json   : ISO-2 -> country display name.

Access (verified 2026-07-10): canada.ca walls the large data-ptime-en.json from
plain HTTP at the transport level (curl times out / HTTP 000, WebFetch 403),
exactly like ircc_ee_rounds; the CDP real browser reads it fine. So this fetches
all three via the shared CDP Chrome (lazy import) and is explicit_only: named
calls only, never the broad fan-out.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from omniseek.core import cache
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

PTIME_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/data-ptime-en.json"
NONCOUNTRY_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/data-ptime-non-country-en.json"
COUNTRY_NAME_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/data-country-name-en.json"
CHECK_PAGE = ("https://www.canada.ca/en/immigration-refugees-citizenship/services/"
              "application/check-processing-times.html")
CACHE_TTL = 86400  # 24h: IRCC refreshes the table roughly monthly, so daily is ample

# Human labels for the outside-Canada application-type keys (data-ptime-en.json).
APP_TYPES = {
    "visitor-outside-canada": "Visitor visa (from outside Canada)",
    "supervisa": "Super visa (parents and grandparents)",
    "study": "Study permit (from outside Canada)",
    "work": "Work permit (from outside Canada)",
    "child_dependent": "Sponsor a dependent child",
    "child_adopted": "Sponsor an adopted child",
    "refugees_gov": "Government-assisted refugee",
    "refugees_private": "Privately sponsored refugee",
}

# Human labels for the in-Canada sub-service keys (data-ptime-non-country-en.json).
# Unknown keys fall back to a prettified form, so a new IRCC key still yields a doc.
IN_CANADA = {
    "visitor_inside_canada": "Visitor record (in Canada)",
    "visitor_extension": "Visitor visa / status extension (in Canada)",
    "study_extension": "Study permit extension (in Canada)",
    "work_extension": "Work permit extension (in Canada)",
    "iec": "International Experience Canada (current season)",
    "iec_past": "International Experience Canada (past season)",
    "eta": "Electronic Travel Authorization (eTA)",
    "skilled_trades_ee": "Federal Skilled Trades (Express Entry)",
    "cit_resumption": "Citizenship: resumption",
    "cit_renunciation": "Citizenship: renunciation",
    "cit_search": "Citizenship: search / proof of records",
    "cit_adoption_part1": "Citizenship for adopted persons (Part 1)",
    "new_pr": "New permanent resident (PR) card",
    "existing_pr": "PR card renewal",
    "vos": "Verification of status (VOS)",
    "replacement": "Replacement of an immigration document",
    "amend_imm": "Amend an immigration document",
    "amend_tr": "Amend a temporary resident document",
    "sawp_current": "Seasonal Agricultural Worker Program (SAWP)",
}

_HAS_DIGIT = re.compile(r"\d")


def _has_time(value) -> bool:
    """A real estimate (not 'No processing time available' / 'Not enough data')."""
    if isinstance(value, dict):
        return any(_has_time(v) for v in value.values())
    return bool(_HAS_DIGIT.search(str(value)))


def _fetch_json(url: str) -> dict:
    """One JSON payload via the CDP real browser (lazy import: never pull
    playwright in when the source is untouched)."""
    from omniseek.core.sources.walled._cdp import cdp_call

    def _nav(page):
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        return page.inner_text("body")

    body = cdp_call(_nav, initial_url=url, timeout=60)
    return json.loads(body)


class IRCCProcessingTimesAdapter:
    name = "ircc_processing_times"
    needs_credentials = False
    kind = "lookup"
    domains = ["immigration"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    explicit_only = "CDP fetch of canada.ca (data-ptime-en.json transport-walled to plain HTTP); named only"
    description = (
        "IRCC 处理时长表 (加拿大官方 JSON, 经 CDP 真浏览器取): 每种申请类型 x 每个国家的当前处理时长估计, "
        "结构化可查, 即官方 Check processing times 工具背后的数据. 覆盖境外服务 (访客 / 学签 / 工签 / 超级签证 / "
        "团聚 / 难民) 与境内服务 (延期 / PR 卡 / 公民 / eTA / IEC...). 例: 'study permit China' / 'work permit India' / "
        "'PR card'. 空 query=各路由一批; 关键词按申请类型 + 国家过滤."
    )

    # table cache (raw JSON payloads, one combined blob)
    def _tables(self) -> Optional[dict]:
        key = cache.make_key("ircc_processing_times", "tables", "v1")
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            tables = {
                "country": _fetch_json(PTIME_JSON),
                "noncountry": _fetch_json(NONCOUNTRY_JSON),
                "names": _fetch_json(COUNTRY_NAME_JSON).get("country-name", {}),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("ircc_processing_times: fetch failed: %s", exc)
            return None
        cache.set(key, tables, ttl=CACHE_TTL)
        return tables

    @staticmethod
    def _updated(noncountry: dict) -> Optional[datetime]:
        raw = ((noncountry.get("default-update") or {}).get("lastupdated") or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%B %d, %Y").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    def _build_docs(self, tables: dict) -> list[Document]:
        names = tables.get("names") or {}
        updated = self._updated(tables.get("noncountry") or {})
        updated_str = updated.date().isoformat() if updated else None
        docs: list[Document] = []

        # Outside-Canada: application type x country code.
        for app_key, countries in (tables.get("country") or {}).items():
            label = APP_TYPES.get(app_key, app_key.replace("-", " ").replace("_", " ").title())
            if not isinstance(countries, dict):
                continue
            for cc, value in countries.items():
                if not _has_time(value):
                    continue
                country = html.unescape(names.get(cc, cc))
                if isinstance(value, dict):  # refugees_private -> {sponsor, refugee}
                    value_str = "  ·  ".join(f"{k}: {v}" for k, v in value.items())
                else:
                    value_str = str(value)
                docs.append(self._doc(
                    source_id=f"{app_key}:{cc}",
                    title=f"{label}, {country}: {value_str}",
                    body=f"{label}\nApply from: {country} ({cc})\n"
                         f"Current IRCC processing time: {value_str}",
                    updated=updated, updated_str=updated_str,
                    tags=["processing-time", "immigration", "ca", app_key],
                    meta={"application_type": app_key, "country_code": cc,
                          "country": country, "processing_time": value_str,
                          "in_canada": False, "last_updated": updated_str, "raw": value},
                ))

        # In-Canada: category x sub-service.
        for cat, subs in (tables.get("noncountry") or {}).items():
            if cat == "default-update" or not isinstance(subs, dict):
                continue
            for sub_key, value in subs.items():
                if not _has_time(value):
                    continue
                label = IN_CANADA.get(sub_key, sub_key.replace("_", " ").title())
                docs.append(self._doc(
                    source_id=f"incanada:{cat}:{sub_key}",
                    title=f"{label} (in Canada): {value}",
                    body=f"{label}\nProcessed in Canada.\n"
                         f"Current IRCC processing time: {value}",
                    updated=updated, updated_str=updated_str,
                    tags=["processing-time", "immigration", "ca", "in-canada", cat],
                    meta={"application_type": cat, "service": sub_key,
                          "processing_time": str(value), "in_canada": True,
                          "last_updated": updated_str, "raw": value},
                ))
        return docs

    def _doc(self, *, source_id, title, body, updated, updated_str, tags, meta) -> Document:
        if updated_str:
            body = f"{body}\n(IRCC estimate as of {updated_str})"
        return Document(
            source=self.name,
            source_id=source_id,
            url=CHECK_PAGE,
            title=title,
            content=body,
            date=updated,
            tags=tags,
            metadata=meta,
        )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        tables = self._tables()
        if not tables:
            return []
        docs = self._build_docs(tables)
        docs = keyword_score_filter(docs, (query or "").strip())
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # structured lookup source; reach it via search

    def health_check(self) -> tuple[Optional[bool], str]:
        # LIGHT: never a full CDP fetch in a health probe (the P19 lesson). Browser
        # liveness + whatever the cache holds is an honest signal.
        try:
            from omniseek.core.sources.walled._cdp import cdp_health
            alive, msg = cdp_health()
        except Exception as exc:  # noqa: BLE001
            return None, f"CDP unavailable: {exc}"
        if not alive:
            return None, f"CDP down: {msg}"
        cached = cache.get(cache.make_key("ircc_processing_times", "tables", "v1"))
        if cached:
            n = sum(len(v) for v in (cached.get("country") or {}).values() if isinstance(v, dict))
            return True, f"OK (CDP up; ~{n} country rows cached)"
        return True, "OK (CDP up; no cache yet)"


from omniseek.core.fetcher import register_adapter

register_adapter(IRCCProcessingTimesAdapter())
