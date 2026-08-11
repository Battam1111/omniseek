"""一亩三分地 (1point3acres.com) adapter — CDP-based.

Same architecture as zhihu_source: connects to the persistent CDP Chrome,
navigates to the search URL, parses HTML. The operator logs in once via VNC.

一亩三分地 is the core community for Chinese students doing PhD/Master's
abroad. Particularly strong on application experiences and post-PhD careers.

Migrated to ``BaseCDPAdapter`` (B2 template method): the base owns the
cdp_call wrapping, the try/except→[] degrade, caching, and registration.
The three hooks below carry every source-specific fact VERBATIM:
``_search_url`` (GBK-encoded Discuz search URL), ``_flow`` (the
wait_through_cloudflare + flood-control retry navigate), and
``_to_documents`` (the .pbw parse with dedup + break-at-limit). The CF-aware
health probe and the image-surfacing fetch_url are kept as overrides because
they are bespoke (not expressible through the base's generic defaults).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.walled._base import BaseCDPAdapter
from penumbra.core.sources.walled._cdp import (
    cdp_call, cdp_health, content_with_media, images_from_page, wait_through_cloudflare,
)

logger = logging.getLogger(__name__)

BASE = "https://www.1point3acres.com"
# Discuz-style search URL. 一亩三分地 (legacy Discuz) requires query encoded
# as GBK, NOT UTF-8 — sending UTF-8 produces garbled-character searches that
# return nothing. The page itself uses UTF-8 for display, but the search
# parameter still expects GBK encoding (historical Discuz behavior).
SEARCH_URL_TEMPLATE = f"{BASE}/bbs/search.php?mod=forum&searchsubmit=yes&srchtxt={{q}}"


def _gbk_url_encode(s: str) -> str:
    """URL-encode a string as GBK bytes (needed for legacy Discuz)."""
    try:
        return quote(s.encode("gbk"))
    except UnicodeEncodeError:
        # Some chars can't be GBK-encoded; fall back to UTF-8
        return quote(s)


# Caller-side pace/serialize gate (2026-07-17). Discuz enforces a ~15s PER-SESSION flood-control
# window, and each reload COUNTS AS A NEW SEARCH that RESETS that window, so concurrent / rapid
# callers (a fleet of agents drilling at once) reset each other and see near-100% flood blocks
# (observed 2026-06/07; a whole session's worth of yipin probes came back blocked). The in-flow
# 16s/24s backoff in _flow only helps a SINGLE caller re-clear ITS window; it cannot stop a second
# caller from resetting it. This gate is the cross-caller fix: at most ONE yipin egress runs at a
# time, and consecutive egresses are >= _YIPIN_MIN_GAP_S apart PROCESS-WIDE, so the window is never
# reset out from under an in-flight search. It wraps _run (the CDP egress leaf) ONLY, so cache HITS
# (which return before _run) are never paced.
_YIPIN_GATE = threading.Lock()
_YIPIN_LAST = [0.0]        # monotonic time the last egress completed
_YIPIN_MIN_GAP_S = 16.0    # > Discuz's ~15s per-session flood window


class YipinsanfendiAdapter(BaseCDPAdapter):
    name = "yipinsanfendi"
    needs_credentials = False  # Login via VNC once; the session persists + now self-heals (below)
    explicit_only = "shared CDP Chrome (precious logged-in session)"
    description = "一亩三分地 — North America CS PhD application + grad school community"
    url_host = "1point3acres.com"
    # Auth self-heal (2026-07-06): the 9222 session logged out -> 游客 cannot search -> silent [].
    # The SSO login (auth.1point3acres.com) is autofill-backed (Chrome remembers the password) and
    # its Cloudflare Turnstile / reCAPTCHA auto-solve invisibly for the trusted profile, so the base
    # can relogin headlessly. logged_out_markers are the guest-block error strings (distinct from the
    # flood-control markers is_blocked catches), so a normal or genuinely-empty result never triggers.
    login_url = "https://www.1point3acres.com/bbs/member.php?mod=logging&action=login"
    logged_out_markers = ("无法进行此操作", "用户组(游客)")
    # fetch_url reads a thread page through the SHARED 9222 CDP pool: Cloudflare wait + a 20s
    # selector wait + serial-pool queueing legitimately outlast the fetcher's 30s default cap.
    # Under that default the fetcher abandons this adapter mid-flight (the URL then falls to the
    # generic web fallback, which CANNOT pass Cloudflare) while the orphaned cdp_call keeps
    # occupying the pool worker up to its own 90s — cascading into the "deep reads wedge under
    # load" class (observed 2026-07-08 under an 11-agent fleet). Declare a budget that CONTAINS
    # cdp_call's 90s default so CDP cleans up before the fetcher bound fires (same convention as
    # xiaohongshu's fetch_timeout=120 > cdp timeout=110).
    fetch_timeout = 100.0

    def _search_url(self, query: str) -> str:
        return SEARCH_URL_TEMPLATE.format(q=_gbk_url_encode(query))

    def _run(self, callback, initial_url):
        # Serialize + pace at the CDP-egress leaf (see _YIPIN_GATE above). Concurrent / back-to-back
        # callers would otherwise reset Discuz's ~15s flood window and get blocked; here at most one
        # yipin egress runs at a time and consecutive ones are >= _YIPIN_MIN_GAP_S apart. Cache hits
        # skip this entirely (the base returns before _run), so only real searches pay the gap. Covers
        # BOTH the search flow and the auth-heal re-flow (both route through _run).
        with _YIPIN_GATE:
            wait = _YIPIN_MIN_GAP_S - (time.monotonic() - _YIPIN_LAST[0])
            if wait > 0:
                time.sleep(wait)
            try:
                return super()._run(callback, initial_url)
            finally:
                _YIPIN_LAST[0] = time.monotonic()

    def _flow(self, page) -> str:
        wait_through_cloudflare(page)  # CF 'Just a moment' auto-solves in ~2s; don't read before it clears
        # Discuz throttles rapid / CONCURRENT searches with a flood-control interstitial that
        # carries NONE of the result selectors → a plain wait times out and the parser sees 0
        # results (a SILENT false-empty: the source works, it was just throttled — proven
        # 2026-06-22, GBK "加拿大" = 35 results when unthrottled, 0 under concurrent probing).
        # Detect the interstitial on EVERY attempt (not just the first) and retry with ESCALATING
        # backoff: a single 6s retry was not enough headroom under load. (Still pace/serialize
        # callers: a [] from a walled CDP source is never authoritative-empty.)
        for attempt in range(3):
            try:
                page.wait_for_selector(".pbw, li.pbw, .nopost", timeout=12000)
                return page.content()
            except Exception:  # noqa: BLE001
                body = ""
                try:
                    body = page.inner_text("body") or ""
                except Exception:  # noqa: BLE001
                    pass
                if attempt < 2 and any(k in body for k in ("间隔", "太快", "频繁", "稍候", "稍后")):
                    # Discuz guest search enforces a ~15s interval, and each reload COUNTS AS A NEW
                    # SEARCH (resets the window). So each single wait must itself exceed 15s: the old
                    # 5/9/13s ladder could mathematically never clear the gate (proven 2026-07-09:
                    # burst sessions saw near-100% flood blocks). 16s/24s puts the FIRST retry outside
                    # the window; 3 attempts total keeps worst-case (~76s incl. selector waits) inside
                    # cdp_call's 90s budget.
                    page.wait_for_timeout(16000 + attempt * 8000)  # 16s, 24s escalating
                    try:
                        page.reload(wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                return page.content()  # genuine no-results / other page — let the parser handle it
        return page.content()

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        html = raw
        soup = BeautifulSoup(html, "lxml")
        # Discuz search results: results are in .pbw blocks
        results = soup.select(".pbw, li.pbw")

        docs: list[Document] = []
        seen = set()
        for r in results:
            try:
                title_a = r.select_one("h3 a, .xs3 a, a.xst")
                if not title_a:
                    continue
                href = title_a.get("href") or ""
                if not href or href in seen:
                    continue
                seen.add(href)
                if href.startswith("/"):
                    full_url = BASE + href
                elif href.startswith("http"):
                    full_url = href
                else:
                    full_url = f"{BASE}/bbs/{href.lstrip('/')}"

                title = title_a.get_text(strip=True)
                if not title:
                    continue

                # Excerpt / snippet
                excerpt_el = r.select_one(".xg1 + p, p.xg1, .quote, .summary")
                excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

                # Author / forum from .xs1 line
                meta_el = r.select_one(".xs1, .xg1")
                author = None
                if meta_el:
                    meta_text = meta_el.get_text(" ", strip=True)
                    m = re.search(r"作者[: ]*([^\s]+)", meta_text)
                    if m:
                        author = m.group(1)

                # tid for source_id
                m = re.search(r"thread-(\d+)|tid=(\d+)", href)
                tid = (m.group(1) or m.group(2)) if m else full_url

                docs.append(
                    Document(
                        source="yipinsanfendi",
                        source_id=tid,
                        url=full_url,
                        title=title,
                        content=excerpt or "(click URL for full thread)",
                        author=author,
                        metadata={"raw": jsonsafe(str(r))},
                    )
                )
                if len(docs) >= limit:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping 1point3acres result: %s", exc)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "1point3acres.com" not in host:
            return None
        def _navigate(page):
            wait_through_cloudflare(page)  # let CF clear before reading the thread
            page.wait_for_selector("#thread_subject, .ts h1, h1", timeout=20000)
            return page.content(), images_from_page(page)

        try:
            html, images = cdp_call(_navigate, initial_url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("1point3acres fetch_url failed: %s", exc)
            return None

        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("#thread_subject, h1.ts, h1")
        title = title_el.get_text(strip=True) if title_el else "(untitled)"

        # A Discuz thread page carries ~10 posts as td.t_f blocks; in a 请教/复盘 thread the
        # substance lives in the REPLIES, so read every post on the page, not just floor #1.
        # (.t_fsz wraps td.t_f on some skins → text-dedup collapses the wrapper duplicates.
        # Multi-page threads: page N is addressable as thread-<tid>-N-1.html, caller paginates.)
        parts: list[str] = []
        seen_texts = set()
        for el in soup.select("td.t_f, .t_fsz, div.t_f"):
            t = el.get_text("\n", strip=True)
            if t and t not in seen_texts:
                seen_texts.add(t)
                parts.append(t)
        body = "\n\n---\n\n".join(parts)

        m = re.search(r"thread-(\d+)|tid=(\d+)", url)
        tid = (m.group(1) or m.group(2)) if m else url

        return Document(
            source="yipinsanfendi",
            source_id=tid,
            url=url,
            title=title,
            content=content_with_media(body, images) or "(no body extracted)",
            media=images,
        )

    def health_check(self) -> tuple[bool, str]:
        cdp_ok, cdp_msg = cdp_health()
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"

        def _title(p) -> str:
            cleared = wait_through_cloudflare(p)  # CF auto-solves ~2s; read AFTER it clears
            return ("" if cleared else "Just a moment...") or p.title()

        try:
            title = cdp_call(_title, initial_url=BASE)
            if "一亩三分地" in title or "1point3acres" in title.lower() or "请稍候" in title:
                return True, "OK (CDP + 1point3acres reachable)"
            if "Just a moment" in title:
                return False, "Cloudflare interactive captcha did not auto-clear (needs VNC)"
            return False, f"unexpected page title: {title[:60]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
