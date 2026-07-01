"""字节跳动 Top Seed 校招 + 实习 招聘适配器.

基于 P7 opus sub-agent 深度调研（2026-05-28，69 tool calls）发现：
字节 jobs.bytedance.com **暴露干净 JSON API**（之前误判为 SPA-only）：

- Endpoint: `POST /api/v1/search/job/posts`
- 无需 auth / cookies / OAuth / sign / 验证码 / CDP
- 关键 header: `website-path: campus` 切到校招 dataset（不带这个 header 默认社招）
- 短 UA (`Mozilla/5.0`) 触发 WAF 返回 405 → 必须用完整 Chrome UA 字符串
- Rate limit: 20+ 顺序请求无 throttle, ~900ms 平均延迟

Top Seed dataset 全景（subject IDs 2026-05-28 实测）：
- `7621018151002507573` — 2027届 Seed 大模型人才校招 (91 posts)
- `7621018569480046853` — Seed 大模型人才实习招聘 (80 posts)
- 其他 2027 前沿 / ByteIntern / 全校招 subjects 可通过 filters meta 自动发现

实施策略：
- 启动时拉 `/api/v1/config/job/filters/3` 得到所有 subject ID → name 映射
- regex 匹配 `r"Seed.*招"` 自动选当年 Top Seed subjects（forward-compat 2028+ cycle）
- search() 默认查 Top Seed subjects，可通过 keyword 进一步过滤
- fetch_url 解析 `/campus/position/{id}/detail` 路径 → GET `/api/v1/job/posts/{id}`

Sub-agent 还发现：同 endpoint 不同 `website-path` 切换字节系全部招聘 portal
(`experienced` / `campus` / `school`)。当前实现聚焦 Top Seed，后续可推广。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import cache
from penumbra.core.normalize import PolarisDocument, jsonsafe

logger = logging.getLogger(__name__)

API_BASE = "https://jobs.bytedance.com/api/v1"
TIMEOUT = 20

# Full Chrome UA — short "Mozilla/5.0" triggers WAF 405.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://jobs.bytedance.com",
    "Referer": "https://jobs.bytedance.com/campus/position",
    "website-path": "campus",  # ← 校招 dataset switch (key finding)
}

# Default subject regex — matches Top Seed campus + intern + future cycles
DEFAULT_TOP_SEED_REGEX = r"Seed.*(?:校招|实习|招)"


class BytedanceSeedAdapter:
    name = "bytedance_seed"
    needs_credentials = False
    explicit_only = "walled 招聘源(字节校招);命名 eye_fetch 才调,不进广搜"
    description = (
        "字节跳动 Top Seed 校招 + 实习 — 大模型 / 前沿技术 PhD 人才招聘 "
        "(httpx 直连 jobs.bytedance.com JSON API, 无需 CDP/auth/sign)"
    )

    def _fetch_filters_meta(self) -> dict:
        """Get subject ID → name map. Cached aggressively (1h)."""
        key = cache.make_key("bytedance_seed", "filters_meta", "v1")
        cached = cache.get(key)
        if cached is not None:
            return cached

        try:
            resp = httpx.get(
                f"{API_BASE}/config/job/filters/3",
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bytedance filters meta failed: %s", exc)
            return {}

        cache.set(key, data, ttl=3600)
        return data

    def _resolve_top_seed_subject_ids(self) -> list[str]:
        """Find current 2027/2028 Top Seed subject IDs via name regex."""
        meta = self._fetch_filters_meta()
        if not meta:
            # Hardcoded fallback if filter meta fetch fails (2027 cycle as of 2026-05)
            return ["7621018151002507573", "7621018569480046853"]

        # Walk meta to find subjects matching our regex
        subjects = []
        regex = re.compile(DEFAULT_TOP_SEED_REGEX, re.IGNORECASE)
        # filters response has nested structure; the subjects live under data.job_subject_list or similar
        def walk(node):
            if isinstance(node, dict):
                # Check if this looks like a subject entry
                if "id" in node and "name" in node:
                    name = node["name"]
                    if isinstance(name, dict):
                        name = name.get("i18n") or name.get("zh-CN") or ""
                    if isinstance(name, str) and regex.search(name):
                        subjects.append(str(node["id"]))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(meta.get("data", meta))
        if not subjects:
            logger.info("bytedance Top Seed subject regex matched 0 — using hardcoded fallback")
            return ["7621018151002507573", "7621018569480046853"]
        return subjects

    def _search_posts(self, keyword: str, subject_ids: list[str], limit: int, offset: int = 0) -> list[dict]:
        """POST to /search/job/posts. Both query params AND body need same filters."""
        common = {
            "keyword": keyword,
            "limit": limit,
            "offset": offset,
            "portal_type": 3,
            "portal_entrance": 1,
        }
        body = {
            **common,
            "job_category_id_list": [],
            "tag_id_list": [],
            "location_code_list": [],
            "subject_id_list": subject_ids,
            "recruitment_id_list": [],
            "job_function_id_list": [],
            "storefront_id_list": [],
        }
        try:
            resp = httpx.post(
                f"{API_BASE}/search/job/posts",
                params=common,  # API requires SAME filters in both URL params and body
                headers=HEADERS,
                content=json.dumps(body),
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bytedance search failed: %s", exc)
            return []

        if data.get("code") != 0:
            logger.warning("bytedance search non-zero code: %s", data.get("code"))
            return []

        return ((data.get("data") or {}).get("job_post_list")) or []

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        key = cache.make_key("bytedance_seed", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [PolarisDocument.model_validate(d) for d in cached]

        subject_ids = self._resolve_top_seed_subject_ids()
        posts = self._search_posts(
            keyword=query if query else "",
            subject_ids=subject_ids,
            limit=min(limit * 3, 100),  # over-fetch then filter for relevance
        )

        # If user gave a query, also filter client-side (subject filter is dataset-level)
        if query and posts:
            query_lower = query.lower()
            filtered = []
            for p in posts:
                blob = (p.get("title", "") + " " +
                        p.get("description", "") + " " +
                        p.get("requirement", "")).lower()
                if query_lower in blob:
                    filtered.append(p)
            posts = filtered or posts  # if filter zeros out, fall back to unfiltered

        docs: list[PolarisDocument] = []
        for post in posts[:limit]:
            try:
                docs.append(self._post_to_document(post))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed bytedance post: %s", exc)

        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=1800)
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        host = (urlparse(url).hostname or "").lower()
        if "jobs.bytedance.com" not in host:
            return None
        # Pattern: /campus/position/{id}/detail
        path = urlparse(url).path.strip("/").split("/")
        post_id = None
        for i, part in enumerate(path):
            if part == "position" and i + 1 < len(path):
                post_id = path[i + 1]
                break
        if not post_id or not post_id.isdigit():
            return None
        try:
            resp = httpx.get(
                f"{API_BASE}/job/posts/{post_id}",
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bytedance fetch_url failed: %s", exc)
            return None
        if data.get("code") != 0:
            return None
        post_detail = ((data.get("data") or {}).get("job_post_detail")) or {}
        if not post_detail:
            return None
        return self._post_to_document(post_detail)

    def health_check(self) -> tuple[bool, str]:
        try:
            meta = self._fetch_filters_meta()
            if meta and meta.get("code") == 0:
                return True, "OK (filters meta cached + JSON API reachable)"
            return False, f"meta code: {meta.get('code') if meta else 'no data'}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _post_to_document(post: dict) -> PolarisDocument:
        post_id = str(post.get("id") or post.get("code") or "?")
        url = f"https://jobs.bytedance.com/campus/position/{post_id}/detail"
        title = post.get("title") or "(no title)"

        # Compose content from description + requirement
        description = post.get("description") or ""
        requirement = post.get("requirement") or ""
        content_parts = []
        if description:
            content_parts.append(description.strip())
        if requirement:
            content_parts.append("--- 任职要求 ---")
            content_parts.append(requirement.strip())
        content = "\n\n".join(content_parts) or "(no content)"

        # Metadata extraction
        category = post.get("job_category") or {}
        category_name = category.get("name") or ""
        category_parent = (category.get("parent") or {}).get("name") or ""
        full_category = f"{category_parent}/{category_name}" if category_parent else category_name

        city_info = post.get("city_info") or {}
        city = city_info.get("name") or ""

        recruit_type = post.get("recruit_type") or {}
        recruit_name = recruit_type.get("name") or ""
        recruit_parent = (recruit_type.get("parent") or {}).get("name") or ""
        full_recruit = f"{recruit_parent}/{recruit_name}" if recruit_parent else recruit_name

        subject = post.get("job_subject") or {}
        subject_name_raw = subject.get("name")
        if isinstance(subject_name_raw, dict):
            subject_name = subject_name_raw.get("i18n") or subject_name_raw.get("zh-CN") or ""
        else:
            subject_name = subject_name_raw or ""

        # Publish time (unix milliseconds)
        publish_time = post.get("publish_time")
        date = None
        if publish_time:
            try:
                date = datetime.fromtimestamp(int(publish_time) / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                pass

        address = ((post.get("job_post_info") or {}).get("address")) or None

        tags = ["bytedance", "校招"]
        if "实习" in full_recruit:
            tags.append("intern")
        if "Seed" in subject_name:
            tags.append("top-seed")
        if city:
            tags.append(f"location:{city}")

        return PolarisDocument(
            source="bytedance_seed",
            source_id=post_id,
            url=url,
            title=title,
            content=content,
            author="字节跳动 Seed",
            date=date,
            tags=tags,
            metadata={
                "post_id": post_id,
                "code": post.get("code"),
                "city": city,
                "category": full_category,
                "recruit_type": full_recruit,
                "subject": subject_name,
                "publish_time": publish_time,
                "address": address,
                "raw": jsonsafe(post),
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(BytedanceSeedAdapter())
