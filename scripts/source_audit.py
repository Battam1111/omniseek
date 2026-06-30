#!/usr/bin/env python3
"""Penumbra Curator P3 Source-Audit Sentinel — a WEEKLY read-only digest to the operator.

The health watchdog reports a source that DIED; the watchtower reports new CONTENT. This reports
the slow structural question: which sources look like dead weight (yield-measured), which feeds went
silent, and — the inverse of a prune — which (domain x mode) coverage cells are EMPTY and want a
NEW source. It PRUNES NOTHING and renders NO verdict: it calls the read-only mechanical gather
(source_audit.gather_source_dossier) and surfaces NEUTRAL candidates + gaps for the spawned audit
agent + the operator. Every keep/watch/prune call is the agent's, downstream of this push.

Discipline mirrors the shared sentinel pattern (_sentinel_common.py):
  - first run per signal-class is a SILENT baseline (no Bark flood on the first ever run);
  - prune-FLAGGING is suppressed for any source below min_evidence_met (cold-start: we have not
    measured it enough to even SURFACE it as a candidate);
  - one deduplicated Bark digest; state + cooldown via _sentinel_common.

State: ``~/.penumbra/state/job.source-audit/state.json``.
launchd: ``com.penumbra.job.source-audit`` (weekly). Exit: 0 clean / 1 alerts pushed / 2 fatal.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "penumbra-mcp"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))  # sibling _sentinel_common

from _sentinel_common import bark_push, load_state, save_state  # noqa: E402

STATE_PATH = Path.home() / ".penumbra" / "state" / "job.source-audit" / "state.json"

# Surfacing floors for the DIGEST only (NOT verdict thresholds — the agent picks those). These bound
# how noisy the weekly push is; a source must clear the cold-start gate (min_evidence_met) before it
# can be SURFACED as a redundant/low-yield candidate at all.
MIN_SEARCHES_TO_FLAG = 30        # only flag sole_share=0 once offered to >= this many searches
DEAD_FAILS_TO_FLAG = 2           # consecutive watchdog fails before surfacing as a DEAD candidate
SILENT_DAYS_TO_FLAG = 30         # live-feed-silent days before surfacing as a low-yield candidate


def _redundant_candidates(sources: list) -> list:
    """Sources offered to enough searches, cold-start cleared, sole_share == 0 (others always
    co-surface their hits) AND not protected/tap-blind/deadline-starved. NEUTRAL surfacing only."""
    out = []
    for s in sources:
        f = s.get("safety_flags") or {}
        if not f.get("min_evidence_met"):
            continue  # cold-start: never surface as a prune candidate
        if f.get("protected_sole_contributor") or f.get("tap_blind") or f.get("deadline_starved"):
            continue
        y = s.get("yield") or {}
        offered = int(y.get("searches_present", 0)) + int(y.get("searches_timed_out", 0))
        if offered < MIN_SEARCHES_TO_FLAG:
            continue
        if (s.get("ratios") or {}).get("sole_share", 0.0) == 0.0 and int(y.get("topk_appearances", 0)) > 0:
            out.append(s["name"])
    return sorted(out)


def _dead_candidates(sources: list) -> list:
    """Sources with >= DEAD_FAILS_TO_FLAG consecutive watchdog fails AND not CDP/credentialed (a
    benign auth failure is not death)."""
    out = []
    for s in sources:
        f = s.get("safety_flags") or {}
        if f.get("is_cdp_or_credentialed"):
            continue
        if int((s.get("watchdog") or {}).get("consecutive_fails", 0)) >= DEAD_FAILS_TO_FLAG:
            out.append(s["name"])
    return sorted(out)


def _silent_carrying(sources: list) -> list:
    """Live feed silent >= SILENT_DAYS_TO_FLAG days but recall still carrying it (from_index_only>0),
    and NOT below its own cadence floor (a quarterly feed between cycles is expected silence)."""
    out = []
    for s in sources:
        f = s.get("safety_flags") or {}
        if f.get("below_cadence_floor"):
            continue
        days = (s.get("ingest") or {}).get("live_feed_silent_days")
        carrying = int((s.get("yield") or {}).get("from_index_only_appearances", 0)) > 0
        if isinstance(days, (int, float)) and days >= SILENT_DAYS_TO_FLAG and carrying:
            out.append(s["name"])
    return sorted(out)


def main() -> int:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] curator source-audit sentinel running")
    try:
        from penumbra.server import load_sources
        load_sources()  # populate the adapter registry (same path server.py uses at startup)
        from penumbra.core.curator import source_audit
    except Exception as exc:
        print(f"  FATAL import: {exc}")
        return 2

    try:
        dossier = source_audit.gather_source_dossier()  # READ-ONLY mechanical gather
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL gather: {exc}")
        return 2

    sources = dossier.get("sources", [])
    redundant = _redundant_candidates(sources)
    dead = _dead_candidates(sources)
    silent = _silent_carrying(sources)
    empty_cells = dossier.get("empty_cells", [])
    single_cells = dossier.get("single_occupant_cells", [])
    revalidation = dossier.get("revalidation_candidates", [])  # stale prior judgments to re-look

    state = load_state(STATE_PATH)
    first_run = not state  # first ever run → SILENT baseline, no Bark flood
    pushed = 0

    # Diff-gate the STANDING structural lines into EDGE alarms (bark-diff fix): the empty-cells +
    # single-occupant lines previously re-fired EVERY week whenever the sets were non-empty (a
    # state-alarm, a standing-warning flood). Compare against the saved baseline + Bark only the
    # cells that JUST changed (a cell that newly emptied / newly dropped to one occupant = a real
    # fragility event). Persist the full current sets below so a cell leaving a set re-arms it.
    prev = state.get("last_candidates") or {}
    newly_empty = sorted(set(empty_cells) - set(prev.get("empty_cells") or []))
    newly_single = sorted(set(single_cells) - set(prev.get("single_occupant_cells") or []))
    # a source whose prior verdict JUST went stale (re-look fruit). Diff-gated like the cells above so
    # a standing stale set does not re-fire every week: only a source newly entering the set barks.
    newly_stale = sorted(set(revalidation) - set(prev.get("revalidation_candidates") or []))

    lines = []
    if redundant:
        lines.append(f"🔁 {len(redundant)} sources sole_share=0 over ≥{MIN_SEARCHES_TO_FLAG} "
                     f"searches: {', '.join(redundant[:8])}" + (" …" if len(redundant) > 8 else ""))
    if dead:
        lines.append(f"🩺 {len(dead)} sources consecutive_fails≥{DEAD_FAILS_TO_FLAG}: "
                     f"{', '.join(dead[:8])}" + (" …" if len(dead) > 8 else ""))
    if silent:
        lines.append(f"💤 {len(silent)} sources live-silent ≥{SILENT_DAYS_TO_FLAG}d but recall "
                     f"carrying: {', '.join(silent[:8])}" + (" …" if len(silent) > 8 else ""))
    if newly_empty:
        lines.append(f"🕳️ {len(newly_empty)} NEWLY-empty (domain×mode) cells = coverage GAPS to ADD: "
                     f"{', '.join(newly_empty[:10])}" + (" …" if len(newly_empty) > 10 else ""))
    if newly_single:
        lines.append(f"⚠️ {len(newly_single)} cells NEWLY dropped to ONE live occupant "
                     f"(prune-protected): {', '.join(newly_single[:10])}"
                     + (" …" if len(newly_single) > 10 else ""))
    if newly_stale:
        _floor = int((dossier.get("policy") or {}).get("verdict_revalidation_floor_days", 90))
        lines.append(f"🕰 {len(newly_stale)} 个源上次裁决 >{_floor}d (复检 fruit): "
                     f"{', '.join(newly_stale[:10])}" + (" …" if len(newly_stale) > 10 else ""))

    body = "\n".join(lines) if lines else "no audit candidates this week"
    print("  " + body.replace("\n", "\n  "))

    if first_run:
        print("  first run → SILENT baseline (no push); candidates recorded for next week's diff")
    elif lines:
        title = (f"📋 源审计周报 · {len(redundant)+len(dead)+len(silent)} 候选 / "
                 f"{len(newly_empty)} 新空格 / {len(newly_stale)} 待复检")
        if bark_push(title, body + "\n\n(中性事实; KEEP/WATCH/PRUNE 由审计 agent 判,本哨兵不裁决)",
                     group="Penumbra-Curator", level="passive"):
            pushed += 1

    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    state["last_candidates"] = {"redundant": redundant, "dead": dead, "silent": silent,
                                "empty_cells": empty_cells, "single_occupant_cells": single_cells,
                                "revalidation_candidates": revalidation}
    save_state(STATE_PATH, state)

    print(f"  done. redundant={len(redundant)} dead={len(dead)} silent={len(silent)} "
          f"empty_cells={len(empty_cells)} (newly={len(newly_empty)}) "
          f"single={len(single_cells)} (newly={len(newly_single)}) "
          f"revalidation={len(revalidation)} (newly={len(newly_stale)}) bark={pushed}")
    return 1 if pushed else 0


if __name__ == "__main__":
    sys.exit(main())
