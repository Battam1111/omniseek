"""nsfc_awards — 国家自然科学基金 (NSFC) 已批准项目检索, keyword-driven (STRUCTURE).

NSFC (National Natural Science Foundation of China) is China's main basic-science funding body,
the peer of the US NSF. Its OWN portal (kd.nsfc.cn) gates every search behind an image captcha and
returns an ENCRYPTED payload, so it is not scriptable without solving both. The least-bad accessible
STRUCTURED path is the LetPub third-party grant index (letpub.com.cn/index.php?page=grant), which
mirrors the approved-project records (PI / institution / amount / grant-number / project-type /
学部 / year / title) and answers a plain keyword POST with a parseable HTML table, no login for the
1997-2021 slice. This is OmniSeek's FIRST Chinese funding source (it had US NSF/NIH + Canadian
NSERC/SSHRC/CIHR, no CN): for a CS/AI researcher tracking who in China holds NSFC grants on a topic
(NLP / ML / vision), the per-award record is direct funding-landscape intel web search can't return.

Razor (STRUCTURE): the per-award record (负责人 + 单位 + 金额 + 项目编号 + 项目类型 + 学部 + 年份 +
题目) beats web search's prose. The user's query IS the 题目 (project-title) keyword, so a CS/AI
query returns the CS/AI slice; a general query returns that topic's grants. Telos: China research-
funding 信息差 (who is funded on what, at which institution).

SHAPE: a keyword-endpoint scrape (BaseScrapeAdapter with a bespoke POST _raw_fetch). The advanced
search POSTs the serialized form to /nsfcfund_search.php?mode=advanced and returns an HTML fragment
whose result table lists each grant as TWO consecutive <tr> rows (a 7-cell metadata row, then a
题目/title row). We fetch page-by-page (10 records/page) up to the requested limit.

LIMITS / RISK (flagged prominently):
  * Third-party ToS is gray (the operator approved the open stance); LetPub is a commercial site and the
    unauthenticated table is the free tier. STABILITY is the real risk: the HTML layout, the free-tier
    year cap, and the field order are LetPub's to change without notice. If the two-row table shape
    shifts, _parse_grants yields nothing and search degrades to [] (the contract) — a smoke-fixture
    regression + the source-audit sentinel will catch it.
  * FREE-TIER YEAR CAP: any search whose range reaches 2022+ triggers a login wall (verified live
    2026-07-10). So we hard-cap endTime at _MAX_YEAR (2021); post-2021 grants are NOT reachable here.
    Bump _MAX_YEAR when LetPub opens a newer year to the free tier (re-verify against the wall first).

explicit_only: a named-query drill (like nserc_awards / cninfo), kept out of the broad sweep.
Verified live 2026-07-10: keyword 自然语言处理 (1997-2021) -> 654 pages, rows parse clean.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from omniseek.core.normalize import Document
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")
_SEARCH_URL = "https://www.letpub.com.cn/nsfcfund_search.php"
_PORTAL_URL = "https://www.letpub.com.cn/index.php?page=grant"
# The metadata row's 7 columns, in order (verified live 2026-07-10).
_HEADERS = ("负责人", "单位", "金额", "项目编号", "项目类型", "所属学部", "批准年份")

# Module-level async client for the POST-form egress (S4b native-async twin). The shared http module
# exposes NO form-POST->text leaf (its public async leaves are aget_json / aget_text (GET) and
# apost_json (a JSON body -> parsed JSON) — none can POST x-www-form-urlencoded data and return HTML
# text); this source's SYNC path went RAW httpx.post for exactly that reason, so its async twin keeps
# its OWN client — lazy, double-checked-lock like _openalex._aget_client / nserc_awards._aget_client —
# with the SAME follow_redirects / timeout as the sync httpx.post it mirrors (the per-request headers,
# incl. the Chrome UA, are passed in _afetch_page exactly as _fetch_page does). Only asearch awaits it;
# the sync search is byte-identical. Same posture as nserc_awards' own client: a fixed public host
# (letpub.com.cn) with the query in the POST body (not the URL), so no SSRF surface is opened.
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()  # construction is sync (no await); double-check like http._aget_client


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = httpx.AsyncClient(
                    headers={"User-Agent": _UA},
                    timeout=30,
                    follow_redirects=True,
                )
    return _aclient


def _parse_grants(html: str) -> list[dict]:
    """Pure fn: one result-fragment HTML -> list of grant dicts (golden-fixture testable).

    Each grant is two consecutive <tr> rows: a 7-<td> metadata row (负责人/单位/金额/项目编号/
    项目类型/所属学部/批准年份) followed by a 题目 row (a '题目' label cell + the project title).
    The header row (7 <th>) and any non-conforming row are skipped."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table_yjfx")
    if table is None:
        return []
    rows = table.find_all("tr")
    grants: list[dict] = []
    i = 0
    while i < len(rows):
        tds = rows[i].find_all("td")
        # A metadata row: exactly 7 <td> (a <th> header row has 0 <td>, so it is skipped).
        if len(tds) != 7:
            i += 1
            continue
        cells = [td.get_text(strip=True) for td in tds]
        pi, inst, amount, no, ptype, division, year = cells
        title = ""
        if i + 1 < len(rows):
            nxt = rows[i + 1].find_all("td")
            # The 题目 row: a label cell '题目' + the title cell (colspan=6).
            if len(nxt) == 2 and nxt[0].get_text(strip=True) == "题目":
                title = nxt[1].get_text(strip=True)
        grants.append({
            "pi": pi, "institution": inst, "amount_wan": amount, "grant_no": no,
            "project_type": ptype, "division": division, "year": year, "title": title,
        })
        i += 2 if title else 1
    return grants


def _grant_to_doc(g: dict) -> Optional[Document]:
    """One grant dict -> Document (pure fn -> golden-fixture testable)."""
    title = (g.get("title") or "").strip()
    pi = (g.get("pi") or "").strip()
    if not (title or pi):
        return None
    inst = (g.get("institution") or "").strip()
    amount = (g.get("amount_wan") or "").strip()
    no = (g.get("grant_no") or "").strip()
    ptype = (g.get("project_type") or "").strip()
    division = (g.get("division") or "").strip()
    year_s = (g.get("year") or "").strip()
    date = None
    if year_s.isdigit():
        try:
            date = datetime(int(year_s), 1, 1, tzinfo=timezone.utc)
        except ValueError:
            date = None
    parts = [f"NSFC 国家自然科学基金. 负责人: {pi} ({inst})." if inst else f"NSFC 国家自然科学基金. 负责人: {pi}."]
    if ptype:
        parts.append(f"项目类型: {ptype}.")
    if amount:
        parts.append(f"金额: {amount} 万元.")
    if division:
        parts.append(f"学部: {division}.")
    if year_s:
        parts.append(f"批准年份: {year_s}.")
    if no:
        parts.append(f"项目批准号: {no}.")
    if title:
        parts.append(f"题目: {title}")
    tags = ["funding", "china", "nsfc"]
    if division:
        tags.append(division)
    return Document(
        source="nsfc_awards",
        source_id=f"nsfc:{no}" if no else f"nsfc:{(pi + title)[:48]}",
        url=_PORTAL_URL,
        title=(title or f"{ptype} — {pi}")[:140],
        content=" ".join(p for p in parts if p),
        author=pi or None,
        date=date,
        tags=tags,
        metadata={"pi": pi, "institution": inst, "amount_wan": amount, "grant_no": no,
                  "project_type": ptype, "division": division, "approval_year": year_s},
    )


class NSFCAwardsAdapter(BaseScrapeAdapter):
    name = "nsfc_awards"
    description = (
        "国家自然科学基金 NSFC 已批准项目检索 — 中国基础科研主资助局 (美国 NSF 的对位), 眼首个中国经费源 "
        "(此前只有美国 NSF/NIH + 加拿大 NSERC/SSHRC/CIHR). 官方门户 kd.nsfc.cn 有验证码 + 加密响应不可脚本化, "
        "故走 LetPub 第三方基金索引 (letpub.com.cn) 的免登录切片. 关键词 (题目) 搜索, 逐笔奖助: 负责人 + 单位 "
        "+ 金额 (万元) + 项目批准号 + 项目类型 + 学部 + 批准年份 + 题目. 博士/研究者查某课题 (NLP/ML/视觉) 谁在"
        "中国拿了基金、在哪家机构、多少钱 — 一手经费格局 (网搜给不出结构记录). 命名钻取 (omniseek_search 单源 raw). "
        "免登录切片仅到 2021 年 (第三方站, 布局/年限可能变动)."
    )
    explicit_only = "NSFC 中国经费 (LetPub 第三方索引, 免登录切片 1997-2021); 命名钻取 (omniseek_search 单源 raw)"
    kind = "lookup"
    domains = ["funding"]
    regions = ["cn"]
    modes = ["STRUCTURE"]
    url_host = "letpub.com.cn"
    cache_ttl = 21600  # 6h: historical (<=2021) records are static; per-query cache

    # LetPub returns its own full-text relevance order; keep it (do not re-rank locally).
    rank = False

    _MAX_YEAR = 2021   # free-tier ceiling: a range reaching 2022+ trips a login wall (verified 2026-07-10)
    _MIN_YEAR = 1997   # earliest year LetPub indexes
    _PER_PAGE = 10     # LetPub fixes 10 records per result page
    _MAX_PAGES = 5     # cap the page walk (50 records) — plenty for a named drill; be a polite client

    def _fetch_page(self, query: str, page: int) -> Optional[str]:
        url = f"{_SEARCH_URL}?mode=advanced&datakind=list&currentpage={page}"
        data = {
            "page": "", "name": query, "person": "", "no": "", "company": "",
            "addcomment_s1": "", "addcomment_s2": "", "addcomment_s3": "", "addcomment_s4": "",
            "money1": "", "money2": "", "startTime": str(self._MIN_YEAR),
            "endTime": str(self._MAX_YEAR), "province_main": "", "subcategory": "",
            "searchsubmit": "true",
        }
        headers = {
            "User-Agent": _UA, "X-Requested-With": "XMLHttpRequest",
            "Referer": _PORTAL_URL,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            r = httpx.post(url, data=data, headers=headers, timeout=30, follow_redirects=True)
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001 — failure -> None -> [] (the adapter contract)
            logger.warning("nsfc_awards: page %d fetch failed: %s", page, exc)
            return None

    async def _afetch_page(self, query: str, page: int) -> Optional[str]:
        # Async twin of _fetch_page: byte-faithful mirror (same url / form data / headers / timeout,
        # and follow_redirects carried on the client). Only the RAW httpx.post egress swaps to the
        # module-level AsyncClient (see _aget_client above for why this source keeps its own client,
        # not a shared http leaf). The data + headers dicts are duplicated inline (not extracted to a
        # shared helper) so the sync _fetch_page stays untouched, matching nsf_awards' _araw_fetch.
        url = f"{_SEARCH_URL}?mode=advanced&datakind=list&currentpage={page}"
        data = {
            "page": "", "name": query, "person": "", "no": "", "company": "",
            "addcomment_s1": "", "addcomment_s2": "", "addcomment_s3": "", "addcomment_s4": "",
            "money1": "", "money2": "", "startTime": str(self._MIN_YEAR),
            "endTime": str(self._MAX_YEAR), "province_main": "", "subcategory": "",
            "searchsubmit": "true",
        }
        headers = {
            "User-Agent": _UA, "X-Requested-With": "XMLHttpRequest",
            "Referer": _PORTAL_URL,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            r = await _aget_client().post(url, data=data, headers=headers, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001 — failure -> None -> [] (the adapter contract)
            logger.warning("nsfc_awards: page %d fetch failed: %s", page, exc)
            return None

    def _raw_fetch(self, query: str, limit: int) -> Optional[list[str]]:
        q = (query or "").strip()
        if not q:
            return None  # this is a keyword drill: an empty query has nothing to match
        pages = max(1, min(self._MAX_PAGES, math.ceil(limit / self._PER_PAGE)))
        htmls: list[str] = []
        for p in range(1, pages + 1):
            h = self._fetch_page(q, p)
            if h is None:
                break
            htmls.append(h)
            # stop early on the last page (fewer than a full page of grants parsed)
            if len(_parse_grants(h)) < self._PER_PAGE:
                break
        return htmls or None

    async def _araw_fetch(self, query: str, limit: int) -> Optional[list[str]]:
        # Async twin of _raw_fetch: same page-count math, same sequential page walk with the SAME
        # early-break on a short page (fewer than a full page of grants parsed); pages are awaited in
        # order (not gathered) because the break depends on each page's parse. _parse_grants is pure
        # CPU on the loop (a small HTML fragment), byte-identical to the sync path.
        q = (query or "").strip()
        if not q:
            return None  # this is a keyword drill: an empty query has nothing to match
        pages = max(1, min(self._MAX_PAGES, math.ceil(limit / self._PER_PAGE)))
        htmls: list[str] = []
        for p in range(1, pages + 1):
            h = await self._afetch_page(q, p)
            if h is None:
                break
            htmls.append(h)
            # stop early on the last page (fewer than a full page of grants parsed)
            if len(_parse_grants(h)) < self._PER_PAGE:
                break
        return htmls or None

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BaseScrapeAdapter.search -> AsyncSearchCapable (the S4a fan-out awaits
        this directly; the page walk costs a COROUTINE, not a held pool thread). Shares the base async
        cache round-trip (_asearch_via: SAME cache key + cache_ttl, off-loop disk IO, rank off here
        since rank=False); egress via the async _araw_fetch page walk; mapping via the SAME pure-CPU
        _to_documents -> byte-identical to search."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _to_documents(self, raw: list[str], query: str, limit: int) -> list[Document]:
        docs: list[Document] = []
        seen: set[str] = set()
        for html in raw:
            for g in _parse_grants(html):
                d = _grant_to_doc(g)
                if d is None or d.source_id in seen:
                    continue
                seen.add(d.source_id)
                docs.append(d)
                if len(docs) >= limit:
                    return docs
        return docs

    def health_check(self) -> tuple[bool, str]:
        raw = self._raw_fetch("机器学习", 1)
        if not raw:
            return False, "fetch failed / blocked"
        n = len(_parse_grants(raw[0]))
        return (n > 0), f"OK ({n} grants on page 1)" if n else "no grants parsed (layout changed?)"
