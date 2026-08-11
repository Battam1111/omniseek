"""AI residency / fellows / scholars / catalyst programs tracker (config-driven).

The 时间敏感 piece of Penumbra: residency application windows are rigid, and
missing one means a year's wait. Programs live as ROWS in ``ai_residencies.json``
(P1-4 rework, 2026-06-10: the former file hand-coded one fetcher per program,
~1050 lines; now there are five generic fetchers and adding a program is a one-row
JSON edit, same as rss_bundles / org_watch / scrape_sites):

  kind=greenhouse  boards-api.greenhouse.io public JSON  (slug, title_filter)
  kind=ashby       api.ashbyhq.com posting-api            (slug, title_filter)
  kind=lever       api.lever.co/v0/postings               (slug, title_filter)
  kind=workable    apply.workable.com widget API          (slug)
  kind=static      a program page; status via the shared open/closed/upcoming
                   heuristics (cn_status=true uses the Chinese variants), deadline
                   via keyword-windowed date extraction

Row fields: kind, lab, tier, chinese_friendly (+ slug/title_filter/program for
boards; url/program/title/location, optional default_status/cn_status for static).
A separate monitor script reuses this adapter to detect changes and Bark-notify.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx
from bs4 import BeautifulSoup

from penumbra.core import cache, diag, http, relevance
from penumbra.core._guard import GateBusy, bounded_async_slot, bounded_slot
from penumbra.core.normalize import Document, jsonsafe

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("ai_residencies.json")
TIMEOUT = 20
USER_AGENT = "penumbra/0.1 (automated retrieval / residency tracking)"


@dataclass
class ResidencyPosition:
    """Internal representation of one residency posting."""

    lab: str                         # "Anthropic" / "MATS" / etc.
    program: str                     # "Fellows" / "Summer 2026" / etc.
    title: str
    url: str
    status: str                      # "open" / "closed" / "rolling" / "upcoming" / "unknown"
    deadline: Optional[datetime] = None
    location: Optional[str] = None
    summary: str = ""
    chinese_friendly: Optional[bool] = None  # remote/global/visa-sponsoring: True
    tier: int = 2
    raw: dict = field(default_factory=dict)  # original source record/page (lossless escape hatch)

    def to_penumbra_doc(self) -> Document:
        tags = [
            "residency",
            f"lab:{self.lab.lower().replace(' ', '_')}",
            f"status:{self.status}",
            f"tier:{self.tier}",
        ]
        if self.deadline:
            tags.append(f"deadline:{self.deadline.strftime('%Y-%m-%d')}")
        if self.location:
            tags.append(f"location:{self.location.lower().replace(' ', '_')}")
        if self.chinese_friendly is True:
            tags.append("chinese-phd-friendly")
        elif self.chinese_friendly is False:
            tags.append("chinese-phd-blocked")

        return Document(
            source="ai_residencies",
            source_id=self.url,
            url=self.url,
            title=f"[{self.lab}] {self.title}",
            content=self.summary or f"{self.lab} {self.program} (status: {self.status})",
            author=self.lab,
            date=self.deadline,
            tags=tags,
            metadata={
                "lab": self.lab,
                "program": self.program,
                "status": self.status,
                "deadline": self.deadline.isoformat() if self.deadline else None,
                "location": self.location,
                "chinese_friendly": self.chinese_friendly,
                "tier": self.tier,
                "raw": jsonsafe(self.raw),
            },
        )


_HOST_MAX_INFLIGHT = 4
_host_semas: dict = {}
_host_semas_lock = threading.Lock()


def _sema_for(url: str) -> threading.BoundedSemaphore:
    """Per-hostname in-flight cap: this source fans out over rows that collapse onto a few shared ATS
    hosts (greenhouse / ashby / lever / workable), so a burst can storm one ATS. A per-host semaphore
    (not one global cap, so distinct hosts do not throttle each other) bounds in-flight per ATS host."""
    host = (urlparse(url).hostname or "").lower()
    s = _host_semas.get(host)
    if s is None:
        with _host_semas_lock:
            s = _host_semas.get(host)
            if s is None:
                s = threading.BoundedSemaphore(_HOST_MAX_INFLIGHT)
                _host_semas[host] = s
    return s


def _http_get(url: str, *, timeout: int = TIMEOUT, **kwargs) -> Optional[httpx.Response]:
    try:
        with bounded_slot(
            _sema_for(url),
            timeout,
            lambda waited: GateBusy(f"ATS gate busy after {waited:.1f}s for {urlparse(url).hostname}"),
        ):
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,zh;q=0.8"},
                timeout=timeout,
                follow_redirects=True,
                **kwargs,
            )
        resp.raise_for_status()
        return resp
    except Exception as exc:  # noqa: BLE001
        logger.warning("HTTP GET failed for %s: %s", url, exc)
        st = getattr(getattr(exc, "response", None), "status_code", None)
        diag.note("ai_residencies.fetch", url=url, status=st, exc=exc)
        return None


async def _ahttp_get(url: str, *, timeout: int = TIMEOUT, **kwargs) -> Optional[httpx.Response]:
    """Native-async twin of ``_http_get`` (S4b): the async egress leaf for asearch's per-row fan-out.
    Mirrors ``_http_get`` faithfully, moving ONLY the blocking waits off the loop so no thread is held:

      - the per-host in-flight cap -> the SAME ``_sema_for`` threading BoundedSemaphore, acquired by
        the shared bounded, cancellation-safe async gate helper with the request timeout as its queue
        budget;
      - the raw ``httpx.get`` -> ``http.aget`` (shared async pool + SSRF guard + cache_only + 30MB cap),
        keeping the SAME User-Agent + Accept-Language + timeout; follow_redirects=True is the shared
        async client's default, matching _http_get's explicit follow_redirects=True.

    http.aget already logs + diag.notes the failure->None (label ``http.get`` not ``ai_residencies.fetch``,
    same trade the hk_universities async twin makes) and applies raise_for_status, so this returns
    Optional[Response] with _http_get's exact None-on-failure contract; the fetchers' ``.json()`` /
    ``.text`` bodies stay byte-identical. cache_only now short-circuits to None here (http.aget's egress
    guard) — a strict improvement the raw sync httpx.get lacked."""
    sema = _sema_for(url)
    try:
        async with bounded_async_slot(
            sema,
            timeout,
            lambda waited: GateBusy(f"ATS gate busy after {waited:.1f}s for {urlparse(url).hostname}"),
        ):
            return await http.aget(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,zh;q=0.8"},
                timeout=timeout,
                **kwargs,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("HTTP GET failed for %s: %s", url, exc)
        st = getattr(getattr(exc, "response", None), "status_code", None)
        diag.note("ai_residencies.fetch", url=url, status=st, exc=exc)
        return None


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Status + deadline heuristics (shared by every fetcher)
# ---------------------------------------------------------------------------

_STATUS_OPEN_RE = re.compile(
    r"(?:applications?|submissions?|nomination?s?)\s+(?:\w+\s+){0,3}\b(?:open|accepting|"
    r"now accepting|now open)\b|\bapply now\b|\bnow accepting\b|\bapply by\b|"
    r"\bapplication deadline\b",
    re.IGNORECASE,
)
_STATUS_CLOSED_RE = re.compile(
    r"(?:applications?|submissions?|nomination?s?)\s+(?:\w+\s+){0,3}\bclosed\b|\bapplication closed\b",
    re.IGNORECASE,
)
_STATUS_UPCOMING_RE = re.compile(
    r"\b(?:coming soon|next\s+(?:cycle|cohort|round|deadline)|opens\s+(?:in|on|next)|"
    r"will\s+open|to be announced|tbd|tba)\b",
    re.IGNORECASE,
)
_STATUS_CN_OPEN_RE = re.compile(r"招生|申请|启动|开放")
_STATUS_CN_CLOSED_RE = re.compile(r"已结束|截止|关闭")


def _detect_open_closed_status(body_text: str, cn: bool = False) -> str:
    """Unified status heuristic: 'open' / 'closed' / 'upcoming' / 'unknown'.
    'closed' wins when both appear (close statements are the more specific ones)."""
    if cn:
        if _STATUS_CN_CLOSED_RE.search(body_text):
            return "closed"
        if _STATUS_CN_OPEN_RE.search(body_text):
            return "open"
        return "unknown"
    if _STATUS_CLOSED_RE.search(body_text):
        return "closed"
    if _STATUS_OPEN_RE.search(body_text):
        return "open"
    if _STATUS_UPCOMING_RE.search(body_text):
        return "upcoming"
    return "unknown"


_DATE_PATTERNS = [
    # "June 7, 2026" / "Jun 7th" (year optional; st/nd/rd/th allowed). The
    # year-optional variant matters for MATS-style "Apply by June 7th EOD AOE".
    (re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
                r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
                r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I), "mdy_opt_year"),
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"), "ymd"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
                r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
                r"dec(?:ember)?)\s+(\d{4})\b", re.I), "dmy"),
]

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _extract_deadline_from_text(text: str) -> Optional[datetime]:
    """Heuristic date extraction near 'deadline' / 'apply by' / 'closes' keywords.

    Only dates within a 200-char window of a deadline keyword count (avoids
    random dates like "founded in 2018"). Among candidates, the earliest one
    that is still in the future wins; if all are past, the most recent past one.
    """
    if not text:
        return None
    keyword_re = re.compile(
        r"deadline|apply by|applications? (?:due|close|closes|closing|closed|end)|"
        r"submission deadline|due date|due by|截止|关闭|结束于|截至",
        re.I,
    )
    keyword_positions = [m.start() for m in keyword_re.finditer(text.lower())]
    if not keyword_positions:
        return None

    candidates: list[datetime] = []
    for kw_pos in keyword_positions:
        window = text[max(0, kw_pos - 100):min(len(text), kw_pos + 200)]
        for pattern, kind in _DATE_PATTERNS:
            for m in pattern.finditer(window):
                date = _parse_date_match(m, kind)
                if date is not None:
                    candidates.append(date)
    if not candidates:
        return None
    now = datetime.now(timezone.utc)
    future = [d for d in candidates if d >= now]
    return min(future) if future else max(candidates)


def _parse_date_match(m: re.Match, kind: str) -> Optional[datetime]:
    try:
        if kind == "mdy_opt_year":
            month = _MONTH_NAMES.get(m.group(1).lower(), _MONTH_NAMES.get(m.group(1).lower()[:3]))
            if month is None:
                return None
            day = int(m.group(2))
            if m.group(3):
                year = int(m.group(3))
            else:
                # Year missing: if the date-in-current-year is >30d past, assume next year.
                now = datetime.now(timezone.utc)
                candidate = datetime(now.year, month, day, 23, 59, 0, tzinfo=timezone.utc)
                year = now.year + 1 if candidate < now - timedelta(days=30) else now.year
        elif kind == "ymd":
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif kind == "dmy":
            day = int(m.group(1))
            month = _MONTH_NAMES.get(m.group(2).lower(), _MONTH_NAMES.get(m.group(2).lower()[:3]))
            if month is None:
                return None
            year = int(m.group(3))
        else:
            return None
        return datetime(year, month, day, 23, 59, 0, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Generic fetchers — one per row ``kind``; every row field is plain data
# ---------------------------------------------------------------------------


def _row_common(row: dict, *, title: str, url: str, status: str, summary: str,
                location: Optional[str], deadline: Optional[datetime],
                raw: dict, program: Optional[str] = None) -> ResidencyPosition:
    return ResidencyPosition(
        lab=row["lab"],
        program=program or row.get("program") or title,
        title=title,
        url=url,
        status=status,
        deadline=deadline,
        location=location,
        summary=summary,
        chinese_friendly=row.get("chinese_friendly"),
        tier=row.get("tier", 2),
        raw=raw,
    )


def _fetch_greenhouse(row: dict) -> list[ResidencyPosition]:
    """boards-api.greenhouse.io/v1/boards/{slug}/jobs — stable public JSON, no auth."""
    resp = _http_get(f"https://boards-api.greenhouse.io/v1/boards/{row['slug']}/jobs?content=true")
    if resp is None:
        return []
    try:
        jobs = resp.json().get("jobs") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Greenhouse JSON parse failed (%s): %s", row["slug"], exc)
        return []
    filter_re = re.compile(row["title_filter"], re.IGNORECASE) if row.get("title_filter") else None
    out = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        url = job.get("absolute_url") or ""
        if not url or (filter_re and not filter_re.search(title)):
            continue
        loc = job.get("location") or {}
        summary = _strip_html(job.get("content") or "")
        out.append(_row_common(row, title=title, url=url, status="open",  # Greenhouse lists open only
                               summary=summary, location=loc.get("name") if isinstance(loc, dict) else None,
                               deadline=_extract_deadline_from_text(summary), raw=job, program=title))
    return out


def _fetch_ashby(row: dict) -> list[ResidencyPosition]:
    """api.ashbyhq.com/posting-api/job-board/{slug} — public JSON."""
    resp = _http_get(f"https://api.ashbyhq.com/posting-api/job-board/{row['slug']}", timeout=30)
    if resp is None:
        return []
    try:
        jobs = resp.json().get("jobs") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ashby JSON parse failed (%s): %s", row["slug"], exc)
        return []
    filter_re = re.compile(row["title_filter"], re.IGNORECASE) if row.get("title_filter") else None
    out = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        if not url or (filter_re and not filter_re.search(title)):
            continue
        summary = _strip_html(job.get("descriptionHtml") or "")
        out.append(_row_common(row, title=title, url=url, status="open",
                               summary=summary, location=job.get("location") or job.get("locationName"),
                               deadline=_extract_deadline_from_text(summary), raw=job))
    return out


def _fetch_lever(row: dict) -> list[ResidencyPosition]:
    """api.lever.co/v0/postings/{slug}?mode=json — public JSON list."""
    resp = _http_get(f"https://api.lever.co/v0/postings/{row['slug']}?mode=json", timeout=30)
    if resp is None:
        return []
    try:
        postings = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lever JSON parse failed (%s): %s", row["slug"], exc)
        return []
    filter_re = re.compile(row["title_filter"], re.IGNORECASE) if row.get("title_filter") else None
    out = []
    for posting in postings:
        title = (posting.get("text") or "").strip()
        url = posting.get("hostedUrl") or posting.get("applyUrl") or ""
        if not url or (filter_re and not filter_re.search(title)):
            continue
        body = posting.get("descriptionBodyPlain") or posting.get("descriptionPlain") or ""
        summary = re.sub(r"\s+", " ", body).strip()
        out.append(_row_common(row, title=title, url=url, status="open",
                               summary=summary, location=(posting.get("categories") or {}).get("location"),
                               deadline=_extract_deadline_from_text(summary), raw=posting))
    return out


def _fetch_workable(row: dict) -> list[ResidencyPosition]:
    """apply.workable.com/api/v1/widget/accounts/{slug} — defensive schema handling."""
    resp = _http_get(f"https://apply.workable.com/api/v1/widget/accounts/{row['slug']}", timeout=20)
    if resp is None:
        return []
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    jobs: list = []
    if isinstance(data.get("jobs"), list):
        jobs = data["jobs"]
    elif isinstance(data.get("widget"), dict) and isinstance(data["widget"].get("jobs"), list):
        jobs = data["widget"]["jobs"]
    elif isinstance(data.get("results"), list):
        jobs = data["results"]

    out = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = job.get("title") or job.get("name") or job.get("shortcode")
        if not title:
            continue
        url = job.get("url") or job.get("application_url") or job.get("apply_url")
        if not url and job.get("shortcode"):
            url = f"https://apply.workable.com/{row['slug']}/j/{job['shortcode']}/"
        if not url:
            continue
        loc = job.get("location") or job.get("city")
        if isinstance(loc, dict):
            location = loc.get("city") or loc.get("country") or loc.get("location_str") or ""
        else:
            location = loc or ""
        location = location or row.get("default_location") or ""
        summary = _strip_html(str(job.get("description") or job.get("requirements") or ""))
        out.append(_row_common(row, title=title, url=url, status="open",
                               summary=summary or f"{row['lab']}: {title}",
                               location=location, deadline=None, raw=job, program=title))
    return out


def _fetch_static(row: dict) -> list[ResidencyPosition]:
    """A program page: shared open/closed/upcoming heuristics + deadline window."""
    resp = _http_get(row["url"])
    if resp is None:
        return []
    body_text = BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True)
    status = _detect_open_closed_status(body_text, cn=bool(row.get("cn_status")))
    if status == "unknown" and row.get("default_status"):
        status = row["default_status"]
    return [_row_common(row, title=row["title"], url=row["url"], status=status,
                        summary=body_text[:500], location=row.get("location"),
                        deadline=_extract_deadline_from_text(body_text),
                        raw={"url": row["url"], "page_text": body_text})]


_KIND_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "ashby": _fetch_ashby,
    "lever": _fetch_lever,
    "workable": _fetch_workable,
    "static": _fetch_static,
}


# ---------------------------------------------------------------------------
# Native-async fetcher twins (S4b) — one per row ``kind``, awaited by asearch's
# asyncio.gather fan-out. Each mirrors its sync twin above LINE-FOR-LINE, changing
# ONLY the egress: ``_http_get(...)`` -> ``await _ahttp_get(...)``. The response
# parse (json walk / BeautifulSoup) is pure CPU and stays ON the loop, byte-identical.
# ---------------------------------------------------------------------------


async def _afetch_greenhouse(row: dict) -> list[ResidencyPosition]:
    """Native-async twin of ``_fetch_greenhouse`` (egress via ``_ahttp_get``; parse identical)."""
    resp = await _ahttp_get(f"https://boards-api.greenhouse.io/v1/boards/{row['slug']}/jobs?content=true")
    if resp is None:
        return []
    try:
        jobs = resp.json().get("jobs") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Greenhouse JSON parse failed (%s): %s", row["slug"], exc)
        return []
    filter_re = re.compile(row["title_filter"], re.IGNORECASE) if row.get("title_filter") else None
    out = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        url = job.get("absolute_url") or ""
        if not url or (filter_re and not filter_re.search(title)):
            continue
        loc = job.get("location") or {}
        summary = _strip_html(job.get("content") or "")
        out.append(_row_common(row, title=title, url=url, status="open",  # Greenhouse lists open only
                               summary=summary, location=loc.get("name") if isinstance(loc, dict) else None,
                               deadline=_extract_deadline_from_text(summary), raw=job, program=title))
    return out


async def _afetch_ashby(row: dict) -> list[ResidencyPosition]:
    """Native-async twin of ``_fetch_ashby`` (egress via ``_ahttp_get``; parse identical)."""
    resp = await _ahttp_get(f"https://api.ashbyhq.com/posting-api/job-board/{row['slug']}", timeout=30)
    if resp is None:
        return []
    try:
        jobs = resp.json().get("jobs") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ashby JSON parse failed (%s): %s", row["slug"], exc)
        return []
    filter_re = re.compile(row["title_filter"], re.IGNORECASE) if row.get("title_filter") else None
    out = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        if not url or (filter_re and not filter_re.search(title)):
            continue
        summary = _strip_html(job.get("descriptionHtml") or "")
        out.append(_row_common(row, title=title, url=url, status="open",
                               summary=summary, location=job.get("location") or job.get("locationName"),
                               deadline=_extract_deadline_from_text(summary), raw=job))
    return out


async def _afetch_lever(row: dict) -> list[ResidencyPosition]:
    """Native-async twin of ``_fetch_lever`` (egress via ``_ahttp_get``; parse identical)."""
    resp = await _ahttp_get(f"https://api.lever.co/v0/postings/{row['slug']}?mode=json", timeout=30)
    if resp is None:
        return []
    try:
        postings = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lever JSON parse failed (%s): %s", row["slug"], exc)
        return []
    filter_re = re.compile(row["title_filter"], re.IGNORECASE) if row.get("title_filter") else None
    out = []
    for posting in postings:
        title = (posting.get("text") or "").strip()
        url = posting.get("hostedUrl") or posting.get("applyUrl") or ""
        if not url or (filter_re and not filter_re.search(title)):
            continue
        body = posting.get("descriptionBodyPlain") or posting.get("descriptionPlain") or ""
        summary = re.sub(r"\s+", " ", body).strip()
        out.append(_row_common(row, title=title, url=url, status="open",
                               summary=summary, location=(posting.get("categories") or {}).get("location"),
                               deadline=_extract_deadline_from_text(summary), raw=posting))
    return out


async def _afetch_workable(row: dict) -> list[ResidencyPosition]:
    """Native-async twin of ``_fetch_workable`` (egress via ``_ahttp_get``; parse identical)."""
    resp = await _ahttp_get(f"https://apply.workable.com/api/v1/widget/accounts/{row['slug']}", timeout=20)
    if resp is None:
        return []
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    jobs: list = []
    if isinstance(data.get("jobs"), list):
        jobs = data["jobs"]
    elif isinstance(data.get("widget"), dict) and isinstance(data["widget"].get("jobs"), list):
        jobs = data["widget"]["jobs"]
    elif isinstance(data.get("results"), list):
        jobs = data["results"]

    out = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = job.get("title") or job.get("name") or job.get("shortcode")
        if not title:
            continue
        url = job.get("url") or job.get("application_url") or job.get("apply_url")
        if not url and job.get("shortcode"):
            url = f"https://apply.workable.com/{row['slug']}/j/{job['shortcode']}/"
        if not url:
            continue
        loc = job.get("location") or job.get("city")
        if isinstance(loc, dict):
            location = loc.get("city") or loc.get("country") or loc.get("location_str") or ""
        else:
            location = loc or ""
        location = location or row.get("default_location") or ""
        summary = _strip_html(str(job.get("description") or job.get("requirements") or ""))
        out.append(_row_common(row, title=title, url=url, status="open",
                               summary=summary or f"{row['lab']}: {title}",
                               location=location, deadline=None, raw=job, program=title))
    return out


async def _afetch_static(row: dict) -> list[ResidencyPosition]:
    """Native-async twin of ``_fetch_static`` (egress via ``_ahttp_get``; BeautifulSoup parse on loop)."""
    resp = await _ahttp_get(row["url"])
    if resp is None:
        return []
    body_text = BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True)
    status = _detect_open_closed_status(body_text, cn=bool(row.get("cn_status")))
    if status == "unknown" and row.get("default_status"):
        status = row["default_status"]
    return [_row_common(row, title=row["title"], url=row["url"], status=status,
                        summary=body_text[:500], location=row.get("location"),
                        deadline=_extract_deadline_from_text(body_text),
                        raw={"url": row["url"], "page_text": body_text})]


_AKIND_FETCHERS = {
    "greenhouse": _afetch_greenhouse,
    "ashby": _afetch_ashby,
    "lever": _afetch_lever,
    "workable": _afetch_workable,
    "static": _afetch_static,
}


def _rows() -> list[dict]:
    return json.loads(_DATA.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AIResidenciesAdapter:
    name = "ai_residencies"
    needs_credentials = False
    kind = "stream"
    description = (
        "AI research residency / fellows / scholars programs — "
        "Anthropic Fellows / MATS / Cohere Scholars + Catalyst Grants / NVIDIA / "
        "OpenAI Safety + Residency / Constellation Astra / EleutherAI SOAR / "
        "Ai2 PYI / 上海 AI Lab / Mistral Intern / Vector Institute (Tier 1-3; "
        "config rows in ai_residencies.json, adding a program is a one-row edit)"
    )

    def _fetch_all_positions(self) -> list[ResidencyPosition]:
        key = cache.make_key("ai_residencies", "all_positions", "v1")
        cached_payload = cache.get(key)
        if cached_payload is not None:
            return [_position_from_dict(d) for d in cached_payload]

        positions: list[ResidencyPosition] = []

        # Each row is an independent fetch (13 static pages on distinct hosts +
        # 5 board APIs); static rows additionally BeautifulSoup-parse a whole
        # page. The work is dominated by independent network wait → parallelize
        # the per-row fan-out. The single-row failure isolation (try/except) and
        # the unknown-kind warning move into the worker unchanged, so one bad row
        # never blocks the rest. copy_context() is captured HERE on the search
        # thread (where the cache `fresh` contextvar is already set) and one
        # private copy is handed to each worker via ctx.run — never copied inside
        # the worker, where it would grab an empty context and silently defeat
        # fresh=True. The 2h cache.set below and ordering are untouched.
        rows = _rows()

        def _fetch_row(row: dict) -> list[ResidencyPosition]:
            fetcher_fn = _KIND_FETCHERS.get(row.get("kind"))
            if fetcher_fn is None:
                logger.warning("ai_residencies: unknown kind %r (row %s)", row.get("kind"), row.get("lab"))
                return []
            try:
                return fetcher_fn(row)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Residency fetcher %s/%s raised: %s",
                               row.get("kind"), row.get("lab"), exc)
                return []

        contexts = [copy_context() for _ in rows]
        with ThreadPoolExecutor(max_workers=min(len(rows), 18) or 1) as ex:
            row_positions = list(ex.map(lambda ctx, r: ctx.run(_fetch_row, r),
                                        contexts, rows))
        for plist in row_positions:
            positions.extend(plist)

        cache.set(key, [_position_to_dict(p) for p in positions], ttl=7200)  # 2h cache
        return positions

    def search(self, query: str, limit: int = 10) -> list[Document]:
        positions = self._fetch_all_positions()
        if not positions:
            return []

        # Lexical match via the shared engine; tier/status/deadline stay primary.
        has_terms = bool(relevance.query_terms(query or ""))
        if has_terms:
            fields = [[(f"{p.title} {p.lab} {p.program}", 3.0), (p.summary, 1.0)]
                      for p in positions]
            q_scores = relevance.field_scores(fields, query)
        else:
            q_scores = [0.0] * len(positions)

        status_priority = {"open": 0, "rolling": 1, "upcoming": 2, "unknown": 3, "closed": 4}
        scored = []
        for p, q in zip(positions, q_scores):
            if has_terms and q == 0.0:
                continue  # query given but nothing matched: honest skip
            sort_key = (
                p.tier,
                status_priority.get(p.status, 5),
                p.deadline or datetime.max.replace(tzinfo=timezone.utc),
                -q,
            )
            scored.append((sort_key, p))
        scored.sort(key=lambda x: x[0])
        return [p.to_penumbra_doc() for _, p in scored[:limit]]

    async def _afetch_all_positions(self) -> list[ResidencyPosition]:
        """Native-async twin of ``_fetch_all_positions`` (S4b): the per-row fan-out becomes CONCURRENT
        coroutines on the one loop instead of ThreadPoolExecutor worker threads, and each row's raw-httpx
        egress goes native async (``_ahttp_get``). Mirrors ``_fetch_all_positions`` line-for-line,
        changing ONLY the blocking waits:
          - the disk cache read + write -> anyio.to_thread.run_sync (SAME cache key as the sync path);
          - the ai_residencies.json row read (``_rows``, a file read) -> off-loop too;
          - the per-row raw egress -> ``_ahttp_get`` (off-loop per-host sema + http.aget), inside the
            per-kind async fetchers;
          - the ThreadPoolExecutor fan-out -> asyncio.gather. Every coroutine runs on the one loop
            thread, so (unlike the sync worker threads) NO copy_context() is needed: fresh / cache_only /
            diag contextvars propagate naturally through await, and gather preserves ROW ORDER, so
            ``positions`` is assembled identically to ex.map's ordered result.
        The single-row failure isolation + unknown-kind warning (``_afetch_row``) and the 2h cache.set are
        byte-identical to the sync path; _position_to_dict / _position_from_dict (pure CPU) stay on the loop."""
        key = cache.make_key("ai_residencies", "all_positions", "v1")
        cached_payload = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached_payload is not None:
            return [_position_from_dict(d) for d in cached_payload]

        positions: list[ResidencyPosition] = []
        rows = await anyio.to_thread.run_sync(_rows)  # ai_residencies.json read OFF loop

        async def _afetch_row(row: dict) -> list[ResidencyPosition]:
            fetcher_fn = _AKIND_FETCHERS.get(row.get("kind"))
            if fetcher_fn is None:
                logger.warning("ai_residencies: unknown kind %r (row %s)", row.get("kind"), row.get("lab"))
                return []
            try:
                return await fetcher_fn(row)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Residency fetcher %s/%s raised: %s",
                               row.get("kind"), row.get("lab"), exc)
                return []

        row_positions = await asyncio.gather(*[_afetch_row(r) for r in rows])
        for plist in row_positions:
            positions.extend(plist)

        payload = [_position_to_dict(p) for p in positions]  # pure CPU, on loop
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, payload, ttl=7200))  # 2h cache
        return positions

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): awaits the async ``_afetch_all_positions`` (concurrent
        per-row egress) then runs the SAME tier/status/deadline scoring + lexical match, all pure CPU on
        the loop. Byte-identical to ``search`` below the fetch: the shared _afetch_all_positions and the
        pure-CPU relevance.query_terms / field_scores + sort guarantee no drift between the two paths."""
        positions = await self._afetch_all_positions()
        if not positions:
            return []

        # Lexical match via the shared engine; tier/status/deadline stay primary.
        has_terms = bool(relevance.query_terms(query or ""))
        if has_terms:
            fields = [[(f"{p.title} {p.lab} {p.program}", 3.0), (p.summary, 1.0)]
                      for p in positions]
            q_scores = relevance.field_scores(fields, query)
        else:
            q_scores = [0.0] * len(positions)

        status_priority = {"open": 0, "rolling": 1, "upcoming": 2, "unknown": 3, "closed": 4}
        scored = []
        for p, q in zip(positions, q_scores):
            if has_terms and q == 0.0:
                continue  # query given but nothing matched: honest skip
            sort_key = (
                p.tier,
                status_priority.get(p.status, 5),
                p.deadline or datetime.max.replace(tzinfo=timezone.utc),
                -q,
            )
            scored.append((sort_key, p))
        scored.sort(key=lambda x: x[0])
        return [p.to_penumbra_doc() for _, p in scored[:limit]]

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        positions = self._fetch_all_positions()
        for p in positions:
            if p.url == url:
                return p.to_penumbra_doc()
        for p in positions:  # no exact match: host match
            p_host = (urlparse(p.url).hostname or "").lower()
            if p_host and (p_host == host or p_host.endswith("." + host) or host.endswith("." + p_host)):
                return p.to_penumbra_doc()
        return None

    def health_check(self) -> tuple[bool, str]:
        # Probe only the first few rows, returning on the FIRST that yields data:
        # running all of them (each a full fetch) blocked the all-source probe.
        rows = _rows()[:3]
        for row in rows:
            try:
                if _KIND_FETCHERS[row["kind"]](row):
                    return True, f"OK (probed {row['kind']}/{row['lab']} of {len(_rows())} rows)"
            except Exception:  # noqa: BLE001
                continue
        return False, f"first {len(rows)} rows returned no data/failed"


def _position_to_dict(p: ResidencyPosition) -> dict:
    return {
        "lab": p.lab,
        "program": p.program,
        "title": p.title,
        "url": p.url,
        "status": p.status,
        "deadline": p.deadline.isoformat() if p.deadline else None,
        "location": p.location,
        "summary": p.summary,
        "chinese_friendly": p.chinese_friendly,
        "tier": p.tier,
        "raw": jsonsafe(p.raw),
    }


def _position_from_dict(d: dict) -> ResidencyPosition:
    deadline = None
    if d.get("deadline"):
        try:
            deadline = datetime.fromisoformat(d["deadline"])
        except (ValueError, TypeError):
            pass
    return ResidencyPosition(
        lab=d["lab"],
        program=d["program"],
        title=d["title"],
        url=d["url"],
        status=d["status"],
        deadline=deadline,
        location=d.get("location"),
        summary=d.get("summary", ""),
        chinese_friendly=d.get("chinese_friendly"),
        tier=d.get("tier", 2),
        raw=d.get("raw") or {},
    )


from penumbra.core.fetcher import register_adapter

register_adapter(AIResidenciesAdapter())
