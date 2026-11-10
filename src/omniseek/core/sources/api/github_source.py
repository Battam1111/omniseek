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

Token: ``<credentials>/github.json`` → ``{"token": "..."}`` (classic *or*
fine-grained — both accept ``Authorization: Bearer``). Without a token, code-search +
discussions are skipped (issues/repos still work at the anonymous 60/h limit); the
adapter never hard-fails on a missing token.

REST egress (search/issues, search/code, repos, git/trees) routes through the shared
``_github`` client: the token, the Search-limit pacer, the 429 / Retry-After breaker
and the single-flight /rate_limit health probe live there, shared with github_trending.
GraphQL (discussions) stays on ``http.post_json`` (a different endpoint shape) but
carries the same token via ``_headers``.

omniseek_read on an issue or pull request (``afetch_url``, 2026-11-09) returns the body uncut plus every
conversation comment, and for a pull request every review and code-line comment, oldest first, paged
serially through ``_github``; search hits keep the short 500-character body (``_issue_to_doc``).
"""

from __future__ import annotations

import logging
import re
from itertools import zip_longest
from typing import Optional
from urllib.parse import urlparse

import anyio

from omniseek.core import _github, cache, diag, fetcher, http
from omniseek.core.fetcher import register_adapter
from omniseek.core.normalize import Document, mk_signal, to_utc
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

GRAPHQL = "https://api.github.com/graphql"  # the REST host lives in _github (BASE); GraphQL is here
TIMEOUT = 20
CACHE_TTL = 10800  # 3 h — the watchtower polls github 6-hourly; a 30-min TTL always-missed the cache
                   # so every poll re-fired the full multi-surface fan-out. 3h < the
                   # 6h poll cadence keeps each poll fresh while killing the inter-poll re-fires.

# A query that is ONLY `org:NAME` or `user:NAME` (no free keywords) → activity mode.
_OWNER_ONLY_RE = re.compile(r"^\s*(org|user):([A-Za-z0-9][A-Za-z0-9-]*)\s*$")
# `tree:owner/repo` (optionally `tree:owner/repo@branch`) → read the repo FILE TREE: lets an
# agent see a repo's structure without cloning (roadmap-④ engineering-craft prereq).
_TREE_RE = re.compile(r"^\s*tree:([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(?:@([\w./-]+))?\s*$")
_TREE_NODE_CAP = 600  # bounded: a sane cap on a recursive tree (the API itself truncates huge ones)


def _clean_tags(*tags) -> list[str]:
    seen, out = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class GitHubAdapter(SourceBase):
    name = "github"
    needs_credentials = True
    description = (
        "GitHub platform: code search + issues/PRs + discussions, plus org/user "
        "newest-repo activity (query `org:NAME` / `user:NAME`) and repo file-tree "
        "browse (`tree:owner/repo` / `tree:owner/repo@branch`). Complements "
        "github_trending (repo discovery) + github_releases (infra release feeds)."
    )

    # Source spec (omniseek.core.filters). Every field translates into a GitHub search qualifier on
    # ``q`` (the same tokens a caller may type), which every surface receives; a qualifier a surface
    # does not know (state / label on code search) makes that surface answer nothing.
    filters = {
        "org": {"type": "keyword", "ops": ["eq"],
                "desc": "organization (org:); with an empty query, that org's newest repos"},
        "user": {"type": "keyword", "ops": ["eq"],
                 "desc": "user account (user:); with an empty query, that user's newest repos"},
        "repo": {"type": "keyword", "ops": ["eq"], "doc_key": "metadata.repo",
                 "desc": "one repository as owner/name (repo:)"},
        "language": {"type": "keyword", "ops": ["eq"], "desc": "programming language (language:)"},
        "label": {"type": "keyword", "ops": ["eq"], "desc": "issue / PR label (label:)"},
        "state": {"type": "keyword", "ops": ["eq"], "values": ["open", "closed"],
                  "doc_key": "metadata.state",
                  "desc": "issue / PR state (state:)"},
        "created": {"type": "date", "ops": ["eq", "gte", "lte", "range"], "doc_key": "date",
                    "desc": "creation date of the issue / PR / discussion (created:)"},
        "updated": {"type": "date", "ops": ["eq", "gte", "lte", "range"],
                    "desc": "last update date of the issue / PR / discussion (updated:)"},
    }
    # omniseek_search(as_of=...) pushes "created <= as_of" upstream through this field: the creation date of the issue / PR / discussion.
    as_of_field = "created"
    coverage = {"since": None, "until": None, "gaps": [],
                "note": "live GitHub search; code search and discussions need a token"}
    license = ("GitHub Terms of Service govern the API; each repository's content stays under that "
               "repository's own license")
    samples = [{"query": "flash attention", "filters": {"language": "Python"}},
               {"query": "memory leak", "filters": {"repo": "pytorch/pytorch", "state": "open"}}]

    def translate_filters(self, query: str, clauses: list) -> str:
        """Append the filters as GitHub search qualifiers (dates as A..B, >=A or <=B)."""
        from omniseek.core.filters import Unsupported
        toks = []
        for c in clauses:
            if c.field in ("created", "updated"):
                lo, hi = c.lo, c.hi
                if lo and hi:
                    toks.append(f"{c.field}:{lo.isoformat()}..{hi.isoformat()}")
                elif lo:
                    toks.append(f"{c.field}:>={lo.isoformat()}")
                else:
                    toks.append(f"{c.field}:<={hi.isoformat()}")
            elif c.field in ("org", "user", "repo", "language", "label", "state"):
                v = str(c.value)
                toks.append(f'{c.field}:"{v}"' if " " in v else f"{c.field}:{v}")
            else:
                raise Unsupported(c.field)
        return " ".join([(query or "").strip(), *toks]).strip()

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

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, S9; this is the only one now) (S4b): OmniSeek's async fan-out awaits this DIRECTLY, so
        github's multi-surface egress costs a COROUTINE, not a held pool thread. Mirrors ``search``
        step-for-step; only the BLOCKING work moves:
          • the disk cache read/write → ``anyio.to_thread.run_sync`` (get_docs / set_docs do file IO);
          • the REST egress (issues / code / owner-repos) → ``await _github.aget_json`` (its byte-faithful
            async twin: SAME breaker / Search-limit pacer / concurrency cap / token auth);
          • the GraphQL discussions egress → ``await http.apost_json`` (async twin of http.post_json).
        The multi-surface fan-out stays SERIAL (awaited one-by-one, NOT ``asyncio.gather``'d) — GitHub
        triggers secondary rate limits on concurrent client requests, exactly why the sync
        ``_multi_surface`` is serial on purpose. The pure-CPU parse/map/merge (``_issue_to_doc`` / the
        inline code+discussion+repo maps / the round-robin ``zip_longest``) stays ON the loop,
        byte-identical to ``search``. SAME cache KEY as ``search`` (async and sync share the cache).
        The ``tree:`` branch is a separate file-tree egress path (not the primary code/issues/discussions
        target of this conversion); its unchanged sync ``_repo_tree`` runs OFF the loop via to_thread —
        faithful + correct, the reddit-comments / _rss precedent for a non-primary path."""
        q = (query or "").strip()
        if not q:
            return []
        key = cache.make_key("github", "search", q, limit)

        async def _aget() -> Optional[list[Document]]:
            tm = _TREE_RE.match(q)
            if tm:
                # Tree-browse: a separate file-tree egress path, not the primary target here. Run its
                # unchanged sync _repo_tree OFF the loop (byte-identical result), never inline on it.
                doc = await anyio.to_thread.run_sync(
                    self._repo_tree, tm.group(1), tm.group(2), tm.group(3), limiter=fetcher._LEGACY_LIMITER)
                docs = [doc] if doc else []
            elif (m := _OWNER_ONLY_RE.match(q)):
                docs = await self._aowner_recent_repos(m.group(1), m.group(2), limit)
            else:
                docs = await self._amulti_surface(q, limit)

            return docs

        return await self._acached(key, CACHE_TTL, _aget, empty_ttl=300)

    async def afetch_url(self, url: str) -> Optional[Document]:
        p = urlparse(url)
        if (p.hostname or "").lower() not in ("github.com", "www.github.com"):
            return None
        parts = p.path.strip("/").split("/")
        # Bare /owner/repo is github_trending's job — we only claim issue/pull/discussion.
        if len(parts) >= 4 and parts[2] in ("issues", "pull", "discussions"):
            owner, repo, kind, num = parts[0], parts[1], parts[2], parts[3]
            if kind in ("issues", "pull"):
                with diag.tap() as seen:
                    it = await _github.aget_json(
                        f"/repos/{owner}/{repo}/issues/{num}", timeout=TIMEOUT,
                    )
                if it:
                    return await self._afull_issue_doc(owner, repo, num, it)
                if any(r.get("status") in (404, 410) for r in seen if isinstance(r, dict)):
                    # GitHub's answer for this issue / pull request is "not there" (deleted, never existed,
                    # or in a repository we cannot see): end the read here, not on github.com's 404 page.
                    diag.note("github.fetch_url", url=url, final=True,
                              body=f"GitHub 上没有这个公开的 {kind}/{num}（{owner}/{repo}：不存在、已删或仓库不公开）")
                return None
            if kind == "discussions":
                return await self._discussion_by_number(owner, repo, num, url)
        return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # Delegate to the shared single-flight /rate_limit probe (one upstream call for all three
        # GitHub-backed sources, token-authenticated, 60s-cached) instead of an own probe.
        return await _github.ahealth()

    async def _amulti_surface(self, q: str, limit: int) -> list[Document]:
        # SERIAL on purpose — GitHub triggers secondary rate limits on concurrency, so these are
        # AWAITED one-by-one (never asyncio.gather'd), mirroring sync _multi_surface exactly.
        issues = await self._asearch_issues(q, limit)
        discussions = await self._asearch_discussions(q, limit)
        code = await self._asearch_code(q, limit)
        merged: list[Document] = []
        for triple in zip_longest(issues, discussions, code):
            for d in triple:
                if d is not None:
                    merged.append(d)
        return merged[:limit] if limit else merged

    async def _asearch_code(self, q: str, limit: int) -> list[Document]:
        # Byte-faithful async mirror of _search_code: ONLY the egress swaps (_github.get_json →
        # await _github.aget_json); the token gate + parse/map is identical, on the loop.
        if not self._token:  # /search/code is auth-required (401 otherwise)
            return []
        data = await _github.aget_json(
            "/search/code",
            params={"q": q, "per_page": min(max(limit, 1), 50)},
            headers={"Accept": "application/vnd.github.text-match+json"},
            timeout=TIMEOUT,
        )
        if not data:  # None on 422 (bad/unqualified q) or any failure → empty
            return []
        out: list[Document] = []
        for it in (data.get("items") or [])[:limit]:
            repo = it.get("repository") or {}
            full = repo.get("full_name") or "?"
            tms = it.get("text_matches") or []
            frag = (tms[0].get("fragment") or "").strip() if tms else ""
            out.append(Document(
                source="github",
                source_id=f"code:{it.get('sha') or it.get('html_url')}",
                url=it.get("html_url") or "",
                title=f"{full}/{it.get('path', '')}",
                content=frag or "",
                author=full.split("/")[0] if "/" in full else None,
                date=None,
                tags=_clean_tags("code", full),
                metadata={"subtype": "code", "repo": full,
                          "path": it.get("path"), "sha": it.get("sha"),
                          "raw": it},  # GitHub code-search item
            ))
        return out

    async def _asearch_issues(self, q: str, limit: int) -> list[Document]:
        # Byte-faithful async mirror of _search_issues: ONLY the egress swaps (_github.get_json →
        # await _github.aget_json); mapping via the SAME pure _issue_to_doc, on the loop.
        data = await _github.aget_json(
            "/search/issues",
            params={"q": q, "per_page": min(max(limit, 1), 50),
                    "sort": "updated", "order": "desc"},
            timeout=TIMEOUT,
        )
        if not data:
            return []
        return [self._issue_to_doc(it) for it in (data.get("items") or [])[:limit] if it]

    async def _asearch_discussions(self, q: str, limit: int) -> list[Document]:
        # Byte-faithful async mirror of _search_discussions: ONLY the egress swaps (http.post_json →
        # await http.apost_json); same GraphQL query + token gate + node parse, on the loop.
        if not self._token:  # graphql is 0/h unauth
            return []
        gql = (
            "query($q:String!,$n:Int!){search(query:$q,type:DISCUSSION,first:$n){"
            "nodes{... on Discussion{title url bodyText createdAt "
            "repository{nameWithOwner} category{name} author{login}}}}}"
        )
        data = await http.apost_json(
            GRAPHQL,
            json={"query": gql, "variables": {"q": q, "n": min(max(limit, 1), 25)}},
            headers=self._headers(), timeout=TIMEOUT,
        )
        if not data:
            return []
        nodes = (((data.get("data") or {}).get("search") or {}).get("nodes")) or []
        out: list[Document] = []
        for n in nodes[:limit]:
            if not n:
                continue
            repo = (n.get("repository") or {}).get("nameWithOwner")
            cat = (n.get("category") or {}).get("name")
            out.append(Document(
                source="github",
                source_id=f"discussion:{n.get('url')}",
                url=n.get("url") or "",
                title=n.get("title") or "(discussion)",
                content=(n.get("bodyText") or "")[:500],
                author=(n.get("author") or {}).get("login"),
                date=to_utc(n.get("createdAt")),
                tags=_clean_tags("discussion", repo, cat),
                metadata={"subtype": "discussion", "repo": repo, "category": cat,
                          "raw": n},  # GitHub GraphQL discussion node
            ))
        return out

    async def _aowner_recent_repos(self, kind: str, name: str, limit: int) -> list[Document]:
        # Byte-faithful async mirror of _owner_recent_repos: ONLY the egress swaps (_github.get_json →
        # await _github.aget_json); same org/user path + params + parse, on the loop.
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
        data = await _github.aget_json(path, params=params, timeout=TIMEOUT)
        if not data or not isinstance(data, list):
            return []
        out: list[Document] = []
        for r in data[:limit]:
            out.append(Document(
                source="github",
                source_id=f"repo:{r.get('id')}",
                url=r.get("html_url") or "",
                title=r.get("full_name") or r.get("name") or "?",
                content=r.get("description") or "",
                author=(r.get("owner") or {}).get("login") or name,
                date=to_utc(r.get(date_field)),
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
    def _repo_tree(self, owner: str, repo: str, branch: Optional[str]) -> Optional[Document]:
        """Read a repo's FILE TREE (recursive, bounded) → a readable Document. Degrades
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
    def _tree_to_doc(owner: str, repo: str, branch: str, data: dict) -> Document:
        """Pure parse of the git/trees API response → a Document whose content is the file
        tree as indented text. Bounded to ``_TREE_NODE_CAP`` nodes. No network."""
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
        return Document(
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

    async def _apages(self, path: str) -> tuple[list, bool]:
        """Every item of a list endpoint, page by page (100 a page, GitHub's maximum), one request at a
        time through the shared ``_github`` egress. ``(items, complete)``: ``complete`` is False when a
        page did not come back (the items read before it are kept)."""
        items: list = []
        page = 1
        while True:
            got = await _github.aget_json(path, params={"per_page": 100, "page": page}, timeout=TIMEOUT)
            if not isinstance(got, list):
                return items, False
            items.extend(got)
            if len(got) < 100:
                return items, True
            page += 1

    async def _afull_issue_doc(self, owner: str, repo: str, num: str, it: dict) -> Document:
        """An issue or pull request read whole (omniseek_read): its body uncut, then every conversation comment,
        and for a pull request also every review and every comment on a code line, each with who, when and
        its full text, oldest first. Serial requests (secondary rate limits). The search path keeps the
        short ``_issue_to_doc``."""
        doc = self._issue_to_doc(it)
        is_pr = bool(it.get("pull_request"))
        base = f"/repos/{owner}/{repo}"
        entries: list[tuple] = []
        missing: list[str] = []

        comments, ok = await self._apages(f"{base}/issues/{num}/comments")
        if not ok:
            missing.append("comments")
        for c in comments:
            entries.append((c.get("created_at"), "comment", c))
        if is_pr:
            reviews, ok = await self._apages(f"{base}/pulls/{num}/reviews")
            if not ok:
                missing.append("reviews")
            for r in reviews:
                entries.append((r.get("submitted_at"), "review", r))
            line_comments, ok = await self._apages(f"{base}/pulls/{num}/comments")
            if not ok:
                missing.append("review comments")
            for c in line_comments:
                entries.append((c.get("created_at"), "line", c))
        entries.sort(key=lambda e: e[0] or "9999")

        parts = []
        for i, (when, kind, c) in enumerate(entries, 1):
            who = (c.get("user") or {}).get("login") or "unknown"
            day = when.replace("T", " ").replace("Z", " UTC") if when else "undated"
            if kind == "review":
                head = f"review ({c.get('state') or 'no state'})"
            elif kind == "line":
                line = c.get("line") or c.get("original_line")
                head = f"review comment on {c.get('path') or '?'}" + (f":{line}" if line else "")
            else:
                head = "comment"
            parts.append(f"## {i}. {head} by {who}, {day}\n\n{c.get('body') or ''}")

        content = it.get("body") or ""
        if parts:
            content += f"\n\n# Comments ({len(parts)}, oldest first)\n\n" + "\n\n".join(parts)
        meta = dict(doc.metadata or {})
        meta.update({"fulltext": True, "comments_read": len(parts)})
        if missing:
            meta["comments_incomplete"] = f"some pages did not come back: {', '.join(missing)}"
            diag.note("github.fetch_url", url=doc.url or None,
                      body=f"{owner}/{repo}#{num}: {meta['comments_incomplete']}; the read carries what came back")
        return doc.model_copy(update={"content": content, "metadata": meta})

    # ------------------------------------------------------------- doc helpers
    def _issue_to_doc(self, it: dict) -> Document:
        is_pr = bool(it.get("pull_request"))
        ru = it.get("repository_url") or ""
        repo = ru.rsplit("/repos/", 1)[-1] if "/repos/" in ru else None
        labels = [l.get("name") for l in (it.get("labels") or []) if isinstance(l, dict)]
        return Document(
            source="github",
            source_id=f"{'pr' if is_pr else 'issue'}:{it.get('id')}",
            url=it.get("html_url") or "",
            title=it.get("title") or "(untitled)",
            content=(it.get("body") or "")[:500],
            author=(it.get("user") or {}).get("login"),
            date=to_utc(it.get("created_at")),
            signals=mk_signal("comments", it.get("comments") or 0,
                              kind="engagement", by="github/comments"),
            tags=_clean_tags("pr" if is_pr else "issue", it.get("state"), repo, *labels[:4]),
            metadata={"subtype": "pr" if is_pr else "issue",
                      "state": it.get("state"), "repo": repo,
                      "comments": it.get("comments"),
                      "raw": it},  # GitHub issue/PR item
        )

    async def _discussion_by_number(self, owner: str, repo: str, num: str,
                                    url: str = "") -> Optional[Document]:
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
        data = await http.apost_json(
            GRAPHQL,
            json={"query": gql, "variables": {"o": owner, "r": repo, "n": number}},
            headers=self._headers(), timeout=TIMEOUT,
        )
        if not data:
            return None
        d = ((data.get("data") or {}).get("repository") or {}).get("discussion")
        if not d:
            errors = data.get("errors") if isinstance(data.get("errors"), list) else []
            if any(isinstance(e, dict) and e.get("type") == "NOT_FOUND" for e in errors):
                # GraphQL's NOT_FOUND: no such repository or discussion that this token can see.
                diag.note("github.fetch_url", url=url or None, final=True,
                          body=f"GitHub 上没有这个公开的 discussions/{num}（{owner}/{repo}：不存在、已删或仓库不公开）")
            return None
        cat = (d.get("category") or {}).get("name")
        full = f"{owner}/{repo}"
        return Document(
            source="github",
            source_id=f"discussion:{d.get('url')}",
            url=d.get("url") or "",
            title=d.get("title") or "(discussion)",
            content=(d.get("bodyText") or "")[:500],
            author=(d.get("author") or {}).get("login"),
            date=to_utc(d.get("createdAt")),
            tags=_clean_tags("discussion", full, cat),
            metadata={"subtype": "discussion", "repo": full, "category": cat,
                      "raw": d},  # GitHub GraphQL discussion node
        )


register_adapter(GitHubAdapter())
