"""org_watch — recent papers from an organisation via OpenAlex affiliation search.

Config-driven (``org_watch.json``): ONE named, monitorable Penumbra source per org
(Sea AI Lab + CN/global frontier labs), each capturing that org's recent papers
by OpenAlex raw-affiliation TEXT search. The point: these orgs lack a clean
OpenAlex *institution* entity, and a free-text name search across arxiv/s2 is
swamped by off-target hits, so per-lab affiliation isolation is the only precise,
monitorable way to track "what is lab X publishing now". (Their papers ALSO reach
broad search via arxiv/s2; the unique value here is the isolated, named, watchable
per-lab stream, hence explicit_only.)

Mechanism (verified 2026-06-09 on Sea AI Lab): for each affiliation string,
works?filter=raw_affiliation_strings.search:"<S>" (phrase search), then keep only
works an author of which actually wrote <S> (the search is fuzzy); merge + dedup
across a row's strings. No CDP, no fragile author-id disambiguation (OpenAlex
merges common CN names into wrong-person profiles).

HTTP + parsing go through penumbra.core._openalex (shared client, circuit breaker,
work parser). Adding a lab = one row in ``org_watch.json``
{name, affiliations[], regions[], description}.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import _openalex as oa
from penumbra.core import cache
from penumbra.core.normalize import Document, keyword_score_filter, mk_signal

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("org_watch.json")
PER_PAGE = 25
CACHE_TTL = 21600  # 6h: new papers land slowly
# Last-good snapshot TTL for the OpenAlex-down fallback. OpenAlex's two $1/day credit buckets reset at
# midnight UTC, so an outage is bounded to <~1 day; 7 days of last-good covers any multi-day budget/
# circuit outage. When OpenAlex is unavailable, serve THIS (the org's real recent papers, slightly
# stale) instead of a blind [] — S2 can't do affiliation search, so the cached real data beats a noisy
# name-query fallback for the per-lab streams.
_LASTGOOD_TTL = 7 * 86400
_SELECT = ("id,doi,title,publication_date,abstract_inverted_index,"
           "primary_location,authorships,cited_by_count")

# AI-model "co-author" crank filter. A growing class of Zenodo / Open-MIND "human-AI collaboration"
# preprints lists an AI MODEL as an author and attributes the lab as that model's affiliation (e.g.
# author "Kimi 2.5 Agent" / "Gemini 3.1 (Flash)" with raw-affiliation "Moonshot AI"), so the fuzzy
# raw_affiliation_strings.search drags them into a lab's stream and — for orgs whose name doubles as a
# model byline (Moonshot/Kimi, DeepMind/Gemini, ...) — can swamp the real output. These authors are
# named as MODEL + version/variant, which no human carries; match THAT precisely so real researchers
# (Claude Shannon, Gemma Boleda, Frederic Mistral) are never dropped. A venue blacklist was considered
# and rejected: it false-dropped real lab works that happen to sit on Zenodo / Open MIND.
_MODEL_BYLINE = re.compile(
    r"\b(?:kimi|gemini|grok|gpt|chatgpt|qwen|claude|deepseek|llama|mistral|gemma|ernie|doubao|hunyuan|glm|copilot)\b"
    r"[\s\-]*"
    r"(?:v?\d|\((?:flash|pro|mini|preview)\)|agent\b|flash\b|opus\b|sonnet\b|haiku\b|turbo\b|preview\b)"
    r"|\b\d+(?:\.\d+)?\s+(?:agent|flash)\b"
    r"|\bgpt-\d",
    re.I,
)


def _is_ai_byline_crank(work: dict) -> bool:
    """True iff any author is named as an AI MODEL (model + version/variant) — the signature of the
    'AI co-author' preprint crank that name-collides into a lab's raw-affiliation stream."""
    for a in (work.get("authorships") or []):
        au = a.get("author") if isinstance(a, dict) else None
        name = au.get("display_name") if isinstance(au, dict) else None
        if name and _MODEL_BYLINE.search(name):
            return True
    return False


class _OrgWatchAdapter:
    needs_credentials = False
    kind = "stream"
    domains = ["papers"]
    backend = "openalex"  # 39 org slices share ONE OpenAlex corpus + API budget + breaker
    explicit_only = ("org_watch: per-org OpenAlex paper stream, named + watchtower only "
                     "(its papers already reach broad search via arxiv/s2)")

    def __init__(self, name: str, affiliations: list[str], description: str,
                 regions: Optional[list[str]] = None) -> None:
        self.name = name
        self.affiliations = affiliations  # precise raw-affiliation strings
        self.description = description
        self.regions = regions or []

    def _matches(self, work: dict) -> bool:
        """The raw_affiliation_strings.search filter is fuzzy; keep only works an
        author of which actually wrote one of our affiliation strings."""
        needles = [a.replace('"', '').strip().lower() for a in self.affiliations]
        for a in (work.get("authorships") or []):
            for raw in (a.get("raw_affiliation_strings") or []):
                low = (raw or "").lower()
                if any(n and n in low for n in needles):
                    return True
        return False

    def _fetch(self) -> "tuple[list[dict], bool]":
        """Return (works, stale). ``stale`` True iff OpenAlex was DOWN (budget exhausted / circuit
        open) and we served the last-good snapshot instead of live results."""
        key = cache.make_key("org_watch", self.name, "works", "|".join(self.affiliations))
        lg_key = cache.make_key("org_watch", self.name, "lastgood", "|".join(self.affiliations))
        cached = cache.get(key)
        if cached is not None:
            return cached, False
        def _one(aff: str) -> list[dict]:
            try:
                data = oa.get_json("/works", {
                    "filter": f'raw_affiliation_strings.search:"{aff}"',
                    "sort": "publication_date:desc",
                    "per-page": PER_PAGE,
                    "select": _SELECT,
                })
                return data.get("results", [])
            except Exception as exc:  # noqa: BLE001 — incl. OpenAlexDown: degrade, never raise
                logger.warning("org_watch[%s]: fetch failed for %r: %s", self.name, aff, exc)
                return []

        # Fetch this org's affiliation strings CONCURRENTLY (a row has 2-3). Each oa.get_json
        # still passes through _openalex's global BoundedSemaphore(8), so the OpenAlex-wide
        # in-flight cap is unchanged (no extra load on the polite pool) — this only folds THIS
        # source's per-string serial wait (meta_fair 3×~3s≈9s → ≈3s). ex.map preserves order,
        # and _matches/dedup/sort below are identical to the old serial path (result unchanged).
        if len(self.affiliations) <= 1:
            results = [_one(a) for a in self.affiliations]
        else:
            with ThreadPoolExecutor(max_workers=min(len(self.affiliations), 3)) as ex:
                results = list(ex.map(_one, self.affiliations))
        merged: dict[str, dict] = {}
        for works in results:
            for w in works:
                if self._matches(w) and not _is_ai_byline_crank(w):
                    merged[w.get("id") or w.get("doi") or w.get("title")] = w
        out = sorted(merged.values(),
                     key=lambda w: w.get("publication_date") or "", reverse=True)
        if out:
            cache.set(key, out, ttl=CACHE_TTL)        # 6h fresh
            cache.set(lg_key, out, ttl=_LASTGOOD_TTL)  # 7d last-good snapshot for the down-fallback
            return out, False
        # Empty result: distinguish OpenAlex being DOWN (budget exhausted / circuit open — the 44-source
        # single point of fragility) from a genuine no-match. If down, serve the last-good snapshot
        # (the org's real recent papers, <=~1d stale) so the per-lab stream stays useful through a
        # budget outage. NEVER cache an empty over the fresh key (the old unconditional set poisoned it
        # for 6h on any failure). A genuine empty (OpenAlex up, no matching papers) just returns [].
        if oa.unavailable():
            stale = cache.get(lg_key)
            if stale:
                return stale, True
        return out, False

    def search(self, query: str, limit: int = 10) -> list[Document]:
        works, stale = self._fetch()
        docs = [d for d in (self._to_doc(w) for w in works) if d]
        docs = keyword_score_filter(docs, (query or "").strip())[:limit]
        if stale:  # mark the served snapshot so the agent knows it is not real-time
            for d in docs:
                d.metadata["stale"] = ("OpenAlex 日预算耗尽 (午夜 UTC 重置); 本结果来自上次成功抓取的"
                                       "缓存快照 (最多约 1 天旧), 非实时")
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "openalex.org" not in host:
            return None
        work_id = urlparse(url).path.strip("/").split("/")[-1]
        if not work_id.startswith("W"):
            return None
        try:
            return self._to_doc(oa.get_json(f"/works/{work_id}"))
        except Exception as exc:  # noqa: BLE001 — 404s and breaker both land here
            logger.warning("org_watch[%s] fetch_url failed: %s", self.name, exc)
            return None

    def health_check(self) -> tuple[bool, str]:
        # Delegate to the shared single-flight upstream probe (see _openalex.health): 39 org_watch
        # rows + openalex + researcher_watch share ONE OpenAlex key + breaker, so probing each row's
        # affiliation separately used to fire ~40 concurrent calls and burst the key into 429. The
        # per-affiliation validity is a config-time concern (curator / source-audit), not an uptime probe.
        return oa.health()

    def _to_doc(self, work: dict) -> Optional[Document]:
        p = oa.parse_work(work)
        if not p["title"]:
            return None
        parts = []
        if p["venue"]:
            parts.append(f"Venue: {p['venue']}")
        if p["authors"]:
            parts.append("Authors: " + ", ".join(p["authors"][:8]))
        if p["abstract"]:
            parts.append("\n" + p["abstract"][:3000])
        return Document(
            source=self.name,
            source_id=p["work_id"] or p["url"],
            url=p["url"],
            title=p["title"],
            content="\n".join(parts) or "(no abstract)",
            author=", ".join(p["authors"][:4]) or None,
            date=p["date"],
            signals=mk_signal("citations", p["cited_by"],
                              kind="citation", by="org_watch/cited_by"),
            tags=[self.name, "paper"] + ([p["venue"]] if p["venue"] else []),
            metadata={"venue": p["venue"], "cited_by_count": p["cited_by"],
                      "publication_date": p["pub_date"], "doi": p["doi"],
                      "work_id": p["work_id"]},
        )


def _register_row(row: dict) -> None:
    from penumbra.core.fetcher import register_adapter
    register_adapter(_OrgWatchAdapter(
        name=row["name"], affiliations=row["affiliations"],
        description=row["description"], regions=row.get("regions"),
    ))


def _load() -> None:
    """Base in-tree rows, THEN curator live-apply overlay rows (base wins; typed-validate + drop a
    bad overlay row). org_watch is a _NEVER_AUTO_FAMILIES family so the one-tap lane never writes a
    row here, but the loader is overlay-aware for symmetry + an operator-promoted reconcile path."""
    base = json.loads(_DATA.read_text(encoding="utf-8"))
    seen = set()
    for row in base:
        _register_row(row)
        seen.add(row["name"])
    try:
        from penumbra.core.curator import apply as _apply
        from penumbra.core.curator import apply_live as _apply_live
        for r in _apply_live.overlay_rows("org_watch"):
            name = r.get("name")
            if name in seen:
                continue  # base wins
            problems = _apply.validate_row_typed("org_watch", r)
            if problems:
                logger.warning("org_watch overlay row %r dropped (invalid): %s", name, problems)
                continue
            _register_row(r)
            seen.add(name)
    except Exception as exc:  # noqa: BLE001, overlay best-effort; base must always load
        logger.warning("org_watch overlay load skipped: %s", exc)


_load()
