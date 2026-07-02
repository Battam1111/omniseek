"""Researcher watch — track a deployer-supplied list of PIs' newest papers via OpenAlex.

Phase 4 P12 (2026-05-28). Tracks the most recent work of a configured set of PIs:
the upstream signal for collaboration / postdoc / hiring scouting. Job boards
(academic_jobs, ai_residencies) tell you when a position is *posted*; this
tells you what a tracked PI is *working on right now*, often before a position
is even advertised.

Mechanism (no CDP, no scraping):
- OpenAlex `works?filter=author.id:<id>&sort=publication_date:desc` returns a
  researcher's most recent works as clean JSON. We already use OpenAlex for the
  `openalex` adapter, so this is zero new infra.
- Author identity is pinned by **hardcoded, manually-verified OpenAlex IDs** —
  NOT runtime name resolution. Name search is unreliable for common names
  (verified 2026-05-28: "Jimmy Ba"→wrong 3-work profile, "Qiang Yang"/"Bo Han"
  →merged multi-person profiles). Every seed ID below was checked against the
  researcher's actual recent paper titles.

Default seed (10 example ML PIs, meant to be EDITED by the deployer):
- Singapore: Bryan Hooi, Min-Yen Kan, Wee Sun Lee (NUS), Bo An (NTU)
- Canada:    Yoshua Bengio, Aaron Courville (Mila), Pascal Poupart (Waterloo),
             Roger Grosse, Jimmy Ba (Toronto/Vector)
- Hong Kong: Dit-Yan Yeung (HKUST)

Deployer customization: drop a JSON file at
`~/.penumbra/credentials/researcher_watch.json`:
    [
      {"name": "My Advisor", "openalex_id": "A5012345678"},
      {"name": "Target PI",  "openalex_id": "A5099999999", "note": "postdoc target"}
    ]
When present it REPLACES the default seed (so the deployer curates their own watch
list). openalex_id is required per entry — find it at
https://openalex.org/works?search=<name> or api.openalex.org/authors?search=<name>.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import platformdirs

from penumbra.core import _openalex as oa
from penumbra.core import cache
from penumbra.core.normalize import Document, keyword_score_filter, mk_signal

logger = logging.getLogger(__name__)

WORKS_PER_PI = 5          # most-recent N papers per researcher
CACHE_TTL = 21600         # 6h: new papers land slowly
_LASTGOOD_TTL = 7 * 86400  # 7d last-good snapshot: serve it when OpenAlex is DOWN (budget/circuit) so a
                           # watched PI's stream survives a budget outage (<~1d, resets midnight UTC)

# (name, institution, openalex_id) — all IDs manually verified 2026-05-28
DEFAULT_SEED: list[tuple[str, str, str]] = [
    # Singapore
    ("Bryan Hooi", "NUS", "A5065675832"),
    ("Min-Yen Kan", "NUS", "A5066305082"),
    ("Wee Sun Lee", "NUS", "A5071864357"),
    ("Bo An", "NTU", "A5017743551"),
    # Canada
    ("Yoshua Bengio", "Mila / Université de Montréal", "A5086198262"),
    ("Aaron Courville", "Mila / Université de Montréal", "A5112608251"),
    ("Pascal Poupart", "University of Waterloo / Vector", "A5050467922"),
    ("Roger Grosse", "University of Toronto / Vector", "A5067036768"),
    ("Jimmy Ba", "University of Toronto / Vector", "A5012276327"),
    # Hong Kong
    ("Dit-Yan Yeung", "HKUST", "A5073139380"),
]


def _load_watch_list() -> list[tuple[str, str, str]]:
    """Return [(name, institution, openalex_id)]. Deployer config replaces seed."""
    cfg = Path(platformdirs.user_config_dir("penumbra")) / "credentials" / "researcher_watch.json"
    # Also accept the credentials dir layout used elsewhere (~/.penumbra/credentials)
    alt = Path.home() / ".penumbra" / "credentials" / "researcher_watch.json"
    for path in (alt, cfg):
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                out = []
                for e in raw:
                    oaid = (e.get("openalex_id") or "").strip()
                    if not oaid:
                        logger.warning("researcher_watch: entry %r missing openalex_id, skipped", e.get("name"))
                        continue
                    out.append((e.get("name") or oaid, e.get("institution") or e.get("note") or "", oaid))
                if out:
                    logger.info("researcher_watch: loaded %d researchers from %s", len(out), path)
                    return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("researcher_watch: failed to parse %s: %s", path, exc)
    return DEFAULT_SEED


# OpenAlex author IDs in the query. We match EITHER a bare ``A<digits>`` token
# (case-insensitive, word-bounded so "A100" inside prose isn't grabbed) OR an
# ``openalex.org/A<digits>`` URL. **ID ONLY** — we deliberately never resolve a
# name at runtime: name→ID disambiguation is unreliable for common (esp. CJK)
# names and has already snared this project (wrong/merged profiles, 2026-05-28).
_AUTHOR_ID_RE = re.compile(
    r"(?:https?://)?(?:openalex\.org/)?(?<![A-Za-z0-9])(A\d{6,})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _parse_author_ids(query: str) -> tuple[str, Optional[list[tuple[str, str, str]]]]:
    """Split OpenAlex author IDs out of the query.

    Returns ``(clean_query, watch)`` where ``watch`` is a synthesized
    ``[(name, institution, openalex_id)]`` list (overriding the default seed)
    when ≥1 author ID is found, else ``None`` — in which case default behaviour
    is unchanged. The matched IDs are stripped from the keyword query.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for m in _AUTHOR_ID_RE.finditer(query or ""):
        oaid = m.group(1).upper()  # canonical: A + digits, upper "A"
        if oaid not in seen:
            seen.add(oaid)
            ids.append(oaid)
    if not ids:
        return (query or "").strip(), None
    clean = _AUTHOR_ID_RE.sub(" ", query).strip()
    # name unknown (we refuse runtime name resolution) → label by the ID itself
    watch = [(oaid, "", oaid) for oaid in ids]
    return clean, watch


class ResearcherWatchAdapter:
    name = "researcher_watch"
    backend = "openalex"  # same OpenAlex corpus + API budget + breaker as openalex / org_watch
    needs_credentials = False
    description = (
        "Researcher watch — newest papers from tracked PIs via OpenAlex "
        "(default: 10 SG/Canada/HK ML faculty; customize via "
        "~/.penumbra/credentials/researcher_watch.json). Postdoc/collab upstream signal."
    )

    def _fetch_pi_works(self, oaid: str) -> "tuple[list[dict], bool]":
        """Return (works, stale). ``stale`` True iff OpenAlex was DOWN and we served this PI's
        last-good snapshot instead of live results."""
        key = cache.make_key("researcher_watch", "pi", oaid)
        lg_key = cache.make_key("researcher_watch", "pi_lastgood", oaid)
        cached = cache.get(key)
        if cached is not None:
            return cached, False
        try:
            works = oa.get_json("/works", {
                "filter": f"author.id:{oaid}",
                "sort": "publication_date:desc",
                "per-page": WORKS_PER_PI,
                # NB: no `host_venue` (removed from the OpenAlex API, 400s);
                # venue comes from primary_location.source instead.
                "select": "id,doi,title,publication_date,abstract_inverted_index,"
                          "primary_location,authorships,cited_by_count",
            }).get("results", [])
        except Exception as exc:  # noqa: BLE001 — incl. the shared circuit breaker
            logger.warning("researcher_watch: OpenAlex fetch failed for %s: %s", oaid, exc)
            works = []
        if works:
            cache.set(key, works, ttl=CACHE_TTL)
            cache.set(lg_key, works, ttl=_LASTGOOD_TTL)
            return works, False
        # OpenAlex DOWN → serve this PI's last-good snapshot (real papers, <=~1d stale) instead of a
        # blind [] (which also poisoned the 6h key on failure). Genuine empty (OpenAlex up) → [].
        if oa.unavailable():
            stale = cache.get(lg_key)
            if stale:
                return stale, True
        return works, False

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # An OpenAlex author ID in the query (e.g. `A5086198262` or
        # `openalex.org/A5086198262`) pins the watch to THAT author, overriding
        # the default seed. ID-only by design — no runtime name resolution. The
        # ID is stripped from the keyword part used for the final filter. No ID →
        # default behaviour (the full seed / the deployer's configured list).
        query, override_watch = _parse_author_ids(query or "")
        watch = override_watch if override_watch is not None else _load_watch_list()
        # Fetch every PI's newest works CONCURRENTLY (was a serial loop: 10 PIs' OpenAlex
        # round-trips SUMMED → ~45s cold). Each worker runs inside a COPIED context so the
        # cache `fresh` flag (a contextvar the fetcher sets in its worker thread) propagates
        # into the sub-thread — without copy_context() a fresh=True call would silently read
        # stale cache. The global _openalex semaphore keeps the fan-out polite (no 429 storm).
        # The PI set, per-PI WORKS_PER_PI, sort, dedup and keyword filter below are all
        # UNCHANGED → identical result set, just gathered in parallel instead of in series.
        all_docs: list[Document] = []
        if watch:
            # Capture one context copy PER PI HERE — in this thread, where the fetcher has
            # set the `fresh` contextvar. (Calling copy_context() inside the worker lambda
            # would capture the WORKER thread's context, where fresh is unset → fresh=True
            # silently reads stale cache. A Context also can't be .run() concurrently, so we
            # need one copy per worker, not one shared.)
            contexts = [copy_context() for _ in watch]
            with ThreadPoolExecutor(max_workers=min(len(watch), 8)) as ex:
                works_per_pi = list(ex.map(
                    lambda ctx, item: ctx.run(self._fetch_pi_works, item[2]),
                    contexts, watch))
            for (name, institution, oaid), (works, stale) in zip(watch, works_per_pi):
                for work in works:
                    doc = self._work_to_document(work, name, institution, oaid)
                    if doc:
                        if stale:  # served from last-good while OpenAlex budget is exhausted
                            doc.metadata["stale"] = ("OpenAlex 日预算耗尽 (午夜 UTC 重置); 本条来自上次"
                                                     "成功抓取的缓存快照 (最多约 1 天旧), 非实时")
                        all_docs.append(doc)

        # Default order: newest first across all PIs
        all_docs.sort(key=lambda d: d.date or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)

        # Dedup by normalized title: the same paper shows up twice when two
        # watched PIs co-author it, or when OpenAlex has preprint + published
        # records. Keep the first (most-recent-sorted); fold the other PI's name
        # into tags so co-authorship stays visible.
        seen: dict[str, Document] = {}
        deduped: list[Document] = []
        for d in all_docs:
            norm = " ".join((d.title.split("] ", 1)[-1]).lower().split())
            if norm in seen:
                prior = seen[norm]
                pi = d.metadata.get("pi_name")
                if pi and pi not in prior.tags:
                    prior.tags.append(pi)
                continue
            seen[norm] = d
            deduped.append(d)
        all_docs = deduped
        # Keyword filter (empty query keeps the date-sorted order)
        all_docs = keyword_score_filter(all_docs, query)
        return all_docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "openalex.org" not in host:
            return None
        # /works/Wxxxx — fetch the single work and label it by its first watched author
        path = urlparse(url).path.strip("/")
        if not path.startswith("works/"):
            return None
        work_id = path.split("/", 1)[1]
        try:
            work = oa.get_json(f"/works/{work_id}")
        except Exception as exc:  # noqa: BLE001 — 404s and the breaker both land here
            logger.warning("researcher_watch fetch_url failed: %s", exc)
            return None
        return self._work_to_document(work, "(tracked researcher)", "", "")

    def health_check(self) -> tuple[bool, str]:
        if not _load_watch_list():
            return False, "no researchers configured"
        # Shared single-flight upstream probe (see _openalex.health): one OpenAlex call for all 40+
        # OpenAlex-backed sources, instead of each probing OpenAlex and bursting the shared key into 429.
        return oa.health()

    @staticmethod
    def _work_to_document(work: dict, pi_name: str, institution: str,
                          oaid: str) -> Optional[Document]:
        p = oa.parse_work(work)
        if not p["title"]:
            return None
        content_parts = []
        if p["venue"]:
            content_parts.append(f"Venue: {p['venue']}")
        if p["authors"]:
            content_parts.append("Authors: " + ", ".join(p["authors"][:6]))
        if p["abstract"]:
            content_parts.append("\n" + p["abstract"][:3000])
        content = "\n".join(content_parts) or "(no abstract)"

        return Document(
            source="researcher_watch",
            source_id=f"{oaid or 'x'}:{p['work_id']}",
            url=p["url"],
            title=f"[{pi_name}] {p['title']}",
            content=content,
            author=pi_name,
            date=p["date"],
            signals=mk_signal("citations", p["cited_by"],
                              kind="citation", by="researcher_watch/cited_by"),
            tags=[pi_name, institution, "paper"] + ([p["venue"]] if p["venue"] else []),
            metadata={
                "pi_name": pi_name,
                "pi_institution": institution,
                "pi_openalex_id": oaid,
                "work_id": p["work_id"],
                "venue": p["venue"],
                "cited_by_count": p["cited_by"],
                "publication_date": p["pub_date"],
                "raw": work,  # OpenAlex work original dict
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(ResearcherWatchAdapter())
