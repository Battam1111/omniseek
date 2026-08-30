"""GitHub Awesome-PhD curated resource lists.

Fetches the README of community-maintained "awesome PhD" repos. Each
README is a structured list of links to PhD advice, methodology guides,
and resources — highest signal-to-noise ratio of any single source.

Search is keyword-based over the README content; each matching link
section is returned as a separate document.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from omniseek.core import cache, http
from omniseek.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

# Curated set of high-quality awesome-PhD / career repos
AWESOME_REPOS = [
    ("pliang279", "awesome-phd-advice", "PhD-specific advice collection"),
    ("helenahartmann", "awesome-PhD", "Comprehensive PhD resources"),
    ("poloclub", "awesome-grad-school", "Grad school survival guide"),
    ("jedyang97", "awesome-cs-phd-application-advice", "CS PhD application advice"),
    ("emptymalei", "awesome-research", "Research tools and workflow"),
    ("dangkhoasdc", "awesome-ai-residency", "Global AI residency programs (OpenAI/Anthropic/DeepMind/etc.)"),
]

DEFAULT_TIMEOUT = 20


class GithubAwesomePhDAdapter:
    name = "github_awesome_phd"
    needs_credentials = False
    description = "GitHub Awesome-PhD curated lists — highest SNR PhD resource collections"

    def _fetch_readme(self, owner: str, repo: str) -> Optional[str]:
        key = cache.make_key("gh_awesome", owner, repo)
        cached = cache.get(key)
        if cached is not None:
            return cached
        # GitHub raw content (try main, then master)
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            text = http.get_text(url, timeout=DEFAULT_TIMEOUT)
            if text:
                cache.set(key, text, ttl=86400)  # README updates rarely; 24h
                return text
        return None

    async def _afetch_readme(self, owner: str, repo: str) -> Optional[str]:
        """Async egress twin of ``_fetch_readme`` (mirrors it exactly): SAME cache key, the disk
        cache read/write pushed OFF the loop via anyio.to_thread, and the README egress swapped to
        its async twin (http.get_text -> await http.aget_text). No CPU work here to keep on-loop."""
        key = cache.make_key("gh_awesome", owner, repo)
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return cached
        # GitHub raw content (try main, then master)
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            text = await http.aget_text(url, timeout=DEFAULT_TIMEOUT)  # async network, ON loop
            if text:
                await anyio.to_thread.run_sync(  # disk write OFF loop
                    functools.partial(cache.set, key, text, ttl=86400))  # README updates rarely; 24h
                return text
        return None

    def search(self, query: str, limit: int = 10) -> list[Document]:
        query_terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 1]
        if not query_terms:
            return []

        all_docs: list[tuple[int, Document]] = []
        for owner, repo, desc in AWESOME_REPOS:
            readme = self._fetch_readme(owner, repo)
            if not readme:
                continue
            # Split README into bullet sections; each bullet often = one resource
            sections = self._extract_link_items(readme)
            for section in sections:
                blob = section["text"].lower()
                score = sum(blob.count(t) for t in query_terms)
                if score == 0:
                    continue
                # The link in this bullet is the "primary" URL
                primary_url = section.get("url") or f"https://github.com/{owner}/{repo}"
                doc = Document(
                    source="github_awesome_phd",
                    source_id=f"{owner}/{repo}#{section.get('anchor', '')}",
                    url=primary_url,
                    title=section.get("title") or section["text"][:80],
                    content=section["text"],
                    author=f"{owner} (Awesome list maintainer)",
                    tags=[f"awesome-list", f"{owner}/{repo}"],
                    metadata={
                        "repo": f"{owner}/{repo}",
                        "repo_description": desc,
                        "raw": jsonsafe(section),
                    },
                )
                all_docs.append((score, doc))

        all_docs.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in all_docs[:limit]]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search``, with the six READMEs fetched CONCURRENTLY (2026-08-30).

        The per-repo egress is ``self._afetch_readme`` (its cache round-trip + http.aget_text run
        off/on the loop correctly). Everything after the fetch is the same PURE CPU code as
        ``search``, in the same AWESOME_REPOS order: ``_extract_link_items``, term scoring, doc
        build, and the final sort, which is stable and therefore ties back to that order. The six
        repos are independent README files and no repo's scoring reads another's text, so awaiting
        them one after another only added six round-trips together. The 24h disk cache means only
        a cold start ever paid the full price, which is why this one ranked below v2ex.

        The main-then-master fallback INSIDE ``_afetch_readme`` stays SERIAL: master is only tried
        because main came back empty, which is a real dependency, not a fan-out."""
        query_terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 1]
        if not query_terms:
            return []

        settled = await asyncio.gather(
            *(self._afetch_readme(owner, repo) for owner, repo, _ in AWESOME_REPOS),
            return_exceptions=True,
        )

        all_docs: list[tuple[int, Document]] = []
        for (owner, repo, desc), readme in zip(AWESOME_REPOS, settled):
            if isinstance(readme, BaseException):
                # http.aget_text returns falsy on failure, so the serial loop could not raise
                # here; one unreadable repo must not cost the other five, which is exactly what
                # the serial loop's "no README -> skip it" branch already guaranteed.
                logger.warning("github_awesome_phd: %s/%s failed: %s", owner, repo, readme)
                continue
            if not readme:
                continue
            # Split README into bullet sections; each bullet often = one resource
            sections = self._extract_link_items(readme)
            for section in sections:
                blob = section["text"].lower()
                score = sum(blob.count(t) for t in query_terms)
                if score == 0:
                    continue
                # The link in this bullet is the "primary" URL
                primary_url = section.get("url") or f"https://github.com/{owner}/{repo}"
                doc = Document(
                    source="github_awesome_phd",
                    source_id=f"{owner}/{repo}#{section.get('anchor', '')}",
                    url=primary_url,
                    title=section.get("title") or section["text"][:80],
                    content=section["text"],
                    author=f"{owner} (Awesome list maintainer)",
                    tags=[f"awesome-list", f"{owner}/{repo}"],
                    metadata={
                        "repo": f"{owner}/{repo}",
                        "repo_description": desc,
                        "raw": jsonsafe(section),
                    },
                )
                all_docs.append((score, doc))

        all_docs.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in all_docs[:limit]]

    def fetch_url(self, url: str) -> Optional[Document]:
        # Match repo URLs from our curated set
        host = urlparse(url).hostname or ""
        if "github.com" not in host:
            return None
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        if not any(o == owner and r == repo for o, r, _ in AWESOME_REPOS):
            return None
        readme = self._fetch_readme(owner, repo)
        if not readme:
            return None
        return Document(
            source="github_awesome_phd",
            source_id=f"{owner}/{repo}",
            url=f"https://github.com/{owner}/{repo}",
            title=f"{owner}/{repo}",
            content=readme,
            author=owner,
            tags=["awesome-list"],
            metadata={"raw": jsonsafe({"owner": owner, "repo": repo, "readme": readme})},
        )

    def health_check(self) -> tuple[bool, str]:
        # Just probe github raw content
        try:
            resp = httpx.get(
                "https://raw.githubusercontent.com/pliang279/awesome-phd-advice/main/README.md",
                timeout=10,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return True, "OK"
            # Try master
            resp = httpx.get(
                "https://raw.githubusercontent.com/pliang279/awesome-phd-advice/master/README.md",
                timeout=10,
                follow_redirects=True,
            )
            return (resp.status_code == 200, f"HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _extract_link_items(markdown: str) -> list[dict]:
        """Extract bullet-style link items from an awesome list.

        An awesome list typically has lines like:
            - [Title](url) — description
        or:
            * [Title](url): description
        We extract these as "items" with title, url, and surrounding text.
        """
        items = []
        # Capture markdown link patterns at start of bullet
        pattern = re.compile(
            r"^\s*[-*+]\s+\[([^\]]+)\]\(([^)]+)\)[\s—:\-]*(.*?)$",
            re.MULTILINE,
        )
        for m in pattern.finditer(markdown):
            title, url, desc = m.group(1), m.group(2), m.group(3)
            items.append(
                {
                    "title": title.strip(),
                    "url": url.strip(),
                    "text": f"{title}: {desc}".strip(),
                    "anchor": title.lower().replace(" ", "-"),
                }
            )
        return items


from omniseek.core.fetcher import register_adapter

register_adapter(GithubAwesomePhDAdapter())
