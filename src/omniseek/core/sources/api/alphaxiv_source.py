"""AlphaXiv — trending preprint buzz + per-paper community discussion + AI overview.

AlphaXiv (alphaxiv.org) overlays community discussion, AI-generated paper
"reports", and an attention/buzz layer on top of arXiv preprints. 2025-11
raised $7M seed from Menlo Ventures + Haystack. Its UNIQUE signal (论文 in-context
讨论 + preprint attention counts) is not covered by OpenReview (审稿前评论), arXiv
(无评论), or OpenAlex (citation-only).

This adapter reaches that signal through alphaXiv's KEYLESS public REST API
(api.alphaxiv.org): every endpoint below returns HTTP 200 with only a
User-Agent, no OAuth / account / key (verified keyless 2026-07-14). The earlier
"OAuth MCP only" rationale is FALSIFIED: the community layer is plain keyless
REST, so there is no need to wrap the OAuth+SSE MCP (which OmniSeek cannot refresh
tokens for, and which does not even expose the community layer).

Keyless endpoints wired here (base https://api.alphaxiv.org):
- TRENDING FEED  GET /papers/v3/feed?pageNum=0&pageSize=N&sort=Hot&interval=7 Days
  The unique additive signal: what preprints are drawing attention right now
  (visits + votes), which arXiv/OpenAlex cannot report. Surfaced by search("")
  (an empty/whitespace query = "what is hot").
- SEARCH         GET /search/v2/paper/fast?q=...&includePrivate=false
  A flat array of arXiv papers (redundant with the arxiv adapter, so kept
  minimal). Surfaced by search(<non-empty query>).
- PAPER+COMMENTS GET /papers/v3/legacy/{arxiv_id}
  The community discussion threads for a paper (fetch_url enrich).
- AI OVERVIEW    GET /papers/v3/{paper_version_id}/overview/en
  alphaXiv's AI-generated paper report (fetch_url enrich).

fetch_url keeps the arXiv-backed base document (delegates to the arxiv adapter
for the canonical abstract/metadata), then keyless-enriches it in place with the
community discussion + AI overview when they exist. Any enrich failure is
best-effort and never sinks the base document.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from omniseek.core import cache, http
from omniseek.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

BASE = "https://api.alphaxiv.org"
TIMEOUT = 15
# Cache: the "Hot 7 Days" trending feed drifts slowly (30min is fresh enough); a
# keyword search is redundant with arxiv so a shorter TTL keeps it cheap.
_TRENDING_TTL = 1800
_SEARCH_TTL = 900

# AlphaXiv canonical URL forms (sub-agent + manual verification):
# - https://www.alphaxiv.org/abs/<arxiv_id>
# - https://www.alphaxiv.org/overview/<arxiv_id>
# - https://www.alphaxiv.org/paper/<arxiv_id>
# - https://www.alphaxiv.org/resources/<arxiv_id>
# arxiv_id forms: "2402.11625", "2402.11625v1", "cs.CL/0612066" (older style)
_ARXIV_ID_RE = re.compile(
    r"/(?:abs|overview|paper|resources)/"
    r"(?P<id>\d{4}\.\d{4,6}(?:v\d+)?|[a-z\-]+/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)


def _parse_date(value) -> Optional[datetime]:
    """Best-effort parse of alphaXiv's first_publication_date / publication_date (ISO datetime OR
    bare 'YYYY-MM-DD'). None (not a fabricated date) on anything unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None


def _author_names(authors) -> list[str]:
    """Coerce the feed's authors[] into a list of display names WITHOUT inventing any: a plain
    string is a name; a dict contributes its name/full_name/username if present; anything else is
    dropped (never guessed)."""
    out: list[str] = []
    for a in authors or []:
        if isinstance(a, str) and a.strip():
            out.append(a.strip())
        elif isinstance(a, dict):
            n = a.get("name") or a.get("full_name") or a.get("username")
            if n:
                out.append(str(n))
    return out


def _org_names(orgs) -> list[str]:
    """organization_info[].name -> a clean list of org names (missing/blank skipped)."""
    out: list[str] = []
    for o in orgs or []:
        if isinstance(o, dict) and o.get("name"):
            out.append(str(o["name"]))
    return out


def _feed_paper_to_doc(p: dict) -> Document:
    """One /papers/v3/feed papers[] item -> a Document carrying the UNIQUE buzz signals
    (visits + votes as engagement facts). Raises on a paper with no universal_paper_id so the
    caller skips it rather than build a broken url."""
    pid = p.get("universal_paper_id")
    if not pid:
        raise ValueError("feed paper missing universal_paper_id")
    metrics = p.get("metrics") or {}
    visits = (metrics.get("visits_count") or {}).get("all")
    votes = metrics.get("public_total_votes")
    signals: dict = {}
    signals.update(mk_signal("views", visits, kind="engagement", by="alphaxiv/visits"))
    signals.update(mk_signal("votes", votes, kind="engagement", by="alphaxiv/votes"))

    topics = [t for t in (p.get("topics") or []) if isinstance(t, str)]
    orgs = _org_names(p.get("organization_info"))
    authors = _author_names(p.get("authors"))

    return Document(
        source="alphaxiv",
        source_id=str(pid),
        url=f"https://www.alphaxiv.org/abs/{pid}",
        title=p.get("title") or "(untitled)",
        content=p.get("abstract") or "",
        author=", ".join(authors) or None,
        date=_parse_date(p.get("first_publication_date")),
        signals=signals,
        tags=topics + orgs,
        metadata={
            "universal_paper_id": pid,
            "github_url": p.get("github_url"),
            "topics": topics,
            "organizations": orgs,
            "raw": jsonsafe(p),
        },
    )


def _search_row_to_doc(r: dict) -> Optional[Document]:
    """One /search/v2/paper/fast row -> a MINIMAL doc (this branch is redundant with the arxiv
    adapter, so it stays lean). None on a row with no usable link/id (no fabrication)."""
    link = r.get("link") or ""
    pid = r.get("paperId")
    if not link and not pid:
        return None
    return Document(
        source="alphaxiv",
        source_id=str(pid or link),
        url=f"https://www.alphaxiv.org{link}" if link.startswith("/") else (link or "https://www.alphaxiv.org"),
        title=r.get("title") or "(untitled)",
        content=r.get("snippet") or "",
        metadata={"paperId": pid, "link": link, "raw": jsonsafe(r)},
    )


def _compact_comments(comments) -> list[dict]:
    """The community threads folded to a compact, agent-facing shape: {title, body[:300], upvotes,
    author}. Only well-formed dicts survive."""
    compact: list[dict] = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        author = c.get("author")
        uname = author.get("username") if isinstance(author, dict) else None
        compact.append({
            "title": c.get("title"),
            "body": (c.get("body") or "")[:300],
            "upvotes": c.get("upvotes"),
            "author": uname,
        })
    return compact


def _comment_digest(compact: list[dict]) -> str:
    """A one-line 'Community discussion (N threads): ...' digest folded into the base doc content."""
    n = len(compact)
    heads = []
    for c in compact[:3]:
        h = (c.get("title") or c.get("body") or "").strip()
        if h:
            heads.append(h[:100])
    joined = " | ".join(heads)
    return f"Community discussion ({n} threads): {joined}" if joined else f"Community discussion ({n} threads)."


class AlphaXivAdapter:
    name = "alphaxiv"
    needs_credentials = False  # keyless public REST: only a User-Agent, no OAuth/account/key
    description = (
        "AlphaXiv — trending preprint buzz (Hot feed: visits + votes) + per-paper community "
        "discussion + AI-generated paper overview, all via the keyless api.alphaxiv.org REST API"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key("alphaxiv", "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        q = (query or "").strip()
        if not q:
            docs = self._trending(limit)   # empty/whitespace query -> "what is hot"
            ttl = _TRENDING_TTL
        else:
            docs = self._search_papers(q, limit)
            ttl = _SEARCH_TTL

        if docs is None:  # network failure (not an authoritative empty) -> honest [], don't cache
            return []
        cache.set_docs(key, docs, ttl=ttl)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (the fetcher's AsyncSearchCapable dispatch awaits this
        DIRECTLY, spending a coroutine on the network wait instead of a held pool thread). Off-loop
        discipline mirrors the shipped async sources (hackernews / declarative): the disk cache
        read/write go OFF the loop (anyio.to_thread), the egress awaits http.aget_json (the async
        wait stays on the loop via epoll), and the pure hit->doc mapping stays on the loop."""
        key = cache.make_key("alphaxiv", "search", query, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached

        q = (query or "").strip()
        if not q:
            docs = await self._atrending(limit)
            ttl = _TRENDING_TTL
        else:
            docs = await self._asearch_papers(q, limit)
            ttl = _SEARCH_TTL

        if docs is None:
            return []
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=ttl))
        return docs

    # -- trending feed (the unique buzz signal) --------------------------------------------------
    def _trending(self, limit: int) -> Optional[list[Document]]:
        data = http.get_json(
            f"{BASE}/papers/v3/feed",
            params={"pageNum": 0, "pageSize": limit, "sort": "Hot", "interval": "7 Days"},
            timeout=TIMEOUT,
        )
        return self._parse_feed(data, limit)

    async def _atrending(self, limit: int) -> Optional[list[Document]]:
        data = await http.aget_json(
            f"{BASE}/papers/v3/feed",
            params={"pageNum": 0, "pageSize": limit, "sort": "Hot", "interval": "7 Days"},
            timeout=TIMEOUT,
        )
        return self._parse_feed(data, limit)

    @staticmethod
    def _parse_feed(data, limit: int) -> Optional[list[Document]]:
        if data is None:
            return None  # network/parse failure -> sentinel, so search() returns [] uncached
        docs: list[Document] = []
        for p in (data.get("papers") if isinstance(data, dict) else None) or []:
            try:
                docs.append(_feed_paper_to_doc(p))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed alphaXiv feed paper: %s", exc)
            if len(docs) >= limit:
                break
        return docs

    # -- keyword search (redundant with arxiv, kept minimal) -------------------------------------
    def _search_papers(self, query: str, limit: int) -> Optional[list[Document]]:
        data = http.get_json(
            f"{BASE}/search/v2/paper/fast",
            params={"q": query, "includePrivate": "false"},
            timeout=TIMEOUT,
        )
        return self._parse_search(data, limit)

    async def _asearch_papers(self, query: str, limit: int) -> Optional[list[Document]]:
        data = await http.aget_json(
            f"{BASE}/search/v2/paper/fast",
            params={"q": query, "includePrivate": "false"},
            timeout=TIMEOUT,
        )
        return self._parse_search(data, limit)

    @staticmethod
    def _parse_search(data, limit: int) -> Optional[list[Document]]:
        if data is None:
            return None
        rows = data if isinstance(data, list) else ((data or {}).get("results") or [])
        docs: list[Document] = []
        for r in rows[:limit]:
            if not isinstance(r, dict):
                continue
            try:
                doc = _search_row_to_doc(r)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed alphaXiv search row: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
        return docs

    # -- per-URL drill: arxiv base doc + keyless community/overview enrich ------------------------
    def fetch_url(self, url: str) -> Optional[Document]:
        doc = self._arxiv_base_doc(url)
        if doc is None:
            return None
        arxiv_id = (doc.metadata or {}).get("arxiv_id")
        if arxiv_id:
            self._enrich_from_community(doc, arxiv_id)  # best-effort: never sinks the base doc
        return doc

    def _arxiv_base_doc(self, url: str) -> Optional[Document]:
        """The canonical arXiv-backed base document for an alphaxiv.org paper URL: delegate to the
        arxiv adapter for the abstract/metadata, then re-brand as an alphaxiv doc. None if the URL
        is not an alphaxiv paper URL or the arxiv delegation fails."""
        host = (urlparse(url).hostname or "").lower()
        if "alphaxiv.org" not in host:
            return None

        m = _ARXIV_ID_RE.search(urlparse(url).path)
        if not m:
            logger.debug("AlphaXiv URL has no extractable arxiv id: %s", url)
            return None
        arxiv_id = m.group("id")

        from omniseek.core import fetcher
        arxiv_adapter = fetcher.get_adapter("arxiv")
        if arxiv_adapter is None:
            logger.warning("arxiv adapter not available for AlphaXiv delegation")
            return None

        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
        try:
            arxiv_doc = arxiv_adapter.fetch_url(arxiv_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("arxiv delegation failed for %s: %s", arxiv_id, exc)
            return None
        if arxiv_doc is None:
            return None

        # Re-brand the arxiv doc as an alphaxiv result (keep the alphaxiv URL canonical).
        arxiv_doc.source = "alphaxiv"
        arxiv_doc.url = url
        arxiv_doc.metadata = dict(arxiv_doc.metadata or {})
        arxiv_doc.metadata["arxiv_id"] = arxiv_id
        arxiv_doc.metadata["arxiv_url"] = arxiv_url
        arxiv_doc.metadata["alphaxiv_url"] = url
        arxiv_doc.metadata["backend"] = "arxiv (base doc) + alphaxiv keyless REST (community + overview)"
        if "alphaxiv-community" not in (arxiv_doc.tags or []):
            arxiv_doc.tags = (arxiv_doc.tags or []) + ["alphaxiv-community"]
        return arxiv_doc

    def _enrich_from_community(self, doc: Document, arxiv_id: str) -> None:
        """Keyless-enrich the base doc with alphaXiv's community discussion + AI overview. Every
        step is best-effort: a failed/empty part is simply skipped and never sinks the base doc."""
        legacy = http.get_json(f"{BASE}/papers/v3/legacy/{arxiv_id}", timeout=TIMEOUT)
        if not isinstance(legacy, dict):
            return

        # (a) community discussion threads
        try:
            compact = _compact_comments(legacy.get("comments"))
            if compact:
                doc.metadata["alphaxiv_comments"] = compact
                doc.content = ((doc.content or "").rstrip() + "\n\n" + _comment_digest(compact)).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("alphaXiv comment enrich skipped for %s: %s", arxiv_id, exc)

        # (b) AI overview (needs the paper_version id from the legacy payload)
        try:
            version = (legacy.get("paper") or {}).get("paper_version") or {}
            version_id = version.get("id")
            if version_id:
                ov = http.get_json(f"{BASE}/papers/v3/{version_id}/overview/en", timeout=TIMEOUT)
                overview = (ov or {}).get("overview") if isinstance(ov, dict) else None
                if overview:
                    doc.metadata["alphaxiv_overview"] = overview
                    excerpt = overview.strip()
                    if len(excerpt) > 500:
                        excerpt = excerpt[:500].rstrip() + "…"
                    doc.content = ((doc.content or "").rstrip()
                                   + "\n\n" + f"alphaXiv AI overview: {excerpt}").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("alphaXiv overview enrich skipped for %s: %s", arxiv_id, exc)

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{BASE}/papers/v3/feed",
                # pageNum is REQUIRED by /papers/v3/feed: without it the endpoint 400s (a false
                # health-down while keyword search + the trending feed both work when pageNum is
                # present, verified 2026-07-23). Mirror _trending's params exactly.
                params={"pageNum": 0, "pageSize": 1, "sort": "Hot", "interval": "7 Days"},
                timeout=10,
                follow_redirects=True,
                headers={"User-Agent": "omniseek/0.1"},
            )
            if resp.status_code == 200 and isinstance(resp.json().get("papers"), list):
                return True, "OK (keyless /papers/v3/feed reachable; papers[] parses)"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from omniseek.core.fetcher import register_adapter

register_adapter(AlphaXivAdapter())
