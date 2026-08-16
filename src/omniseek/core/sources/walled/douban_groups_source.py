"""豆瓣小组 (Douban groups) — grassroots overseas-study / immigration / diaspora-life discussion graph.

Douban hosts China's densest grassroots-community graph: member-run 小组 (groups) where overseas-study,
immigration, and diaspora-life experience is shared candidly (the lived, unofficial counterpart to the
institutional sources OmniSeek already covers). It fills a COMMUNITY gap: the first-person "我在德国/加拿大/
新加坡的真实生活" thread, the group organized around a destination, the answer to "哪个组在讨论 X".

Why CDP-login (rebuilt 2026-07-10; was a bespoke anonymous httpx scraper):
- douban.com/group/search now 302-redirects anonymous requests to sec.douban.com (its anti-bot gateway)
  → a 403 login-redirect page ("豆瓣 - 登录跳转页"). The old adapter followed the redirect, got a
  results-less page, and returned [] SILENTLY (its bespoke httpx was invisible to OmniSeek's diag layer).
  Anonymous group search is gone.
- The m.douban.com rexxar JSON search API returns structured JSON gated ONLY by the web session cookie
  (dbcl2/ck on .douban.com) — it is NOT request-signed (that is the frodo app API; avoid). So we drive
  the logged-in 9222 Chrome (the same cn-forums cluster as zhihu / 一亩三分地 / 小木虫) to fetch it
  same-origin: page.goto(m.douban.com) so the real session + fingerprint bypass the sec wall, then
  page.evaluate a fetch of /rexxar/api/v2/search (credentials + Referer attach automatically).

ONE rexxar call yields BOTH facets: ``smart_box`` = the matched GROUP (STRUCTURE: name + member/topic
count + url), ``contents.items`` = the discussion TOPICS (UNWALL: title + abstract + engagement + when).
A logged-out session returns {"msg":"need_login","code":103}; we detect it, emit a typed diagnostic, and
return [] with the SHORT floor — so a [] is never mistaken for 'no results' (the silent-failure class the
old adapter had, now killed because every path routes through cdp_call + diag).

Login: the operator logs into douban.com once via VNC on the 9222 Chrome; the dbcl2 cookie covers
m.douban.com. No password is stored — OmniSeek reuses the browser session, same as zhihu.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from omniseek.core import cache, diag
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.walled._base import EMPTY_TTL
from omniseek.core.sources.walled._cdp import cdp_call, cdp_health

logger = logging.getLogger(__name__)

_SEARCH = "https://m.douban.com/rexxar/api/v2/search"

# The JS the logged-in 9222 Chrome runs from the m.douban.com origin: fetch the rexxar search JSON
# same-origin (credentials + Referer attach automatically) so the session cookie bypasses the
# sec.douban.com anonymous wall. type=group returns smart_box (the group) + contents (the topics).
# Auth (2026-07-16): rexxar TIGHTENED and now answers need_login (code 103) to a bare cookie-only
# fetch even with a valid dbcl2. The real m.douban.com frontend calls it as an XHR (X-Requested-With)
# AND carries the ck cookie as a query param; either alone re-authenticates a logged-in session
# (verified via the auth-variant probe), and we mirror BOTH to match the frontend exactly.
_JS_FETCH = """async ({q, count}) => {
    const ckc = document.cookie.split('; ').find(c => c.indexOf('ck=') === 0);
    const ck = ckc ? ckc.slice(3) : '';
    const r = await fetch('https://m.douban.com/rexxar/api/v2/search?q=' + encodeURIComponent(q)
                          + '&type=group&start=0&count=' + count
                          + (ck ? '&ck=' + encodeURIComponent(ck) : ''),
                          {credentials: 'include',
                           headers: {'Referer': 'https://m.douban.com/', 'X-Requested-With': 'XMLHttpRequest'}});
    return await r.text();
}"""


def _int_before(text: str, marker: str) -> Optional[int]:
    """Pull the integer right before a marker out of a card subtitle, e.g. '10赞 · 260回复'."""
    m = re.search(r"(\d[\d,]*)\s*" + re.escape(marker), text or "")
    return int(m.group(1).replace(",", "")) if m else None


def _parse_dt(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


class DoubanGroupsAdapter:
    name = "douban_groups"
    needs_credentials = False  # login happens once via VNC on the 9222 Chrome; we reuse the session
    explicit_only = "CDP-login rexxar JSON via shared 9222 Chrome (precious logged-in douban session)"
    description = ("豆瓣小组: China grassroots community graph for overseas-study/immigration/diaspora "
                   "life (groups + discussion threads via m.douban.com rexxar, logged-in 9222 CDP)")
    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["community", "immigration"]
    modes = ["UNWALL", "STRUCTURE"]
    regions = ["cn"]
    fetch_timeout = 100.0  # contain cdp_call's 90s default before the fetcher bound fires (zhihu lesson)

    def search(self, query: str, limit: int = 10) -> list[Document]:
        q = (query or "").strip()
        if not q:
            return []
        key = cache.make_key("douban_groups", "search", q, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        def _flow(page):
            page.wait_for_timeout(1200)  # let the session/fingerprint settle before the same-origin fetch
            return page.evaluate(_JS_FETCH, {"q": q, "count": max(limit * 2, 20)})

        try:
            txt = cdp_call(_flow, initial_url="https://m.douban.com/", timeout=60)
            data = json.loads(txt)
        except json.JSONDecodeError as exc:
            logger.warning("douban_groups: rexxar returned non-JSON: %s", str(exc)[:100])
            diag.note("douban_groups.non_json", url=_SEARCH,
                      body="rexxar returned non-JSON (a sec.douban.com wall or an HTML page) even via CDP")
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("douban_groups CDP search failed: %s", exc)
            return []

        # Logged-out: the 9222 Chrome's douban session expired → the rexxar API answers need_login. A []
        # must NEVER read as 'no results': emit a typed diagnostic (the session-warmer/health Bark it) and
        # fall through to the SHORT-TTL empty so it self-heals after a VNC re-login. douban login is
        # account/password (no autofill-relogin), needs a human on the 9222 Chrome — the zhihu pattern.
        if isinstance(data, dict) and (data.get("code") == 103 or data.get("msg") == "need_login"):
            diag.note("douban_groups.auth_expired", url=_SEARCH, body=(
                "AUTH_EXPIRED: the 9222 Chrome's douban session is logged out (rexxar need_login). Needs a "
                "VNC re-login on the mini (the 9222 cn-forums Chrome, douban.com). NOT authoritative-empty."))
            return []

        docs: list[Document] = []
        # GROUPS (STRUCTURE facet): the matched community from smart_box.
        for box in (data.get("smart_box") or []):
            if box.get("target_type") == "group":
                doc = self._group_doc(box.get("target") or {})
                if doc:
                    docs.append(doc)
        # TOPICS (UNWALL facet): the discussion threads — the lived experience.
        for it in ((data.get("contents") or {}).get("items") or []):
            if it.get("target_type") != "topic":
                continue
            doc = self._topic_doc(it.get("target") or {})
            if doc:
                docs.append(doc)
            if len(docs) >= limit:
                break

        docs = docs[:limit]
        # Real results → full TTL; empty → the short floor (a transient blip / expired session must not
        # blind the query for 15 min; it self-heals on the next call after a re-login).
        ttl = 900 if docs else EMPTY_TTL
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=ttl)
        return docs

    def _group_doc(self, g: dict) -> Optional[Document]:
        name, url = g.get("name"), g.get("url")
        if not name or not url:
            return None
        return Document(
            source="douban_groups",
            source_id=f"group:{g.get('id') or url}",
            url=url,
            title=f"[小组] {name}",
            content=(g.get("desc_abstract") or "").strip() or "(no description)",
            signals=mk_signal("members", g.get("member_count"), kind="engagement", by="douban/member_count"),
            tags=["豆瓣小组", "group"],
            metadata={"member_count": g.get("member_count"), "topic_count": g.get("topic_count"),
                      "member_name": g.get("member_name"), "raw": jsonsafe(g)},
        )

    def _topic_doc(self, t: dict) -> Optional[Document]:
        title = t.get("title")
        if not title:
            return None
        # target_id is the topic id; the web thread url is /group/topic/<id>/ (uri is douban://...).
        topic_id = t.get("target_id") or ""
        if not topic_id:
            m = re.search(r"/topic/(\d+)", str(t.get("uri") or ""))
            topic_id = m.group(1) if m else ""
        url = f"https://www.douban.com/group/topic/{topic_id}/" if topic_id else (t.get("uri") or "")
        sub = t.get("card_subtitle") or ""  # e.g. "豆友oGOTvWcgaM 10赞 · 260回复"
        replies = _int_before(sub, "回复")
        return Document(
            source="douban_groups",
            source_id=f"topic:{topic_id or url}",
            url=url,
            title=title,
            content=(t.get("abstract") or "").strip() or "(click URL for the full thread)",
            author=(t.get("owner") or {}).get("name"),
            date=_parse_dt(t.get("create_time")),
            signals=mk_signal("replies", replies, kind="engagement", by="douban/reply_count"),
            tags=["豆瓣小组", "topic"],
            metadata={"likes": _int_before(sub, "赞"), "replies": replies, "card_subtitle": sub,
                      "photos_count": t.get("photos_count"), "raw": jsonsafe(t)},
        )

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # search-only; the topic abstract already carries the gist

    def health_check(self) -> tuple[bool, str]:
        cdp_ok, cdp_msg = cdp_health()
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"
        try:
            docs = self.search("上海租房", limit=3)
            if docs:
                return True, f"OK ({len(docs)} results, rexxar via CDP)"
            return False, "douban rexxar returned 0 — session logged out? needs a VNC re-login on 9222"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"


from omniseek.core.fetcher import register_adapter

register_adapter(DoubanGroupsAdapter())
