"""Conference deadlines — ML/AI submission deadline tracker via ccfddl.

Phase 4 P12 (2026-05-28). A PhD student's calendar is ruled by submission
deadlines; missing a window costs months. ccfddl (ccf-deadlines, an actively
maintained community project) publishes a single compiled YAML covering 345
CS conferences with per-edition timelines.

Source: https://ccfddl.github.io/conference/allconf.yml (346KB YAML; the
`.com` JSON mirror was 503 at build time, the GitHub Pages `.yml` is the
canonical artifact). Each conference entry:
    title, description, sub (category), rank{ccf,core,thcpl}, dblp,
    confs: [{year, id, link, timeline:[{abstract_deadline, deadline}],
             timezone, date, place}]

We surface the **AI category (55 confs)** — every core ML/AI venue: NeurIPS,
ICML, ICLR, CVPR, ICCV, ECCV, ACL, EMNLP, NAACL, AAAI, IJCAI, UAI, AISTATS,
COLT, COLM, CoRL, ICRA, IROS, KR, LOG, WACV, ... — and rank each conference by
its **nearest upcoming deadline**. Conferences whose latest known deadline has
passed (awaiting next CFP) sort last but still appear, so the venue is visible.

Requires PyYAML (added to pyproject P12).
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import anyio
import yaml

from omniseek.core import cache, diag, http, portal
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter
from omniseek.core.sources._base import SourceBase, aparse

logger = logging.getLogger(__name__)

YAML_URL = "https://ccfddl.github.io/conference/allconf.yml"
TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; OmniSeek/0.1)"
CACHE_TTL = 21600  # 6h — deadlines change infrequently

# CCF sub-categories to include. 'AI' = all 55 core ML/AI/CV/NLP/robotics venues.
INCLUDE_SUBS = {"AI"}

# Deadline timestamps look like "2025-05-28 20:00:00" (occasionally "TBD"/missing)
_DEADLINE_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_deadline(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s.lower() in ("tbd", "tba", "none"):
        return None
    for fmt in (_DEADLINE_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Filter tokens this source reads out of the query (what translate_filters writes; a caller may type
# them too): ccf:A|B  core:A*|A  deadline:2026-01-01..2026-06-30 (either side may be empty)
# upcoming:true. They select over the whole ccfddl list after it is fetched (no upstream query).
_TOKEN_RE = re.compile(r"(?:^|\s)(ccf|core|deadline|upcoming):(\S+)", re.IGNORECASE)


def _split_filters(query: str) -> tuple[str, dict]:
    preds: dict = {}
    for m in _TOKEN_RE.finditer(query or ""):
        preds[m.group(1).lower()] = m.group(2)
    return _TOKEN_RE.sub(" ", query or "").strip(), preds


def _keep(doc: Document, preds: dict) -> bool:
    md = doc.metadata or {}
    if "ccf" in preds and str(md.get("ccf_rank") or "N").upper() not in {
            v.upper() for v in preds["ccf"].split("|")}:
        return False
    if "core" in preds and str(md.get("core_rank") or "").upper() not in {
            v.upper() for v in preds["core"].split("|")}:
        return False
    if "upcoming" in preds and bool(md.get("is_upcoming")) != (preds["upcoming"].lower() == "true"):
        return False
    if "deadline" in preds:
        lo, _, hi = preds["deadline"].partition("..")
        d = doc.date.date().isoformat() if doc.date else None
        if d is None or (lo and d < lo) or (hi and d > hi):
            return False
    return True


class ConferenceDeadlinesAdapter(SourceBase):
    name = "conference_deadlines"
    needs_credentials = False
    description = (
        "ML/AI conference submission deadlines (ccfddl): NeurIPS / ICML / ICLR "
        "/ CVPR / ACL / AAAI / IJCAI / CoRL / ICRA + 47 more AI venues, ranked by "
        "nearest upcoming deadline with CCF/CORE rank + location"
    )

    # Source spec (omniseek.core.filters). All fields select over the fetched ccfddl list (the
    # query tokens above); the date is the shown edition's submission deadline.
    filters = {
        "ccf": {"type": "keyword", "ops": ["eq", "any"], "values": ["A", "B", "C", "N"],
                "doc_key": "metadata.ccf_rank", "desc": "CCF rank (N = not ranked)"},
        "core": {"type": "keyword", "ops": ["eq", "any"], "doc_key": "metadata.core_rank",
                 "desc": "CORE rank such as A*, A, B"},
        "deadline": {"type": "date", "ops": ["eq", "gte", "lte", "range"], "doc_key": "date",
                     "desc": "submission deadline of the shown edition (nearest upcoming, else latest past)"},
        "upcoming": {"type": "bool", "ops": ["eq"], "doc_key": "metadata.is_upcoming",
                     "desc": "true = the deadline is still ahead"},
    }
    coverage = {"since": None, "until": None, "gaps": ["only ccfddl's AI category (about 55 venues)"],
                "note": "one edition per venue: the nearest upcoming deadline, else the latest past one"}
    samples = [{"query": "", "filters": {"ccf": "A", "upcoming": True}},
               {"query": "NeurIPS"}]

    def translate_filters(self, query: str, clauses: list) -> str:
        """Append the filters as this source's query tokens (ccf: core: deadline: upcoming:)."""
        from omniseek.core.filters import Unsupported
        toks = []
        for c in clauses:
            if c.field in ("ccf", "core"):
                vals = [c.value] if c.op == "eq" else list(c.value)
                if any(" " in str(v) or "|" in str(v) for v in vals):
                    raise Unsupported(c.field, "a value with a space or | cannot be sent")
                toks.append(f"{c.field}:{'|'.join(str(v) for v in vals)}")
            elif c.field == "deadline":
                toks.append(f"deadline:{c.lo.isoformat() if c.lo else ''}..{c.hi.isoformat() if c.hi else ''}")
            elif c.field == "upcoming":
                toks.append(f"upcoming:{'true' if c.value else 'false'}")
            else:
                raise Unsupported(c.field)
        return " ".join([(query or "").strip(), *toks]).strip()

    async def _afetch_confs(self) -> list[dict]:
        """Async replacement of the deleted sync ``_fetch_confs`` (11-04, async step seven; this is the only one now) (S4b). SAME cache key + TTL + INCLUDE_SUBS
        filter as the sync path; only the two blocking legs change:
          - the disk cache read + write -> anyio.to_thread.run_sync (get/set do file IO);
          - the raw ``httpx.get`` -> ``http.aget`` (shared pool + SSRF guard + cache_only + the
            30MB cap; the ~346KB YAML is far under the cap, so no per-source AsyncClient needed).
        ``http.aget`` keeps follow_redirects=True (client-level) and takes the SAME omniseek UA
        + timeout. It handles the HTTP failure branch itself (non-2xx / timeout / SSRF / oversize
        -> None + its own diag.note under the "http.get" label), so this returns [] on None; a
        200-but-unparseable YAML is the ONE failure http.aget can't see, so it keeps this source's
        own diag.note("conference_deadlines.fetch"). Parsing stays byte-identical: ``yaml.safe_load``
        runs on ``resp.content`` (the same bytes the sync path fed it), on the CPU lane (pure CPU; the
        pure-Python yaml scanner kept the loop busy for ~0.8 s here)."""
        key = cache.make_key("conference_deadlines", "all")
        cached = await cache.aget_once(key)  # disk read OFF loop
        if cached is not None:
            return cached
        resp = await http.aget(YAML_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if resp is None:
            return []  # http.aget already logged + diag.note'd the egress failure
        try:
            data = await aparse(len(resp.content), yaml.safe_load, resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("conference_deadlines: parse failed: %s", exc)
            diag.note("conference_deadlines.fetch", url=YAML_URL, status=resp.status_code, exc=exc)
            return []
        if not isinstance(data, list):
            return []
        confs = [c for c in data if c.get("sub") in INCLUDE_SUBS]
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, confs, ttl=CACHE_TTL), limiter=portal.IO_LANE)
        return confs

    def _best_edition(self, conf: dict) -> Optional[tuple[dict, Optional[datetime], bool]]:
        """Pick the edition to surface: nearest upcoming deadline, else latest past.

        Returns (edition, deadline_dt, is_upcoming) or None if no parseable
        deadline anywhere.
        """
        now = _now()
        upcoming: list[tuple[datetime, dict]] = []
        past: list[tuple[datetime, dict]] = []
        for ed in conf.get("confs") or []:
            for tl in ed.get("timeline") or []:
                dl = _parse_deadline(tl.get("deadline"))
                if dl is None:
                    continue
                (upcoming if dl >= now else past).append((dl, ed))
        if upcoming:
            dl, ed = min(upcoming, key=lambda x: x[0])  # soonest future
            return ed, dl, True
        if past:
            dl, ed = max(past, key=lambda x: x[0])       # most recent past
            return ed, dl, False
        return None

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, S9; this is the only one now) (S4b): the async omniseek_search fan-out awaits this DIRECTLY
        (no held pool thread), so the dominant NETWORK wait — the one ~346KB YAML fetch — costs a
        COROUTINE, not a thread. Mirrors ``search`` line-for-line, changing ONLY the egress+cache
        leg: it awaits ``_afetch_confs`` (shared async leaf + off-loop disk cache, SAME cache key)
        instead of the sync ``_fetch_confs``. Everything after — the best-edition pick per conf, the
        doc build, the upcoming-first sort, and the BM25 ``keyword_score_filter`` — is pure CPU and
        stays ON the loop, byte-identical to ``search`` (a duplicated body the parity golden guards)."""
        confs = await self._afetch_confs()
        docs: list[Document] = []
        for conf in confs:
            best = self._best_edition(conf)
            if not best:
                continue
            ed, dl, is_upcoming = best
            doc = self._to_document(conf, ed, dl, is_upcoming)
            if doc:
                docs.append(doc)

        # Default order: upcoming first (soonest deadline ascending), then past
        # editions after (also by date — recent past nearest the boundary).
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        docs.sort(key=lambda d: (0 if d.metadata.get("is_upcoming") else 1,
                                 d.date or far_future))

        query, preds = _split_filters(query)
        if preds:
            docs = [d for d in docs if _keep(d, preds)]
        docs = keyword_score_filter(docs, query)
        return docs[:limit]

    async def afetch_url(self, url: str) -> Optional[Document]:
        # Aggregator of conference deadlines — no per-URL resolution.
        return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # SAME key _afetch_confs reads: while the YAML is cached, _afetch_confs answers from the cache
        # and asks ccfddl nothing, so this check verifies nothing (None).
        ck = cache.make_key("conference_deadlines", "all")
        hit = await anyio.to_thread.run_sync(cache.get, ck, limiter=portal.IO_LANE)  # disk read OFF loop
        if hit is not None:
            return None, "not probed (the ccfddl YAML is still cached; it is fetched again when it expires)"
        confs = await self._afetch_confs()
        if not confs:
            return False, "no AI conferences parsed from ccfddl YAML"
        upcoming = 0
        for c in confs:
            b = self._best_edition(c)
            if b and b[2]:
                upcoming += 1
        return True, f"OK ({len(confs)} AI confs; {upcoming} with upcoming deadlines)"

    def _to_document(self, conf: dict, ed: dict, deadline: Optional[datetime],
                     is_upcoming: bool) -> Optional[Document]:
        title = conf.get("title") or "(conf)"
        year = ed.get("year")
        rank = conf.get("rank") or {}
        ccf = rank.get("ccf")
        core = rank.get("core")

        # Find the abstract deadline paired with the chosen final deadline
        abstract_dl = None
        for tl in ed.get("timeline") or []:
            if _parse_deadline(tl.get("deadline")) == deadline:
                abstract_dl = tl.get("abstract_deadline")
                break

        days_left = None
        if deadline and is_upcoming:
            days_left = (deadline - _now()).days

        lines = []
        rank_bits = []
        if ccf:
            rank_bits.append(f"CCF-{ccf}")
        if core:
            rank_bits.append(f"CORE-{core}")
        if rank_bits:
            lines.append("Rank: " + " / ".join(rank_bits))
        if is_upcoming:
            dl_str = deadline.strftime("%Y-%m-%d %H:%M UTC") if deadline else "?"
            lines.append(f"⏰ Submission deadline: {dl_str}"
                         + (f"  ({days_left} days left)" if days_left is not None else ""))
        else:
            dl_str = deadline.strftime("%Y-%m-%d") if deadline else "?"
            lines.append(f"Last known deadline: {dl_str} (passed — awaiting next CFP)")
        if abstract_dl:
            lines.append(f"Abstract deadline: {abstract_dl}")
        if ed.get("date"):
            lines.append(f"Conference: {ed.get('date')}")
        if ed.get("place"):
            lines.append(f"Location: {ed.get('place')}")
        if conf.get("description"):
            lines.append(f"\n{conf.get('description')}")

        link = ed.get("link") or ""
        # source_id stable per edition
        source_id = ed.get("id") or f"{title}-{year}"

        status = "upcoming" if is_upcoming else "past"
        title_str = f"{title} {year}" + (f" — deadline in {days_left}d" if days_left is not None else f" — {status}")

        return Document(
            source="conference_deadlines",
            source_id=str(source_id),
            url=link or f"https://ccfddl.github.io/conference/{title.lower()}",
            title=title_str,
            content="\n".join(lines),
            author=None,
            date=deadline,
            tags=[title, conf.get("sub", "AI")]
                 + ([f"CCF-{ccf}"] if ccf else [])
                 + ([f"CORE-{core}"] if core else [])
                 + (["upcoming"] if is_upcoming else ["past"]),
            metadata={
                "conference": title,
                "year": year,
                "ccf_rank": ccf,
                "core_rank": core,
                "is_upcoming": is_upcoming,
                "days_left": days_left,
                "place": ed.get("place"),
                "conf_date": ed.get("date"),
                "raw": jsonsafe(conf),
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(ConferenceDeadlinesAdapter())
