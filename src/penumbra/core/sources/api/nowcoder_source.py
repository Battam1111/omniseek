"""牛客网 (nowcoder) 面经 — interview-experience feed via the public JSON gateway.

牛客 is the canonical CN venue for 面经 (interview experiences) + 内推 (referrals):
the REAL, current AI/ML interview bar written within days of interviewing — whether
a firm leans 八股 vs 重思维/paper-driven, full hiring-loop timelines, live referral
codes. The SPA's gateway endpoint returns full records with NO auth (the older
RSSHub /discuss/experience/json route is degraded → empty for anonymous callers).

Config (optional) ``~/.polaris/credentials/nowcoder.json``: {"job_ids": [645, ...]}
— 645 = 算法工程师 (the AI/ML interview position tag).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from penumbra.core import auth, cache
from penumbra.core.normalize import PolarisDocument, jsonsafe, keyword_score_filter, mk_signal

logger = logging.getLogger(__name__)

API = "https://gw-c.nowcoder.com/api/sparta/job-experience/experience/job/list"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": UA,
    "Referer": "https://www.nowcoder.com/",
    "Origin": "https://www.nowcoder.com",
}
DEFAULT_JOB_IDS = [645]  # 算法工程师 (AI/ML)
CACHE_TTL = 3600

auth.write_template("nowcoder", {"_comment": "牛客面经跟踪. job_ids: 职位 tag (645=算法工程师).", "job_ids": [645]})


class NowcoderAdapter:
    name = "nowcoder"
    needs_credentials = False
    # The direct JSON gateway is now Aliyun-WAF-walled for datacenter IPs (returns an HTML
    # challenge, verified 2026-06-11), so the working path is a Brave site:-search fallback that
    # fires an engine query — explicit_only keeps it OUT of the broad fan-out (name it to use it).
    explicit_only = "direct JSON API WAF-blocked → Brave site-search fallback (engine quota)"
    description = (
        "牛客网 面经 + 内推 — 中文 AI/ML 真实面试 bar (八股 vs 重思维 / 全流程时间线 / 内推码), "
        "成于面试后数天. 直连 JSON 已被 Aliyun WAF 墙 → 降级走 site:nowcoder.com Brave 检索 (snippet). "
        "默认 算法工程师(645), 可配 ~/.polaris/credentials/nowcoder.json job_ids"
    )

    def _job_ids(self) -> list:
        creds = auth.load("nowcoder")
        if creds and isinstance(creds.get("job_ids"), list) and creds["job_ids"]:
            return creds["job_ids"]
        return DEFAULT_JOB_IDS

    def _fetch_job(self, job_id, order: int = 3, pages: int = 1) -> list[PolarisDocument]:
        key = cache.make_key("nowcoder", "job", job_id, order, pages)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        docs: list[PolarisDocument] = []
        for page in range(1, pages + 1):
            try:
                r = httpx.post(API, headers=HEADERS,
                               json={"jobId": job_id, "order": order, "page": page, "companyList": []}, timeout=25)
                recs = ((r.json() or {}).get("data") or {}).get("records") or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("nowcoder job %s page %s failed: %s", job_id, page, exc)
                break
            for it in recs:
                if it.get("contentType") != 250:  # 250 = real post; 74 = ad/recommend card
                    continue
                cd = it.get("contentData") or {}
                if cd.get("uuid"):
                    docs.append(self._to_doc(it, cd, job_id))
        if docs:
            cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    def _to_doc(self, it: dict, cd: dict, job_id) -> PolarisDocument:
        uuid = cd.get("uuid")
        body = cd.get("content") or re.sub(r"<[^>]+>", " ", cd.get("richText") or cd.get("newContent") or "")
        body = re.sub(r"\s+", " ", body).strip() or "(no content)"
        date = None
        ts = cd.get("createTime")
        if ts:
            try:
                date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None
        freq = it.get("frequencyData") or {}
        return PolarisDocument(
            source="nowcoder",
            source_id=str(uuid),
            url=f"https://www.nowcoder.com/feed/main/detail/{uuid}",
            title=(cd.get("title") or cd.get("newTitle") or "(untitled)").strip(),
            content=body,
            author=(it.get("userBrief") or {}).get("nickname"),
            date=date,
            signals=mk_signal("likes", freq.get("likeCnt"),
                              kind="engagement", by="nowcoder/likeCnt"),
            tags=["面经", cd.get("typeName") or "", f"job:{job_id}"],
            metadata={"job_id": job_id, "comment_cnt": freq.get("commentCnt"),
                      "view_cnt": freq.get("viewCnt"), "raw": jsonsafe(it)},
        )

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        docs: list[PolarisDocument] = []
        for jid in self._job_ids():
            docs.extend(self._fetch_job(jid))
        q = (query or "").strip()
        if docs:  # direct API alive → full posts
            if q:
                return keyword_score_filter(docs, q)[:limit]
            docs.sort(key=lambda d: d.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return docs[:limit]
        # Direct gateway yielded nothing (Aliyun-WAF-walled) → site:-scoped web search fallback.
        return self._search_fallback(q, limit)

    def _search_fallback(self, query: str, limit: int) -> list[PolarisDocument]:
        from penumbra.core.sources.api._search_backend import search_web
        q = f"site:nowcoder.com/feed {query}".strip() if query else "site:nowcoder.com/feed 面经 算法工程师"
        docs: list[PolarisDocument] = []
        for r in search_web(q, n=min(max(limit * 2, 6), 15)):
            url = r.get("url")
            if not url or "nowcoder.com" not in url:
                continue
            docs.append(PolarisDocument(
                source="nowcoder", source_id=url, url=url,
                title=r.get("title") or "(untitled)",
                content=r.get("snippet") or "(snippet only — open the URL for the full 面经)",
                tags=["面经", "search-index-fallback"],
                metadata={"via": "brave-fallback (direct API WAF-blocked)", "raw": jsonsafe(r)},
            ))
            if len(docs) >= limit:
                break
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        return None  # search-only; the list endpoint already carries full post content

    def health_check(self) -> tuple[bool, str]:
        try:
            r = httpx.post(API, headers=HEADERS,
                           json={"jobId": DEFAULT_JOB_IDS[0], "order": 3, "page": 1, "companyList": []}, timeout=12)
            recs = ((r.json() or {}).get("data") or {}).get("records") or []
            n = sum(1 for it in recs if it.get("contentType") == 250)
            if n:
                return True, f"OK ({n} posts, direct API)"
        except Exception:  # noqa: BLE001 — WAF returns an HTML challenge → JSON decode fails
            pass
        # Direct API down / WAF-walled → the source still works IFF the Brave fallback is alive.
        from penumbra.core.sources.api._search_backend import backend_ping
        ok, msg = backend_ping()
        return (True, f"OK (direct API WAF-blocked; Brave fallback: {msg})") if ok \
            else (False, f"direct API WAF-blocked + Brave fallback down: {msg}")


from penumbra.core.fetcher import register_adapter

register_adapter(NowcoderAdapter())
