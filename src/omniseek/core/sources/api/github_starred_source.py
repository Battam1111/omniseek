"""GitHub starred: the repos one GitHub user starred, newest star first (``starred_at``).

Feeds the shadow run (``shadow_feed = True``): a daily sensor over this source finds the new
stars and the sensor writes them to ``~/.omniseek/state/shadow_feed/github_starred.jsonl`` (format in
``omniseek.core.shadow_feed``). The digest's own GitHub-stars puller keeps running until the
comparison passes.

Endpoint: ``GET /users/<user>/starred?per_page=100&sort=created&direction=desc`` with
``Accept: application/vnd.github.star+json``, so each row is ``{starred_at, repo}``. Egress goes
through the shared ``_github`` client (OmniSeek's GitHub token, pacing, 429 breaker), the way every
GitHub-backed source does.

Increment by ``starred_at``: a run lists the stars of the last 30 days (at most 3 pages) and the
sensor's (source, source_id) baseline decides which are new. No watermark is kept here on purpose:
a named drill would advance it and the sensor would then miss what the drill saw.

The user is the digest puller's (``Battam1111``, its ``config.example.json``); ``$EYE_GITHUB_STARRED_USER``
overrides it. source_id is the repo full name (the comparison's primary key).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Optional
from urllib.parse import quote

from omniseek.core import _github, diag
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter, mk_signal
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

DEFAULT_USER = "Battam1111"
ENV_USER = "EYE_GITHUB_STARRED_USER"
PER_PAGE = 100
MAX_PAGES = 3
LOOKBACK_DAYS = 30
TIMEOUT = 20
STAR_ACCEPT = "application/vnd.github.star+json"
_WILDCARD = {"", "*", "all"}


def starred_user() -> str:
    return os.environ.get(ENV_USER, "").strip() or DEFAULT_USER


def _parse(value) -> Optional[_dt.datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class GitHubStarredAdapter(BaseAPIAdapter):
    name = "github_starred"
    description = (
        "GitHub starred: the repos one GitHub user starred in the last 30 days, newest star first "
        "(repo, description, language, stars, starred_at). Personal feed for a digest shadow run."
    )
    needs_credentials = True
    explicit_only = (
        "one user's personal star list for a digest shadow run: named drill or its sensor only, "
        "never the broad sweep"
    )
    kind = "stream"
    domains = ["bookmarks", "code"]
    regions: list = []
    modes = ["MONITOR", "RECALL"]
    rank_locally = False
    cache_ttl = 1800
    search_label = "starred"
    sensor_window = PER_PAGE * MAX_PAGES
    shadow_feed = True

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # One cache entry and one upstream run serve every query and limit (keyed on the user so an
        # override never reads another user's cache); a non-wildcard query filters lexically.
        docs = super().search(f"user:{starred_user()}", self.sensor_window)
        q = (query or "").strip().lower()
        if q not in _WILDCARD:
            docs = keyword_score_filter(docs, query)
        return docs[:limit]

    def _raw_fetch(self, query: str, limit: int) -> list:
        user = starred_user()
        cutoff = _now() - _dt.timedelta(days=LOOKBACK_DAYS)
        rows: list = []
        for page in range(1, MAX_PAGES + 1):
            data = _github.get_json(
                f"/users/{quote(user, safe='')}/starred",
                params={"per_page": PER_PAGE, "sort": "created", "direction": "desc", "page": page},
                headers={"Accept": STAR_ACCEPT},
                timeout=TIMEOUT,
            )
            if not isinstance(data, list):
                if page == 1:
                    raise RuntimeError(f"github_starred: listing the stars of {user} failed")
                diag.note("github_starred.list", body=f"page {page} failed; kept the {len(rows)} stars before it")
                break
            older = False
            for row in data:
                if not isinstance(row, dict) or not isinstance(row.get("repo"), dict):
                    continue
                t = _parse(row.get("starred_at"))
                if t is None:
                    # Without starred_at the Accept header did not take: refuse the page rather than
                    # pass off plain repos as new stars.
                    raise RuntimeError("github_starred: a row lacks starred_at (star+json Accept not honoured)")
                if t < cutoff:
                    older = True
                    break
                rows.append(row)
            if older or len(data) < PER_PAGE:
                break
        return rows

    def health_check(self) -> tuple[Optional[bool], str]:
        return _github.health()

    def _to_document(self, row: dict) -> Optional[Document]:
        repo = row.get("repo") or {}
        full_name = repo.get("full_name")
        if not full_name:
            return None
        topics = repo.get("topics") or []
        language = repo.get("language") or ""
        tags = list(topics)[:10]
        if language and language not in tags:
            tags.append(language)
        stars = repo.get("stargazers_count") or 0
        return Document(
            source="github_starred",
            source_id=full_name,
            url=repo.get("html_url") or f"https://github.com/{full_name}",
            title=full_name,
            content=repo.get("description") or "",
            author=(repo.get("owner") or {}).get("login"),
            date=_parse(row.get("starred_at")),
            signals=mk_signal("stars", stars, kind="engagement", by="github_starred/stars"),
            tags=tags,
            metadata={
                "starred_at": row.get("starred_at"),
                "starred_by": starred_user(),
                "full_name": full_name,
                "language": language,
                "topics": topics,
                "stars": stars,
                "pushed_at": repo.get("pushed_at"),
                "raw": jsonsafe({k: repo.get(k) for k in (
                    "id", "full_name", "html_url", "description", "language", "topics",
                    "stargazers_count", "forks_count", "pushed_at", "created_at")}),
            },
        )
