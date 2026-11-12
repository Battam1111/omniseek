"""Overseas industry AI-lab full-time research roles (RS / RE / MTS).

Phase 4 (2026-05-29). Covers one job category the other adapters lack:
**full-time research scientist / research engineer / member-of-technical-staff
roles at overseas industry AI labs** — distinct from `ai_residencies`
(early-career programs), `academic_jobs` (faculty/postdoc), and the Chinese ATS
adapters (mokahr / feishu / bytedance).

A research pass (40+ calls, 2026-05-29) verified each lab's ATS + board token by
hitting the live JSON API and confirming real research roles + SG/Canada/remote
reach. Greenhouse / Ashby / Lever / SmartRecruiters / Workable all expose public
no-auth JSON.

⚠️ Location filtering MUST read the SECONDARY location fields, not just the
primary — e.g. Cohere's "Member of Technical Staff" roles list primary=London
but Toronto/Montreal in `secondaryLocations[]` (98 of 129 roles). Missing that
loses the entire Canada signal. Each fetcher below reads every location field.

Singapore note: A*STAR (CFAR/I2R) and Sea AI Lab — the two biggest SG-native
employers — run self-built portals with no public JSON API (defer to a future
CDP batch; overseas + public so no account risk). DeepMind / Reka / Together /
Mistral already surface real Singapore research roles via their public ATS.
"""

from __future__ import annotations

import asyncio
import functools
import html
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import anyio

from omniseek.core import cache, diag, http, portal
from omniseek.core._guard import GateBusy, bounded_async_slot
from omniseek.core.normalize import Document, html_fragment_text, jsonsafe, keyword_score_filter
from omniseek.core.sources._base import SourceBase, aparse

logger = logging.getLogger(__name__)

TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; OmniSeek/0.1)"

# Per-host in-flight cap. This source fans out (ThreadPoolExecutor below) over rows that COLLAPSE onto
# a few shared ATS hosts (boards-api.greenhouse.io, api.ashbyhq.com, ...), so one search can put ~7
# concurrent requests on greenhouse, and N concurrent agents multiply that. A per-HOSTNAME semaphore
# (NOT one global cap: distinct ATS hosts must not throttle each other) bounds in-flight per host so a
# burst paces through each ATS instead of storming it. Mirrors the _s2 / reddit pattern, keyed by host.
_HOST_MAX_INFLIGHT = 4
_host_semas: dict = {}
_host_semas_lock = threading.Lock()


def _sema_for(url: str) -> threading.BoundedSemaphore:
    """Per-hostname in-flight cap so a fan-out / burst paces through each shared ATS host instead of
    storming it; distinct hosts get distinct caps (they do not throttle each other)."""
    host = (urlparse(url).hostname or "").lower()
    s = _host_semas.get(host)
    if s is None:
        with _host_semas_lock:
            s = _host_semas.get(host)
            if s is None:
                s = threading.BoundedSemaphore(_HOST_MAX_INFLIGHT)
                _host_semas[host] = s
    return s
CACHE_TTL = 10800  # 3h — job boards change daily

# Research-role title filter (full-time RS/RE/MTS; excludes sales/marketing/ops).
RESEARCH_RE = re.compile(
    r"research scientist|research engineer|research manager|research lead|research director|"
    r"applied research|member of technical staff|\bMTS\b|"
    r"machine learning (?:scientist|engineer|researcher)|\bML (?:scientist|engineer|researcher)|"
    r"\bAI (?:scientist|researcher)\b|(?:scientist|researcher), |"
    r"post[- ]?training|pre[- ]?training|interpretability|"
    r"alignment (?:scientist|researcher|engineer)",
    re.IGNORECASE,
)

# The deployer's configured target geographies (Singapore + Canada), for tagging/ranking.
SGCA_RE = re.compile(
    r"singapore|\bcanada\b|toronto|montr[eé]al|vancouver|ottawa|waterloo|"
    r"ontario|qu[eé]bec|british columbia|calgary|edmonton",
    re.IGNORECASE,
)
REMOTE_RE = re.compile(r"\bremote\b|anywhere|distributed|work from home", re.IGNORECASE)

# (label, ats_type, token) — tokens verified live 2026-05-29. Note the exact
# slugs: togetherai / scaleai / inflectionai / deepmind (NOT together / scale-ai
# / googledeepmind). health_check probes one; add new labs by appending a row.
SITES: list[tuple[str, str, str]] = [
    ("Cohere", "ashby", "cohere"),                 # ⭐ secondaryLocations Toronto/Montreal
    ("Google DeepMind", "gh", "deepmind"),         # ⭐ RE Applied AI @ Singapore
    ("Reka", "ashby", "reka"),                     # ⭐ SG + all-remote
    ("Mistral", "lever", "mistral"),               # SG Forward-Deployed
    ("Scale AI", "gh", "scaleai"),
    ("Perplexity", "ashby", "perplexity"),
    ("Anthropic", "gh", "anthropic"),
    ("xAI", "gh", "xai"),
    ("Together AI", "gh", "togetherai"),            # SG LLM Inference
    ("Stability AI", "gh", "stabilityai"),
    ("Character.AI", "ashby", "character"),
    ("Inflection", "gh", "inflectionai"),
    ("Hugging Face", "workable", "huggingface"),    # all-remote, small
    ("ServiceNow Research", "smartrecruiters", "ServiceNow"),  # Montreal (former Element AI)
]


@dataclass
class _Job:
    lab: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    remote: bool = False
    team: str = ""
    employment_type: str = ""
    summary: str = ""
    raw: dict = field(default_factory=dict)  # original ATS API record (lossless escape hatch)

    def to_doc(self) -> Document:
        locs = [l for l in dict.fromkeys(self.locations) if l]  # dedup, keep order
        loc_blob = " ".join(locs)
        sgca = bool(SGCA_RE.search(loc_blob))
        remote = self.remote or bool(REMOTE_RE.search(loc_blob))
        tags = ["overseas-job", f"lab:{self.lab.lower().replace(' ', '_')}"]
        if sgca:
            tags.append("sg-or-canada")
        if remote:
            tags.append("remote-ok")
        loc_str = ", ".join(locs) or "—"
        content = f"Location(s): {loc_str}"
        if self.team:
            content += f"  ·  Team: {self.team}"
        if self.employment_type:
            content += f"  ·  {self.employment_type}"
        if self.summary:
            content += f"\n\n{self.summary}"
        return Document(
            source="overseas_ai_jobs",
            source_id=self.url,
            url=self.url,
            title=f"[{self.lab}] {self.title}",
            content=content,
            author=self.lab,
            tags=tags,
            metadata={
                "lab": self.lab,
                "locations": locs,
                "remote": remote,
                "sg_or_canada": sgca,
                "team": self.team,
                "employment_type": self.employment_type,
                "raw": jsonsafe(self.raw),
            },
        )


async def _aget_body(url: str) -> Optional[bytes]:
    """Async egress twin of ``_get``: the answer's body, or None (the source's failure -> [] contract).

    The RAW ``httpx.get`` is the shared async leaf ``http.aget`` (pooled AsyncClient + SSRF guard +
    cache_only + 30MB cap), with the SAME source User-Agent + timeout and the SAME per-host in-flight
    cap. The per-host ``threading.BoundedSemaphore`` is the VERY SAME object the sync ``_get`` holds (via
    ``_sema_for``), so a mixed sync+async burst can never DOUBLE a shared ATS host's in-flight
    (greenhouse collapses 7 sites onto one host) — the reddit shared-cap rule. The shared bounded,
    cancellation-safe helper acquires it off-loop with the existing wire timeout as the finite queue
    budget. NOTE: an egress FAILURE taps ``diag.note('http.get')`` (the shared leaf's evidence); the URL
    + status are captured either way, so an /eye-fix drill still sees the wall."""
    sema = _sema_for(url)
    try:
        async with bounded_async_slot(
            sema,
            TIMEOUT,
            lambda waited: GateBusy(f"ATS gate busy after {waited:.1f}s for {urlparse(url).hostname}"),
        ):
            resp = await http.aget(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            return resp.content if resp is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("overseas_ai_jobs GET failed (%s): %s", url, exc)
        st = getattr(getattr(exc, "response", None), "status_code", None)
        diag.note("overseas_ai_jobs.fetch", url=url, status=st, exc=exc)
        return None


def _decode_then(parse: Any, body: bytes, *args: Any) -> tuple[Any, Optional[str]]:
    """``parse(<body's JSON>, *args)`` and the decode error (None when the body was JSON). A body that
    is not JSON is parsed as None, as ``http.aget_json`` answered it. Module-level: it may run in a
    worker of the process lane (``aparse``)."""
    try:
        data = json.loads(body)  # what httpx's Response.json() does with the same bytes
    except Exception as exc:  # noqa: BLE001
        return parse(None, *args), f"{type(exc).__name__}: {exc}"
    return parse(data, *args), None


def _research_roles(data: Any) -> Optional[int]:
    """How many jobs of a Greenhouse board answer (``{"jobs": [...]}``) have a research title, or None
    when the answer carries no jobs list. Module-level: it may run in a worker of the process lane."""
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    return sum(1 for j in jobs if isinstance(j, dict) and RESEARCH_RE.search((j.get("title") or "").strip()))


async def _aget_parsed(url: str, parse: Any, *args: Any) -> Any:
    """GET ``url`` and ``parse(data, *args)`` its JSON (``data`` None when the GET failed or the body is
    not JSON). The decode and the parse run together through ``aparse``: here for a small answer, in the
    process lane for a large one (a greenhouse board with ``content=true`` is up to ~10 MB), so neither
    holds the loop. ``parse`` is a module-level function of plain data."""
    body = await _aget_body(url)
    if body is None:
        return parse(None, *args)
    out, bad = await aparse(len(body), functools.partial(_decode_then, parse), body, *args)
    if bad is not None:  # the same evidence http.aget_json leaves for a body that is not JSON
        logger.warning("http.get_json parse failed (%s): %s", url, bad)
        diag.note("http.get_json", url=url, status=200, body=body.decode("utf-8", "replace"),
                  exc=ValueError(bad))
    return out


# ── THE ATS PARSERS (one per ATS) and their async fetchers ──────────────────────────────────────
# Each parser maps one decoded answer (None: the GET failed) to research ``_Job``s: the title filter,
# the location walk, the ``_Job`` build. Pure CPU of plain data, module-level, so ``_aget_parsed``
# runs it in a worker process when the answer is large. The fetchers only GET and hand over.
def _gh_jobs(data: Any, token: str, lab: str) -> list[_Job]:
    jobs = (data.get("jobs") if data else None) or []
    out: list[_Job] = []
    for j in jobs:
        title = (j.get("title") or "").strip()
        if not RESEARCH_RE.search(title):
            continue
        url = j.get("absolute_url") or ""
        if not url:
            continue
        locs = []
        lo = j.get("location") or {}
        if isinstance(lo, dict) and lo.get("name"):
            locs.append(lo["name"])
        for off in (j.get("offices") or []):
            if isinstance(off, dict) and off.get("name"):
                locs.append(off["name"])
        summary = html_fragment_text(html.unescape(j.get("content") or ""))
        out.append(_Job(lab, title, url, locs, summary=summary, raw=j))
    return out


async def _agh(token: str, lab: str) -> list[_Job]:
    return await _aget_parsed(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
                              _gh_jobs, token, lab)


def _ashby_jobs(data: Any, token: str, lab: str) -> list[_Job]:
    jobs = (data.get("jobs") if data else None) or []
    out: list[_Job] = []
    for j in jobs:
        title = (j.get("title") or "").strip()
        if not RESEARCH_RE.search(title):
            continue
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        if not url:
            continue
        locs = []
        if j.get("location"):
            locs.append(j["location"])
        for sl in (j.get("secondaryLocations") or []):
            if isinstance(sl, dict):
                loc = sl.get("location")
                if isinstance(loc, str):
                    locs.append(loc)
                elif isinstance(loc, dict):
                    locs.append(loc.get("locationName") or loc.get("name") or "")
            elif isinstance(sl, str):
                locs.append(sl)
        out.append(_Job(lab, title, url, locs, bool(j.get("isRemote")),
                        team=j.get("team") or "", employment_type=j.get("employmentType") or "",
                        raw=j))
    return out


async def _aashby(token: str, lab: str) -> list[_Job]:
    return await _aget_parsed(
        f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false", _ashby_jobs, token, lab)


def _lever_jobs(data: Any, token: str, lab: str) -> list[_Job]:
    jobs = (data if data else None) or []
    out: list[_Job] = []
    for j in jobs:
        title = (j.get("text") or "").strip()
        if not RESEARCH_RE.search(title):
            continue
        url = j.get("hostedUrl") or j.get("applyUrl") or ""
        if not url:
            continue
        cats = j.get("categories") or {}
        locs = []
        if cats.get("location"):
            locs.append(cats["location"])
        for l in (cats.get("allLocations") or []):
            if isinstance(l, str):
                locs.append(l)
        remote = (j.get("workplaceType") == "remote") or any("remote" in str(l).lower() for l in locs)
        out.append(_Job(lab, title, url, locs, remote, team=cats.get("team") or "", raw=j))
    return out


async def _alever(token: str, lab: str) -> list[_Job]:
    return await _aget_parsed(f"https://api.lever.co/v0/postings/{token}?mode=json", _lever_jobs, token, lab)


def _smartrecruiters_page(d: Any, token: str, lab: str) -> Optional[tuple[list[_Job], int, int]]:
    """One page: its research jobs, how many postings it held, and ``totalFound``; None when the GET
    failed (the paging loop stops, as the sync ``if not r: break``)."""
    if d is None:
        return None
    items = d.get("content") or []
    out: list[_Job] = []
    for j in items:
        title = (j.get("name") or "").strip()
        if not RESEARCH_RE.search(title):
            continue
        ref = j.get("ref") or ""
        jid = ref.rstrip("/").split("/")[-1] if ref else (j.get("id") or "")
        url = f"https://jobs.smartrecruiters.com/{token}/{jid}" if jid else ref
        lo = j.get("location") or {}
        full = lo.get("fullLocation") or " ".join(
            x for x in [lo.get("city"), lo.get("region"), lo.get("country")] if x)
        out.append(_Job(lab, title, url, [full] if full else [], bool(lo.get("remote")), raw=j))
    return out, len(items), (d.get("totalFound") or 0) if items else 0


async def _asmartrecruiters(token: str, lab: str, max_pages: int = 6) -> list[_Job]:
    out: list[_Job] = []
    offset = 0
    while offset < max_pages * 100:
        page = await _aget_parsed(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}",
            _smartrecruiters_page, token, lab)
        if page is None:  # egress failure (mirror sync's ``if not r: break`` — a Response is never falsy)
            break
        jobs, n_items, total = page
        if not n_items:
            break
        out.extend(jobs)
        offset += n_items
        if offset >= total:
            break
    return out


def _workable_jobs(data: Any, token: str, lab: str) -> list[_Job]:
    if not data:
        return []
    jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
    out: list[_Job] = []
    for j in jobs:
        title = (j.get("title") or j.get("name") or "").strip()
        if not title or not RESEARCH_RE.search(title):
            continue
        sc = j.get("shortcode")
        url = j.get("url") or j.get("application_url") or (f"https://apply.workable.com/{token}/j/{sc}/" if sc else "")
        if not url:
            continue
        loc = j.get("location") or {}
        locs = []
        remote = False
        if isinstance(loc, dict):
            ls = loc.get("location_str") or loc.get("city") or loc.get("country")
            if ls:
                locs.append(ls)
            remote = bool(loc.get("workplace_type") == "remote" or loc.get("telecommuting"))
        elif isinstance(loc, str):
            locs.append(loc)
        out.append(_Job(lab, title, url, locs, remote, raw=j))
    return out


async def _aworkable(token: str, lab: str) -> list[_Job]:
    return await _aget_parsed(f"https://apply.workable.com/api/v1/widget/accounts/{token}",
                              _workable_jobs, token, lab)


AHELPERS = {
    "gh": _agh,
    "ashby": _aashby,
    "lever": _alever,
    "smartrecruiters": _asmartrecruiters,
    "workable": _aworkable,
}


class OverseasAIJobsAdapter(SourceBase):
    name = "overseas_ai_jobs"
    needs_credentials = False
    description = (
        "海外工业界 AI lab 全职研究岗 (RS/RE/MTS)：Cohere / DeepMind / Reka / "
        "Mistral / Anthropic / xAI / Together / Scale + 更多, 跨 Greenhouse/Ashby/"
        "Lever/SmartRecruiters/Workable; 标注 Singapore/Canada/remote (按部署方配置的目标地区)"
    )

    async def _a_all_docs(self) -> list[Document]:
        """Native-async twin of ``_all_docs``: the SAME cache key + the SAME 14-site assembly, changing
        ONLY the blocking waits (the load-bearing off-loop discipline). Mirrors ``_all_docs``:
          - the disk cache read/write -> anyio.to_thread.run_sync (SAME 'overseas_ai_jobs'/'all' key,
            SAME model_dump(mode='json') value shape — so async and sync SHARE the cache entry);
          - the ThreadPoolExecutor(max_workers=14) site fan-out -> asyncio.gather over the SAME SITES
            order (gather preserves order, so the assembly + sort are byte-identical to ex.map's);
          - each site's raw ``_get`` egress -> the async ``_aget_parsed`` (shared pool + per-host cap; the
            JSON decode and the ATS parser through ``aparse``: off the loop for a large board);
          - the per-site failure isolation (the try/except inside the worker) stays, in ``_afetch_site``.
        Every coroutine runs on the ONE loop thread, so (unlike the sync worker threads) NO
        copy_context() is needed: the cache ``fresh`` / cache_only contextvars propagate naturally.
        Assembly (to_doc), the sort, and the model_dump payload build are pure CPU and stay ON the loop;
        only the disk read + write themselves go off-loop. Byte-identical result to ``_all_docs``."""
        key = cache.make_key("overseas_ai_jobs", "all")
        cached = await cache.aget_once(key)  # disk read OFF loop
        if cached is not None:
            return [Document.model_validate(d) for d in cached]  # pure CPU, on loop
        docs: list[Document] = []

        async def _afetch_site(item: tuple[str, str, str]) -> list[_Job]:
            lab, ats, token = item
            fetch = AHELPERS.get(ats)
            if not fetch:
                return []
            try:
                return await fetch(token, lab)
            except Exception as exc:  # noqa: BLE001
                logger.warning("overseas_ai_jobs %s (%s) failed: %s", lab, ats, exc)
                return []

        site_jobs = await asyncio.gather(*[_afetch_site(it) for it in SITES])
        for jobs in site_jobs:
            for job in jobs:
                docs.append(job.to_doc())
        # SG/Canada first, then remote-friendly, then the rest.
        docs.sort(key=lambda d: (0 if "sg-or-canada" in d.tags else 1,
                                 0 if "remote-ok" in d.tags else 1))
        payload = [d.model_dump(mode="json") for d in docs]  # pure CPU, on loop
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, payload, ttl=CACHE_TTL), limiter=portal.IO_LANE)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, this is the only one now) (routes this source to the fetcher's native dispatch
        branch): the async ``_a_all_docs`` egress runs on the loop, then the SAME
        ``keyword_score_filter`` (pure CPU) ranks + truncates, byte-identical to ``search``."""
        return keyword_score_filter(await self._a_all_docs(), query)[:limit]

    async def afetch_url(self, url: str) -> Optional[Document]:
        return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # The board itself must answer with its jobs list: _gh reads ANY failure as an empty board,
        # so until 2026-10-04 this read True on a 429, a 5xx or a timeout. A failed GET is False here;
        # the health funnel re-reads one that failed only on a 429 (or a busy gate) as not verified.
        url = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true"
        try:
            body = await _aget_body(url)
            if body is None:
                return False, "the Anthropic Greenhouse board did not answer (see the overseas_ai_jobs.fetch diag note)"
            n, bad = await aparse(len(body), functools.partial(_decode_then, _research_roles), body)
            if bad is not None:
                return False, bad
            if n is None:
                return False, "the Anthropic Greenhouse board answered without a jobs list"
            return True, f"OK ({len(SITES)} labs configured; Anthropic board → {n} research roles)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from omniseek.core.fetcher import register_adapter

register_adapter(OverseasAIJobsAdapter())
