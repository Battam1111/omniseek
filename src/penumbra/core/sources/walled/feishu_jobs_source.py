"""Feishu 招聘 (jobs.feishu.cn) ATS 适配器 — 6 个 Tier 1 大模型 startup.

Phase 4 P11 opus sub-agent 二次调研（2026-05-28，30+ tool calls）确认：
飞书的 SaaS 招聘门户暴露完全可 httpx 直连的 JSON API（**比 Mokahr 更简单**，
无 AES 加密，纯 plaintext JSON 响应）。多租户通过 subdomain + `website-path`
header 区分。

技术要点：
- List endpoint: `POST {sub}.jobs.feishu.cn/api/v1/search/job/posts?keyword=&limit=200&offset=0&portal_type=6&portal_entrance=1`
- 必需 headers: 完整 Chrome UA + `Referer`/`Origin` + `Content-Type: application/json`
  + **`website-path: {website_path}`** (per-portal slug) + `portal-channel: saas-career` + `portal-platform: pc`
- 必需 JSON body: 大模板含空 filter 数组（见 BODY_TEMPLATE）
- 响应明文：`data.job_post_list[*]` + `data.count`，含 description + requirement，
  **无需二次 detail 请求**
- 多租户：纯 subdomain 隔离，无 tenant ID。`website-path` 大部分 portal 是 `index`，
  百川/无问芯穹自定义 slug。

总活跃 dataset (2026-05-28 实测)：**549 jobs**
- MiniMax: 163 (subdomain vrfi1sk8a0)
- 智谱 Zhipu AI: 222 (subdomain zhipu-ai)
- 01.AI: 52 (subdomain 01ai)
- 生数科技 Shengshu: 45 (subdomain shengshu)
- 无问芯穹 Infinigence: 40 (subdomain infinigence, website-path=infinigence)
- 百川 Baichuan: 27 (subdomain cq6qe6bvfr6, website-path=baichuanzhaopin)

价值定位（与 mokahr_ats + bytedance_seed 互补，构成中国 AI 招聘三足鼎立）：
- mokahr_ats: 月之暗面 Kimi / 智源 BAAI / DeepSeek / StepFun / ModelBest / BIGAI
  / 芯片四小龙 / 自动驾驶 / CV 大厂 = 3432 jobs
- bytedance_seed: 字节 Seed 校招 + 实习
- feishu_jobs: MiniMax / 百川 / 智谱 / 01.AI / 生数 / 无问芯穹 = 549 jobs

弃用判断:
- 阶跃星辰 / 月暗 也在飞书托管简历投递入口，但**职位列表**走 Mokahr（已覆盖）
- 智谱 (zhipu-ai) 同时被 Mokahr/Feishu 两边引用——以飞书 API 为准（更全 222 vs 0）

风险 / 陷阱：
1. **GET 返回 HTML 入口页** —— 必须 POST + JSON body
2. **短 UA 被 block**（返 HTML loading shell）—— 必须完整 Chrome UA
3. **`website-path` per-portal**：错配 → 405。表里硬编码 6 条
4. **rate limit 实测无**（10× rapid POST 全过, ~700ms/req），保守仍加 sleep
5. **zhipu 222 > 200 默认 limit** —— 必须翻页 (offset += 200)
"""

from __future__ import annotations

import logging
import time
import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse
import threading

import httpx

from penumbra.core import cache, diag
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter

EMPTY_TTL = 300  # a transient all-portals-fail must not pin [] for the full 1800s (masks the outage)

logger = logging.getLogger(__name__)

TIMEOUT = 20
PAGE_LIMIT = 200
INTER_REQUEST_SLEEP = 0.5  # 保守 throttle

# Global in-flight cap on the shared Feishu hiring backend (*.jobs.feishu.cn = one Feishu SaaS). A
# single search fans out 6-wide (ThreadPoolExecutor below) over the 6 portals, but nothing bounded the
# load ACROSS concurrent agents. This semaphore (held only around the egress in _feishu_post) caps
# concurrent requests to 6 = a single search's own fan-out width, so a lone search is never throttled
# while an N-agent burst paces through instead of piling onto the one SaaS backend.
_FEISHU_MAX_INFLIGHT = 6
_feishu_sema = threading.BoundedSemaphore(_FEISHU_MAX_INFLIGHT)


def _feishu_post(url: str, **kwargs):
    """Single Feishu egress chokepoint: both httpx.post to *.jobs.feishu.cn pass through here so the
    global in-flight cap (_feishu_sema) bounds concurrent requests to the shared Feishu hiring backend."""
    with _feishu_sema:
        return httpx.post(url, **kwargs)

# Chrome desktop UA — 短 UA 会被 Feishu 反爬 block 成 HTML
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Empty-filter body template; offset is patched per page
BODY_TEMPLATE: dict = {
    "keyword": "",
    "limit": PAGE_LIMIT,
    "offset": 0,
    "job_category_id_list": [],
    "tag_id_list": [],
    "location_code_list": [],
    "subject_id_list": [],
    "recruitment_id_list": [],
    "portal_type": 6,
    "job_function_id_list": [],
    "storefront_id_list": [],
    "portal_entrance": 1,
}

# (label, subdomain, website_path, tier)
# Tier 1 = 大模型基础模型 / 国家队
SITES: list[tuple[str, str, str, int]] = [
    ("MiniMax", "vrfi1sk8a0", "index", 1),
    ("智谱 Zhipu AI", "zhipu-ai", "index", 1),
    ("01.AI", "01ai", "index", 1),
    ("生数科技 Shengshu", "shengshu", "index", 1),
    ("无问芯穹 Infinigence", "infinigence", "infinigence", 1),
    ("百川 Baichuan", "cq6qe6bvfr6", "baichuanzhaopin", 1),
]


def _make_headers(subdomain: str, website_path: str) -> dict:
    base = f"https://{subdomain}.jobs.feishu.cn"
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{base}/{website_path}",
        "Origin": base,
        "Content-Type": "application/json",
        # Feishu-specific per-portal headers (mandatory; missing → 405)
        "website-path": website_path,
        "portal-channel": "saas-career",
        "portal-platform": "pc",
    }


def _list_portal_jobs(subdomain: str, website_path: str) -> list[dict]:
    """Page through all jobs for one portal. Plaintext JSON; no decryption."""
    base = f"https://{subdomain}.jobs.feishu.cn"
    url = (
        f"{base}/api/v1/search/job/posts?"
        f"keyword=&limit={PAGE_LIMIT}&offset=0&portal_type=6&portal_entrance=1"
    )
    headers = _make_headers(subdomain, website_path)

    all_jobs: list[dict] = []
    offset = 0
    total_count: Optional[int] = None
    page_idx = 0
    while True:
        body = dict(BODY_TEMPLATE)
        body["offset"] = offset
        try:
            resp = _feishu_post(url, headers=headers, json=body, timeout=TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("feishu portal %s page %d failed: %s",
                           subdomain, page_idx, exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("feishu.post", url=url, status=st, exc=exc)
            break

        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        data = payload.get("data") or {}
        page_jobs = data.get("job_post_list") or []
        if total_count is None:
            total_count = data.get("count") or 0

        if not page_jobs:
            break
        all_jobs.extend(page_jobs)

        offset += len(page_jobs)
        page_idx += 1

        # Termination: page underfilled, or we hit total count, or safety cap
        if len(page_jobs) < PAGE_LIMIT or offset >= (total_count or 0):
            break
        if page_idx >= 20:  # safety: 20 pages × 200 = 4000 jobs ceiling
            logger.warning("feishu portal %s hit page cap (4000 jobs)", subdomain)
            break
        time.sleep(INTER_REQUEST_SLEEP)

    return all_jobs


def _job_to_document(job: dict, label: str, subdomain: str,
                     website_path: str, tier: int) -> Optional[Document]:
    job_id = str(job.get("id") or "")
    if not job_id:
        return None

    title = (job.get("title") or "").strip() or "(untitled)"
    cities = ", ".join(c.get("name", "") for c in (job.get("city_list") or []) if c.get("name"))

    description = job.get("description") or ""
    requirement = job.get("requirement") or ""
    content = description + ("\n\n要求：\n" + requirement if requirement else "")

    # Category / recruit type
    job_category = (job.get("job_category") or {}).get("name") or ""
    recruit_type = job.get("recruit_type") or {}
    recruit_label = recruit_type.get("name") or ""
    recruit_parent = (recruit_type.get("parent") or {}).get("name") or ""
    recruit_full = f"{recruit_parent}·{recruit_label}" if recruit_parent else recruit_label

    # Publish time (epoch ms)
    publish_ms = job.get("publish_time")
    date = None
    if publish_ms:
        try:
            from datetime import datetime, timezone
            date = datetime.fromtimestamp(int(publish_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            pass

    url = f"https://{subdomain}.jobs.feishu.cn/{website_path}/position/{job_id}/detail"

    tags = [label, "招聘", f"tier:{tier}"]
    if cities:
        tags.append(cities)
    if recruit_label:
        tags.append(recruit_label)
    if job_category:
        tags.append(job_category)

    return Document(
        source="feishu_jobs",
        source_id=f"{subdomain}-{job_id}",
        url=url,
        title=f"[{label}] {title}",
        content=content,
        author=label,
        date=date,
        tags=tags,
        metadata={
            "org": label,
            "subdomain": subdomain,
            "website_path": website_path,
            "job_id": job_id,
            "cities": cities,
            "recruit_type": recruit_full,
            "job_category": job_category,
            "tier": tier,
            "publish_time_ms": publish_ms,
            "raw": jsonsafe(job),
        },
    )


class FeishuJobsAdapter:
    name = "feishu_jobs"
    needs_credentials = False
    explicit_only = "walled 招聘源(飞书);命名钻取 (penumbra_search 单源 raw) 才调,不进广搜"
    description = (
        "Feishu 招聘 — 6 个 Tier 1 大模型 startup (MiniMax/智谱/01.AI/生数/"
        "无问芯穹/百川), 549+ 活跃岗位; 与 mokahr_ats + bytedance_seed 互补"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key("feishu_jobs", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        # Fetch the 6 portals CONCURRENTLY (each a different subdomain, plaintext JSON; feishu
        # measured no rate limit). Was serial = ~7s; collapses to ~the slowest portal. Per-portal
        # pagination (with its own inter-page sleep) + the tier-sort/keyword-filter below unchanged.
        def _one(site):
            label, subdomain, website_path, tier = site
            out: list[Document] = []
            try:
                jobs = _list_portal_jobs(subdomain, website_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Feishu portal %s (%s) failed: %s", label, subdomain, exc)
                return out
            for job in jobs:
                doc = _job_to_document(job, label, subdomain, website_path, tier)
                if doc:
                    out.append(doc)
            return out
        all_docs: list[Document] = []
        # Run each portal in a COPY of the caller's context so an armed diag capture (a drill's
        # contextvar) reaches the worker threads: a plain ex.map loses worker-thread failure notes,
        # so an all-portals-down drill used to report captures: [] (the RSS diag-loss class).
        portals = [(s, contextvars.copy_context()) for s in SITES]
        with ThreadPoolExecutor(max_workers=len(SITES)) as ex:
            for docs in ex.map(lambda p: p[1].run(_one, p[0]), portals):
                all_docs.extend(docs)

        # Default order: tier 1 first, then by org label (used when query empty
        # or as the pool the keyword filter ranks).
        all_docs.sort(key=lambda d: (d.metadata.get("tier", 99),
                                     d.metadata.get("org", "")))

        # Query filter — tokenized CJK-aware scoring (shared helper). Empty
        # query keeps the tier-sorted order; real query with no hits → [].
        all_docs = keyword_score_filter(all_docs, query)
        all_docs = all_docs[:limit]

        cache.set(key, [d.model_dump(mode="json") for d in all_docs],
                  ttl=1800 if all_docs else EMPTY_TTL)
        return all_docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if not host.endswith(".jobs.feishu.cn"):
            return None
        subdomain = host.split(".")[0]
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        # /{website_path}/position/{job_id}/detail
        if len(parts) < 4 or parts[1] != "position":
            return None
        website_path = parts[0]
        target_id = parts[2]
        # Find the SITE config matching this subdomain (we trust our config)
        site_cfg = next(
            ((label, sd, wp, tier) for (label, sd, wp, tier) in SITES if sd == subdomain),
            None,
        )
        if not site_cfg:
            return None
        label, _, _, tier = site_cfg
        try:
            for job in _list_portal_jobs(subdomain, website_path):
                if str(job.get("id") or "") == target_id:
                    return _job_to_document(job, label, subdomain, website_path, tier)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu fetch_url %s failed: %s", url, exc)
        return None

    def health_check(self) -> tuple[bool, str]:
        # Smoke test on the first portal only (avoid 6× network calls)
        label, subdomain, website_path, _ = SITES[0]
        url = (
            f"https://{subdomain}.jobs.feishu.cn/api/v1/search/job/posts?"
            f"keyword=&limit=1&offset=0&portal_type=6&portal_entrance=1"
        )
        try:
            body = dict(BODY_TEMPLATE)
            body["limit"] = 1
            resp = _feishu_post(
                url, headers=_make_headers(subdomain, website_path),
                json=body, timeout=10,
            )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            data = (resp.json() or {}).get("data") or {}
            cnt = data.get("count") or 0
            return True, f"OK ({len(SITES)} portals; first ({label}) reports {cnt} jobs)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from penumbra.core.fetcher import register_adapter

register_adapter(FeishuJobsAdapter())
