"""Full-text via the CDP real browser, for venues that BLOCK headless fetches but
render fine for a real browser. The search-index sources give only snippets for these
walled venues, yet the full post / article / answer IS reachable through the shared
Chrome (a real browser fingerprint; no login needed for the public content).

Verified by direct CDP test 2026-06-07 (NO login required for any of these):
  - quora.com      full answers (after expanding every "Continue Reading")
  - teamblind.com  full original post (comments may need login)
  - glassdoor.com  interview/review overview + Glassdoor-AI summary
  - maimai.cn      full /article/detail body (comments need login)
  - linkedin.com   full public POST body (/posts/ only; profiles/Track B are off-limits)
  - x.com          public profile timeline + tweets (twitter.com alias)
NOT usable from our datacenter egress IP (served a login/gateway page): HardwareZone
(forums.hardwarezone.com.sg) -> keep using its search-index source.

Workflow: DISCOVER via the matching search-index source, then omniseek_read the result
URL here for the full text. READ-ONLY, no login, URL-only (search() is []).
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_md

from omniseek.core.normalize import Document, strip_base64_images
from omniseek.core.sources.walled._cdp import cdp_call, cdp_health, content_with_media, images_from_page

logger = logging.getLogger(__name__)

_MAX_CHARS = 200_000
# Host substrings claimed for full-text CDP fetch (each verified to render for a real browser).
_HOSTS = ("quora.com", "teamblind.com", "glassdoor.com", "maimai.cn",
          "linkedin.com", "x.com", "twitter.com")
_HEALTH_URL = "https://www.quora.com/"

# Quora truncates long answers behind "Continue Reading"; clicking expands inline (no login).
# A no-op on the other venues (no such element).
_EXPAND_JS = (
    "() => { let n=0;"
    " const els=[...document.querySelectorAll('div,span,p,a,button')]"
    "  .filter(e=>e.children.length===0 && /^\\s*Continue Reading\\s*$/i.test(e.textContent||''));"
    " for(const e of els){ try{ e.click(); n++; }catch(_){} } return n; }"
)


class CdpFulltextAdapter:
    name = "cdp_fulltext"
    needs_credentials = False
    kind = "portal"
    explicit_only = "CDP real-browser full-text (pair with the search-index sources)"
    description = (
        "Full-text via the CDP real browser for venues that wall/403 headless but render for a "
        "real browser (Quora, Blind/teamblind, Glassdoor, 脉脉/maimai, LinkedIn public posts, "
        "X/Twitter profiles+tweets). Discover via the matching search-index source, then "
        "omniseek_read the URL here to turn a snippet into the full post/article/answer. "
        "READ-ONLY, no login. LinkedIn limited to /posts/ (public Track A only)."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        return []  # discovery is the search-index sources' job; this adapter is URL-only

    def fetch_url(self, url: str) -> Optional[Document]:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not any(h in host for h in _HOSTS):
            return None
        # LinkedIn: ONLY public posts (Track A). Never profiles / relationship-graph (Track B,
        # legally off-limits per the operator's constraint).
        if "linkedin.com" in host and "/posts/" not in (parsed.path or ""):
            return None

        def _nav(page):
            page.wait_for_load_state("domcontentloaded", timeout=25000)
            # Adaptive settle: proceed as soon as the network goes quiet (most pages ~1-2s)
            # instead of a flat 3s dead-wait, capped at 6s so a chatty page still proceeds. The
            # 4 lazy-load scrolls below are UNCHANGED → recall is preserved; only the head wait shrinks.
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:  # noqa: BLE001 — networkidle not reached within the cap → proceed
                pass
            for _ in range(4):  # scroll to load lazy-rendered content
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                page.wait_for_timeout(1100)
            try:  # expand Quora-style "Continue Reading" (no-op on other venues)
                if page.evaluate(_EXPAND_JS):
                    page.wait_for_timeout(2000)
                    page.evaluate(_EXPAND_JS)
                    page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001
                pass
            return {"url": page.url, "title": page.title() or "", "html": page.content(),
                    "images": images_from_page(page)}

        try:
            r = cdp_call(_nav, initial_url=url, timeout=95)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cdp_fulltext fetch_url failed (%s): %s", url, exc)
            return None

        soup = BeautifulSoup(r["html"], "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside"]):
            tag.decompose()
        main = soup.find("main") or soup.body
        # Emit MARKDOWN (Document.content contract) not flattened text, mirroring OmniSeek's other
        # markdownify paths (wechat/_rss/_stackexchange); try/except falls back to the old get_text on
        # pathological HTML, and strip_base64_images defuses inline data-URI blobs.
        if main:
            try:
                body = html_to_md(str(main), heading_style="ATX").strip()
            except Exception:  # noqa: BLE001 — markdownify can be picky on weird HTML
                body = main.get_text("\n", strip=True)
            body = strip_base64_images(body)
        else:
            body = ""
        body = re.sub(r"\n{3,}", "\n\n", body).strip()[:_MAX_CHARS]

        return Document(
            source="cdp_fulltext", source_id=url, url=r["url"],
            title=(r["title"] or "(untitled)").strip()[:200],
            content=content_with_media(body, r.get("images") or []) or "(no text extracted)",
            media=(r.get("images") or []),
        )

    def health_check(self) -> tuple[Optional[bool], str]:
        cdp_ok, cdp_msg = cdp_health()
        if not cdp_ok:
            return None, f"CDP not reachable: {cdp_msg}"

        def _nav(page):
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            return len(page.content())

        try:
            n = cdp_call(_nav, initial_url=_HEALTH_URL, timeout=50)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        return (True, "OK (CDP real-browser fulltext)") if n > 5000 else (False, "probe page too small (blocked?)")


from omniseek.core.fetcher import register_adapter

register_adapter(CdpFulltextAdapter())
