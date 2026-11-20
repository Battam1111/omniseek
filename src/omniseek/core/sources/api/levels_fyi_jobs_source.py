"""levels.fyi job board through levels.fyi's own MCP server (2026-11-19).

Why a second levels.fyi source: the www.levels.fyi pages sit behind AWS WAF (see levels_fyi_source's
WAF note: 202 / 405 challenges on some exits, and on every /zh-cn/... page from the sandbox exit), and
the job board (/jobs) is a client-rendered page with nothing in its HTML. levels.fyi publishes a free
MCP server for agents (https://mcp.levels.fyi/, listed in www.levels.fyi/llms.txt, server card
/.well-known/mcp/server-card.json) that needs no account: 20 tool calls a minute shared by everyone on
the same network address (a free sign-in raises it to 60). Its ``search-jobs`` tool searches the job
board by keywords and a location slug and returns the pay range a posting states; ``get-job`` returns one
posting's full description. Salaries stay with levels_fyi (local currency from the pages); this
source is the job listings, by city.

Reliance map (checked 2026-11-19 from the sandbox exit, anonymous):
  - endpoint https://mcp.levels.fyi/ (streamable HTTP; no Mcp-Session-Id issued), through OmniSeek's
    MCP client (sources/_mcp.py: the shared pooled client, its SSRF guard and the declared gate of
    the levels_fyi_mcp upstream);
  - tool ``search-jobs`` {query, locationSlug, limit} -> {jobs: [{jobId, title, company, companySlug,
    locations[], workArrangement, postedAt, baseSalaryRange|null, totalCompensationRange|null
    ({min, max, currency}), url, applyUrl}], totalMatchingJobs, nextOffset}; an unknown locationSlug
    is an isError result "Error: <slug> not found";
  - tool ``get-job`` {jobId} -> the same fields plus employmentTypes and description (plain text);
  - location slugs are levels.fyi's own (hong-kong-sar, singapore, china, greater-toronto-area,
    san-francisco-bay-area ...): the place lexicon of levels_fyi_source (_location) names them.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import anyio

from omniseek.core import _probe, cache, diag, fetcher
from omniseek.core.normalize import Document, jsonsafe, to_utc
from omniseek.core.sources._base import SourceBase
from omniseek.core.sources.scrape.levels_fyi_source import _LOC_RULES, _location

logger = logging.getLogger(__name__)

ENDPOINT = "https://mcp.levels.fyi/"
JOB_URL = "https://www.levels.fyi/jobs?jobId={id}"
TIMEOUT = 30
CACHE_TTL = 3600


def _split_place(query: str) -> tuple[Optional[str], str]:
    """(levels.fyi location slug of a place the query names, the query without that place). Pure."""
    slug = _location(query)
    if not slug:
        return None, (query or "").strip()
    rest = f" {query} "
    for phrases, s in _LOC_RULES:
        if s == slug:
            for p in sorted(phrases, key=len, reverse=True):
                rest = re.sub(re.escape(p.strip()), " ", rest, flags=re.IGNORECASE)
    return slug, " ".join(rest.split())


def _pay(r) -> str:
    if not isinstance(r, dict) or r.get("min") is None:
        return ""
    lo, hi, cur = r.get("min"), r.get("max"), r.get("currency") or ""
    try:
        return f"{cur} {int(lo):,}" + (f" - {int(hi):,}" if hi not in (None, lo) else "")
    except (TypeError, ValueError):
        return ""


def _job_doc(j: dict, *, full: bool = False) -> Optional[Document]:
    jid, title = str(j.get("jobId") or ""), (j.get("title") or "").strip()
    if not jid or not title:
        return None
    company = j.get("company") or ""
    locs = [str(x) for x in (j.get("locations") or []) if x]
    base, tc = _pay(j.get("baseSalaryRange")), _pay(j.get("totalCompensationRange"))
    head = [f"地点: {'; '.join(locs)}" if locs else "",
            f"base（岗位标明，年）: {base}" if base else "",
            f"总包（岗位标明，年）: {tc}" if tc else "",
            f"工作方式: {j.get('workArrangement')}" if j.get("workArrangement") else "",
            f"投递: {j.get('applyUrl')}" if j.get("applyUrl") else ""]
    content = "\n".join(x for x in head if x)
    if full and j.get("description"):
        content = f"{content}\n\n{j['description']}".strip()
    return Document(
        source="levels_fyi_jobs",
        source_id=jid,
        url=JOB_URL.format(id=jid),
        title=f"[{company}] {title}" if company else title,
        content=content,
        author=company,
        date=to_utc(j.get("postedAt")),
        tags=["jobs", *(f"location:{x}" for x in locs[:5])],
        metadata={"company": company, "company_slug": j.get("companySlug"), "locations": locs,
                  "base_range": j.get("baseSalaryRange"), "total_comp_range": j.get("totalCompensationRange"),
                  "work_arrangement": j.get("workArrangement"), "apply_url": j.get("applyUrl"),
                  "raw": jsonsafe({k: v for k, v in j.items() if k != "description"})},
    )


async def _acall(tool: str, arguments: dict, _why: Optional[list] = None) -> Optional[dict]:
    """One tools/call on the levels.fyi MCP server; None on failure, with a note saying what failed
    (and the message appended to ``_why`` when given)."""
    from omniseek.core.sources._mcp import MCPTransportError, get_client
    try:
        client = get_client(ENDPOINT, timeout_s=TIMEOUT)
        res = await anyio.to_thread.run_sync(client.call_tool, tool, arguments,
                                             limiter=fetcher._LEGACY_LIMITER)   # the MCP client is sync
    except MCPTransportError as exc:
        diag.note(f"levels_fyi_jobs.{tool}", method="POST", url=ENDPOINT, body=f"MCP {tool}: {exc}")
        if _why is not None:
            _why.append(str(exc))
        return None
    return res if isinstance(res, dict) else None


class LevelsFyiJobsAdapter(SourceBase):
    name = "levels_fyi_jobs"
    needs_credentials = False
    explicit_only = ("levels.fyi 官方 MCP 匿名每分钟 20 次、同一出口共用;命名钻取 (omniseek_search 单源 raw) 才调,"
                     "不进广搜")
    description = (
        "levels.fyi 求职板 (官方 MCP, 免登录): 按关键词 + 城市查在招岗位, 带岗位标明的 base / 总包区间 (USD), "
        "全球 7 万+ 岗位. 查询里写城市即按城市筛 (\"machine learning hong kong\" / \"research scientist "
        "toronto\"); omniseek_read 一个 levels.fyi/jobs?jobId=... 取全文. 薪资分位用 levels_fyi."
    )

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        slug, words = _split_place(query or "")
        key = cache.make_key("levels_fyi_jobs", "search", words, slug or "", limit)

        async def _aget() -> Optional[list[Document]]:
            args: dict = {"query": words, "limit": min(max(limit, 1), 50)}
            if slug:
                args["locationSlug"] = slug
            res = await _acall("search-jobs", args)
            if res is None:
                return None   # failed and noted: not cached
            jobs = res.get("jobs") or []
            docs = [d for d in (_job_doc(j) for j in jobs if isinstance(j, dict)) if d][:limit]
            if not docs:
                diag.note("levels_fyi_jobs.no_match", url=ENDPOINT,
                          body=(f"levels.fyi job board has no open job for {words!r}"
                                f"{f' in {slug}' if slug else ''} (totalMatchingJobs="
                                f"{res.get('totalMatchingJobs')})"))
                return None   # an empty answer is not kept: it would come back without its reason
            return docs

        return await self._acached(key, CACHE_TTL, _aget)

    async def afetch_url(self, url: str) -> Optional[Document]:
        """www.levels.fyi/jobs?jobId=<id> (the url every listing doc carries): the posting's full text."""
        try:
            p = urlsplit(url)
        except ValueError:
            return None
        if (p.hostname or "").lower() not in ("www.levels.fyi", "levels.fyi") or not p.path.rstrip("/").endswith("/jobs"):
            return None
        jid = (parse_qs(p.query).get("jobId") or [""])[0]
        if not re.fullmatch(r"[a-z0-9]+", jid or ""):
            return None
        res = await _acall("get-job", {"jobId": jid})
        if res is None:
            return None
        return _job_doc(res, full=True)

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        why: list = []
        res = await _acall("search-jobs", {"query": "software engineer", "limit": 1}, why)
        if res is None:
            if why and _probe.says_rate_limited(why[0]):   # it answered, but not whether search serves
                return None, _probe.rate_limited("mcp.levels.fyi")
            return False, f"MCP search-jobs failed: {(why or ['?'])[0][:200]}"
        n = res.get("totalMatchingJobs")
        return (bool(res.get("jobs")), f"OK (search-jobs answered, {n} matching)" if res.get("jobs")
                else "search-jobs answered with no job")


from omniseek.core.fetcher import register_adapter

register_adapter(LevelsFyiJobsAdapter())
