"""AlphaXiv — community-layered arXiv paper discussion (URL resolver).

AlphaXiv (alphaxiv.org) overlays community discussion + AI insights + paper
"blogs" on top of arXiv preprints. 2025-11 raised $7M seed from Menlo
Ventures + Haystack. Identified by sub-agent as new infrastructure with
unique signal (论文 in-context 讨论) not covered by OpenReview (审稿前评论)
or arXiv (无评论).

**Current implementation scope (2026-05-28)**: minimal URL resolver only.

Rationale for minimal scope:
- AlphaXiv exposes NO public unauthenticated REST/RSS API
  (verified 2026-05-28: /sitemap.xml 404, /api/* 404, /feed 308 → 404,
  only /health and root respond unauthenticated)
- The official programmatic channel is api.alphaxiv.org/mcp/v1 with
  **OAuth 2.0 + SSE transport** — wrapping requires Penumbra to act
  as an OAuth client, store/refresh tokens, and bridge MCP-in-MCP. This
  is high implementation cost (~100+ tool calls), questionable architecture
  (nested MCP servers), and the *unique* value is community discussion
  which alphaXiv MCP also requires user auth to access.
- Without community discussion access, alphaXiv ≈ arXiv mirror, and
  Penumbra already has a solid arXiv adapter.

What this adapter DOES provide:
- Recognize alphaxiv.org URLs (/abs/, /overview/, /paper/, /resources/)
- Extract arXiv ID from path
- Delegate to arXiv adapter for paper content
- Annotate the result with the alphaxiv.org URL as metadata, so a user
  can manually visit alphaxiv.org for community discussion + AI insights

What this adapter DOES NOT provide:
- search() over alphaXiv-specific content (returns empty; arXiv adapter
  is the recommended path for paper search)
- Community comments / AI Q&A / paper blogs (requires OAuth wrapping —
  upgrade path when alphaXiv exposes simpler public API)

Upgrade trigger: when alphaXiv exposes an unauthenticated REST/JSON API
for trending + community comments, OR when the deployer explicitly invests in
OAuth wrapping, expand this adapter to fetch full content.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

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


class AlphaXivAdapter:
    name = "alphaxiv"
    needs_credentials = False
    description = (
        "AlphaXiv — community-layered arXiv discussion (URL resolver only; "
        "full community / AI insights require OAuth MCP, not yet wrapped)"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # AlphaXiv has no public search API; arXiv adapter covers paper search.
        # Returning [] is intentional — search_many() handles empty gracefully.
        return []

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "alphaxiv.org" not in host:
            return None

        m = _ARXIV_ID_RE.search(urlparse(url).path)
        if not m:
            logger.debug("AlphaXiv URL has no extractable arxiv id: %s", url)
            return None
        arxiv_id = m.group("id")

        # Delegate to arxiv adapter via constructed arxiv URL
        from penumbra.core import fetcher
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

        # Re-brand as alphaxiv source + add metadata for community discussion access
        arxiv_doc.source = "alphaxiv"
        arxiv_doc.url = url  # keep the alphaxiv URL as canonical
        arxiv_doc.metadata = dict(arxiv_doc.metadata or {})
        arxiv_doc.metadata["arxiv_id"] = arxiv_id
        arxiv_doc.metadata["arxiv_url"] = arxiv_url
        arxiv_doc.metadata["alphaxiv_url"] = url
        arxiv_doc.metadata["community_discussion_url"] = url
        arxiv_doc.metadata["backend"] = "arxiv (alphaxiv community via browser)"
        if "alphaxiv-community" not in (arxiv_doc.tags or []):
            arxiv_doc.tags = (arxiv_doc.tags or []) + ["alphaxiv-community"]
        return arxiv_doc

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                "https://api.alphaxiv.org/health",
                timeout=10,
                follow_redirects=True,
                headers={"User-Agent": "penumbra/0.1"},
            )
            if resp.status_code == 200:
                return True, (
                    "OK (api.alphaxiv.org/health reachable; "
                    "community discussion via OAuth-only MCP, not wrapped)"
                )
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from penumbra.core.fetcher import register_adapter

register_adapter(AlphaXivAdapter())
