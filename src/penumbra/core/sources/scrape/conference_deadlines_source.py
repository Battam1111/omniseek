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

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
import yaml

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter

logger = logging.getLogger(__name__)

YAML_URL = "https://ccfddl.github.io/conference/allconf.yml"
TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; Penumbra/0.1)"
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


class ConferenceDeadlinesAdapter:
    name = "conference_deadlines"
    needs_credentials = False
    description = (
        "ML/AI conference submission deadlines (ccfddl) — NeurIPS / ICML / ICLR "
        "/ CVPR / ACL / AAAI / IJCAI / CoRL / ICRA + 47 more AI venues, ranked by "
        "nearest upcoming deadline with CCF/CORE rank + location"
    )

    def _fetch_confs(self) -> list[dict]:
        key = cache.make_key("conference_deadlines", "all")
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = httpx.get(YAML_URL, headers={"User-Agent": USER_AGENT},
                             timeout=TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            data = yaml.safe_load(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("conference_deadlines: fetch/parse failed: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        confs = [c for c in data if c.get("sub") in INCLUDE_SUBS]
        cache.set(key, confs, ttl=CACHE_TTL)
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

    def search(self, query: str, limit: int = 10) -> list[Document]:
        confs = self._fetch_confs()
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

        docs = keyword_score_filter(docs, query)
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        # Aggregator of conference deadlines — no per-URL resolution.
        return None

    def health_check(self) -> tuple[bool, str]:
        confs = self._fetch_confs()
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


from penumbra.core.fetcher import register_adapter

register_adapter(ConferenceDeadlinesAdapter())
