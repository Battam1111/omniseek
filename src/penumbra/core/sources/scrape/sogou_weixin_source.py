"""微信公众号 (WeChat official-account) keyword search via Sogou Weixin.

WHY (the razor — UNWALL): 公众号 articles are a massive corpus of Chinese first-hand /
specialist writing (行业号 industry analysis, 学院/官方号, niche expertise, 经验贴) that
Google does NOT index at all — mp.weixin.qq.com is closed to general crawlers. The eye's
other wechat source (wewe-rss) only follows a handful of SUBSCRIBED accounts; it cannot
SEARCH the whole 公众号 space by keyword. Sogou Weixin (weixin.sogou.com) is the one free,
no-login index over all 公众号 articles, so this adapter UNWALLs Chinese hidden knowledge
the open web can't reach. (Proven 2026-06-18: a person's 暨大 公众号 footprint was findable
ONLY via Sogou Weixin, never Google.)

SHAPE: anti-bot HTML scrape with a BESPOKE curl_cffi session (browser-impersonated TLS;
the shared http.py helper is bypassed on purpose — its docstring carves out anti-bot
sources). Result article links are TEMPORARY Sogou redirects (/link?url=...&token=...)
bound to the search session's cookie; we resolve each to its PERMANENT
mp.weixin.qq.com/s?... URL WHILE THE SESSION IS HOT (the redirect page reassembles the url
from JS `url += '...'` pieces), so every doc carries a stable drill-in handle the agent can
eye_add_url later (the eye's web fetch reads mp.weixin articles).

explicit_only: anti-bot + Chinese-only + the per-result resolve fan-out make it unfit for
the blind broad fan-out; the router still surfaces it as excluded_relevant for thematically
matching (Chinese/community) queries, where the agent names it.

Recon trail: brain note eye-recon-sogou_weixin.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from penumbra.core.normalize import PolarisDocument
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_SEARCH = "https://weixin.sogou.com/weixin?type=2&query={query}&ie=utf8"
_BASE = "https://weixin.sogou.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_RESOLVE_MIN, _RESOLVE_MAX = 0.4, 0.9  # jittered pacing between link resolutions (anti-bot courtesy)

# Global in-flight cap on the shared anti-bot host (weixin.sogou.com). A single search already fires
# ~1 + N serial requests, but nothing bounded the load ACROSS concurrent agents (each can name + call
# this), so an N-agent burst storms the one anti-bot host and trips its antispider, emptying the source
# for the whole burst. This semaphore (held only around the egress in _sogou_get) caps concurrent Sogou
# requests to 2, deliberately tiny (heavy anti-bot host, no in-call parallelism), so a burst paces
# through. The SogouBlocked breaker stays as the after-block backstop; this stops self-inflicted bursts.
_SOGOU_MAX_INFLIGHT = 2
_sogou_sema = threading.BoundedSemaphore(_SOGOU_MAX_INFLIGHT)


def _sogou_get(sess, url: str, **kwargs):
    """Single Sogou egress chokepoint: every weixin.sogou.com GET (search, link-resolve, health) passes
    through here so the global in-flight cap (_sogou_sema) bounds concurrent requests to the anti-bot host."""
    with _sogou_sema:
        return sess.get(url, **kwargs)

try:
    from bs4 import BeautifulSoup
    from curl_cffi import requests as _creq
    _DEPS_OK = True
except Exception as exc:  # noqa: BLE001 — missing deps must never break server import
    logger.warning("sogou_weixin: bs4/curl_cffi unavailable (%s) — adapter inert", exc)
    _DEPS_OK = False


class SogouBlocked(RuntimeError):
    """Sogou served an anti-bot/captcha page (HTTP 200 with a verify body). Raised — NOT
    swallowed to [] — so the fetcher surfaces a BLOCK as an error: a silent 0 here would
    masquerade as 'no 公众号 articles exist' when the truth is 'the IP/session is throttled'.
    A caller seeing this should BACK OFF and retry later (cooler IP / paced), not conclude empty."""


def _parse_items(html: str, limit: int) -> list[dict]:
    """Pure HTML → item dicts (NO network), so it can be golden-fixture tested offline.
    Each item: {title, snippet, account, ts, link} where link is the relative Sogou /link."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for li in soup.select("ul.news-list li")[:limit]:
        a = li.select_one("h3 a")
        if not a or not a.get_text(strip=True):
            continue
        snip = li.select_one("p.txt-info")
        acct = li.select_one(".s-p .all-time-y2") or li.select_one(".all-time-y2")
        tsm = re.search(r"timeConvert\('(\d+)'\)", str(li))
        out.append({
            "title": a.get_text(strip=True),
            "link": a.get("href") or "",
            "snippet": snip.get_text(" ", strip=True) if snip else "",
            "account": acct.get_text(strip=True) if acct else None,
            "ts": int(tsm.group(1)) if tsm else None,
        })
    return out


def _is_blocked(html: str) -> bool:
    return "antispider" in html or "请输入验证码" in html or "auto_jump" in html


class SogouWeixinAdapter(BaseScrapeAdapter):
    name = "sogou_weixin"
    description = (
        "微信公众号文章关键词搜索 (Sogou Weixin) — the only free keyword index over WeChat "
        "公众号 articles, which Google does NOT crawl. Reach for Chinese first-hand / "
        "specialist 公众号 writing (industry/行业号, 学院官方号, niche expertise, 经验贴) on a "
        "topic. Returns title + snippet + 公众号 name + date + a permanent mp.weixin.qq.com link."
    )
    explicit_only = "anti-bot HTML scrape (Sogou Weixin); 中文公众号; name it for Chinese topic search"
    kind = "lookup"
    domains = ["community", "news", "media"]
    regions = ["cn"]
    modes = ["UNWALL"]
    cache_ttl = 3600
    rank = False  # Sogou returns server-side relevance order; keep it

    @staticmethod
    def _session():
        s = _creq.Session(impersonate="chrome")
        s.headers.update({"user-agent": _UA, "accept-language": "zh-CN,zh;q=0.9"})
        return s

    def _resolve(self, sess, href: str) -> Optional[str]:
        """Sogou /link redirect → PERMANENT mp.weixin.qq.com url (reassembled from the JS
        `url += '...'` pieces; the page strips an injected '@'). None if it can't (graceful:
        the caller keeps the Sogou link as a temporary fallback)."""
        if not href:
            return None
        url = href if href.startswith("http") else _BASE + href
        try:
            time.sleep(random.uniform(_RESOLVE_MIN, _RESOLVE_MAX))
            r = _sogou_get(sess, url, timeout=20)
            parts = re.findall(r"url \+= '([^']*)'", r.text)
            if parts:
                mp = "".join(parts).replace("@", "")
                if "mp.weixin.qq.com" in mp:
                    return mp
            m = re.search(r"(https?://mp\.weixin\.qq\.com/s[^'\"\s]+)", r.text)
            return m.group(1) if m else None
        except Exception:  # noqa: BLE001
            return None

    def _raw_fetch(self, query: str, limit: int):
        if not _DEPS_OK:
            return None
        try:
            sess = self._session()
            r = _sogou_get(sess, _SEARCH.format(query=quote(query)), timeout=20)
            if _is_blocked(r.text):
                # captcha is HTTP 200 with a verify body → a silent [] would read as a query miss.
                # RAISE (the base does NOT swallow _raw_fetch exceptions) so the fetcher surfaces it.
                raise SogouBlocked("Sogou Weixin anti-bot/captcha — back off and retry later")
            if r.status_code != 200:
                logger.warning("sogou_weixin: HTTP %s", r.status_code)
                return None
            items = _parse_items(r.text, limit)
            for it in items:  # resolve each temporary Sogou link → permanent mp url (hot session, paced)
                it["url"] = self._resolve(sess, it["link"]) or (
                    _BASE + it["link"] if it["link"].startswith("/") else it["link"])
            return items
        except SogouBlocked:
            raise  # a BLOCK is not a miss: let it surface as an error, never swallow to None
        except Exception as exc:  # noqa: BLE001 — genuine transient failure → None → [] (contract)
            logger.warning("sogou_weixin fetch failed: %s", exc)
            return None

    def _to_documents(self, raw, query, limit) -> list[PolarisDocument]:
        docs: list[PolarisDocument] = []
        for it in raw:
            if not it.get("title") or not it.get("url"):
                continue
            date = None
            if it.get("ts"):
                try:
                    date = datetime.fromtimestamp(int(it["ts"]), tz=timezone.utc)
                except (ValueError, OSError, TypeError):
                    date = None
            docs.append(PolarisDocument(
                source="sogou_weixin",
                source_id=it["url"],
                url=it["url"],
                title=it["title"],
                content=it.get("snippet") or it["title"],
                author=it.get("account"),
                date=date,
                metadata={
                    "account": it.get("account"),
                    "permanent_url": "mp.weixin.qq.com" in (it.get("url") or ""),
                },
            ))
        return docs

    def health_check(self) -> tuple[bool, str]:
        """Light liveness: one search request, no link-resolution fan-out. Distinguishes
        answered (200, not anti-bot) from blocked/error."""
        if not _DEPS_OK:
            return False, "bs4/curl_cffi not installed"
        try:
            r = _sogou_get(self._session(), _SEARCH.format(query=quote("科技")), timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:60]}"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        if _is_blocked(r.text):
            return False, "anti-bot/captcha"
        return True, f"OK ({len(_parse_items(r.text, 10))} results)"
