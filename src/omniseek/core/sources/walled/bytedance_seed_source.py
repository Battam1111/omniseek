"""字节跳动 Top Seed 校招 + 实习 招聘适配器.

基于 phase 7 深度调研（2026-05-28，69 tool calls）发现：
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

2026-11-19：「香港」查回 0 条。根因两层：查法上，地名被当成 keyword 送去，没走
城市筛选（filters meta 的 `city_list` 有 `CT_159 中国香港`）；站上，Top Seed 两个 subject 在香港
真没有岗位（按 `location_code_list=["CT_159"]` 筛也是 0，同日 Seed 共 153 岗，抽样在北京、上海、
深圳；校招其他 subject 在香港有 5 岗）。现在查询里的地名按 `city_list`（中文名、去掉「中国」前缀
的名、去掉括号的英文名）换成 `location_code_list`，其余词才作 keyword；查回 0 条时留一条
`bytedance_seed.no_match` 记录说清是哪个地方、哪些 subject 没有，以及校招其他 subject 在那里有几岗；
请求失败留 `bytedance_seed.search` 记录（失败与 0 条要分得开）。
依赖：`/config/job/filters/3` 的 `city_list[].code/name/en_name` 与 `job_subject_list`；
`/search/job/posts` 的 `location_code_list`、`subject_id_list` 与返回的 `data.count`。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

import anyio

from omniseek.core import _probe, cache, diag, fetcher
from omniseek.core.sources._base import SourceBase
from omniseek.core.normalize import Document, jsonsafe

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


def _places_in(query: str, meta: dict) -> tuple[list[str], list[str], str]:
    """The campus portal's cities named in ``query`` -> (location codes, their names, the query without
    them). A city is named by its Chinese name, that name without the 「中国」 prefix (中国香港 -> 香港),
    or its English name without the bracket ("Hong Kong (China)" -> hong kong, whole words). Pure."""
    rest = query or ""
    codes: list[str] = []
    names: list[str] = []
    for c in ((meta or {}).get("data") or {}).get("city_list") or []:
        if not isinstance(c, dict) or not c.get("code"):
            continue
        zh = str(c.get("name") or "")
        en = re.sub(r"\s*\(.*?\)", "", str(c.get("en_name") or "")).strip()
        cands = sorted({x for x in (zh, zh.removeprefix("中国"), en) if len(x) >= 2}, key=len, reverse=True)
        for x in cands:
            pat = (re.escape(x) if not x.isascii() else r"(?<![A-Za-z])" + re.escape(x) + r"(?![A-Za-z])")
            if re.search(pat, rest, flags=re.IGNORECASE):
                rest = re.sub(pat, " ", rest, flags=re.IGNORECASE)
                codes.append(str(c["code"]))
                names.append(zh or en)
                break
    return codes, names, " ".join(rest.split())


class BytedanceSeedAdapter(SourceBase):
    name = "bytedance_seed"
    needs_credentials = False
    explicit_only = "walled 招聘源(字节校招);命名钻取 (omniseek_search 单源 raw) 才调,不进广搜"
    description = (
        "字节跳动 Top Seed 校招 + 实习：大模型 / 前沿技术 PhD 人才招聘 "
        "(httpx 直连 jobs.bytedance.com JSON API, 无需 CDP/auth/sign)"
    )

    def _fetch_filters_meta(self, _status: Optional[list] = None) -> dict:
        """Get subject ID → name map. Cached aggressively (1h). ``_status``: a list the HTTP status
        of a failed fetch is appended to (the health check reads a 429 from it)."""
        key = cache.make_key("bytedance_seed", "filters_meta", "v1")
        cached = cache.get_once(key)
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
            if _status is not None:
                _status.append(getattr(getattr(exc, "response", None), "status_code", None))
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

    def _search_posts(self, keyword: str, subject_ids: list[str], limit: int, offset: int = 0,
                      location_codes: Optional[list] = None, _count: Optional[list] = None) -> Optional[list[dict]]:
        """POST to /search/job/posts. Both query params AND body need same filters. None when the request
        failed (noted), [] when it answered with no posting; ``_count`` gets the answer's ``data.count``."""
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
            "location_code_list": list(location_codes or []),
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
            diag.note("bytedance_seed.search", method="POST", url=f"{API_BASE}/search/job/posts",
                      status=getattr(getattr(exc, "response", None), "status_code", None), exc=exc)
            return None

        if data.get("code") != 0:
            logger.warning("bytedance search non-zero code: %s", data.get("code"))
            diag.note("bytedance_seed.search", method="POST", url=f"{API_BASE}/search/job/posts",
                      status=resp.status_code, body=f"API answered code {data.get('code')}: {data.get('message')}")
            return None

        if _count is not None:
            _count.append((data.get("data") or {}).get("count"))
        return ((data.get("data") or {}).get("job_post_list")) or []

    def _note_no_match(self, keyword: str, codes: list[str], places: list[str], subject_ids: list[str]) -> None:
        """Say why the Top Seed search came back empty: which place / keyword the Top Seed subjects have
        nothing for, and (for a place) how many campus postings other subjects have there."""
        what = (f"in {'/'.join(places)} (location {','.join(codes)})" if codes else "")
        what += (f"{' ' if what else ''}matching keyword {keyword!r}" if keyword else "")
        body = f"no Top Seed posting {what or 'at all'} (subjects {','.join(subject_ids)})"
        if codes:
            n: list = []
            others = self._search_posts("", [], 1, location_codes=codes, _count=n)
            if others is not None and n:
                body += (f"; jobs.bytedance.com campus lists {n[0]} posting(s) there under other subjects "
                         f"(not Top Seed)")
        diag.note("bytedance_seed.no_match", url=f"{API_BASE}/search/job/posts", body=body)

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key("bytedance_seed", "search", query, limit)
        cached = cache.get_once(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        subject_ids = self._resolve_top_seed_subject_ids()
        # a place in the query is the portal's city filter, not a keyword (「香港」 as a keyword matched
        # nothing in Top Seed and read as a silent 0)
        codes, places, keyword = _places_in(query or "", self._fetch_filters_meta())
        posts = self._search_posts(
            keyword=keyword,
            subject_ids=subject_ids,
            limit=min(limit * 3, 100),  # over-fetch then filter for relevance
            location_codes=codes,
        )
        if posts is None:
            return []   # failed and noted; not cached
        if not posts:
            self._note_no_match(keyword, codes, places, subject_ids)

        # If user gave a query, also filter client-side (subject filter is dataset-level)
        if keyword and posts:
            query_lower = keyword.lower()
            filtered = []
            for p in posts:
                blob = (p.get("title", "") + " " +
                        p.get("description", "") + " " +
                        p.get("requirement", "")).lower()
                if query_lower in blob:
                    filtered.append(p)
            posts = filtered or posts  # if filter zeros out, fall back to unfiltered

        docs: list[Document] = []
        for post in posts[:limit]:
            try:
                docs.append(self._post_to_document(post))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed bytedance post: %s", exc)

        if docs:   # an empty answer is not kept: a cached [] would come back without its no_match reason
            cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=1800)
        return docs

    async def afetch_url(self, url: str) -> Optional[Document]:
        if "jobs.bytedance.com" not in (urlparse(url).hostname or "").lower():
            return None
        # the posting API is read with the sync httpx client this source shares with its search: a worker thread
        return await anyio.to_thread.run_sync(self._read_url, url, limiter=fetcher._LEGACY_LIMITER)

    def _read_url(self, url: str) -> Optional[Document]:
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

    def health_check(self) -> tuple[Optional[bool], str]:
        # SAME key _fetch_filters_meta reads: while the meta is cached (1 h), it answers from the cache
        # and asks the JSON API nothing, so this check verifies nothing (None).
        if cache.get(cache.make_key("bytedance_seed", "filters_meta", "v1")) is not None:
            return None, "not probed (the filters meta is still cached; the JSON API is asked again when it expires)"
        try:
            status: list = []
            meta = self._fetch_filters_meta(_status=status)
            if meta and meta.get("code") == 0:
                return True, "OK (filters meta cached + JSON API reachable)"
            if 429 in status:   # it answered, but not whether the API serves: not verified
                return None, _probe.rate_limited("jobs.bytedance.com")
            return False, f"meta code: {meta.get('code') if meta else 'no data'}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _post_to_document(post: dict) -> Document:
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
        content = "\n\n".join(content_parts)

        # Metadata extraction
        category = post.get("job_category") or {}
        category_name = category.get("name") or ""
        category_parent = (category.get("parent") or {}).get("name") or ""
        full_category = f"{category_parent}/{category_name}" if category_parent else category_name

        city_info = post.get("city_info") or {}
        city = city_info.get("name") or ""
        # every city the posting is open in (city_info is only the first; the location filter matches any)
        cities = [str(c.get("name")) for c in (post.get("city_list") or []) if isinstance(c, dict) and c.get("name")]

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
        for c in dict.fromkeys([city, *cities]):
            if c:
                tags.append(f"location:{c}")

        return Document(
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
                "cities": cities or ([city] if city else []),
                "category": full_category,
                "recruit_type": full_recruit,
                "subject": subject_name,
                "publish_time": publish_time,
                "address": address,
                "raw": jsonsafe(post),
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(BytedanceSeedAdapter())
