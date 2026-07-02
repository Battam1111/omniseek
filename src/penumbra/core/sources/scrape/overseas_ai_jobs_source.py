"""Overseas industry AI-lab full-time research roles (RS / RE / MTS).

Phase 4 P14 (2026-05-29). Covers one job category the other adapters lack:
**full-time research scientist / research engineer / member-of-technical-staff
roles at overseas industry AI labs** — distinct from `ai_residencies`
(early-career programs), `academic_jobs` (faculty/postdoc), and the Chinese ATS
adapters (mokahr / feishu / bytedance).

opus sub-agent (40+ calls, 2026-05-29) verified each lab's ATS + board token by
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

import html
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter

logger = logging.getLogger(__name__)

TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; PenumbraEye/0.1)"

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


def _get(url: str) -> Optional[httpx.Response]:
    try:
        with _sema_for(url):  # per-host in-flight cap: bound concurrent requests to each shared ATS host
            r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                          follow_redirects=True)
        r.raise_for_status()
        return r
    except Exception as exc:  # noqa: BLE001
        logger.warning("overseas_ai_jobs GET failed (%s): %s", url, exc)
        return None


def _gh(token: str, lab: str) -> list[_Job]:
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    jobs = (r.json().get("jobs") if r else None) or []
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
        summary = html.unescape(j.get("content") or "")
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()
        out.append(_Job(lab, title, url, locs, summary=summary, raw=j))
    return out


def _ashby(token: str, lab: str) -> list[_Job]:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false")
    jobs = (r.json().get("jobs") if r else None) or []
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


def _lever(token: str, lab: str) -> list[_Job]:
    r = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
    jobs = (r.json() if r else None) or []
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


def _smartrecruiters(token: str, lab: str, max_pages: int = 6) -> list[_Job]:
    out: list[_Job] = []
    offset = 0
    while offset < max_pages * 100:
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}")
        if not r:
            break
        d = r.json()
        items = d.get("content") or []
        if not items:
            break
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
        total = d.get("totalFound") or 0
        offset += len(items)
        if offset >= total:
            break
    return out


def _workable(token: str, lab: str) -> list[_Job]:
    r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    if not r:
        return []
    data = r.json()
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


HELPERS = {
    "gh": _gh,
    "ashby": _ashby,
    "lever": _lever,
    "smartrecruiters": _smartrecruiters,
    "workable": _workable,
}


class OverseasAIJobsAdapter:
    name = "overseas_ai_jobs"
    needs_credentials = False
    description = (
        "海外工业界 AI lab 全职研究岗 (RS/RE/MTS) — Cohere / DeepMind / Reka / "
        "Mistral / Anthropic / xAI / Together / Scale + 更多, 跨 Greenhouse/Ashby/"
        "Lever/SmartRecruiters/Workable; 标注 Singapore/Canada/remote (按部署方配置的目标地区)"
    )

    def _all_docs(self) -> list[Document]:
        key = cache.make_key("overseas_ai_jobs", "all")
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]
        docs: list[Document] = []

        # Each ATS is a distinct host with no shared rate limit, so the 14-site
        # fan-out is pure independent network wait → parallelize it. One worker
        # fetches one site; failures stay isolated per site (the try/except moves
        # into the worker, identical to the old per-site guard). copy_context()
        # is captured HERE in the search thread (where the fetcher has set the
        # cache `fresh` contextvar) and one private copy is handed to each worker
        # via ctx.run — never copied inside the worker, where it would capture an
        # empty context and silently defeat fresh=True. Assembly (to_doc) + sort
        # + cache are unchanged and stay on this thread.
        def _fetch_site(item: tuple[str, str, str]) -> list[_Job]:
            lab, ats, token = item
            fetch = HELPERS.get(ats)
            if not fetch:
                return []
            try:
                return fetch(token, lab)
            except Exception as exc:  # noqa: BLE001
                logger.warning("overseas_ai_jobs %s (%s) failed: %s", lab, ats, exc)
                return []

        contexts = [copy_context() for _ in SITES]
        with ThreadPoolExecutor(max_workers=min(len(SITES), 14)) as ex:
            site_jobs = list(ex.map(lambda ctx, it: ctx.run(_fetch_site, it),
                                    contexts, SITES))
        for jobs in site_jobs:
            for job in jobs:
                docs.append(job.to_doc())
        # SG/Canada first, then remote-friendly, then the rest.
        docs.sort(key=lambda d: (0 if "sg-or-canada" in d.tags else 1,
                                 0 if "remote-ok" in d.tags else 1))
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=CACHE_TTL)
        return docs

    def search(self, query: str, limit: int = 10) -> list[Document]:
        return keyword_score_filter(self._all_docs(), query)[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            jobs = _gh("anthropic", "Anthropic")
            return True, f"OK ({len(SITES)} labs configured; Anthropic board → {len(jobs)} research roles)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from penumbra.core.fetcher import register_adapter

register_adapter(OverseasAIJobsAdapter())
