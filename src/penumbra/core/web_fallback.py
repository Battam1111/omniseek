"""Render-fallback for ad-hoc URLs no adapter claims: plain fetch, then Jina on a thin page.

``fetcher.fetch_url`` (the ``penumbra_add_url`` path) tries every registered adapter until one
CLAIMS the URL. ~167 adapters cover specific sources, but there is NO generic "read any web
page" adapter, so a URL outside every adapter (a Next.js / SPA marketing page, a JS-walled
doc, an arbitrary blog) returns ``matched=false`` today: the eye does not reach it at all.

This is the LAST RESORT, invoked by ``fetch_url`` ONLY after the adapter loop claims nothing,
so it costs the happy path zero: a claiming adapter returns first and this never runs.

Two-tier, cheapest-first:
  1. Plain ``http.get`` + a static-HTML text extraction (the same bs4 path news_scraper uses).
     If the page is server-rendered, that text is the whole content and we stop (no 2nd call).
  2. Only if the extracted text is THIN (a JS-wall / SPA shell whose body is client-rendered),
     re-fetch through ``https://r.jina.ai/<url>`` (Jina Reader runs real headless Chrome and
     returns LLM-ready markdown; free, keyless at 20 RPM, no key wired). We return that markdown.

THE RAZOR: this RETRIEVES and returns raw page text / markdown. It renders NO judgment about
what the page MEANS; the agent reads the returned content and judges. The thin-trigger is a
mechanical char-count of extracted text, not a judgment of value. No LLM, no summarizer.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import cache, diag, http
from penumbra.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

# Below this many chars of EXTRACTED visible text, a plain fetch is treated as a JS-wall / SPA
# shell (nav + boilerplate, no body) and we escalate to the Jina render. A real server-rendered
# article clears this easily; a client-rendered shell does not. Mechanical, not a value judgment.
_THIN_CHARS = 600
_JINA_ENDPOINT = "https://r.jina.ai/"
_JINA_TIMEOUT = 30          # headless-Chrome render is slower than a plain GET
_FALLBACK_CACHE_TTL = 3600  # an ad-hoc page is re-read rarely; an hour is plenty
_MAX_CHARS = 200_000        # keep a pathologically long render payload sane


def _extract_text(html: str) -> tuple[str, str]:
    """(title, visible_text) from static HTML, the news_scraper extraction centralized here.
    Strips script/style, prefers <main>/<article>/<body>. Returns ('','') if bs4 is absent."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_fallback: bs4 unavailable: %s", exc)
        return "", ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    title_el = soup.find(["h1"]) or soup.title
    title = title_el.get_text(strip=True) if title_el else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True)).strip()
    return title, text


def _jina_markdown(url: str) -> Optional[str]:
    """Fetch ``url`` through r.jina.ai (real headless Chrome to markdown). None on failure.
    Keyless free tier (20 RPM); we send no Authorization header (no key is wired)."""
    # Jina takes the target URL appended raw after the endpoint; it follows its own redirects.
    resp = http.get(_JINA_ENDPOINT + url, timeout=_JINA_TIMEOUT,
                    headers={"Accept": "text/plain", "X-Return-Format": "markdown"})
    if resp is None:
        return None
    md = (resp.text or "").strip()
    return md or None


def _is_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.hostname)
    except Exception:  # noqa: BLE001
        return False


def read_via_fallback(url: str) -> Optional[Document]:
    """Last-resort read for a URL no adapter claimed. Plain fetch; escalate to Jina if thin.
    Returns a Document(source='web') or None (None keeps penumbra_add_url matched=false)."""
    if not _is_http_url(url):
        return None  # only http(s); never let a file:// or odd scheme through
    if cache.cache_only():
        return None  # respect the single egress guard

    key = cache.make_key("web", "fallback", url)
    cached = cache.get(key)
    if cached is not None:
        return Document(**cached) if isinstance(cached, dict) else cached

    resp = http.get(url)  # shared UA + redirects + 30MB cap; None on any failure
    plain_title, plain_text = "", ""
    if resp is not None:
        ctype = (resp.headers.get("content-type") or "").lower()
        # Only HTML is extraction-worthy here; a PDF/json/binary is some other adapter's job
        # (pdf_source already claimed *.pdf earlier in the loop), so do not mis-handle it.
        if "html" in ctype or ctype == "" or ctype.startswith("text/"):
            plain_title, plain_text = _extract_text(resp.text)

    via = "plain"
    title, content = plain_title, plain_text
    if len(plain_text) < _THIN_CHARS:
        # JS-wall / SPA shell (or a hard failure): escalate to the headless render.
        md = _jina_markdown(url)
        if md and len(md) > len(plain_text):
            via, content = "jina", md[:_MAX_CHARS]
            if not title:
                m = re.match(r"\s*Title:\s*(.+)", md)
                if m:
                    title = m.group(1).strip()

    if not content:
        diag.note("web_fallback", url=url,
                  body=f"plain+jina both thin (plain={len(plain_text)} chars)")
        return None  # genuinely nothing: keep matched=false, never a fake empty doc

    doc = Document(
        source="web",
        source_id=url,
        url=url,
        title=(title or url),
        content=content,
        tags=["web", f"render:{via}"],
        metadata={"raw": jsonsafe({"url": url, "rendered_via": via,
                                   "plain_chars": len(plain_text)})},
    )
    cache.set(key, doc.model_dump(mode="json"), ttl=_FALLBACK_CACHE_TTL)
    return doc
