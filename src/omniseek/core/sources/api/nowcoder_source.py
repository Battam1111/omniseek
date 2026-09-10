"""牛客网 (nowcoder) 面经 — interview-experience feed via the public JSON gateway.

牛客 is the canonical CN venue for 面经 (interview experiences) + 内推 (referrals):
the REAL, current AI/ML interview bar written within days of interviewing — whether
a firm leans 八股 vs 重思维/paper-driven, full hiring-loop timelines, live referral
codes. The SPA's gateway endpoint returns full records with NO auth (the older
RSSHub /discuss/experience/json route is degraded → empty for anonymous callers).

Config (optional) ``~/.omniseek/credentials/nowcoder.json``: {"job_ids": [645, ...]}
— 645 = 算法工程师 (the AI/ML interview position tag).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from omniseek.core import auth, cache, diag
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter, mk_signal
from omniseek.core.sources.walled._cdp import cdp_call, cdp_health

logger = logging.getLogger(__name__)

API = "https://gw-c.nowcoder.com/api/sparta/job-experience/experience/job/list"
DEFAULT_JOB_IDS = [645]  # 算法工程师 (AI/ML)
CACHE_TTL = 3600

auth.write_template("nowcoder", {"_comment": "牛客面经跟踪. job_ids: 职位 tag (645=算法工程师).", "job_ids": [645]})


class NowcoderAdapter:
    name = "nowcoder"
    needs_credentials = False
    # The gateway JSON API is Aliyun-WAF-walled for datacenter httpx (an HTML sliding-captcha), but the
    # WAF is FINGERPRINT-gated not login-gated, so the native path is restored by fetching it from inside
    # the shared 9222 CDP Chrome (real fingerprint passes clean, no login — verified 2026-07-10: 200 JSON,
    # full records with real dates + engagement). The site search is the venue's KEYWORD search and runs
    # alongside the feed whenever a query is given (it is also the whole answer when CDP is down).
    # explicit_only keeps it OUT of the broad fan-out (the 9222 Chrome is a serial shared resource).
    explicit_only = ("native JSON via shared 9222 CDP Chrome (WAF is fingerprint-gated); a query also "
                     "fires the site search on the shared paced backend (brave->ddg)")
    description = (
        "牛客网 面经 + 内推 — 中文 AI/ML 真实面试 bar (八股 vs 重思维 / 全流程时间线 / 内推码), "
        "成于面试后数天. 两条路: 无 query = 拉最近面经流 (默认岗位 tag 645 算法工程师, 可配 "
        "~/.omniseek/credentials/nowcoder.json job_ids), 经共享 9222 CDP Chrome 原生取 JSON "
        "(WAF 是指纹闸非登录闸, 无需登录); 有 query = 在这批最近面经里做关键词筛选, 并合并 "
        "site:nowcoder.com 的网页搜索结果 (真正的站内关键词搜索, 走共享搜索后端), 每条 "
        "metadata.via 标明来路 (native-feed / native-feed-filter / site-search), 两半并集按与 query "
        "的相关度排序后再截到 limit (含具体公司名的站内结果排在只命中泛词的面经前面). "
        "泛词 (实习 / 面试) 会命中几乎所有面经, 查具体公司或岗位时用 公司名 + 岗位 + 具体词."
    )

    def _job_ids(self) -> list:
        creds = auth.load("nowcoder")
        if creds and isinstance(creds.get("job_ids"), list) and creds["job_ids"]:
            return creds["job_ids"]
        return DEFAULT_JOB_IDS

    # The JS the real Chrome runs from the nowcoder.com origin: fetch the gateway JSON API. The WAF
    # (Aliyun aliyunCaptcha) is FINGERPRINT / JS-execution gated, NOT login-gated — a datacenter httpx
    # POST gets an HTML sliding-captcha page (JSONDecodeError), but a fetch from inside the fingerprinted
    # 9222 Chrome (after it has loaded nowcoder.com, so the WAF challenge cookie is set) returns 200 JSON.
    _JS_FETCH = """async ({jobId, order, pg}) => {
        const r = await fetch('https://gw-c.nowcoder.com/api/sparta/job-experience/experience/job/list', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify({jobId, order, page: pg, companyList: []}),
            credentials: 'include'
        });
        return await r.text();
    }"""

    def _fetch_job(self, job_id, order: int = 3, pages: int = 2) -> list[Document]:
        key = cache.make_key("nowcoder", "job", job_id, order, pages)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        def _flow(page) -> list:
            # nowcoder.com is already loaded (initial_url); let the WAF JS-challenge settle so its
            # cleared-cookie is set before the cross-subdomain fetch to gw-c.nowcoder.com carries it.
            page.wait_for_timeout(1500)
            out: list = []
            for pg in range(1, pages + 1):
                txt = page.evaluate(self._JS_FETCH, {"jobId": job_id, "order": order, "pg": pg})
                try:
                    data = json.loads(txt)
                except (ValueError, TypeError):
                    # a WAF challenge HTML body (not JSON) — the fingerprint pass failed this run;
                    # stop and let the caller's brave fallback serve, but note it so it's VISIBLE.
                    diag.note("nowcoder.waf_html", url=API,
                              body="gw-c returned non-JSON (WAF challenge) even via CDP; brave fallback")
                    break
                recs = ((data or {}).get("data") or {}).get("records") or []
                if not recs:
                    break
                out.extend(recs)
            return out

        try:
            recs = cdp_call(_flow, initial_url="https://www.nowcoder.com/", timeout=60)
        except Exception as exc:  # noqa: BLE001
            logger.warning("nowcoder CDP fetch job %s failed: %s", job_id, exc)
            diag.note("nowcoder.cdp_fetch", url=API, exc=exc)
            return []
        docs: list[Document] = []
        for it in recs:
            if it.get("contentType") != 250:  # 250 = real post; 74 = ad/recommend card
                continue
            cd = it.get("contentData") or {}
            if cd.get("uuid"):
                docs.append(self._to_doc(it, cd, job_id))
        if docs:
            cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    def _to_doc(self, it: dict, cd: dict, job_id) -> Document:
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
        return Document(
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

    def search(self, query: str, limit: int = 10) -> list[Document]:
        """No query = the recent 面经 FEED. A query = keyword search, both ways at once.

        The feed is a STREAM of ~20 recent posts, so filtering it was never a search: a generic word
        (实习 / 面试) matched nearly every post and a specific one (a company, a role) missed
        everything older than the window, while the caller read the result as "what nowcoder has on
        this". So a query now ALSO runs the site search, which is the venue's real keyword search
        over its archive, and every doc says which path it came from in ``metadata.via``.
        """
        feed: list[Document] = []
        for jid in self._job_ids():
            feed.extend(self._fetch_job(jid))
        scanned = len(feed)
        q = (query or "").strip()
        if not q:
            if not feed:  # gateway yielded nothing (CDP down) → the site search still serves
                return self._search_fallback("", limit)
            feed.sort(key=lambda d: d.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            out = feed[:limit]
            for d in out:
                d.metadata["via"] = "native-feed"
                d.metadata["feed_scanned"] = scanned
            return out
        native = keyword_score_filter(feed, q)
        for d in native:
            d.metadata["via"] = "native-feed-filter"
            d.metadata["feed_scanned"] = scanned
        try:
            site = self._search_fallback(q, limit)
        except RuntimeError as exc:
            # The site search now runs on EVERY query, which puts the SHARED backend's cooldown in
            # front of a path that used to be feed-only. Re-raise when there is nothing else to hand
            # back (the error contract: an empty must never be published as "nowcoder has no such
            # 面经"), but a cooling backend must not DELETE feed matches we already hold — degrade to
            # them and capture why, so the caller reads a partial degrade instead of a dead source.
            if not native:
                raise
            diag.note("nowcoder.backend", url=API, exc=exc,
                      body="site search unavailable; returning the native-feed matches only")
            site = []
        # Merge, then RANK the union by relevance to the query BEFORE cutting to limit. "Native
        # first" was wrong in practice (live, 2026-09-10): the generic query word (实习) matched 16 of
        # the 25 feed posts, none about the company asked for, while the four site hits that WERE
        # about it sat at positions 17-20, cut off at any normal limit. BM25 over the union puts the
        # docs carrying the rare query term first wherever they came from; a site hit whose snippet
        # carries no query token still came from a keyword search, so it stays, after the scored ones.
        pool: list[Document] = []
        seen: set = set()
        for d in native + site:
            key = (d.url or "").split("?", 1)[0]  # the engine's ?urlSource=... twin is the same post
            if key in seen:
                continue
            seen.add(key)
            pool.append(d)
        ranked = keyword_score_filter(pool, q)
        ranked_ids = {id(d) for d in ranked}
        docs = (ranked + [d for d in pool if id(d) not in ranked_ids])[:limit]
        if not docs:  # failure branch only, per diag.note's contract
            diag.note("nowcoder.no_match", url=API,
                      body=f"feed scanned {scanned} recent posts (job tags {self._job_ids()}), "
                           f"none matched {q!r}; site search returned 0")
        return docs

    def _search_fallback(self, query: str, limit: int) -> list[Document]:
        """site:-scoped web search over nowcoder — the venue's real keyword search (and the whole
        source when the CDP native path is down)."""
        from omniseek.core.sources.api._search_backend import search_web
        q = f"site:nowcoder.com/feed {query}".strip() if query else "site:nowcoder.com/feed 面经 算法工程师"
        docs: list[Document] = []
        for r in search_web(q, n=min(max(limit * 2, 6), 15)):
            url = r.get("url")
            if not url or "nowcoder.com" not in url:
                continue
            docs.append(Document(
                source="nowcoder", source_id=url, url=url,
                title=r.get("title") or "(untitled)",
                content=r.get("snippet") or "(snippet only — open the URL for the full 面经)",
                tags=["面经", "search-index-fallback"],
                metadata={"via": "site-search", "raw": jsonsafe(r)},
            ))
            if len(docs) >= limit:
                break
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # search-only; the list endpoint already carries full post content

    def health_check(self) -> tuple[bool, str]:
        # Native path via CDP: drive the 9222 Chrome to fetch the gateway once and count real posts.
        cdp_ok, cdp_msg = cdp_health(ensure=True)
        if cdp_ok:
            try:
                docs = self._fetch_job(DEFAULT_JOB_IDS[0], pages=1)
                if docs:
                    return True, f"OK ({len(docs)} posts, native JSON via CDP)"
            except Exception:  # noqa: BLE001 — fall through to the fallback ping
                pass
        # CDP down / native yielded nothing → the source still works IFF the Brave fallback is alive.
        from omniseek.core.sources.api._search_backend import backend_ping
        ok, msg = backend_ping()
        return (True, f"OK (native via CDP unavailable [{cdp_msg}]; Brave fallback: {msg})") if ok \
            else (False, f"native CDP path down [{cdp_msg}] + Brave fallback down: {msg}")


from omniseek.core.fetcher import register_adapter

register_adapter(NowcoderAdapter())
