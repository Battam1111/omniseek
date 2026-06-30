"""GitHub Trending — recently active ML/AI repos via GitHub Search API.

GitHub's /trending/<language> HTML page is the canonical "what's hot in
open-source" surface, but lacks an official API. We use GitHub's Search
Repositories API (which IS official) with the same intent: recently
pushed + sorted by stars.

Egress routes through the shared ``_github`` client, so this source finally
sends the token (off the unauth ~10/min Search ceiling onto the shared token),
gets paced under the 30/min Search limit, and shares the 429 / Retry-After
breaker + the single-flight /rate_limit health probe with the other GitHub
adapters (it used to send NO token and have no pacing of its own).

This is DIFFERENT from `github_awesome_phd` adapter which surfaces curated
"awesome" lists. Here we surface dynamic / current-momentum repos.

Endpoint: GET /search/repositories?q=<query>+pushed:>YYYY-MM-DD&sort=stars
Response item: name, full_name, description, html_url, stargazers_count,
forks_count, language, topics[], owner.login, updated_at.

Migrated to ``BaseAPIAdapter`` (B2 template method): the cache/registration/
ranking boilerplate now lives in the base; the two hooks below carry every
source-specific fact verbatim. ``rank_locally=False`` because the GitHub
Search API already ranks server-side (sort=stars, order=desc) — the base
preserves that server order, exactly as the hand-written version did (it
never called keyword_score_filter).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import _github
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

TIMEOUT = 15


class GitHubTrendingAdapter(BaseAPIAdapter):
    name = "github_trending"
    needs_credentials = False
    description = (
        "GitHub Trending — recently active ML/AI repos by stars "
        "(via GitHub Search API; complement to github_awesome_phd curated lists)"
    )
    # The GitHub Search API already orders by stars server-side; the base must
    # preserve that order verbatim (the hand-written version never lexically
    # re-ranked) → rank_locally=False.
    rank_locally = False
    cache_ttl = 1800
    search_label = "search"

    def _raw_fetch(self, query: str, limit: int) -> list:
        # Constrain to repos pushed in the last 30 days. This is the "trending"
        # signal — we want active not stale. The caller can refine via query.
        days_back = 30
        pushed_filter = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        q = f"{query} pushed:>{pushed_filter}"

        data = _github.get_json(
            "/search/repositories",
            params={
                "q": q,
                "sort": "stars",
                "order": "desc",
                "per_page": min(limit, 30),
            },
            timeout=TIMEOUT,
        )
        if data is None:
            return []
        return data.get("items") or []

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if host != "github.com":
            # Don't claim raw.githubusercontent.com URLs (handled by github_awesome_phd)
            return None
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            return None
        # Skip nested paths like /owner/repo/pulls/123
        if len(parts) > 2 and parts[2] in ("pulls", "issues", "actions", "wiki", "blob", "tree"):
            return None
        owner, repo_name = parts[0], parts[1]
        repo = _github.get_json(
            f"/repos/{owner}/{repo_name}",
            timeout=TIMEOUT,
        )
        if repo is None:
            return None
        return self._to_document(repo)

    def health_check(self) -> tuple[bool, str]:
        # Delegate to the shared single-flight /rate_limit probe (one upstream call for all three
        # GitHub-backed sources, token-authenticated, 60s-cached) instead of an own unauth probe.
        return _github.health()

    def _to_document(self, repo: dict) -> Optional[Document]:
        full_name = repo.get("full_name") or repo.get("name") or "(unnamed)"
        url = repo.get("html_url") or f"https://github.com/{full_name}"
        description = repo.get("description") or "(no description)"
        stars = repo.get("stargazers_count") or 0
        forks = repo.get("forks_count") or 0
        language = repo.get("language") or ""
        topics = repo.get("topics") or []
        owner = (repo.get("owner") or {}).get("login")

        updated_at = repo.get("pushed_at") or repo.get("updated_at")
        date = None
        if updated_at:
            try:
                date = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        tags = list(topics)[:10]
        if language and language not in tags:
            tags.append(language)

        return Document(
            source="github_trending",
            source_id=str(repo.get("id") or full_name),
            url=url,
            title=full_name,
            content=description,
            author=owner,
            date=date,
            signals=mk_signal("stars", stars,
                              kind="engagement", by="github_trending/stars"),
            tags=tags,
            metadata={
                "full_name": full_name,
                "stars": stars,
                "forks": forks,
                "language": language,
                "topics": topics,
                "open_issues": repo.get("open_issues_count"),
                "raw": jsonsafe(repo),
            },
        )
