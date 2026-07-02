"""巨潮资讯网 CNINFO — A-share 公司公告/财报 full-text disclosure index (the Chinese EDGAR).

WHY (STRUCTURE + UNWALL): CNINFO (www.cninfo.com.cn) is the SSE/SZSE OFFICIAL disclosure
repository — every listed company's 年报/季报/招股书/ad-hoc 公告 as a queryable index with a
DIRECT PDF link per hit. Google returns tutorials about CNINFO, never the structured filing
index or the PDFs themselves. No login, no token (verified 2026-06-18: keyword '数据安全' →
永信至诚/立思辰/天喻信息 公告 + finalpage/*.PDF links). Each adjunctUrl resolves to a fetchable
PDF on static.cninfo.com.cn → pair with penumbra_read for full-text filing analysis. Telos
Chinese 信息差 (authoritative financial disclosure).

SHAPE: a JSON POST endpoint (BaseScrapeAdapter, bespoke curl_cffi). search(query) → keyword
full-text search across ALL filings (the `searchkey`/`tabName=fulltext` mode is global; column
'szse' and 'sse' return identical results). Company-scoped filings (stock=code,orgId via the
topSearch orgId resolver) are a future facet, not v1.

Recon trail: brain note eye-recon-cninfo.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from penumbra.core.normalize import Document
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_PDF_BASE = "https://static.cninfo.com.cn/"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    from curl_cffi import requests as _creq
    _DEPS_OK = True
except Exception as exc:  # noqa: BLE001 — missing deps must never break server import
    logger.warning("cninfo: curl_cffi unavailable (%s) — adapter inert", exc)
    _DEPS_OK = False


def _clean_title(t: Optional[str]) -> str:
    """Strip CNINFO's <em>…</em> keyword-highlight tags from the announcement title."""
    return re.sub(r"</?em>", "", t or "").strip()


def _ann_to_doc(a: dict) -> Optional[Document]:
    """One CNINFO announcement dict → Document (pure fn → golden-fixture testable)."""
    title = _clean_title(a.get("announcementTitle"))
    adj = (a.get("adjunctUrl") or "").lstrip("/")
    if not title or not adj:
        return None
    date = None
    ts = a.get("announcementTime")
    if ts:
        try:
            date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            date = None
    sec_name, sec_code = a.get("secName"), a.get("secCode")
    return Document(
        source="cninfo",
        source_id=str(a.get("announcementId") or adj),
        url=_PDF_BASE + adj,  # the filing PDF — penumbra_read it for the full body
        title=title,
        content=title,  # the index carries no abstract; the PDF at `url` IS the body
        author=sec_name,
        date=date,
        tags=[sec_code] if sec_code else [],
        metadata={"sec_name": sec_name, "sec_code": sec_code, "adjunct_url": adj,
                  "is_pdf": adj.lower().endswith(".pdf")},
    )


class CninfoAdapter(BaseScrapeAdapter):
    name = "cninfo"
    description = (
        "巨潮资讯网 CNINFO — the SSE/SZSE OFFICIAL A-share disclosure repository (the Chinese EDGAR). "
        "Keyword full-text search over every listed-company filing (年报/季报/招股书/ad-hoc 公告), each "
        "hit a DIRECT PDF link (static.cninfo.com.cn) — pair with penumbra_read for the body. "
        "Google can't return this structured filing index. No login. Reach for 上市公司/A股 "
        "disclosures / 财报 / 年报 / 公告 / 招股书 on a company or topic."
    )
    explicit_only = "Chinese A-share filings (CNINFO JSON API); name it for 财报/公告/disclosure search"
    kind = "lookup"
    domains = ["finance"]
    regions = ["cn"]
    modes = ["STRUCTURE", "UNWALL"]
    cache_ttl = 3600
    rank = False  # CNINFO returns its own relevance/time order

    def _raw_fetch(self, query: str, limit: int):
        if not _DEPS_OK:
            return None
        data = {"pageNum": "1", "pageSize": str(min(limit, 30)), "column": "szse",
                "tabName": "fulltext", "plate": "", "stock": "", "searchkey": query,
                "secid": "", "category": "", "trade": "", "seDate": "",
                "sortName": "", "sortType": "", "isHLtitle": "true"}
        headers = {"user-agent": _UA, "x-requested-with": "XMLHttpRequest",
                   "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                   "referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                   "accept": "*/*"}
        try:
            r = _creq.post(_QUERY, data=data, headers=headers, impersonate="chrome", timeout=20)
            return (r.json() or {}).get("announcements") or []
        except Exception as exc:  # noqa: BLE001 — failure → None → [] (adapter contract)
            logger.warning("cninfo fetch failed: %s", exc)
            return None

    def _to_documents(self, raw, query, limit) -> list[Document]:
        return [d for a in raw[:limit] if (d := _ann_to_doc(a))]

    def health_check(self) -> tuple[bool, str]:
        if not _DEPS_OK:
            return False, "curl_cffi not installed"
        raw = self._raw_fetch("年报", 1)
        if raw is None:
            return False, "fetch failed / blocked"
        return True, f"OK ({len(raw)} announcements)"
