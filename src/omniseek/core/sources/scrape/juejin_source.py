"""掘金 Juejin — Chinese developer-article search (keyless, no login).

WHY (STRUCTURE): OmniSeek has NO Chinese dev-knowledge source. Juejin (juejin.cn) is a major CN
developer community; its public search API returns a STRUCTURED, engagement-ranked feed of dev
articles (title / brief / author / 赞 digg / 看 views / 收藏) that Google's blue links don't — for
"the most-engaged Chinese dev深度 writing on X". No login. (Individual articles ARE Google-indexed,
so the edge is the ranked structured feed + engagement signals, not raw UNWALL — a deliberate,
honest STRUCTURE-only claim.)

SHAPE: JSON POST (BaseScrapeAdapter + bespoke curl_cffi). Verified 2026-06-18: 向量数据库 → 20 dev
articles with digg/view counts + authors.

Recon trail: brain note eye-recon-juejin.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from omniseek.core.normalize import Document, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_SEARCH = "https://api.juejin.cn/search_api/v1/search"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    from curl_cffi import requests as _creq
    _DEPS_OK = True
except Exception as exc:  # noqa: BLE001
    logger.warning("juejin: curl_cffi unavailable (%s) — adapter inert", exc)
    _DEPS_OK = False


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _article_to_doc(rm: dict) -> Optional[Document]:
    """One search result_model → Document (pure fn → golden-fixture testable). Non-article
    result types (沸点/课程/…) carry no article_info → None (skipped)."""
    ai = rm.get("article_info") or {}
    au = rm.get("author_user_info") or {}
    aid = ai.get("article_id")
    title = ai.get("title")
    if not aid or not title:
        return None
    date = None
    ct = ai.get("ctime")
    if ct:
        try:
            date = datetime.fromtimestamp(int(ct), tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            date = None
    return Document(
        source="juejin",
        source_id=str(aid),
        url=f"https://juejin.cn/post/{aid}",
        title=title,
        content=ai.get("brief_content") or title,
        author=au.get("user_name"),
        date=date,
        signals=mk_signal("likes", _int(ai.get("digg_count")), kind="engagement", by="juejin/digg_count"),
        metadata={"views": _int(ai.get("view_count")), "diggs": _int(ai.get("digg_count")),
                  "comments": _int(ai.get("comment_count")), "collects": _int(ai.get("collect_count"))},
    )


class JuejinAdapter(BaseScrapeAdapter):
    name = "juejin"
    description = (
        "掘金 Juejin — Chinese developer-article search (keyless). query → an engagement-ranked feed "
        "of CN dev深度 articles: title / brief / author / 赞(digg) / 看(views). OmniSeek's source for "
        "Chinese dev/tech writing (前端/后端/AI工程/架构) that web search can't rank or structure. No login."
    )
    explicit_only = "Chinese dev articles (Juejin); name it for 中文技术/开发 topic search"
    kind = "lookup"
    domains = ["community"]
    regions = ["cn"]
    modes = ["STRUCTURE"]
    cache_ttl = 3600
    rank = False  # Juejin returns its own relevance order

    def _raw_fetch(self, query: str, limit: int):
        if not _DEPS_OK:
            return None
        try:
            r = _creq.post(_SEARCH, params={"aid": "2608", "uuid": "0"},
                           json={"id_type": 2, "cursor": "0", "limit": min(limit, 20),
                                 "key_word": query or "", "search_type": 0},
                           headers={"user-agent": _UA, "referer": "https://juejin.cn/",
                                    "content-type": "application/json"},
                           impersonate="chrome", timeout=15)
            j = r.json()
            if j.get("err_no") not in (0, None):
                logger.warning("juejin err_no=%s msg=%s", j.get("err_no"), j.get("err_msg"))
                return None
            return j.get("data") or []
        except Exception as exc:  # noqa: BLE001 — failure → None → [] (adapter contract)
            logger.warning("juejin fetch failed: %s", exc)
            return None

    def _to_documents(self, raw, query, limit) -> list[Document]:
        return [d for it in raw[:limit] if (d := _article_to_doc(it.get("result_model") or {}))]

    def health_check(self) -> tuple[bool, str]:
        if not _DEPS_OK:
            return False, "curl_cffi not installed"
        raw = self._raw_fetch("python", 1)
        if raw is None:
            return False, "fetch failed"
        return True, f"OK ({len(raw)} results)"
