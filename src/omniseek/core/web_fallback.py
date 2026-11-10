"""Render-fallback for ad-hoc URLs no adapter claims: plain fetch, then Jina on a thin page.

``fetcher.afetch_url_with_reason`` (the ``omniseek_read`` URL path) tries every registered adapter until one
CLAIMS the URL. ~167 adapters cover specific sources, but there is NO generic "read any web
page" adapter, so a URL outside every adapter (a Next.js / SPA marketing page, a JS-walled
doc, an arbitrary blog) returns ``matched=false`` today: OmniSeek does not reach it at all.

This is the LAST RESORT, invoked by ``fetch_url`` ONLY after the adapter loop claims nothing,
so it costs the happy path zero: a claiming adapter returns first and this never runs.

Two-tier, cheapest-first:
  1. Plain ``http.get`` + a static-HTML text extraction (the same bs4 path news_scraper uses).
     If the page is server-rendered, that text is the whole content and we stop (no 2nd call).
     A JSON answer (``application/json`` or a ``+json`` type) is data, not a page: its body is the
     content as is (pretty-printed when it parses), tagged ``format:json``, never sent to Jina.
  2. Only if the extracted text is THIN (a JS-wall / SPA shell whose body is client-rendered),
     re-fetch through ``https://r.jina.ai/<url>`` (Jina Reader runs real headless Chrome and
     returns LLM-ready markdown; free, keyless at 20 RPM, no key wired). We return that markdown.
A page that is not there is no content on either tier: a plain 404 / 410 (or a 400 / 422, the request
itself refused) stops before Jina, and a Jina render whose header says the target answered 4xx / 5xx is
that error page, never a read.

THE RAZOR: this RETRIEVES and returns raw page text / markdown. It renders NO judgment about
what the page MEANS; the agent reads the returned content and judges. The thin-trigger is a
mechanical char-count of extracted text, not a judgment of value. No LLM, no summarizer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

from omniseek.core import _netguard, cache, diag, dns_absence, http, safeurl
from omniseek.core._guard import counts_against_upstream
from omniseek.core.normalize import Document, jsonsafe, strip_base64_images

logger = logging.getLogger(__name__)

# Below this many chars of EXTRACTED visible text, a plain fetch is treated as a JS-wall / SPA
# shell (nav + boilerplate, no body) and we escalate to the Jina render. A real server-rendered
# article clears this easily; a client-rendered shell does not. Mechanical, not a value judgment.
_THIN_CHARS = 600
_JINA_ENDPOINT = "https://r.jina.ai/"
_JINA_TIMEOUT = 30          # headless-Chrome render is slower than a plain GET
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

# Distinctive anti-bot interstitial phrases (Cloudflare / generic CAPTCHA walls / 知乎 / Anubis).
# Deliberately narrow: each string is challenge-page boilerplate no real article carries.
# Anubis (Techaro's proof-of-work wall) answers 200 text/html; seen 2026-11-03 on
# inria.hal.science/<id>/document, which came back as a matched render:plain read. Its static asset
# path is in every such page and survives a site rewriting the default sentence.
# SiteGround's IP-reputation wall answers 202 with a bare meta refresh to /.well-known/sgcaptcha/
# (seen 2026-11-07 on lemire.me and theprofessorisin.com from OmniSeek's exit); that fixed path is
# what a render of the wall carries (Jina's "URL Source:" line, for one), whatever the page says.
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
    "set up Anubis to protect the server",
    "/.within.website/x/cmd/anubis/",
    "/.well-known/sgcaptcha/",
    # OpenReview's browser check (Cloudflare Turnstile behind its own page), seen 2026-11-09: the site
    # answers 307 to /challenge?redirect=<path>, titled "Verifying your browser". The redirect path is
    # the fixed part (checked on the redirect chain too, _challenge_redirect); the sentence catches a
    # render or a cached copy that no longer carries the address.
    "/challenge?redirect=",
    "Complete the check below to continue to OpenReview",
)

# Error screens a site's own framework shows when the page failed to load. Seen 2026-09-25 (routing
# audit): a Jina render came back reading "This page couldn’t load" (U+2019), was returned as a
# clean read and cached for an hour. That text is the h1 of Next.js's built-in error screen (next.js
# packages/next/src/client/components/builtin/global-error.tsx), whose document has no <title>.
# Older Next.js (the v14.2 default GlobalError in error-boundary.tsx) shows "Application error: a
# client-side / server-side exception has occurred" instead. Refused only when the page body is
# thin, i.e. the screen IS the page: an article quoting the message keeps its real body and is
# read as before.
_ERROR_SCREEN_MARKERS = (
    "This page couldn’t load",
    "Application error: a client-side exception has occurred",
    "Application error: a server-side exception has occurred",
)

# HTTP says the page is not there (RFC 9110 15.5.5 404 Not Found, 15.5.11 410 Gone): the server has no
# representation of it, so a headless render cannot make one, and the body sent is the site's own
# error page. Seen 2026-11-04: https://www.python.org/<a missing path> answered 404, went to Jina as a
# thin plain read and came back as a matched render:jina doc titled "Welcome to Python.org". Jina is
# another channel to the same server, so OmniSeek's one rule for retries and channels decides
# (_guard.counts_against_upstream, EYE-RETRY-404): 404 / 410, and 400 / 422 (the request itself refused),
# are the server's answer and do not go to Jina.
_NOT_THERE = (404, 410)
# safe_fetch failures with no answer from the host: the connection or TLS handshake (fetch_error, timeout)
# or the local resolver (dns). Only these ask public DNS whether the name exists (dns_absence).
_NO_ANSWER = ("fetch_error", "timeout", "dns")
# Jina's header block names the status its headless Chrome got for the target, in a line
# "Warning: Target URL returned error 404: Not Found". Any 4xx / 5xx there means the render IS an
# error page (not found, refused, rate limited, server error), whatever the plain fetch saw.
_JINA_TARGET_ERROR = re.compile(r"^Warning: Target URL returned error (\d{3})", re.M)


def challenge_marker(title: str, content: str) -> Optional[str]:
    """The _CHALLENGE_MARKERS entry in ``title`` or the head of ``content`` (where a wall shows it), or
    None. The one test of "this page is a bot check": a live read and a cached read both ask it."""
    head = f"{title or ''}\n{(content or '')[:1500]}"
    return next((m for m in _CHALLENGE_MARKERS if m in head), None)


def _challenge_redirect(res: dict) -> Optional[str]:
    """The address a plain fetch was sent to that is a challenge page (a redirect hop or the final URL
    carrying a _CHALLENGE_MARKERS entry), or None."""
    hops = [h.get("to") or "" for h in (res.get("redirect_chain") or []) if isinstance(h, dict)]
    for target in [*hops, res.get("final_url") or ""]:
        if any(m in target for m in _CHALLENGE_MARKERS):
            return target
    return None


def _site_readers(url: str) -> list[str]:
    """Names of OmniSeek's sources declared on ``url``'s site (upstreams.json), the ones to read it
    through when the site walls the generic read; [] when none is."""
    try:
        from omniseek.core import fetcher, upstreams
        return [a.name for a in fetcher.fetch_url_route(url)
                if getattr(a, "name", None) and upstreams.source_on_site(a.name, url, fetcher.backend_of(a.name))]
    except Exception as exc:  # noqa: BLE001 - naming a way round must never break the refusal
        logger.debug("web_fallback: no site readers for %s: %s", url, exc)
        return []


def challenge_reason(url: str, via: str, marker: str) -> str:
    """The reason a read refused as a bot check carries: blocked, by what, and which source to use."""
    readers = _site_readers(url)
    instead = (f"read it through the {', '.join(readers)} source instead (omniseek_read again once it is "
               f"healthy, or omniseek_search with sources={readers!r})" if readers
               else "no source of OmniSeek covers this site")
    return (f"blocked: the site answered an anti-bot challenge page, not the content (via={via}, shows "
            f"{marker!r}); {instead}")


def _jina_target_error(md: Optional[str]) -> Optional[int]:
    """The 4xx / 5xx status Jina's header block reports for the target, or None. Only the header block
    (before "Markdown Content:") is read, so an article quoting that line is not taken for one."""
    if not md:
        return None
    m = _JINA_TARGET_ERROR.search(md.split("Markdown Content:", 1)[0])
    return int(m.group(1)) if m and int(m.group(1)) >= 400 else None


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
    if host in {"muchong.com", "www.muchong.com", "emuch.net", "www.emuch.net"}:
        return bool(re.search(r"/t-\d+", parsed.path or ""))
    if host not in {"xiaohongshu.com", "www.xiaohongshu.com", "rednote.com", "www.rednote.com"}:
        return False
    return bool(re.search(r"/(?:explore|discovery/item|search_result|mobile/question)/", parsed.path or ""))


def _extract_text(html: str) -> tuple[str, str]:
    """(title, markdown) from a static HTML page: OmniSeek's one whole-page extraction (this
    fallback and news_scraper's fetch_url both call it; a fragment of markup goes through
    normalize.html_fragment_text instead). Strips script/style, prefers <main>/<article>/<body>,
    drops nav/footer/aside inside it. Returns ('','') if bs4 is absent."""
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


def _is_json_type(ctype: str) -> bool:
    """``application/json`` or any ``+json`` structured-syntax type (RFC 6839), parameters aside."""
    media = ctype.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


def _json_text(raw: str) -> str:
    """A JSON body as content: pretty-printed (2-space indent) when it parses, else as it came."""
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except ValueError:
        return raw


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
    Returns a Document(source='web') or None (None keeps omniseek_add_url matched=false)."""
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
    # No page cache of its own: its one caller (fetcher.fetch_url_with_reason) keeps successful reads in
    # the whole-URL read cache, and a second copy kept credential-bearing addresses on disk
    # (EYE-CACHE-SECRET-R2, 2026-11-03).

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
    if not res["ok"] and res["blocked_reason"] == "rate_gate":
        # A declared upstream's gate (upstreams.json) did not admit the read in time. Escalating to
        # Jina would fetch the same URL through Jina's servers, around that upstream's published
        # limit, so stop here: nothing was sent, try again later.
        diag.note("web_fallback", url=url,
                  body="declared upstream gate did not admit the read in time; not escalating to Jina")
        return None
    if not res["ok"] and res["blocked_reason"] in _SSRF_REFUSE:
        # An SSRF-refused target (private_ip / bad_scheme / userinfo / bad_port): return None and do
        # NOT escalate to Jina. A benign failure (timeout / fetch_error / oversize / redirect_loop /
        # dns) is not in _SSRF_REFUSE, so it falls through to the Jina escalation like the old path.
        diag.note("web_fallback", url=url,
                  body=f"refused untrusted target (safe_fetch blocked_reason="
                       f"{res['blocked_reason']}); not escalating to Jina")
        return None
    if not res["ok"] and res["blocked_reason"] in _NO_ANSWER and res["status"] is None:
        # Failed at the connection or TLS step (or the local resolver): the host machine's fake-ip proxy makes a
        # name that does not exist fail exactly like a broken network. Ask public DNS once; NXDOMAIN is
        # a settled answer (final: no Jina, no retry, not an upstream fault). Else the old path below.
        chain = res.get("redirect_chain") or []
        gone = dns_absence.absent(urlparse(chain[-1]["to"] if chain else url).hostname or "")
        if gone:
            diag.note("web_fallback", url=url, body=gone, final=True)
            return None
    if res["ok"] and res["status"] and not counts_against_upstream(status=res["status"]):
        said = ("this page does not exist" if res["status"] in _NOT_THERE
                else "the request is wrong")
        diag.note("web_fallback", url=url,
                  body=f"HTTP {res['status']}: the server says {said}; not escalating to Jina")
        return None  # not there is a FAILURE, never content (and never cached)
    walled_at = _challenge_redirect(res)
    if walled_at:
        # Sent to a challenge page: what comes back (and a Jina render of the same URL) is the wall.
        marker = next(m for m in _CHALLENGE_MARKERS if m in walled_at)
        diag.note("web_fallback", url=url, body=challenge_reason(url, "plain", marker))
        return None  # a challenge wall is a FAILURE, never content (and never cached)
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
        if _is_json_type(ctype) and (res["text"] or "").strip():
            # JSON is data, not a page to render: the body IS the content. Jina would hand back a
            # rendering of it that no longer parses as JSON.
            raw = res["text"]
            doc = Document(
                source="web", source_id=url, url=url, title=url, content=_json_text(raw),
                tags=["web", "render:plain", "format:json"],
                metadata={"raw": jsonsafe({"url": url, "rendered_via": "plain", "format": "json",
                                           "content_type": ctype, "plain_chars": len(raw)})},
            )
            return doc
        # Only HTML is extraction-worthy here; a PDF/binary is some other adapter's job
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
        err = _jina_target_error(md)
        if err is not None:
            diag.note("web_fallback", url=url,
                      body=f"the Jina render is an error page (the target answered HTTP {err}), not content")
            return None  # an error page is a FAILURE, never content (and never cached)
        if md and len(md) > len(plain_text):
            via, content = "jina", md[:_MAX_CHARS]
            if not title:
                # Rest of the "Title:" line only: \s* after the colon crossed the line break, so an
                # empty title took the next header line ("URL Source: ...") as the title.
                m = re.match(r"\s*Title:(.*)", md)
                if m:
                    title = m.group(1).strip()

    if not content:
        diag.note("web_fallback", url=url,
                  body=f"plain+jina both thin (plain={len(plain_text)} chars)")
        return None  # genuinely nothing: keep matched=false, never a fake empty doc

    head = f"{title}\n{content[:1500]}"
    marker = challenge_marker(title, content)
    if marker:
        diag.note("web_fallback", url=url, body=challenge_reason(url, via, marker))
        return None  # a challenge wall is a FAILURE, never content (and never cached)

    # A thin page carrying a known error screen IS that screen. For a Jina render the body is what
    # follows its header block, so a long URL or a Warning line there does not count as body.
    body = content.split("Markdown Content:", 1)[-1] if via == "jina" else content
    screen = next((m for m in _ERROR_SCREEN_MARKERS if m in head), None)
    if screen and len(body.strip()) < _THIN_CHARS:
        diag.note("web_fallback", url=url,
                  body=f"refused an error screen, not the page (via={via}, shows {screen!r}): "
                       f"the page failed to load, so there is no content to return")
        return None  # a failed load is a FAILURE, never content (and never cached)

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
    return doc
