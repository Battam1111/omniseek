"""中国政府网 政策文件库 — authoritative 国务院/国办 policy-document search (gov.cn).

WHY (STRUCTURE + UNWALL + MONITOR): sousuo.www.gov.cn's 政策库 is the AUTHORITATIVE corpus of
国务院 / 国办 policy documents (法规/条例/通知/国令) with server-side faceting by year / issuing
vehicle (国发/国办发/国令) / theme. Google indexes individual gov.cn pages but cannot return a
faceted, structured policy index keyed to 文号 + issuing-org + publish-date. No login, no token
(verified 2026-06-18: q=数据安全 → 《网络数据安全管理条例》国令第790号 国务院 + the gov.cn 原文 url).
Each hit's url is the full policy text on gov.cn → penumbra_read it for the body. Strongest telos
fit for Chinese-policy 信息差 / policy-trend (RECALL) work.

SHAPE: a JSON GET endpoint (BaseScrapeAdapter, bespoke curl_cffi). search(query) → the 国务院
policy library (t=zhengcelibrary_gw). Other libraries (部门文件 zhengcelibrary_bm, 地方 etc.) are
a future facet via the `t` param.

Recon trail: brain note eye-recon-gov_policy.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from penumbra.core.normalize import Document
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_API = ("https://sousuo.www.gov.cn/search-gov/data?t=zhengcelibrary_gw&q={q}"
        "&p=1&n={n}&sort=score&sortType=1&searchfield=title:content")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    from curl_cffi import requests as _creq
    _DEPS_OK = True
except Exception as exc:  # noqa: BLE001 — missing deps must never break server import
    logger.warning("gov_policy: curl_cffi unavailable (%s) — adapter inert", exc)
    _DEPS_OK = False


def _strip_em(s: Optional[str]) -> str:
    return re.sub(r"</?em>", "", s or "").strip()


def _list_of(j: dict) -> list:
    return (((j.get("searchVO") or {}).get("listVO"))
            or ((j.get("data") or {}).get("searchVO") or {}).get("listVO") or [])


def _doc_from_policy(it: dict) -> Optional[Document]:
    """One gov.cn policy-library item → Document (pure fn → golden-fixture testable)."""
    title = _strip_em(it.get("title"))
    url = it.get("url")
    if not title or not url:
        return None
    date = None
    ts = it.get("pubtime") or it.get("ptime")
    if ts:
        try:
            date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            date = None
    wenhao = _strip_em(it.get("wenhao")) or _strip_em(it.get("pcode")) or _strip_em(it.get("fwzh")) or None
    puborg = _strip_em(it.get("puborg")) or None
    return Document(
        source="gov_policy",
        source_id=str(it.get("code") or it.get("id") or url),
        url=url,
        title=title,
        content=_strip_em(it.get("summary")) or title,
        author=puborg,
        date=date,
        tags=[t for t in [puborg, _strip_em(it.get("childtype")) or None] if t],
        metadata={"wenhao": wenhao, "puborg": puborg, "childtype": _strip_em(it.get("childtype")) or None,
                  "source_site": it.get("source"), "shixiao": it.get("shixiao")},
    )


class GovPolicyAdapter(BaseScrapeAdapter):
    name = "gov_policy"
    description = (
        "中国政府网 政策文件库 (gov.cn) — the AUTHORITATIVE 国务院/国办 policy-document corpus (法规/条例/"
        "通知/国令), keyword search with each hit linking the FULL policy text on gov.cn (penumbra_read it). "
        "Google can't return this faceted policy index keyed to 文号 + issuing-org + date. No login. Reach "
        "for Chinese central-government policy / 政策 on a topic (incl. policy-trend / longitudinal work)."
    )
    explicit_only = "Chinese central-gov policy library (gov.cn); name it for 政策/法规/国务院文件 search"
    kind = "lookup"
    domains = ["news"]
    regions = ["cn"]
    modes = ["STRUCTURE", "UNWALL", "MONITOR"]
    cache_ttl = 3600
    rank = False  # gov.cn returns its own score/time order

    def _raw_fetch(self, query: str, limit: int):
        if not _DEPS_OK:
            return None
        url = _API.format(q=quote(query or ""), n=min(limit, 20))
        try:
            r = _creq.get(url, headers={"user-agent": _UA, "accept": "application/json",
                          "referer": "https://sousuo.www.gov.cn/sousuo/search-gov.shtml"},
                          impersonate="chrome", timeout=20)
            return _list_of(r.json() or {})
        except Exception as exc:  # noqa: BLE001 — failure → None → [] (adapter contract)
            logger.warning("gov_policy fetch failed: %s", exc)
            return None

    def _to_documents(self, raw, query, limit) -> list[Document]:
        return [d for it in raw[:limit] if (d := _doc_from_policy(it))]

    def health_check(self) -> tuple[bool, str]:
        if not _DEPS_OK:
            return False, "curl_cffi not installed"
        raw = self._raw_fetch("数据", 1)
        if raw is None:
            return False, "fetch failed / blocked"
        return True, f"OK ({len(raw)} docs)"
