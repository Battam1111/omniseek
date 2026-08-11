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

from penumbra.core import _netguard, cache, diag, http, safeurl
from penumbra.core.normalize import Document, jsonsafe, strip_base64_images

logger = logging.getLogger(__name__)

# Below this many chars of EXTRACTED visible text, a plain fetch is treated as a JS-wall / SPA
# shell (nav + boilerplate, no body) and we escalate to the Jina render. A real server-rendered
# article clears this easily; a client-rendered shell does not. Mechanical, not a value judgment.
_THIN_CHARS = 600
_JINA_ENDPOINT = "https://r.jina.ai/"
_JINA_TIMEOUT = 30          # headless-Chrome render is slower than a plain GET
_FALLBACK_CACHE_TTL = 3600  # an ad-hoc page is re-read rarely; an hour is plenty
_MAX_CHARS = 200_000        # keep a pathologically long render payload sane

# A safe_fetch blocked_reason in THIS set means the untrusted target was SSRF-REFUSED: return None and
# do NOT escalate to Jina (never launder a blocked target through Jina's server-side fetch). It is
# EXACTLY _netguard's canonical SSRF-class reasons (private_ip / bad_scheme / bad_port / userinfo).
# "dns" is deliberately NOT here: _netguard.security_block_reason treats a dns miss as non-blocking
# ("not a security block, the real fetch will error itself"), so a merely-unresolvable URL falls
# through to Jina (which may resolve what our resolver could not) exactly like the old plain-fetch-
# failed path, staying consistent with that ONE _netguard decision. A benign failure (timeout /
# fetch_error / oversize / redirect_loop) likewise falls through.
_SSRF_REFUSE = _netguard.SECURITY_BLOCK_REASONS

# Distinctive anti-bot interstitial phrases (Cloudflare / generic CAPTCHA walls / 知乎).
# Deliberately narrow: each string is challenge-page boilerplate no real article carries.
# Why refuse instead of return: a challenge page IS a failure. Returning it as a doc MASKS
# the true cause (an adapter that claims this host failed/timed out upstream and the URL
# leaked here), which is exactly how the 2026-07 "1p3a thread bodies unreadable" class stayed
# invisible for weeks: health saw liveness, callers saw matched=true junk, no failure surface.
_CHALLENGE_MARKERS = (
    "Just a moment...",
    "Performing security verification",
    "Verification successful. Waiting for",
    "Checking your browser before accessing",
    "page maybe requiring CAPTCHA",
    "验证您是否是真人",
    "安全验证 - 知乎",
)


def _is_known_walled_shell(url: str, title: str, content: str) -> bool:
    """Recognize verified XHS deep-link shells returned as successful HTML.

    These pages are not generic thin SPA pages: the static response is a large navigation shell,
    so the character threshold alone cannot detect the failure. Keep this check narrow and tied to
    the observed host/path/title markers so real articles are not judged by the fallback layer.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not re.search(r"/(?:explore|discovery/item|search_result|mobile/question)/", path):
        return False
    clean_title = (title or "").strip().casefold()
    text = content or ""
    if "xiaohongshu.com" in host and clean_title == "小红书 - 你的生活兴趣社区".casefold():
        return True
    if "rednote.com" in host and clean_title == "rednote" and "channel_type=web_error_page" in text:
        return True
    return False


def _is_known_walled_deep_link(url: str) -> bool:
    """Return whether ``url`` belongs to a walled note route with a source adapter.

    A known walled deep link must never be sent to the generic web reader. If its adapter
    declines because the account is logged out, rate-limited, or the note is gone, returning
    a public login/error shell as ``source='web'`` is a false positive, not graceful fallback.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"xiaohongshu.com", "www.xiaohongshu.com", "rednote.com", "www.rednote.com"}:
        return False
    return bool(re.search(r"/(?:explore|discovery/item|search_result|mobile/question)/", parsed.path or ""))


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
    # Strip conventional chrome SCOPED TO main (borrowed idea: readability/ArchiveBox body isolation),
    # AFTER title capture so the h1/lede is never lost, and deliberately KEEPING <header> (article
    # headers carry the h1/lede). Effect: a JS shell whose nav/footer boilerplate inflated the char
    # count past _THIN_CHARS now drops below it -> correct escalation to the jina render, instead of
    # returning chrome as if it were the article (a silent-wrong). A real server-rendered article's body
    # dwarfs its in-main chrome, so it stays well above the threshold (no false escalation).
    for _t in main(["nav", "footer", "aside"]):
        _t.decompose()
    plain = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True)).strip()
    # Return MARKDOWN (Document.content contract) not flattened text; markdown length >= plain
    # for real content, so the caller's _THIN_CHARS escalation gate keeps firing on SPA shells and at
    # worst pays one fail-safe Jina render. try/except falls back to plain on pathological HTML.
    try:
        from markdownify import markdownify as _html_to_md
        text = strip_base64_images(_html_to_md(str(main), heading_style="ATX").strip())
    except Exception:  # noqa: BLE001 — markdownify can be picky; plain text is the safe fallback
        text = plain
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
    if _is_known_walled_deep_link(url):
        diag.note(
            "web_fallback",
            url=url,
            body="known walled deep link was not claimed by its source adapter; generic web fallback refused",
        )
        return None
    if cache.cache_only():
        return None  # respect the single egress guard

    key = cache.make_key("web", "fallback", url)
    cached = cache.get(key)
    if cached is not None:
        return Document(**cached) if isinstance(cached, dict) else cached

    # Plain fetch through the SSRF-pinned per-hop fetcher (safeurl.safe_fetch), NOT the shared
    # follow-redirects pool: this URL is attacker-influenceable (no adapter claimed it), so EVERY
    # redirect hop is IP-pinned + re-validated (a user URL that 302s to 169.254.169.254 can no longer
    # be followed). safe_fetch already read + decoded + capped the body, so we take res["text"]
    # directly (no httpx.Response handling here). Keep the old 30MB untrusted-read cap (http.MAX_BYTES)
    # so C2 changes ONLY the SSRF posture, not the body ceiling (safe_fetch's own default is 5MB).
    # max_redirects=20 preserves the OLD http.get ceiling (httpx's pooled default was 20, safe_fetch's
    # own default is 5): each of the 20 hops is still IP-pinned + revalidated, so a longer pinned chain
    # is safe and we do not silently reject a legitimate 6-to-20-hop redirect a real page used to reach.
    res = safeurl.safe_fetch(url, max_bytes=http.MAX_BYTES, max_redirects=20)
    if not res["ok"] and res["blocked_reason"] in _SSRF_REFUSE:
        # An SSRF-refused target (private_ip / bad_scheme / userinfo / bad_port): return None and do
        # NOT escalate to Jina. A benign failure (timeout / fetch_error / oversize / redirect_loop /
        # dns) is not in _SSRF_REFUSE, so it falls through to the Jina escalation like the old path.
        diag.note("web_fallback", url=url,
                  body=f"refused untrusted target (safe_fetch blocked_reason="
                       f"{res['blocked_reason']}); not escalating to Jina")
        return None
    plain_title, plain_text = "", ""
    # Gate the plain-extract on a 2xx status. safe_fetch returns ok=True for ANY terminal (non-3xx)
    # response regardless of status class, so a 4xx/5xx carries an error page in res["text"]. The OLD
    # http.get called raise_for_status(), turning a 4xx/5xx into None -> thin -> Jina; without this
    # gate we would extract the error body and could return it as a source='web' doc, never trying
    # Jina. Treating only a 2xx body as content leaves plain_text empty on a non-2xx, so the _THIN_CHARS
    # trigger below escalates to Jina exactly like the old raise_for_status path. (The SSRF-refuse
    # branch above is unchanged.)
    if res["ok"] and 200 <= (res["status"] or 0) < 300:
        ctype = (res["content_type"] or "").lower()
        # Only HTML is extraction-worthy here; a PDF/json/binary is some other adapter's job
        # (pdf_source already claimed *.pdf earlier in the loop), so do not mis-handle it.
        if "html" in ctype or ctype == "" or ctype.startswith("text/"):
            plain_title, plain_text = _extract_text(res["text"])

    via = "plain"
    title, content = plain_title, plain_text
    if _is_known_walled_shell(url, title, content):
        diag.note("web_fallback", url=url,
                  body="deep XHS URL resolved to a verified homepage/error shell, not note content")
        return None
    if len(plain_text) < _THIN_CHARS:
        # JS-wall / SPA shell (or a benign hard failure): escalate to the headless render, BUT never
        # launder a blocked target through Jina's server-side fetch. safe_fetch already refused an
        # SSRF-class target above; this is the belt-and-suspenders guard for the thin/benign-failure
        # path (security_block_reason is None for a dns miss, matching the "benign" fall-through).
        if _netguard.security_block_reason(url) is not None:
            diag.note("web_fallback", url=url,
                      body="thin plain read but URL is SSRF-blocked; not escalating to Jina")
            return None  # genuinely nothing we may safely fetch
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

    head = f"{title}\n{content[:1500]}"
    if any(m in head for m in _CHALLENGE_MARKERS):
        diag.note("web_fallback", url=url,
                  body=f"refused anti-bot challenge page (via={via}); if an adapter claims "
                       f"this host, it failed upstream and the URL leaked to the fallback")
        return None  # a challenge wall is a FAILURE, never content (and never cached)

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
