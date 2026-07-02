"""小木虫 (muchong.com) adapter — CDP-based because the site renders search via JS.

We initially tried httpx + BeautifulSoup, but 小木虫's search page just
returns a "正在加载中..." (loading) skeleton and fetches real results via
JavaScript. So we route through the persistent CDP Chrome — no login
required for 小木虫, but the JS execution is.

This is the only "scrape/" adapter that uses CDP (because of the JS
rendering requirement). Other CDP-based adapters live in walled/.

Migrated onto ``walled._base.BaseCDPAdapter`` (template method): the base owns the
cdp_call wrapping + try/except→[] degrade + cache round-trip + registration. The
GBK-encoded search URL lives in ``_search_url``, the JS-wait navigate in ``_flow``
(verbatim), and the th.t_new+/t- anchor parse in ``_to_documents`` (verbatim).
``fetch_url`` (dual-host muchong.com/emuch.net claim) and ``health_check`` (login-state
evaluate) keep their bespoke flows as full overrides — neither maps onto a base default.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.walled._base import BaseCDPAdapter
from penumbra.core.sources.walled._cdp import cdp_call, cdp_health, content_with_media, images_from_page

logger = logging.getLogger(__name__)

BASE = "https://muchong.com"
# Discuz search URL. 2026-06-14: the search param changed kw -> wd (the live homepage form is
# GET search.php?wd=<q>) and result links became /t-<tid>-<page>. The old kw= URL silently
# returned the "正在加载中" skeleton (no search ran) -> 0 results = silent breadth loss. Query
# stays GBK-encoded (legacy Discuz; UTF-8 is misread as GBK -> off-topic recent posts, not the query).
SEARCH_URL_TEMPLATE = f"{BASE}/bbs/search.php?wd={{q}}"


def _gbk_url_encode(s: str) -> str:
    try:
        return quote(s.encode("gbk"))
    except UnicodeEncodeError:
        return quote(s)


class XiaomuchongAdapter(BaseCDPAdapter):
    name = "xiaomuchong"
    needs_credentials = False  # Public search; no login needed (but JS render needed)
    explicit_only = "shared CDP Chrome (JS render, slow)"
    description = "小木虫 — China's oldest PhD/master's academic forum (since 2001, 5M users)"
    cache_ttl = 1800

    # ------------------------------------------------------------------ hooks
    def _search_url(self, query: str) -> str:
        return SEARCH_URL_TEMPLATE.format(q=_gbk_url_encode(query))

    def _flow(self, page) -> str:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        try:
            # Wait for the result rows (the /t-<tid> thread links) to render. The page keeps a
            # stray "正在加载中" string even after results appear, so we wait on the LINKS, not
            # the loading text (waiting on that text is what hung 15s then returned 0).
            page.wait_for_function(
                '() => document.querySelectorAll(\'a[href*="/t-"]\').length > 0',
                timeout=15000,
            )
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1200)  # small hydration buffer
        return page.content()

    def _to_documents(self, raw: str, query: str, limit: int) -> list[Document]:
        soup = BeautifulSoup(raw, "lxml")

        # Results live in the Discuz result table's title cells (th.t_new). Prefer those to exclude
        # sidebar / hot-thread /t- links; fall back to ALL /t- anchors if that class drifts again
        # (robustness over one brittle selector). Thread links are now /t-<tid>-<page>.
        anchors = soup.select('th.t_new a[href*="/t-"]') or [
            a for a in soup.find_all("a", href=True) if re.search(r"/t-\d+", a["href"])
        ]
        docs: list[Document] = []
        seen: set[str] = set()
        for a in anchors:
            href = a["href"]
            m = re.search(r"/t-(\d+)", href)
            if not m:
                continue
            tid = m.group(1)
            if tid in seen:
                continue
            seen.add(tid)

            text = a.get_text(strip=True)
            if not text or len(text) < 4:
                continue

            # Resolve URL (live links are absolute; handle relative defensively)
            if href.startswith("http"):
                full_url = href
            elif href.startswith("/"):
                full_url = BASE + href
            else:
                full_url = f"{BASE}/{href.lstrip('/')}"

            docs.append(
                Document(
                    source="xiaomuchong",
                    source_id=tid,
                    url=full_url,
                    title=text,
                    content="(click URL for full thread; 小木虫 search returns titles only)",
                    metadata={
                        "discuz_thread_id": tid,
                        "raw": jsonsafe({"thread_id": tid, "title": text, "url": full_url, "href": href}),
                    },
                )
            )
            if len(docs) >= limit:
                break

        return docs

    # ----- bespoke overrides (no base default expresses these) -----------------
    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "muchong.com" not in host and "emuch.net" not in host:
            return None
        def _navigate(page):
            page.wait_for_selector("h1, #thread_subject, .ts h1", timeout=15000)
            return page.content(), images_from_page(page)

        try:
            html, images = cdp_call(_navigate, initial_url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("小木虫 fetch_url failed: %s", exc)
            return None

        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("#thread_subject, .ts h1, h1")
        title_text = title_el.get_text(strip=True) if title_el else "(untitled)"

        body_el = soup.select_one("td.t_f, .t_fsz, div.t_f, .t_msgfont")
        body_text = body_el.get_text("\n", strip=True) if body_el else "(no body extracted)"

        m = re.search(r"thread-(\d+)|tid=(\d+)", url)
        tid = (m.group(1) or m.group(2)) if m else url

        return Document(
            source="xiaomuchong",
            source_id=tid,
            url=url,
            title=title_text,
            content=content_with_media(body_text, images),
            media=images,
            metadata={"raw": jsonsafe({"thread_id": tid, "title": title_text,
                                       "url": url, "body": body_text})},
        )

    def health_check(self) -> tuple[bool, str]:
        cdp_ok, cdp_msg = cdp_health()
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"
        def _check(page) -> dict:
            page.wait_for_timeout(2000)
            return page.evaluate("""() => {
                const loginLink = document.querySelector('a[href*="logging&action=login"], a[href*="login.php"]');
                const userArea = document.querySelector('#um, .ui_username, [id*="user"][class*="info"]');
                const userVisible = userArea && userArea.offsetParent !== null;
                return {
                    logged_in: !!userVisible || !loginLink,
                    has_login_link: !!loginLink,
                    title: document.title,
                };
            }""")

        try:
            login_state = cdp_call(_check, initial_url=BASE)
            if not login_state.get("logged_in"):
                return False, "未登录: operator 需要在 CDP Chrome 里登录 muchong.com"
            if "小木虫" in (login_state.get("title") or ""):
                return True, "OK (CDP + 小木虫 logged in)"
            return False, f"unexpected page title: {login_state.get('title', '')[:60]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
