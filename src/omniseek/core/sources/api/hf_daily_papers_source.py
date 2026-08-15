"""HuggingFace Daily Papers — AK-curated daily ML paper feed.

The HF community runs a daily "Papers" curation (originally by @akhaliq, now
broader community submissions + upvote ranking). Compared to raw arXiv:
- Pre-filtered by humans who care about ML signal-to-noise
- Upvotes & comments aggregate community attention
- ai_summary + ai_keywords fields are LLM-generated synopses
- githubRepo + githubStars link papers to their code (when available)

Public JSON API: `huggingface.co/api/daily_papers` (no auth, no rate limit).
Returns ~50 most-recently-featured papers as a list of objects.

JSON shape (validated 2026-05-28):
  [
    {
      "paper": {"id": "<arxiv-id>", "title": ..., "authors": [...],
                "summary": ..., "upvotes": int, "ai_summary": ...,
                "ai_keywords": [...], "githubRepo": ..., "githubStars": int},
      "publishedAt": "<iso8601>", "title": ..., "summary": ...,
      "numComments": "<int>", "submittedBy": {...}, "organization": {...}
    },
    ...
  ]

Distinct from huggingface_hub adapter which surfaces models/datasets/spaces
— this is the *daily research curation* layer.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from omniseek.core import cache, http
from omniseek.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

API_URL = "https://huggingface.co/api/daily_papers"
TIMEOUT = 15
USER_AGENT = "omniseek/0.1 (automated retrieval)"
CACHE_TTL = 3600  # 1h — daily curation, but multiple intra-day visits possible


class HFDailyPapersAdapter:
    name = "hf_daily_papers"
    needs_credentials = False
    description = (
        "HuggingFace Daily Papers — AK / community-curated daily ML papers, "
        "with upvotes, ai_summary, githubRepo links. Curation layer above raw arXiv."
    )

    def _fetch_all(self) -> list[dict]:
        key = cache.make_key(self.name, "all")
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = http.get_json(API_URL, timeout=TIMEOUT)
        if data is None:
            return []
        cache.set(key, data, ttl=CACHE_TTL)
        return data

    async def _afetch_all(self) -> list[dict]:
        """Async twin of ``_fetch_all`` (S4b): SAME cache key (async + sync share the disk cache),
        the disk read/write pushed OFF the loop (cache.get/set do file IO), the ONE network egress
        swapped to its async twin (http.get_json -> await http.aget_json, epoll not a held thread).
        Byte-identical logic to ``_fetch_all``; only the blocking syscalls move off-loop."""
        key = cache.make_key(self.name, "all")
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return cached
        data = await http.aget_json(API_URL, timeout=TIMEOUT)  # async network, on loop
        if data is None:
            return []
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, data, ttl=CACHE_TTL))
        return data

    def search(self, query: str, limit: int = 10) -> list[Document]:
        items = self._fetch_all()
        if not items:
            return []
        q_terms = [t.lower() for t in query.split() if len(t) > 1]
        scored: list[tuple[int, dict]] = []
        if not q_terms:
            scored = [(0, it) for it in items[:limit]]
        else:
            for it in items:
                paper = it.get("paper") or {}
                title = (it.get("title") or paper.get("title") or "").lower()
                summary = (it.get("summary") or paper.get("summary") or "").lower()
                ai_sum = (paper.get("ai_summary") or "").lower()
                kws = " ".join(paper.get("ai_keywords") or []).lower()
                blob = " ".join([title, summary, ai_sum, kws])
                score = sum(blob.count(t) for t in q_terms)
                # Title hits 3x weight
                score += 3 * sum(title.count(t) for t in q_terms)
                if score > 0:
                    scored.append((score, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        docs: list[Document] = []
        for sc, it in scored[:limit]:
            doc = self._item_to_document(it)
            if doc:
                docs.append(doc)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): the fan-out awaits this DIRECTLY (no pool thread),
        so this source's dominant NETWORK wait costs a coroutine, not a held thread. Mirrors ``search``
        line-for-line: only the cached fetch swaps to the async ``_afetch_all`` (async egress + off-loop
        cache); the keyword score / sort / map below is PURE CPU and stays ON the loop, byte-identical."""
        items = await self._afetch_all()
        if not items:
            return []
        q_terms = [t.lower() for t in query.split() if len(t) > 1]
        scored: list[tuple[int, dict]] = []
        if not q_terms:
            scored = [(0, it) for it in items[:limit]]
        else:
            for it in items:
                paper = it.get("paper") or {}
                title = (it.get("title") or paper.get("title") or "").lower()
                summary = (it.get("summary") or paper.get("summary") or "").lower()
                ai_sum = (paper.get("ai_summary") or "").lower()
                kws = " ".join(paper.get("ai_keywords") or []).lower()
                blob = " ".join([title, summary, ai_sum, kws])
                score = sum(blob.count(t) for t in q_terms)
                # Title hits 3x weight
                score += 3 * sum(title.count(t) for t in q_terms)
                if score > 0:
                    scored.append((score, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        docs: list[Document] = []
        for sc, it in scored[:limit]:
            doc = self._item_to_document(it)
            if doc:
                docs.append(doc)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "huggingface.co" not in host:
            return None
        path = urlparse(url).path.strip("/")
        if not path.startswith("papers/"):
            return None
        paper_id = path.split("/", 1)[1].split("/")[0]
        if not paper_id:
            return None
        for it in self._fetch_all():
            paper = it.get("paper") or {}
            if paper.get("id") == paper_id:
                return self._item_to_document(it)
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(API_URL, headers={"User-Agent": USER_AGENT}, timeout=8)
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            d = resp.json()
            return bool(d), f"OK ({len(d)} papers in feed)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _item_to_document(it: dict) -> Optional[Document]:
        paper = it.get("paper") or {}
        paper_id = paper.get("id")
        if not paper_id:
            return None
        title = it.get("title") or paper.get("title") or "(untitled)"
        # Both arXiv and HF papers URL forms are valid; pick HF (canonical for this curation)
        url = f"https://huggingface.co/papers/{paper_id}"

        # Authors
        authors = paper.get("authors") or []
        if authors and isinstance(authors[0], dict):
            author = ", ".join((a.get("name") or "") for a in authors[:5] if a.get("name"))
        else:
            author = ", ".join(str(a) for a in authors[:5])

        # Date
        date = None
        for k in ("publishedAt", "submittedOnDailyAt"):
            v = it.get(k) or paper.get(k)
            if v:
                try:
                    date = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    break
                except (ValueError, TypeError):
                    pass

        # Content: prefer ai_summary, fall back to summary
        summary_text = paper.get("ai_summary") or paper.get("summary") or it.get("summary") or ""
        content = summary_text
        # Prepend GitHub repo if known (huge value signal). The HF API gives
        # githubRepo as a full URL most of the time, but occasionally as a bare
        # "org/repo" slug — normalize to avoid the "https://github.com/https://..."
        # double-prefix bug.
        github = paper.get("githubRepo")
        stars = paper.get("githubStars")
        repo_url = None
        if github:
            repo_url = github if github.startswith("http") else f"https://github.com/{github}"
            line = f"GitHub: {repo_url}"
            if stars:
                line += f" (★{stars})"
            content = f"{line}\n\n{summary_text}"

        upvotes = paper.get("upvotes") or 0
        tags = list(paper.get("ai_keywords") or [])[:8]

        return Document(
            source="hf_daily_papers",
            source_id=paper_id,
            url=url,
            title=title,
            content=content,
            author=author or None,
            date=date,
            signals=mk_signal("upvotes", upvotes,
                              kind="engagement", by="hf_daily_papers/upvotes"),  # community attention as engagement signal
            tags=tags,
            metadata={
                "arxiv_id": paper_id,
                "upvotes": upvotes,
                "github_repo": repo_url,
                "github_stars": stars,
                "num_comments": it.get("numComments"),
                "raw": jsonsafe(it),
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(HFDailyPapersAdapter())
