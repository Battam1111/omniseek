"""Google Patents: prior-art / patent search via the keyless XHR query endpoint.

Ordinary web search is weak at prior-art: patents are long, formulaic, and live behind a
JS app, so a plain query rarely surfaces the right publication. Google Patents has an
undocumented keyless XHR JSON endpoint that the patents.google.com front-end itself calls,
which returns structured hits (publication number, title, assignee, inventor, dates, a
highlighted snippet) for any query. This adapter wraps that endpoint so Penumbra can do
patent / prior-art lookup that the open web cannot.

Endpoint (probed live, undocumented):

    GET https://patents.google.com/xhr/query?url=q%3D<urlencoded query>&exp=

i.e. the ``url`` query-param is itself a URL-encoded ``q=<query>`` string (double-encoded:
the query is encoded once for the inner ``q=...`` and the whole ``q=...`` is encoded again
for the outer ``url=...``). A real browser User-Agent is required (the Penumbra UA is
403-gated), so this adapter passes a Chrome UA through the shared http client.

Response shape (confirmed against several live queries):

    {"results": {"total_num_results": N, "cluster": [{"result": [
        {"id": ..., "rank": ..., "patent": {
            "publication_number": "US11942620B2",
            "title": " <b>Solid state battery</b> ... &hellip;",   # leading space + HTML entities + <b> tags
            "snippet": " In the instances of <b>solid-state batteries</b> ...",  # may be ""
            "assignee": "GM Global Technology Operations LLC",
            "inventor": "Kohei IJIRO",
            "priority_date": "2022-01-31", "filing_date": ..., "publication_date": ...,
            "publication_number": ..., "language": "en", "pdf": "f8/02/.../US11942620.pdf",  # relative
            "thumbnail": "", "figures": null,
        }}, ...
    ]}]}}

Titles/snippets carry HTML entities (``&hellip;``, ``&amp;``) and ``<b>`` highlight tags;
we unescape + strip tags with stdlib only (no bs4 / markdownify, to keep this source
dependency-free per the no-new-deps rule). The canonical patent page is
``https://patents.google.com/patent/<publication_number>``; the relative ``pdf`` path
resolves under ``https://patentimages.storage.googleapis.com/``.

The body is normally bare JSON, but XHR endpoints sometimes prefix an anti-hijack token
(``)]}'`` or ``while(1);``); ``_parse_body`` strips to the first ``{`` and parses robustly.

BaseScrapeAdapter (template method): the cache check / atomic set_docs / self-registration
ritual lives in the base; this adapter fills the two hooks (_raw_fetch = the XHR GET with a
browser UA; _to_documents = the patent -> Document map). ``rank`` stays default-False:
Google Patents returns its own server-relevance order, kept byte-faithful here.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

XHR_URL = "https://patents.google.com/xhr/query"
PATENT_PAGE_BASE = "https://patents.google.com/patent"
PDF_BASE = "https://patentimages.storage.googleapis.com"
# patents.google.com 403s the default Penumbra UA; the XHR endpoint wants a real browser UA.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 15

_TAG_RE = re.compile(r"<[^>]+>")  # strip <b>...</b> highlight tags (and any stray markup)


class GooglePatentsAdapter(BaseScrapeAdapter):
    name = "google_patents"
    needs_credentials = False
    description = "Google Patents: patent / prior-art search (publication, assignee, inventor, abstract) via the keyless XHR endpoint"
    cache_ttl = 900
    # patents.google.com anti-automation 503s a bare HTTP request from our datacenter IP, so the
    # data path falls back to the shared CDP Chrome (a same-origin in-page fetch that the
    # anti-automation does NOT gate). That uses the precious shared browser, so keep this
    # named-only (out of the broad fan-out): query patents deliberately, by name.
    explicit_only = "patents.google.com anti-automation: data path may use the shared CDP Chrome"

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["patents", "code"]
    modes = ["STRUCTURE", "UNWALL"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # The ``url`` param is a URL-encoded ``q=<query>`` string (the inner query is itself
        # encoded), so the live wire is ``url=q%3D<double-encoded query>``.
        inner = "q=" + quote(query)
        body = http.get_text(
            XHR_URL, params={"url": inner, "exp": ""},
            headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT,
        )
        parsed = _parse_body(body) if body else None
        if isinstance(parsed, dict) and (parsed.get("results") or {}).get("cluster"):
            return parsed
        # Bare HTTP got 503'd (anti-automation flags our datacenter IP) or returned nothing:
        # fall back to the real shared Chrome, which fetches the SAME xhr/query endpoint from
        # inside the patents.google.com origin (real browser session, cookies, JS) and is NOT
        # anti-automation-gated even from the same IP (verified live 2026-06-17).
        return self._cdp_fetch(query)

    def _cdp_fetch(self, query: str) -> Optional[Any]:
        """Resolve the XHR via a same-origin in-page fetch in the shared CDP Chrome."""
        from penumbra.core.sources.walled._cdp import cdp_call

        def _flow(page):
            # Establish the patents.google.com ORIGIN with wait_until='commit' (returns as soon as
            # the navigation commits), NOT cdp_call's default domcontentloaded goto — that heavy
            # SPA can exceed the 30s goto timeout in the shared Chrome (observed live). A
            # same-origin fetch needs the origin + cookies, not a fully-rendered DOM.
            page.goto("https://patents.google.com/", wait_until="commit", timeout=20000)
            js = (
                "async (q) => {"
                "  const inner = 'q=' + encodeURIComponent(q);"
                "  const r = await fetch('/xhr/query?url=' + encodeURIComponent(inner) + '&exp=',"
                "                        {headers: {'Accept': 'application/json'}});"
                "  return await r.text();"
                "}"
            )
            return page.evaluate(js, query)

        try:
            body = cdp_call(_flow, initial_url=None)  # navigate inside _flow with a fast 'commit' wait
        except Exception as exc:  # noqa: BLE001 — CDP/network failure degrades to a miss (-> [])
            logger.warning("google_patents: CDP fallback failed: %s", exc)
            return None
        return _parse_body(body)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        results = (raw.get("results") or {})
        clusters = results.get("cluster") or []
        docs: list[Document] = []
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            for item in cluster.get("result") or []:
                if len(docs) >= limit:
                    return docs
                if not isinstance(item, dict):
                    continue
                doc = self._item_to_doc(item)
                if doc is not None:
                    docs.append(doc)
        return docs

    def _item_to_doc(self, item: dict) -> Optional[Document]:
        patent = item.get("patent")
        if not isinstance(patent, dict):
            return None

        number = (patent.get("publication_number") or "").strip()
        if not number:
            return None

        title = _clean(patent.get("title")) or "(untitled patent)"
        snippet = _clean(patent.get("snippet"))
        assignee = _clean(patent.get("assignee")) or None
        inventor = _clean(patent.get("inventor")) or None

        url = f"{PATENT_PAGE_BASE}/{number}"
        date = _parse_date(patent.get("priority_date"))

        # content: the snippet (the closest thing to an abstract this endpoint returns),
        # plus the structured header so a bare-snippet result is still self-describing.
        content_lines: list[str] = []
        if snippet:
            content_lines.append(snippet)
        header: list[str] = [f"Publication: {number}"]
        if assignee:
            header.append(f"Assignee: {assignee}")
        if inventor:
            header.append(f"Inventor(s): {inventor}")
        prio = _clean(patent.get("priority_date"))
        if prio:
            header.append(f"Priority date: {prio}")
        content_lines.append("  |  ".join(header))
        content = "\n\n".join(content_lines)

        # the relative pdf path resolves under the patentimages storage host.
        pdf_rel = (patent.get("pdf") or "").strip()
        pdf_url = f"{PDF_BASE}/{pdf_rel}" if pdf_rel else None

        # thumbnail (when present) is a viewable patent figure image.
        media: list[str] = []
        thumb = (patent.get("thumbnail") or "").strip()
        if thumb:
            media.append(thumb if thumb.startswith("http") else f"{PDF_BASE}/{thumb}")

        metadata: dict[str, Any] = {
            "number": number,
            "inventor": inventor,
            "assignee": assignee,
            "priority_date": _clean(patent.get("priority_date")) or None,
            "filing_date": _clean(patent.get("filing_date")) or None,
            "publication_date": _clean(patent.get("publication_date")) or None,
            "grant_date": _clean(patent.get("grant_date")) or None,
            "language": patent.get("language") or None,
            "pdf_url": pdf_url,
            "raw": jsonsafe(item),
        }

        return Document(
            source=self.name,
            source_id=number,
            url=url,
            title=title,
            content=content,
            author=assignee,  # the assignee is the patent's owning entity (the "author")
            date=date,
            # no signals: the XHR endpoint reports no citation / engagement count, and an
            # empty-valued Signal is pure noise (the numbers-through-mk_signal rule is for
            # ACTUAL numbers; this source has none to report).
            media=media,
            metadata=metadata,
        )


def _parse_body(body: str) -> Optional[Any]:
    """Parse the XHR body robustly. It is normally bare JSON, but XHR endpoints sometimes
    prefix an anti-hijack token ()]}' / while(1);), so strip to the first '{' before parsing."""
    if not body:
        return None
    text = body.strip()
    if not text.startswith("{"):
        brace = text.find("{")
        if brace == -1:
            return None
        text = text[brace:]
    try:
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001: a malformed body degrades to a miss (-> [])
        logger.warning("google_patents: body parse failed: %s", exc)
        return None


def _clean(s: Any) -> str:
    """Decode HTML entities and strip <b> highlight tags from a Google Patents string.
    Stdlib only (html.unescape + a tag-stripping regex) to stay dependency-free."""
    import html as _html

    if not isinstance(s, str) or not s.strip():
        return ""
    out = _TAG_RE.sub("", s)
    out = _html.unescape(out)
    return out.strip()


def _parse_date(s: Any) -> Optional[datetime]:
    """priority_date is ISO YYYY-MM-DD (sometimes a year or year-month)."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
