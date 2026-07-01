"""GitHub *platform* adapter — code search + issues/PRs + discussions + org/user activity.

Complements the two existing GitHub adapters without overlapping them:
  - ``github_trending``  — repo discovery by stars (``/search/repositories``)
  - ``github_releases``  — infra release feeds (release.atom)
This one covers the platform surfaces those don't:
  1. **code search**       ``GET /search/code``           (auth-required, ~9 req/min)
  2. **issues + PRs**      ``GET /search/issues``          (30 req/min)
  3. **discussions**       GraphQL ``search(type:DISCUSSION)`` (auth-required)
  4. **org/user activity** newest repos for an owner       (``core`` bucket)

``search(query)`` routing:
  - a bare ``org:NAME`` / ``user:NAME`` (no other terms) → that owner's NEWEST repos
    (the "what did they just publish" activity-tracking intent; also what the
    watchtower polls).
  - anything else → code + issues/PR + discussions, fetched SERIALLY (GitHub asks
    clients to avoid concurrent requests — secondary rate limits) and round-robin
    merged so the result is a mix, capped at ``limit``.

GitHub search qualifiers (``org:`` ``repo:`` ``language:`` ``path:`` ``is:`` ``label:``
``state:`` …) pass straight through ``q`` to every surface.

Token: ``~/.polaris/credentials/github.json`` → ``{"token": "..."}`` (classic *or*
fine-grained — both accept ``Authorization: Bearer``). Without a token, code-search +
discussions are skipped (issues/repos still work at the anonymous 60/h limit); the
adapter never hard-fails on a missing token.

REST egress (search/issues, search/code, repos, git/trees) routes through the shared
``_github`` client: the token, the Search-limit pacer, the 429 / Retry-After breaker
and the single-flight /rate_limit health probe live there, shared with github_trending.
GraphQL (discussions) stays on ``http.post_json`` (a different endpoint shape) but
carries the same token via ``_headers``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from itertools import zip_longest
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import _github, cache, http
from penumbra.core.fetcher import register_adapter
from penumbra.core.normalize import PolarisDocument, mk_signal

logger = logging.getLogger(__name__)

GRAPHQL = "https://api.github.com/graphql"  # the REST host lives in _github (BASE); GraphQL is here
TIMEOUT = 20
CACHE_TTL = 10800  # 3 h — the watchtower polls github 6-hourly; a 30-min TTL always-missed the cache
                   # (finding M4), so every poll re-fired the full multi-surface fan-out. 3h < the
                   # 6h poll cadence keeps each poll fresh while killing the inter-poll re-fires.

# A query that is ONLY `org:NAME` or `user:NAME` (no free keywords) → activity mode.
_OWNER_ONLY_RE = re.compile(r"^\s*(org|user):([A-Za-z0-9][A-Za-z0-9-]*)\s*$")
# `tree:owner/repo` (optionally `tree:owner/repo@branch`) → read the repo FILE TREE: lets an
# agent see a repo's structure without cloning (roadmap-④ engineering-craft prereq).
_TREE_RE = re.compile(r"^\s*tree:([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(?:@([\w./-]+))?\s*$")
_TREE_NODE_CAP = 600  # bounded: a sane cap on a recursive tree (the API itself truncates huge ones)


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _clean_tags(*tags) -> list[str]:
    seen, out = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class GitHubAdapter:
    name = "github"
    needs_credentials = True
    description = (
        "GitHub platform — code search + issues/PRs + discussions, plus org/user "
        "newest-repo activity (query `org:NAME` / `user:NAME`) and repo file-tree "
        "browse (`tree:owner/repo` / `tree:owner/repo@branch`). Complements "
        "github_trending (repo discovery) + github_releases (infra release feeds)."
    )

    def __init__(self) -> None:
        # The token now lives in the shared _github client (factored loader). Keep a local copy ONLY
        # for the token-gated code/discussions surfaces below (an absent token skips them) and for the
        # GraphQL _headers (that endpoint is not routed through _github.get_json).
        self._token = _github._token

    # ------------------------------------------------------------------ auth
    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        # Only the GraphQL (post_json) calls still build their own headers; the REST surfaces let
        # _github.get_json inject Authorization + Accept + the API version centrally.
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    # -------------------------------------------------------------- Protocol
    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        q = (query or "").strip()
        if not q:
            return []
        key = cache.make_key("github", "search", q, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        tm = _TREE_RE.match(q)
        if tm:
            doc = self._repo_tree(tm.group(1), tm.group(2), tm.group(3))
            docs = [doc] if doc else []
        elif (m := _OWNER_ONLY_RE.match(q)):
            docs = self._owner_recent_repos(m.group(1), m.group(2), limit)
        else:
            docs = self._multi_surface(q, limit)

        cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        p = urlparse(url)
        if (p.hostname or "").lower() not in ("github.com", "www.github.com"):
            return None
        parts = p.path.strip("/").split("/")
        # Bare /owner/repo is github_trending's job — we only claim issue/pull/discussion.
        if len(parts) >= 4 and parts[2] in ("issues", "pull", "discussions"):
            owner, repo, kind, num = parts[0], parts[1], parts[2], parts[3]
            if kind in ("issues", "pull"):
                it = _github.get_json(
                    f"/repos/{owner}/{repo}/issues/{num}", timeout=TIMEOUT,
                )
                return self._issue_to_doc(it) if it else None
            if kind == "discussions":
                return self._discussion_by_number(owner, repo, num)
        return None

    def health_check(self) -> tuple[bool, str]:
        # Delegate to the shared single-flight /rate_limit probe (one upstream call for all three
        # GitHub-backed sources, token-authenticated, 60s-cached) instead of an own probe.
        return _github.health()

    # --------------------------------------------------------- search surfaces
    def _multi_surface(self, q: str, limit: int) -> list[PolarisDocument]:
        # SERIAL on purpose — GitHub triggers secondary rate limits on concurrency.
        issues = self._search_issues(q, limit)
        discussions = self._search_discussions(q, limit)
        code = self._search_code(q, limit)
        merged: list[PolarisDocument] = []
        for triple in zip_longest(issues, discussions, code):
            for d in triple:
                if d is not None:
                    merged.append(d)
        return merged[:limit] if limit else merged

    def _search_code(self, q: str, limit: int) -> list[PolarisDocument]:
        if not self._token:  # /search/code is auth-required (401 otherwise)
            return []
        data = _github.get_json(
            "/search/code",
            params={"q": q, "per_page": min(max(limit, 1), 50)},
            headers={"Accept": "application/vnd.github.text-match+json"},
            timeout=TIMEOUT,
        )
        if not data:  # None on 422 (bad/unqualified q) or any failure → empty
            return []
        out: list[PolarisDocument] = []
        for it in (data.get("items") or [])[:limit]:
            repo = it.get("repository") or {}
            full = repo.get("full_name") or "?"
            tms = it.get("text_matches") or []
            frag = (tms[0].get("fragment") or "").strip() if tms else ""
            out.append(PolarisDocument(
                source="github",
                source_id=f"code:{it.get('sha') or it.get('html_url')}",
                url=it.get("html_url") or "",
                title=f"{full}/{it.get('path', '')}",
                content=frag or it.get("path") or "(code match)",
                author=full.split("/")[0] if "/" in full else None,
                date=None,
                tags=_clean_tags("code", full),
                metadata={"subtype": "code", "repo": full,
                          "path": it.get("path"), "sha": it.get("sha"),
                          "raw": it},  # GitHub code-search item
            ))
        return out

    def _search_issues(self, q: str, limit: int) -> list[PolarisDocument]:
        data = _github.get_json(
            "/search/issues",
            params={"q": q, "per_page": min(max(limit, 1), 50),
                    "sort": "updated", "order": "desc"},
            timeout=TIMEOUT,
        )
        if not data:
            return []
        return [self._issue_to_doc(it) for it in (data.get("items") or [])[:limit] if it]

    def _search_discussions(self, q: str, limit: int) -> list[PolarisDocument]:
        if not self._token:  # graphql is 0/h unauth
            return []
        gql = (
            "query($q:String!,$n:Int!){search(query:$q,type:DISCUSSION,first:$n){"
            "nodes{... on Discussion{title url bodyText createdAt "
            "repository{nameWithOwner} category{name} author{login}}}}}"
        )
        data = http.post_json(
            GRAPHQL,
            json={"query": gql, "variables": {"q": q, "n": min(max(limit, 1), 25)}},
            headers=self._headers(), timeout=TIMEOUT,
        )
        if not data:
            return []
        nodes = (((data.get("data") or {}).get("search") or {}).get("nodes")) or []
        out: list[PolarisDocument] = []
        for n in nodes[:limit]:
            if not n:
                continue
            repo = (n.get("repository") or {}).get("nameWithOwner")
            cat = (n.get("category") or {}).get("name")
            out.append(PolarisDocument(
                source="github",
                source_id=f"discussion:{n.get('url')}",
                url=n.get("url") or "",
                title=n.get("title") or "(discussion)",
                content=(n.get("bodyText") or "")[:500],
                author=(n.get("author") or {}).get("login"),
                date=_parse_dt(n.get("createdAt")),
                tags=_clean_tags("discussion", repo, cat),
                metadata={"subtype": "discussion", "repo": repo, "category": cat,
                          "raw": n},  # GitHub GraphQL discussion node
            ))
        return out

    def _owner_recent_repos(self, kind: str, name: str, limit: int) -> list[PolarisDocument]:
        if kind == "org":
            path = f"/orgs/{name}/repos"
            params = {"sort": "created", "direction": "desc",
                      "type": "public", "per_page": min(max(limit, 1), 50)}
            date_field = "created_at"
        else:
            path = f"/users/{name}/repos"
            params = {"sort": "created", "direction": "desc",
                      "per_page": min(max(limit, 1), 50)}
            date_field = "created_at"
        data = _github.get_json(path, params=params, timeout=TIMEOUT)
        if not data or not isinstance(data, list):
            return []
        out: list[PolarisDocument] = []
        for r in data[:limit]:
            out.append(PolarisDocument(
                source="github",
                source_id=f"repo:{r.get('id')}",
                url=r.get("html_url") or "",
                title=r.get("full_name") or r.get("name") or "?",
                content=r.get("description") or "(no description)",
                author=(r.get("owner") or {}).get("login") or name,
                date=_parse_dt(r.get(date_field)),
                signals=mk_signal("stars", r.get("stargazers_count") or 0,
                                  kind="engagement", by="github/stargazers_count"),
                tags=_clean_tags("repo", r.get("language"), kind),
                metadata={"subtype": "repo", "stars": r.get("stargazers_count"),
                          "language": r.get("language"),
                          "created_at": r.get("created_at"),
                          "pushed_at": r.get("pushed_at"),
                          "raw": r},  # GitHub repo item
            ))
        return out

    # ----------------------------------------------------------------- tree mode
    def _repo_tree(self, owner: str, repo: str, branch: Optional[str]) -> Optional[PolarisDocument]:
        """Read a repo's FILE TREE (recursive, bounded) → a readable PolarisDocument. Degrades
        gracefully without a token (anonymous 60/h limit; never hard-fails on a missing token)."""
        full = f"{owner}/{repo}"
        if not branch:
            # Un-pinned: try git/trees/HEAD first (HEAD resolves the default branch server-side), so
            # the common case costs ONE call instead of a GET /repos default-branch round-trip + the
            # tree call. Only fall back to resolving the default branch by name when HEAD doesn't
            # answer (None covers 404 / empty repo / any failure), so an odd repo still degrades the
            # same as before.
            data = _github.get_json(f"/repos/{full}/git/trees/HEAD",
                                    params={"recursive": "1"}, timeout=TIMEOUT)
            if data:
                return self._tree_to_doc(owner, repo, "HEAD", data)
            meta = _github.get_json(f"/repos/{full}", timeout=TIMEOUT)
            branch = (meta or {}).get("default_branch") or "main"
        data = _github.get_json(
            f"/repos/{full}/git/trees/{branch}",
            params={"recursive": "1"}, timeout=TIMEOUT,
        )
        if not data:  # None on a private/404 repo, bad branch, or any failure → no doc
            return None
        return self._tree_to_doc(owner, repo, branch, data)

    @staticmethod
    def _tree_to_doc(owner: str, repo: str, branch: str, data: dict) -> PolarisDocument:
        """Pure parse of the git/trees API response → a PolarisDocument whose content is the file
        tree as indented text. Bounded to ``_TREE_NODE_CAP`` nodes. No network (smoke-testable)."""
        full = f"{owner}/{repo}"
        entries = data.get("tree") or []
        total = len(entries)
        shown = entries[:_TREE_NODE_CAP]
        # blob = file, tree = directory; sort so the listing reads like a stable directory walk.
        paths = sorted((e.get("path") or "", e.get("type") or "blob", e.get("size"))
                       for e in shown if e.get("path"))
        lines: list[str] = []
        for path, etype, size in paths:
            depth = path.count("/")
            name = path.rsplit("/", 1)[-1] + ("/" if etype == "tree" else "")
            tail = f"  ({size}B)" if (etype == "blob" and isinstance(size, int)) else ""
            lines.append("  " * depth + name + tail)
        n_files = sum(1 for _, t, _ in paths if t == "blob")
        n_dirs = sum(1 for _, t, _ in paths if t == "tree")
        truncated = bool(data.get("truncated")) or total > _TREE_NODE_CAP
        header = f"{full}@{branch}: {n_files} file(s), {n_dirs} dir(s)" + (
            f" (showing {len(paths)} of {total}+ nodes; tree truncated)" if truncated else "")
        return PolarisDocument(
            source="github",
            source_id=f"tree:{full}@{data.get('sha') or branch}",
            url=f"https://github.com/{full}/tree/{branch}",
            title=f"{full} file tree ({branch})",
            content=header + "\n\n" + "\n".join(lines),
            author=owner,
            date=None,
            tags=_clean_tags("tree", full, branch),
            metadata={"subtype": "tree", "repo": full, "branch": branch,
                      "node_count": total, "shown": len(paths), "truncated": truncated,
                      "raw": data},  # GitHub git/trees response
        )

    # ------------------------------------------------------------- doc helpers
    def _issue_to_doc(self, it: dict) -> PolarisDocument:
        is_pr = bool(it.get("pull_request"))
        ru = it.get("repository_url") or ""
        repo = ru.rsplit("/repos/", 1)[-1] if "/repos/" in ru else None
        labels = [l.get("name") for l in (it.get("labels") or []) if isinstance(l, dict)]
        return PolarisDocument(
            source="github",
            source_id=f"{'pr' if is_pr else 'issue'}:{it.get('id')}",
            url=it.get("html_url") or "",
            title=it.get("title") or "(untitled)",
            content=(it.get("body") or "")[:500],
            author=(it.get("user") or {}).get("login"),
            date=_parse_dt(it.get("created_at")),
            signals=mk_signal("comments", it.get("comments") or 0,
                              kind="engagement", by="github/comments"),
            tags=_clean_tags("pr" if is_pr else "issue", it.get("state"), repo, *labels[:4]),
            metadata={"subtype": "pr" if is_pr else "issue",
                      "state": it.get("state"), "repo": repo,
                      "comments": it.get("comments"),
                      "raw": it},  # GitHub issue/PR item
        )

    def _discussion_by_number(self, owner: str, repo: str, num: str) -> Optional[PolarisDocument]:
        if not self._token:
            return None
        try:
            number = int(num)
        except (ValueError, TypeError):
            return None
        gql = (
            "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
            "discussion(number:$n){title url bodyText createdAt "
            "category{name} author{login}}}}"
        )
        data = http.post_json(
            GRAPHQL,
            json={"query": gql, "variables": {"o": owner, "r": repo, "n": number}},
            headers=self._headers(), timeout=TIMEOUT,
        )
        if not data:
            return None
        d = ((data.get("data") or {}).get("repository") or {}).get("discussion")
        if not d:
            return None
        cat = (d.get("category") or {}).get("name")
        full = f"{owner}/{repo}"
        return PolarisDocument(
            source="github",
            source_id=f"discussion:{d.get('url')}",
            url=d.get("url") or "",
            title=d.get("title") or "(discussion)",
            content=(d.get("bodyText") or "")[:500],
            author=(d.get("author") or {}).get("login"),
            date=_parse_dt(d.get("createdAt")),
            tags=_clean_tags("discussion", full, cat),
            metadata={"subtype": "discussion", "repo": full, "category": cat,
                      "raw": d},  # GitHub GraphQL discussion node
        )


register_adapter(GitHubAdapter())
