"""什么值得买 (smzdm.com) deal posts: dated, mall-attributed prices of NEW goods, by keyword.

EYE-XIANYU 乙 (2026-10-24). Admission (the razor, STRUCTURE): for 「锐龙 5 5600 盒装」 web search gave one
undated figure (「约839元」, citing a JD search page that needs a login), while this list gave per-post
prices with the mall, the post time and whether the deal has expired (683.14元 京东自营 10-20; 799元 过期;
1029元 抖音商城 套装 ...). A product's posts over time are the nearest thing to a price history that needs
no login; a real per-item price-history chart (smzdm 百科, 慢慢买) needs a login and is not read here.

Endpoint: ``https://api.smzdm.com/v1/list?keyword=<kw>&type=good_price&offset=0&limit=<n>`` (the list the
smzdm apps and mobile site read). Anonymous GET, no signature, JSON
``{"error_code":"0","data":{"total_num","rows":[...]}}``. The web search page (search.smzdm.com) is behind a
Tencent captcha WAF, which this source never touches. Recon trail: brain note eye-recon-smzdm.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.api._base import BaseAPIAdapter

_LIST = "https://api.smzdm.com/v1/list"
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万)?元")


def _price(text: str) -> Optional[float]:
    m = _PRICE_RE.search((text or "").replace(",", ""))
    if not m:
        return None
    v = float(m.group(1))
    return v * 10000 if m.group(2) else v


def _int(v) -> Optional[int]:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def list_rows(payload) -> Optional[list]:
    """The rows of a list answer; None when the answer is not a successful list (an error code or another shape)."""
    if not isinstance(payload, dict) or str(payload.get("error_code")) != "0":
        return None
    rows = (payload.get("data") or {}).get("rows") if isinstance(payload.get("data"), dict) else None
    return rows if isinstance(rows, list) else None


class SmzdmAdapter(BaseAPIAdapter):
    name = "smzdm"
    description = ("什么值得买 好价爆料：按关键词取新品的优惠帖，每条带价格、商城（京东、天猫、抖音商城等）、发帖时间、"
                   "是否已过期、值与评论数、链接。一个商品的历次爆料可当价格走势看；不是官方实时价，也没有登录后才有的历史价曲线。")
    explicit_only = "shopping prices on request only (an unofficial app list endpoint; upstream gate smzdm_api)"
    kind = "lookup"
    domains = ["shopping"]
    regions = ["cn"]
    modes = ["STRUCTURE"]
    cache_ttl = 1800
    rank_locally = True   # the list mixes in loosely related posts (a camera bag for 「RX 6600」); BM25 drops them
    license = "upstreams:smzdm_api"

    def _url(self, query: str, limit: int) -> tuple[str, dict]:
        return _LIST, {"keyword": query, "type": "good_price", "offset": 0, "limit": max(1, min(int(limit), 50))}

    def _raw_fetch(self, query: str, limit: int) -> list:
        url, params = self._url(query, limit)
        resp = http.get(url, params=params, timeout=15)
        if resp is None:
            raise RuntimeError("smzdm list: request failed")
        rows = list_rows(resp.json())
        if rows is None:
            raise RuntimeError("smzdm list: not a successful list answer")
        return rows

    def _to_document(self, raw) -> Optional[Document]:
        url, title = raw.get("article_url"), raw.get("article_title")
        if not url or not title:
            return None
        price_text = str(raw.get("article_price") or "")
        expired = raw.get("article_is_timeout") == "is_timeout" or raw.get("article_status_name") == "过期"
        ts = _int(raw.get("publish_date_lt"))
        meta = {"price_text": price_text, "mall": raw.get("article_mall") or None, "expired": expired,
                "brand": raw.get("article_brand") or None, "raw": jsonsafe(raw)}
        p = _price(price_text)
        if p is not None:
            meta["price"] = p
        if raw.get("article_status_name"):
            meta["status"] = raw["article_status_name"]
        signals = mk_signal("worthy", _int(raw.get("article_worthy")), kind="engagement", by="smzdm/article_worthy")
        signals.update(mk_signal("comments", _int(raw.get("article_comment")), kind="engagement",
                                 by="smzdm/article_comment"))
        return Document(
            source=self.name, source_id=str(raw.get("article_id") or url), url=url, title=str(title),
            content="", author=raw.get("article_referrals") or None,
            date=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
            signals=signals, tags=[t for t in (raw.get("article_tag_arr") or []) if isinstance(t, str)],
            metadata=meta)

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        url, params = self._url("内存条", 1)
        resp = await http.aget(url, params=params, timeout=15)
        if resp is None:
            return False, "request failed (timeout / network / refused)"
        try:
            rows = list_rows(resp.json())
        except ValueError:
            rows = None
        if rows is None:
            return False, f"HTTP {resp.status_code}: not a successful list answer"
        return True, f"OK ({len(rows)} row)"
