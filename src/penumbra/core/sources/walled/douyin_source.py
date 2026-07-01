"""douyin (抖音) — login-walled web search over China's #1 short-video surface.

抖音 is a PRIMARY first-hand CN information surface, NOT just entertainment: 留学/移民/海外生活
一手 (study-abroad, immigration, overseas-life firsthand), 政务号/官方号 announcements, and
real-time creator commentary live here in video-native form that zhihu (text Q&A) / xiaohongshu
(lifestyle notes) / 一亩三分地 (NA tech-immigration forum) do NOT duplicate. UNWALL mode: the web
search endpoint is login-gated (guest → status_code 2483 "请先登录"), so a disposable 小号 logs in
ONCE via VNC into an ISOLATED Chrome profile (port 9225, ~/.polaris/chrome-douyin) and the eye
reuses that session — the SAME pattern as xiaohongshu's 9223.

Mechanism (verified 2026-06-22, decoded from MediaCrawler media_platform/douyin/client.py):
  * Search = GET https://www.douyin.com/aweme/v1/web/general/search/single/ with a fixed
    common-params block + the query params. Crucially the search endpoint SKIPS a-bogus signing
    (client.py:119 `if "/v1/web/general/search" not in uri: a_bogus = ...`) — only DEEPER
    endpoints (comment/detail) need it. So search needs NO JS signing: just the login cookies +
    msToken (read browser-side from localStorage `xmst`) + a same-origin fetch.
  * We drive the logged-in Chrome to the douyin search page (correct origin + Referer + cookies),
    read msToken, and page.evaluate a same-origin fetch (credentials:'include'). The browser
    attaches the login cookies automatically — the xhs same-origin-fetch trick.
  * data[] items carry aweme_info (or aweme_mix_info.mix_items[0]); each = one video doc. The
    video's spoken content is NOT in the API — the caption (desc) + creator + engagement are the
    discovery signal; the agent can then eye_transcribe a promising video's url for the speech.

Isolation rationale: identical to xiaohongshu (launch_cdp_xhs.sh header) — a disposable
小号 must not inherit the 大号's device lineage nor re-expose zhihu/一亩三分地 to 抖音's detection
surface, so it gets its OWN profile + port, never the shared 9222. Single-flight is inherited
(cdp_call's per-Chrome Semaphore(1) keyed on cdp_url ⇒ 9225 never contends with 9222/9223/9224).
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, unquote

from penumbra.core import diag
from penumbra.core.normalize import PolarisDocument, mk_signal
from penumbra.core.sources.walled._base import BaseCDPAdapter

logger = logging.getLogger(__name__)

DOUYIN_CDP = "http://127.0.0.1:9225"
_HYDRATE_MS = 4500  # let the SPA settle + xmst land in localStorage before the same-origin fetch


def _web_id() -> str:
    """Random 19-char webid (decoded from MediaCrawler douyin/help.get_web_id — pure client-side
    randomness, no browser dependency, so it lives in Python not the page)."""
    def e(t):
        if t is not None:
            return str(t ^ (int(16 * random.random()) >> (t // 4)))
        return "".join([str(int(1e7)), "-", str(int(1e3)), "-", str(int(4e3)),
                        "-", str(int(8e3)), "-", str(int(1e11))])
    wid = "".join(e(int(x)) if x in "018" else x for x in e(None))
    return wid.replace("-", "")[:19]


# Stable PC-web common-params block (decoded from client.py __process_req_params). msToken is read
# browser-side (localStorage xmst, injected in _SEARCH_JS); webid is per-call random; the rest are
# a fixed desktop-Chrome profile. a_bogus is deliberately ABSENT — the search endpoint skips it.
_COMMON = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "version_code": "190600", "version_name": "19.6.0", "update_version_code": "170400",
    "pc_client_type": "1", "cookie_enabled": "true", "browser_language": "zh-CN",
    "browser_platform": "MacIntel", "browser_name": "Chrome", "browser_version": "125.0.0.0",
    "browser_online": "true", "engine_name": "Blink", "os_name": "Mac OS", "os_version": "10.15.7",
    "cpu_core_num": "8", "device_memory": "8", "engine_version": "109.0", "platform": "PC",
    "screen_width": "2560", "screen_height": "1440", "effective_type": "4g", "round_trip_time": "50",
}

# Same-origin fetch, run INSIDE the logged-in page (so the browser attaches login cookies and the
# request is first-party). Reads msToken from localStorage, merges the caller-built params, GETs the
# search endpoint with credentials, returns a structured result (never throws — errors → fields).
_SEARCH_JS = """
async ([params, keyword]) => {
  params = Object.assign({}, params);
  params.msToken = window.localStorage.getItem('xmst') || '';
  const qs = new URLSearchParams(params).toString();
  const ref = 'https://www.douyin.com/search/' + encodeURIComponent(keyword) + '?type=general';
  try {
    const r = await fetch('/aweme/v1/web/general/search/single/?' + qs, {
      method: 'GET', headers: {'Referer': ref}, credentials: 'include' });
    const text = await r.text();
    let j = null; try { j = JSON.parse(text); } catch (e) {}
    if (j === null) return {http: r.status, parse_error: true, head: text.slice(0, 300)};
    return {http: r.status, status_code: j.status_code, status_msg: j.status_msg || '',
            data: j.data || [], extra: j.extra || {}};
  } catch (e) { return {fetch_error: String(e)}; }
}
"""

_LOGIN_PROBE_JS = (
    "() => ({login: window.localStorage.getItem('HasUserLogin'),"
    " status: (document.cookie.match(/LOGIN_STATUS=([^;]+)/) || [])[1] || ''})"
)


class DouyinAdapter(BaseCDPAdapter):
    name = "douyin"
    description = (
        "抖音 — 中国第一短视频平台的登录墙网页搜索 (UNWALL). 一手 留学/移民/海外生活、政务/官方号公告、"
        "创作者实时评论, 视频原生, 与 知乎(文字问答)/小红书(生活笔记)/一亩三分地(北美技术移民) 不重叠. "
        "隔离 小号 会话 (9225 专属 Chrome, 同小红书 9223 模式). 返回视频的标题/文案、作者、互动数 + 视频 URL "
        "(再对该 url 调 eye_transcribe 可转写语音正文). 命名调用 (eye_fetch), 不进广搜."
    )
    needs_credentials = True
    explicit_only = "抖音 walled (isolated 9225 小号 session, account-rate-sensitive); named via eye_fetch"
    cdp_url = DOUYIN_CDP
    cdp_timeout = 60
    cache_ttl = 1800  # 30 min: balances freshness vs sparing the single 小号 session a retry-storm
    kind = "stream"
    domains = ["social", "immigration", "career"]
    regions = ["cn"]
    modes = ["UNWALL"]
    url_host = "douyin.com"

    # ── hooks ──────────────────────────────────────────────────────────────
    def _search_url(self, query: str) -> str:
        # Navigate to the REAL search page: correct origin + cookies + a natural Referer context.
        # _flow recovers the keyword from page.url (no per-call instance state → concurrency-safe,
        # though the 9225 gate already serializes).
        return f"https://www.douyin.com/search/{quote(query)}?type=general"

    def _flow(self, page) -> Any:
        try:
            page.wait_for_timeout(_HYDRATE_MS)
        except Exception:  # noqa: BLE001
            pass
        keyword = self._keyword_from_url(page.url)
        params = dict(_COMMON)
        params["webid"] = _web_id()
        params.update({
            "search_channel": "aweme_general", "enable_history": "1", "keyword": keyword,
            "search_source": "tab_search", "query_correct_type": "1", "is_filter_search": "0",
            "from_group_id": "", "offset": "0", "count": "15",
            "need_filter_settings": "1", "list_type": "multi", "search_id": "",
        })
        return page.evaluate(_SEARCH_JS, [params, keyword])

    @staticmethod
    def _keyword_from_url(url: str) -> str:
        try:
            after = (url or "").split("/search/", 1)[1]
            return unquote(after.split("?", 1)[0])
        except Exception:  # noqa: BLE001
            return ""

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        # Login wall (guest/expired session) — an AUTHORITATIVE "needs login", NOT a flood block.
        if raw.get("status_code") == 2483 or "请先登录" in (raw.get("status_msg") or ""):
            diag.note("douyin.needs_login", body=(
                "抖音 search returned 2483 '请先登录': the 9225 小号 session is logged out/expired. "
                "VNC into the mini's 抖音 Chrome (port 9225) and re-scan-login a disposable 小号."))
            return []
        if raw.get("fetch_error") or raw.get("parse_error"):
            diag.note("douyin.fetch_error", body=str(raw)[:200])
            return []
        docs: list[PolarisDocument] = []
        for item in (raw.get("data") or []):
            doc = self._doc_from_item(item)
            if doc is not None:
                docs.append(doc)
            if len(docs) >= limit:
                break
        return docs

    @staticmethod
    def _doc_from_item(item: dict) -> Optional[PolarisDocument]:
        """One search data[] item → a video doc. Items can be a single video (aweme_info) or a mix
        card (aweme_mix_info.mix_items[0]); non-video cards (users/live/no aweme_id) are dropped."""
        if not isinstance(item, dict):
            return None
        info = item.get("aweme_info")
        if info is None:
            mix = item.get("aweme_mix_info") or {}
            mix_items = mix.get("mix_items") or []
            info = mix_items[0] if mix_items else None
        if not isinstance(info, dict):
            return None
        aweme_id = str(info.get("aweme_id") or "").strip()
        if not aweme_id:
            return None
        desc = (info.get("desc") or "").strip()
        author = info.get("author") or {}
        nickname = author.get("nickname") or None
        stats = info.get("statistics") or {}
        digg = stats.get("digg_count")
        date = None
        create_time = info.get("create_time")
        if isinstance(create_time, (int, float)) and create_time > 0:
            try:
                date = datetime.fromtimestamp(int(create_time), tz=timezone.utc)
            except Exception:  # noqa: BLE001
                date = None
        # Video cover (a vision-capable agent can view it). play_addr is short-lived + token-gated,
        # so the durable handle stays the canonical /video/<id> page url (also eye_transcribe's target).
        media: list[str] = []
        cover = (((info.get("video") or {}).get("cover") or {}).get("url_list") or [])
        if cover:
            media.append(cover[0])
        title = (desc.split("\n", 1)[0][:80] if desc else "") or f"抖音视频 {aweme_id}"
        return PolarisDocument(
            source="douyin",
            source_id=aweme_id,
            url=f"https://www.douyin.com/video/{aweme_id}",
            title=title,
            content=desc or "(无文案; 正文在视频里 — 可对此 url 调 eye_transcribe 转写语音)",
            author=nickname,
            date=date,
            signals=mk_signal("likes", digg, kind="engagement", by="douyin/digg_count"),
            media=media,
            tags=["douyin", "cn", "video"],
            metadata={
                "comment_count": stats.get("comment_count"),
                "share_count": stats.get("share_count"),
                "collect_count": stats.get("collect_count"),
                "play_count": stats.get("play_count"),
                "sec_uid": author.get("sec_uid"),
                "duration_ms": (info.get("video") or {}).get("duration"),
            },
        )

    # ── health ─────────────────────────────────────────────────────────────
    def health_check(self) -> tuple[bool, str]:
        """CDP 9225 reachable + the 小号 is actually logged in. Login is probed CHEAPLY (a home
        navigation reading localStorage HasUserLogin / cookie LOGIN_STATUS — the same signal
        client.py pong() uses) so a sweep never spends a real search. Honest by design: an
        un-logged-in session reports False with the VNC fix, exactly the post-deploy state until
        the operator scan-logs a 小号."""
        from penumbra.core.sources.walled._cdp import cdp_health
        ok, msg = cdp_health(self.cdp_url)
        if not ok:
            return False, f"CDP 9225 down: {msg} (抖音 Chrome — launchd com.penumbra.cdp.douyin)"
        try:
            state = self._run(lambda p: p.evaluate(_LOGIN_PROBE_JS), "https://www.douyin.com")
        except Exception as exc:  # noqa: BLE001
            return False, f"9225 up but login probe failed: {type(exc).__name__}: {str(exc)[:80]}"
        logged_in = (state or {}).get("login") == "1" or (state or {}).get("status") == "1"
        if not logged_in:
            return False, "9225 up but 抖音 小号 NOT logged in — VNC into the 抖音 Chrome + scan-login a 小号"
        return True, "OK (9225 up, 小号 logged in)"
