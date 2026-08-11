"""Remotive — curated remote-job board with its own taxonomy, via the keyless API.

Remotive is an independent, curated remote-only job board: structured listings with a
category taxonomy, candidate-required-location (Worldwide / region), job type, tags and a
canonical url. Penumbra's remote-jobs STRUCTURE source: a filterable remote-AI/ML job
feed web search cannot assemble. Reinforces the jobs cell (a keyless remote-native source
alongside the keyed Adzuna aggregator and the per-company overseas_ai_jobs ATS crawler).

Access via the public API (no auth, no key):
  GET https://remotive.com/api/remote-jobs?search=<q>&limit=<n>
  -> {"job-count", "jobs": [{id, title, company_name, category, candidate_required_location,
       job_type, salary, publication_date, url, tags: [...], description: "<html>"}, ...]}
The url is a real field (extraction); the HTML description is stripped to plain text.

RATE LIMIT (load-bearing): Remotive's public API bans clients over ~2 requests/minute and
expects only a few calls/day. So this is explicit_only (never in the broad fan-out) with a
long cache_ttl, and is meant to be a curated / low-frequency drill, never a hot path.

Recon trail: brain note eye-free-api-probe-round2-2026-06-21 (live-probed keyless 200).
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://remotive.com/api/remote-jobs"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DESC_CAP = 4000


class RemotiveAdapter(BaseScrapeAdapter):
    name = "remotive"
    needs_credentials = False
    description = ("Remotive — curated REMOTE job board (category taxonomy, "
                   "candidate-required-location, job type, salary, tags) via the keyless API; "
                   "name it to drill remote AI/ML roles. STRUCTURE, keyless. Rate-limited "
                   "(a few calls/day): a low-frequency curated drill, not a hot path.")
    cache_ttl = 21600  # 6h: the API is rate-limited, so cache hard
    kind = "lookup"
    domains = ["jobs"]
    modes = ["STRUCTURE"]
    explicit_only = ("remotive: a remote-job drill, never broad-fan-out fodder (the public API "
                     "bans clients over ~2 req/min and expects only a few calls/day)")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        params = {"limit": max(1, min(int(limit), 50))}
        if query and query.strip():
            params["search"] = query.strip()
        return http.get_json(API_URL, params=params, timeout=20)

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: byte-faithful mirror (same URL, params, timeout, control
        flow); only the shared-http egress swaps to its async twin (get_json -> aget_json)."""
        params = {"limit": max(1, min(int(limit), 50))}
        if query and query.strip():
            params["search"] = query.strip()
        return await http.aget_json(API_URL, params=params, timeout=20)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip;
        egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents (byte-identical to search)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        jobs = raw.get("jobs") or []
        docs: list[Document] = []
        for job in jobs[:limit]:  # the API does not strictly honor limit, so truncate here
            doc = self._job_to_doc(job)
            if doc is not None:
                docs.append(doc)
        return docs

    def _job_to_doc(self, job: Any) -> Optional[Document]:
        if not isinstance(job, dict):
            return None
        url = job.get("url")
        title = job.get("title")
        if not url or not title:
            return None  # no canonical url / title -> unusable
        company = job.get("company_name")
        category = job.get("category")
        location = job.get("candidate_required_location")
        tags = job.get("tags") if isinstance(job.get("tags"), list) else []
        return Document(
            source=self.name,
            source_id=str(job.get("id")) if job.get("id") else url,
            url=url,
            title=title,
            content=self._strip_html(job.get("description")) or title,
            author=company,
            date=self._parse_date(job.get("publication_date")),
            signals={},  # salary is a free-text string (often empty), not a clean numeric signal
            tags=[t for t in ([category, location, job.get("job_type")] + tags) if t],
            metadata={
                "company": company,
                "category": category,
                "candidate_required_location": location,
                "job_type": job.get("job_type"),
                "salary": job.get("salary"),
                "raw": jsonsafe(job),
            },
        )

    @staticmethod
    def _strip_html(s: Any) -> str:
        """HTML description -> plain text (drop tags, unescape entities, collapse ws, cap).
        Pure, total."""
        if not isinstance(s, str) or not s:
            return ""
        text = html.unescape(_TAG_RE.sub(" ", s))
        return _WS_RE.sub(" ", text).strip()[:_DESC_CAP]

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        """Remotive publication_date is 'YYYY-MM-DDTHH:MM:SS'. None on anything else."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(raw.strip()[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
