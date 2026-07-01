"""Curator P3, the source audit: a READ-ONLY mechanical fact-gather that joins the accumulated
P2 yield + recall ingest watermarks + watchdog failures + the facets (domain x mode) coverage grid
into a per-source NEUTRAL dossier with NO verdict key. A spawned AGENT reads the dossier and renders
KEEP / WATCH / PRUNE per source; a weekly sentinel surfaces candidates + empty cells to the operator.

THE RAZOR holds exactly as in yield_tap.py / candidates.py / evidence.py: this module records /
counts / measures / joins (pure facts + LABELED descriptive ratios). It emits NOT ONE key in the
banned verdict set at any depth and no verdict TOKEN in any string value. The keep/watch/prune word
appears for the FIRST time only in the agent's output + the agent-authored record_source_verdict
payload (stamped by="agent"; this module computes nothing about the verdict).

The 8 mechanical SAFETY FLAGS (gather_source_dossier) physically encode the operator's coverage red-lines
(audit_policy.json, DATA): record_source_verdict RAISES on a prune the flags forbid, so the agent
CANNOT record an unsafe prune and there is NO code path to a live config mutation. A prune stages a
reversible operator case (explicit_only="retired:..." + the smoke frozen list) with a mandatory
coverage_impact block (every cell occupants_before/after) so a sequence of individually-safe prunes
cannot silently walk a cell to a single point of failure. auto_appliable is ALWAYS False.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from penumbra.core import cache
from penumbra.core.curator import yield_tap as _yt

logger = logging.getLogger(__name__)

# Verdict write-back lives in the SAME tree as candidates.json / yield.json (survives redeploys,
# rides the weekly state-backup launchd, keeps the read-only deploy tree pristine). Created on first
# write. The gather + the policy are READ-ONLY; only record_source_verdict ever writes here.
STATE_DIR = Path.home() / ".polaris" / "state" / "curator"
SOURCE_VERDICTS_PATH = STATE_DIR / "source_verdicts.json"

_POLICY_PATH = Path(__file__).with_name("audit_policy.json")
_COVERAGE_TARGETS_PATH = Path(__file__).with_name("coverage_targets.json")

# Guards ONLY the atomic write of source_verdicts.json (mirrors candidates._LOCK / yield_tap._LOCK).
_LOCK = threading.Lock()

# The set of keys NO gather path may emit at any depth (smoke §14 walks for these with the existing
# _walk_banned_keys scanner). Identical to evidence.BANNED_KEYS + the P2/P3-specific judgment words.
BANNED_KEYS = frozenset({
    "score", "verdict", "passes", "recommend", "admit", "reject", "good", "quality", "rating",
    "confidence", "decision", "redundant", "dead", "low_yield", "prune", "keep",
    "beats_web_search",
})

# The three prune CLASSES the agent may assign (definitions live in the prompt + this docstring,
# NOT as a computed field). A class is the agent's word; the matrix below maps each class to the
# flags that make a prune of it un-offerable.
PRUNE_CLASSES = ("DEAD", "low-yield", "redundant")

# ── the class-vs-flag RAISE matrix (the enforcement chokepoint) ───────────────────────────────
# record_source_verdict RAISES on a PRUNE of <class> when ANY listed flag is True on the source.
# A KEEP / WATCH always succeeds. The matrix ENCODES audit_policy.json's red-lines mechanically so
# the agent physically cannot record an unsafe prune. (Reasoning, per spec B.3/B.4:)
#  - coverage_critical / coverage_unknown apply to EVERY class: never empty a cell, never prune what
#    we cannot place in the grid (can't prove safe).
#  - protected_sole_contributor / tap_blind / deadline_starved / min_evidence_unmet block the
#    YIELD-measured classes (redundant + low-yield): those rest on a yield reading the source was
#    either load-bearing for, never allowed to earn (tap_blind / deadline_starved), or not yet
#    measured enough for (cold-start). DEAD is EXEMPT from these (it rests on watchdog failure, not
#    yield) but blocked by is_cdp_or_credentialed (a benign auth/credential failure is NOT death) and
#    by watchdog_untracked (no watchdog data ⇒ no failure evidence to prove death).
#  - below_cadence_floor blocks ONLY low-yield (a quarterly feed between cycles is expected silence;
#    it does not exempt a watchdog-dead source or a redundant high-volume one).
_RAISE_MATRIX = {
    "DEAD": ("coverage_critical", "coverage_unknown", "is_cdp_or_credentialed", "watchdog_untracked"),
    "low-yield": ("coverage_critical", "coverage_unknown", "protected_sole_contributor",
                  "tap_blind", "deadline_starved", "below_cadence_floor", "min_evidence_unmet"),
    "redundant": ("coverage_critical", "coverage_unknown", "protected_sole_contributor",
                  "tap_blind", "deadline_starved", "min_evidence_unmet"),
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_policy() -> dict:
    """Read audit_policy.json (operator DATA). Tolerant: a missing/corrupt file degrades to a
    conservative built-in (the last-occupant rule + a moderate cold-start floor) so the gather
    never crashes and the safety flags stay strict, never loose."""
    try:
        data = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("curator audit_policy.json unreadable (%s) -> conservative built-in", exc)
    return {
        "coverage_floor": 1,
        "coverage_floor_overrides": {},
        "min_evidence": {"searches_floor": 30, "min_age_days": 14},
        "cadence_floor_days": {"_fallback": 14},
        "deadline_starved_timeout_rate": 0.5,
        "verdict_revalidation_floor_days": 90,
    }


def load_coverage_targets() -> list:
    """Read coverage_targets.json (operator DATA): the (domain x mode) cells Polaris INTENDS to
    cover, as a list of ``domainXmode`` strings, independent of what sources currently exist.
    This DECOUPLES the P4 discovery gap signal + the STOP streak from the live roster (Attack-1):
    a cold-start domain no living source declares can still be a discovery target, and STOP means
    'all INTENDED cells filled', never the vacuous 'all cells some living source declares'.

    Ships EMPTY in scaffold mode -> discovery has no target -> the loop is near-idle (spec section
    0). Tolerant: missing/corrupt -> [] (logged), never raises into the gather. Filters to
    non-empty strings; deduped + sorted (a stable, fact-only universe)."""
    try:
        data = json.loads(_COVERAGE_TARGETS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator coverage_targets.json unreadable (%s) -> empty []", exc)
        return []
    if not isinstance(data, list):
        logger.warning("curator coverage_targets.json is not a list -> empty []")
        return []
    return sorted({s for s in data if isinstance(s, str) and s.strip()})


# ── recall ingest watermark (FRESH read from the index sqlite; mirrors store._ran_at) ───────────
def _ingest_watermark(source: str) -> dict:
    """Latest ingest run for a source from the recall index ingest_runs table (one row per source:
    the watermark + the doc_count of that run). live_feed_silent_days = age of ran_at. NEVER raises:
    a disabled / missing / locked index degrades to nulls (the agent reads 'unknown', not 'dead')."""
    out = {"last_ingest_at": None, "last_ingest_doc_count": None, "live_feed_silent_days": None}
    try:
        from penumbra.core.recall import store
        if getattr(store, "_disabled", False):
            return out
        con = store._read_con()
        if con is None:
            return out
        r = con.execute(
            "SELECT ran_at, doc_count FROM ingest_runs WHERE source = ?", (source,)
        ).fetchone()
        if r and r[0]:
            out["last_ingest_at"] = float(r[0])
            out["last_ingest_doc_count"] = int(r[1]) if r[1] is not None else None
            out["live_feed_silent_days"] = round((time.time() - float(r[0])) / 86400.0, 1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("curator ingest watermark read failed for %s: %s", source, exc)
    return out


def _yield_rows() -> tuple[dict, int]:
    """The accumulated P2 yield: (per-source counter rows, total_searches_observed). FRESH read of
    yield.json via yield_tap._load_all (corrupt -> empty, never raises)."""
    try:
        data = _yt._load_all()
        return (data.get("sources") or {}), int(data.get("total_searches_observed") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("curator yield read failed: %s", exc)
        return {}, 0


# ── the (domain x mode) coverage grid, from a FRESH list_sources read (never a cached constant) ──
def _facet_cell(domain: str, mode: str) -> str:
    return f"{domain}x{mode}"


def _is_presumed_dark(silent_days, cadence_floor) -> bool:
    """P3.1: a source whose health is 'unknown' (never probed by the watchdog, e.g. a CDP source)
    is presumed-dark iff it has an ingest watermark that is SILENT past its cadence floor. A source
    with NO watermark (silent_days is None) is NOT presumed-dark (fail SAFE: keep shielding its
    cell). Pure fact: this only ever SHRINKS an occupant list (newly exposes an empty/critical cell),
    never hides one. The caller restricts this to health=='unknown' (never 'down'/'ok')."""
    if silent_days is None or cadence_floor is None:
        return False
    if not isinstance(silent_days, (int, float)) or not isinstance(cadence_floor, (int, float)):
        return False
    return silent_days > cadence_floor


def _build_grid(roster: list, liveness_by_name: Optional[dict] = None) -> dict:
    """Map every (domain x mode) cell to its LIVE occupants from a FRESH list_sources roster.
    A cell's occupant must be LIVE: health != 'down' (a watchdog-dead source cannot hold a cell
    open), AND not PRESUMED-DARK (P3.1: a health=='unknown' CDP source silent past its cadence floor
    stops shielding a cell, over-protecting a corpse falsely keeps coverage_critical True for a real
    empty cell). NEVER _index (the memory layer, filtered out upstream). Pure set membership.

    This is the PRUNE-side, conservative, health-aware grid (the last-occupant rule). The
    presumed-dark filter touches ONLY this view; the discovery-side _build_grid_by_placement must
    NOT re-target a cell on a health flap, so it does NOT take the filter. The liveness_by_name map
    is {name: {presumed_dark: bool}} from the per-source watermark+cadence computed in the gather; a
    name absent from it is treated as not presumed-dark (fail safe: keep it as an occupant)."""
    liveness_by_name = liveness_by_name or {}
    grid: dict[str, list[str]] = {}
    for s in roster:
        name = s.get("name")
        if not name or name == "_index":
            continue
        if s.get("health") == "down":
            continue  # a dead source does not count as a live occupant of its cells
        if (liveness_by_name.get(name) or {}).get("presumed_dark"):
            continue  # P3.1: a presumed-dark (unknown + silent-past-floor) source is not an occupant
        for dom in (s.get("domains") or []):
            for mode in (s.get("modes") or []):
                grid.setdefault(_facet_cell(dom, mode), []).append(name)
    return {cell: sorted(set(members)) for cell, members in grid.items()}


def _build_grid_by_placement(roster: list) -> dict:
    """Map every (domain x mode) cell to its occupants by PLACEMENT, NOT health (Attack-1, the
    P4 discovery + STOP gap signal). A cell counts as occupied iff a registered, non-retired
    source DECLARES it (its facets domains x modes) -- independent of this month's watchdog
    result. A source going ``health=='down'`` for a month is a watchdog/prune event (P3's job),
    NOT a coverage gap to re-discover, so the discovery signal does not oscillate on a health
    flap. ``explicit_only='retired:...'`` (the P3 reversible-retire marker) un-declares the cell
    (a retired source no longer holds a target cell open). _index is never an occupant. Pure set
    membership; renders NO verdict. (The prune-side _build_grid stays health-aware + conservative
    for the last-occupant rule; this is a SEPARATE view added beside it, no change to the prune
    path.)"""
    grid: dict[str, list[str]] = {}
    for s in roster:
        name = s.get("name")
        if not name or name == "_index":
            continue
        eo = s.get("explicit_only")
        if isinstance(eo, str) and eo.startswith("retired:"):
            continue  # a retired source un-declares its cells (no longer a placement occupant)
        for dom in (s.get("domains") or []):
            for mode in (s.get("modes") or []):
                grid.setdefault(_facet_cell(dom, mode), []).append(name)
    return {cell: sorted(set(members)) for cell, members in grid.items()}


def _source_cells(s: dict) -> list[str]:
    """The (domain x mode) cells a source occupies (its declared domains x modes)."""
    cells = []
    for dom in (s.get("domains") or []):
        for mode in (s.get("modes") or []):
            cells.append(_facet_cell(dom, mode))
    return sorted(set(cells))


def _cadence_floor_days(domains: list, policy: dict) -> Optional[int]:
    """A source's effective cadence floor: the MAX over its declared domains (its slowest
    expectation wins). Returns None when the source declares NO domain (coverage_unknown handles
    that case). Every facets domain token maps in audit_policy.json; _fallback covers a token a
    future facets edit adds before the policy catches up (strict-leaning: the fallback is short)."""
    table = policy.get("cadence_floor_days") or {}
    fallback = int(table.get("_fallback", 14))
    floors = []
    for dom in (domains or []):
        v = table.get(dom)
        floors.append(int(v) if isinstance(v, (int, float)) else fallback)
    return max(floors) if floors else None


def _verdict_age_days(at_iso, now: float) -> Optional[float]:
    """Days since a recorded verdict's `at` timestamp (the SAME _now_iso() shape the write-back
    stamps: %Y-%m-%dT%H:%M:%SZ). Returns None when there is no row / an unparseable timestamp
    (fail SAFE: an un-aged verdict is never treated as fresh). Pure arithmetic, no verdict word."""
    if not isinstance(at_iso, str):
        return None
    try:
        return round((now - time.mktime(time.strptime(at_iso, "%Y-%m-%dT%H:%M:%SZ"))) / 86400.0, 1)
    except Exception:  # noqa: BLE001
        return None


def gather_source_dossier() -> dict:
    """Assemble the per-source NEUTRAL dossier: facts + LABELED descriptive ratios + the 8 mechanical
    SAFETY FLAGS. READ-ONLY (a FRESH list_sources / watchdog / ingest / yield read; never a cached
    constant). Emits NO verdict key at any depth and NO verdict token in any string. _index is never
    a prune candidate (filtered out). The agent reads this; it renders every verdict.
    """
    from penumbra.core import fetcher

    policy = load_policy()
    coverage_floor = int(policy.get("coverage_floor", 1) or 1)
    floor_overrides = policy.get("coverage_floor_overrides") or {}
    min_ev = policy.get("min_evidence") or {}
    searches_floor = int(min_ev.get("searches_floor", 30))
    min_age_days = int(min_ev.get("min_age_days", 14))
    timeout_rate_cut = float(policy.get("deadline_starved_timeout_rate", 0.5))
    revalidation_floor = int(policy.get("verdict_revalidation_floor_days", 90) or 90)

    roster = fetcher.list_sources()  # FRESH (carries health/kind/domains/modes/regions/...)

    # READ-BACK of the agent's prior judgments (the ONLY read of source_verdicts.json on the gather
    # path; the write-back owns the write). We surface ONLY age + presence + a stale-revalidation
    # nudge per source so a judgment the agent made and forgot ('reddit/dblp = WATCH, awaiting
    # evidence') resurfaces for a re-look next cycle, instead of being stored-and-forgotten. We DO
    # NOT echo the recorded word or any verdict token (smoke §14 walks for that): the gather stays a
    # neutral fact-joiner; the agent re-renders the verdict from the live facts.
    prior_rows = _load_verdicts().get("verdicts") or {}

    # P3.1: pre-compute per-source liveness (presumed-dark) so the prune-side grid can drop a
    # health=='unknown' CDP corpse silent past its cadence floor as a live occupant. A fresh ingest
    # watermark + cadence floor read per source; restricted to health=='unknown' (never 'down'/'ok').
    liveness_by_name: dict[str, dict] = {}
    for s in roster:
        nm = s.get("name")
        if not nm or nm == "_index":
            continue
        sd = _ingest_watermark(nm).get("live_feed_silent_days")
        cf = _cadence_floor_days(s.get("domains") or [], policy)
        pd = (s.get("health") == "unknown") and _is_presumed_dark(sd, cf)
        liveness_by_name[nm] = {"presumed_dark": bool(pd), "silent_days": sd, "cadence_floor": cf}

    grid = _build_grid(roster, liveness_by_name)     # prune-side: health-aware + presumed-dark (P3.1)
    grid_by_placement = _build_grid_by_placement(roster)  # P4 discovery-side: placement, not health
    coverage_targets = load_coverage_targets()       # operator DATA: the intended (domain x mode) cells
    fails, tracked, watchdog_as_of = fetcher._watchdog_health()
    yields, total_searches = _yield_rows()
    now = time.time()

    sources: list[dict] = []
    for s in roster:
        name = s.get("name")
        if not name or name == "_index":  # _index is the memory layer, never a prune candidate
            continue

        domains = s.get("domains") or []
        modes = s.get("modes") or []
        kind = s.get("kind")
        explicit_only = bool(s.get("explicit_only"))
        needs_credentials = bool(s.get("needs_credentials"))
        health = s.get("health")

        yr = yields.get(name) or {}
        topk = int(yr.get("topk_appearances", 0) or 0)
        sole = int(yr.get("sole_contributions", 0) or 0)
        from_index_only = int(yr.get("from_index_only_appearances", 0) or 0)
        title_soft = int(yr.get("title_soft_coappearances", 0) or 0)
        searches_present = int(yr.get("searches_present", 0) or 0)
        searches_timed_out = int(yr.get("searches_timed_out", 0) or 0)
        searches_errored = int(yr.get("searches_errored", 0) or 0)
        first_recorded_at = yr.get("first_recorded_at")

        # LABELED descriptive ratios — facts with NO threshold attached (the agent picks thresholds).
        sole_share = round(sole / topk, 3) if topk else 0.0
        presence_rate = round(searches_present / total_searches, 3) if total_searches else 0.0
        timeout_rate = round(searches_timed_out / total_searches, 3) if total_searches else 0.0

        # age of the yield record (cold-start measure); None when the source was never recorded.
        age_days = None
        if isinstance(first_recorded_at, str):
            try:
                age_days = round((now - time.mktime(time.strptime(
                    first_recorded_at, "%Y-%m-%dT%H:%M:%SZ"))) / 86400.0, 1)
            except Exception:  # noqa: BLE001
                age_days = None

        consecutive_fails = int(fails.get(name, 0) or 0)
        ingest = _ingest_watermark(name)
        cells = _source_cells(s)
        cadence_floor = _cadence_floor_days(domains, policy)

        # ── the 8 mechanical SAFETY FLAGS (pure facts; the agent is BOUND by them, never judges) ──
        # 1. protected_sole_contributor: this source was the unique-and-live surfacer of >=1 top-K
        #    hit. sole_contributions>0 ⇒ KEEP at any volume (a source's value is its mode/coverage).
        protected_sole_contributor = sole > 0
        # 2. coverage_critical: sole LIVE occupant of any (domain x mode) cell (counting THIS source).
        #    Per the policy floor (default 1 = last-occupant rule; the operator may raise a critical cell).
        coverage_critical_cells = [
            cell for cell in cells
            if len(grid.get(cell, [])) <= int(floor_overrides.get(cell, coverage_floor))
        ]
        coverage_critical = bool(coverage_critical_cells)
        # 3. coverage_unknown: can't place it in the grid ⇒ can't prove a prune safe.
        coverage_unknown = (not modes) or (kind is None) or (not domains)
        # 4. tap_blind: explicit_only ⇒ never in broad fan-out ⇒ the yield tap never measured it.
        tap_blind = explicit_only
        # 5. is_cdp_or_credentialed: the source GENUINELY needs credentials (a benign auth/credential
        #    failure is NOT source death). Accurate by name — a public no-auth API is NOT flagged here.
        is_cdp_or_credentialed = needs_credentials
        # 5b. watchdog_untracked: the watchdog never probed it (CDP / explicit_only / brand-new →
        #    health 'unknown'), so there is NO failure evidence to call it DEAD on. SPLIT OUT of
        #    is_cdp_or_credentialed (which used to be `needs_credentials OR not-tracked` and so
        #    mislabeled public no-auth APIs like crossref/nserc as 'credentialed'). BOTH stay
        #    DEAD-exempt (see _RAISE_MATRIX["DEAD"]), so the prune protection is byte-unchanged.
        watchdog_untracked = name not in tracked
        # 6. deadline_starved: high timeout_rate ⇒ the yield was never fairly measured.
        deadline_starved = timeout_rate >= timeout_rate_cut
        # 7. below_cadence_floor: live feed silent FEWER days than its slowest domain's floor ⇒
        #    expected silence (a quarterly feed between cycles), not low yield.
        silent_days = ingest.get("live_feed_silent_days")
        below_cadence_floor = (
            cadence_floor is not None and isinstance(silent_days, (int, float))
            and silent_days < cadence_floor
        )
        # 8. min_evidence_met: offered to enough real searches AND old enough (cold-start guard).
        offered = searches_present + searches_timed_out
        min_evidence_met = (offered >= searches_floor) and (age_days is not None
                                                             and age_days >= min_age_days)

        # ── stale-judgment read-back (NEUTRAL: age + presence + a re-look nudge; NO verdict word) ──
        # last_judged_present: did the agent ever record a row for this source? verdict_age_days: how
        # old is it (None = never judged). revalidation_candidate: surface it for a re-look iff it is
        # stale (never judged OR older than the SEPARATE verdict_revalidation_floor_days clock) AND it
        # has cleared the cold-start gate — so day-one does NOT drown the sentinel in ~144 'never
        # judged' cold-start sources. We read the row's `at`, never its recorded word.
        prior_row = prior_rows.get(name) or {}
        last_judged_present = bool(prior_row)
        verdict_age_days = _verdict_age_days(prior_row.get("at"), now)
        revalidation_candidate = (
            (verdict_age_days is None or verdict_age_days > revalidation_floor)
            and min_evidence_met
        )

        sources.append({
            "name": name,
            "kind": kind,
            "domains": domains,
            "modes": modes,
            "regions": s.get("regions") or [],
            "needs_credentials": needs_credentials,
            "explicit_only": explicit_only,
            # NEUTRAL fragility class (stable < keyed < scrape < walled), straight off the roster.
            # A fact for prioritising repairs (fix the brittle first), NOT a verdict — carries no
            # verdict token, so the dossier still passes the §14 banned-key / verdict-token walk.
            "stability": s.get("stability"),
            "health": health,
            "health_as_of": s.get("health_as_of"),
            "occupies_cells": cells,
            # raw yield counters (facts)
            "yield": {
                "topk_appearances": topk,
                "sole_contributions": sole,
                "from_index_only_appearances": from_index_only,
                "title_soft_coappearances": title_soft,
                "searches_present": searches_present,
                "searches_timed_out": searches_timed_out,
                "searches_errored": searches_errored,
                "best_rank_seen": yr.get("best_rank_seen"),
                "rank_histogram": yr.get("rank_histogram") or {},
                "last_topk_at": yr.get("last_topk_at"),
                "first_recorded_at": first_recorded_at,
                "yield_age_days": age_days,
            },
            # LABELED descriptive ratios (facts, NO threshold attached)
            "ratios": {
                "sole_share": sole_share,
                "presence_rate": presence_rate,
                "timeout_rate": timeout_rate,
            },
            # watchdog (the DEAD signal, raw)
            "watchdog": {
                "consecutive_fails": consecutive_fails,
                "last_status": health,
                "as_of": watchdog_as_of,
            },
            # recall ingest watermark (the low-yield seen-delta + silent-days fact)
            "ingest": ingest,
            "cadence_floor_days": cadence_floor,
            # P3.1 liveness facts (NEUTRAL, no verdict token): is this a presumed-dark occupant?
            # (health=='unknown' AND silent past the cadence floor). Surfaced so the agent SEES why
            # a cell's occupant count dropped; the flag set (safety_flags) is frozen + unchanged.
            "liveness": {
                "presumed_dark": bool((liveness_by_name.get(name) or {}).get("presumed_dark")),
                "silent_days": silent_days,
                "cadence_floor": cadence_floor,
            },
            # stale-judgment read-back (NEUTRAL): how old is the agent's prior judgment, is there one
            # at all, and is it stale enough (past the SEPARATE verdict_revalidation_floor_days clock,
            # cold-start gate cleared) to be worth a re-look. NO verdict word is echoed here.
            "judgment_recency": {
                "verdict_age_days": verdict_age_days,
                "last_judged_present": last_judged_present,
                "revalidation_candidate": revalidation_candidate,
            },
            # the 8 mechanical SAFETY FLAGS
            "safety_flags": {
                "protected_sole_contributor": protected_sole_contributor,
                "coverage_critical": coverage_critical,
                "coverage_critical_cells": coverage_critical_cells,
                "coverage_unknown": coverage_unknown,
                "tap_blind": tap_blind,
                "is_cdp_or_credentialed": is_cdp_or_credentialed,
                "watchdog_untracked": watchdog_untracked,
                "deadline_starved": deadline_starved,
                "below_cadence_floor": below_cadence_floor,
                "min_evidence_met": min_evidence_met,
            },
        })

    # empty (domain x mode) cells = coverage GAPS to ADD a source (the inverse of a prune): every
    # cell some source DECLARES (via facets) but no LIVE source currently occupies. We surface the
    # declared-but-unoccupied cells from the FACETS universe so a sentinel can ask the operator to ADD.
    declared_cells: set = set()
    try:
        for fb in (getattr(fetcher, "_FACETS", {}) or {}).values():
            for dom in (fb.get("domains") or []):
                for mode in (fb.get("modes") or []):
                    declared_cells.add(_facet_cell(dom, mode))
    except Exception as exc:  # noqa: BLE001
        logger.debug("curator declared-cell derivation failed: %s", exc)
    empty_cells = sorted(declared_cells - set(grid.keys()))
    single_occupant_cells = sorted(c for c, m in grid.items() if len(m) == 1)

    # the sources whose prior judgment has gone stale (a re-look set the sentinel diff-gates on). A
    # NEUTRAL fact list mirroring empty_cells / single_occupant_cells: NO verdict word, just names.
    revalidation_candidates = sorted(
        s["name"] for s in sources if s["judgment_recency"]["revalidation_candidate"])

    # P4 discovery gap signal (Attack-1, load-bearing): the INTENDED cells (coverage_targets, operator
    # DATA) that no placement occupant declares. Decoupled from health + from the existing-roster
    # facets universe, so a cold-start domain can be a target and STOP means 'all intended cells
    # filled'. Ships EMPTY in scaffold mode (coverage_targets == []) -> no discovery target -> idle.
    empty_cells_for_discovery = sorted(set(coverage_targets) - set(grid_by_placement.keys()))

    return {
        "generated_at": _now_iso(),
        "total_searches_observed": total_searches,
        "watchdog_as_of": watchdog_as_of,
        "policy": {
            "coverage_floor": coverage_floor,
            "coverage_floor_overrides": floor_overrides,
            "min_evidence": {"searches_floor": searches_floor, "min_age_days": min_age_days},
            "deadline_starved_timeout_rate": timeout_rate_cut,
            "verdict_revalidation_floor_days": revalidation_floor,
        },
        "coverage_grid": grid,
        "empty_cells": empty_cells,
        "single_occupant_cells": single_occupant_cells,
        # the stale-judgment re-look set (NEUTRAL names; the per-source judgment_recency block carries
        # the age/presence detail). The sentinel diff-gates the EDGE (newly-stale) of this set.
        "revalidation_candidates": revalidation_candidates,
        # P3.1: the health=='unknown' sources silent past their cadence floor that the prune-side
        # grid no longer counts as live occupants (a neutral fact list, no verdict token).
        "presumed_dark_sources": sorted(
            n for n, v in liveness_by_name.items() if v.get("presumed_dark")),
        # P4 discovery-side keys (placement-based, decoupled from the live-roster health). Added
        # BESIDE the unchanged prune-side keys above; the prune path reads none of these.
        "coverage_targets": coverage_targets,
        "grid_by_placement": grid_by_placement,
        "empty_cells_for_discovery": empty_cells_for_discovery,
        "sources": sources,
        "field_guide": (
            "MECHANICAL FACTS only; you render KEEP / WATCH / PRUNE per source + a one-paragraph "
            "rationale, and for any PRUNE name the class (DEAD / low-yield / redundant). The "
            "safety_flags ALREADY enforce the hard rules (the write-back raises a forbidden prune): "
            "protected_sole_contributor / coverage_critical / coverage_unknown / tap_blind / "
            "deadline_starved / min_evidence_met=False bar a prune for the listed classes; "
            "is_cdp_or_credentialed is DEAD-exempt; below_cadence_floor is low-yield-exempt. A "
            "source's value is its MODE / coverage, NEVER its hit volume: sole_contributions>0 means "
            "hold the source at ANY count. title_soft_coappearances is a WEAK signal (fingerprint "
            "title-collisions); never let it alone drive a redundant call. judgment_recency tells you "
            "whether YOUR prior call has gone stale (revalidation_candidate = no judgment yet, or "
            "older than the verdict_revalidation_floor_days clock, AND cold-start cleared): re-look "
            "those FIRST (a stale WATCH 'awaiting evidence' may now have the evidence). For every "
            "PRUNE you assemble the coverage_impact block (cells before/after). You stage an operator "
            "case; you never mutate live config."
        ),
    }


# ── the agent's write-back: the ENFORCEMENT CHOKEPOINT (mirrors candidates.record_verdict) ───────
def _load_verdicts() -> dict:
    """Read source_verdicts.json. Tolerant: missing/corrupt -> fresh {} (mirrors yield_tap._load_all
    / candidates._load_all), logged, never raised."""
    if not SOURCE_VERDICTS_PATH.exists():
        return {"version": 1, "updated_at": None, "verdicts": {}}
    try:
        data = json.loads(SOURCE_VERDICTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator source_verdicts.json unreadable (%s) -> treating as empty {}", exc)
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), dict):
        return {"version": 1, "updated_at": None, "verdicts": {}}
    data.setdefault("version", 1)
    data.setdefault("updated_at", None)
    return data


def _save_verdicts(state: dict) -> None:
    """Atomic write via cache._atomic_write_text (tmp-in-same-dir + os.replace). MUST be called
    under _LOCK. Bounded by source cardinality (one row per judged source)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cache._atomic_write_text(
        SOURCE_VERDICTS_PATH, json.dumps(state, default=str, ensure_ascii=False, indent=1))


def _safety_flags_for(name: str) -> dict:
    """Fetch the live mechanical safety flags for ONE source from a fresh gather. Used by the
    write-back to physically enforce the prune red-lines: the flags are recomputed from the CURRENT
    roster/yield, never trusted from a passed-in (and possibly stale or doctored) argument."""
    dossier = gather_source_dossier()
    for s in dossier.get("sources", []):
        if s.get("name") == name:
            return s.get("safety_flags") or {}
    # a name absent from the live roster: treat as un-placeable (coverage_unknown) — never prunable.
    return {"coverage_unknown": True, "min_evidence_met": False}


def record_source_verdict(name: str, verdict: str, rationale: str,
                          prune_class: str = "", coverage_impact: Optional[dict] = None) -> dict:
    """The AGENT's write-back: persist the agent-rendered KEEP / WATCH / PRUNE for a source. This
    module computes NOTHING about the verdict (it stores what the agent decided), with ONE
    exception that is pure SAFETY, not judgment: it RAISES on a PRUNE that the source's mechanical
    safety flags forbid (the class-vs-flag matrix). A KEEP / WATCH always succeeds. The agent
    physically cannot record an unsafe prune; there is no code path to a live config mutation.

    Payload stamped by="agent" + a timestamp. RAISES on an unknown verdict / prune_class / a
    forbidden prune. Returns the persisted row.
    """
    verdict = (verdict or "").strip().lower()
    if verdict not in ("keep", "watch", "prune"):
        raise ValueError(f"verdict must be keep|watch|prune, got {verdict!r}")

    forbidden_by: list[str] = []
    if verdict == "prune":
        pc = (prune_class or "").strip()
        if pc not in PRUNE_CLASSES:
            raise ValueError(f"a prune must name a class in {PRUNE_CLASSES}, got {prune_class!r}")
        flags = _safety_flags_for(name)
        # min_evidence_UNMET is the matrix token (the flag is min_evidence_met=True when SAFE).
        active = dict(flags)
        active["min_evidence_unmet"] = not flags.get("min_evidence_met", False)
        for flag in _RAISE_MATRIX[pc]:
            if active.get(flag):
                forbidden_by.append(flag)
        if forbidden_by:
            raise ValueError(
                f"un-offerable {pc} prune of {name!r}: mechanical safety flag(s) {forbidden_by} "
                f"forbid it (operator coverage red-line). A KEEP/WATCH is always allowed; only the "
                f"reversible retire path stages to the operator, and only for a non-forbidden prune."
            )

    with _LOCK:
        state = _load_verdicts()
        state["verdicts"][name] = {
            "verdict": verdict,
            "prune_class": (prune_class or None) if verdict == "prune" else None,
            "rationale": rationale or "",
            "coverage_impact": coverage_impact if (verdict == "prune") else None,
            "by": "agent",
            "at": _now_iso(),
        }
        state["updated_at"] = _now_iso()
        _save_verdicts(state)
        return state["verdicts"][name]


def compute_coverage_impact(name: str) -> dict:
    """Mechanical set-arithmetic over the FRESH coverage grid for a prune operator case: for every
    (domain x mode) cell the source occupies, occupants_before + occupants_after + a
    leaves_single_occupant warning. Closes the cumulative-erosion hole (a sequence of individually
    -safe prunes walking a cell to a single point of failure must surface to the operator). Pure facts."""
    dossier = gather_source_dossier()
    grid = dossier.get("coverage_grid", {})
    src = next((s for s in dossier.get("sources", []) if s.get("name") == name), None)
    cells = (src or {}).get("occupies_cells", [])
    per_cell = []
    leaves_single = []
    for cell in cells:
        before = sorted(grid.get(cell, []))
        after = sorted(x for x in before if x != name)
        per_cell.append({"cell": cell, "occupants_before": before, "occupants_after": after})
        if len(after) == 1:
            leaves_single.append(cell)
    return {
        "source": name,
        "cells": per_cell,
        "leaves_single_occupant": sorted(leaves_single),
        "empties_a_cell": sorted(c["cell"] for c in per_cell if not c["occupants_after"]),
    }


def prepare_source_prune_case(name: str, reason: str) -> dict:
    """Stage a REVERSIBLE operator case for a PRUNE (mirrors apply.prepare_owner_case). NEVER a
    live mutation: it renders the exact two reversible edits + the mandatory coverage_impact block.
    auto_appliable is ALWAYS False. The operator commits + redeploys; flipping explicit_only back fully
    reverses it (the adapter + its index history are untouched).
    """
    retire_value = f"retired:{reason} {time.strftime('%Y-%m-%d', time.gmtime())}"
    impact = compute_coverage_impact(name)
    return {
        "auto_appliable": False,  # ALWAYS False: no code path mutates live config
        "source": name,
        "reversible_edits": [
            {"edit": "set_explicit_only",
             "target": "the source's adapter attribute / config row",
             "value": retire_value,
             "effect": ("leaves the broad fan-out (no longer searched by default) but stays "
                        "penumbra_fetch-able + indexed + recoverable")},
            {"edit": "add_to_smoke_frozen_explicit_only_list",
             "target": "tests/smoke.py the frozen explicit_only set",
             "value": name,
             "effect": "freezes the retire so it can only change by a deliberate later edit"},
        ],
        "coverage_impact": impact,
        "note": ("Curator P3 stages this reversible retire for the operator: set explicit_only + freeze "
                 "it in smoke, commit, redeploy. It reduces broad fan-out cost; it does NOT delete "
                 "the adapter or its index history. Flip explicit_only back to fully reverse. "
                 "auto_appliable is always false; no code path applies this automatically."),
    }
