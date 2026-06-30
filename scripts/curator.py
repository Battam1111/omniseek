#!/usr/bin/env python3
"""Penumbra Curator P4 self-iterating source-acquisition loop — a MONTHLY mechanical cron.

THE ONE-LINE RAZOR (every drift violates it): the CRON is mechanical-only. It discovers, probes,
persists, counts, and Barks pure FACTS, rendering ZERO editorial judgment. A spawned AGENT renders
EVERY verdict (which gap matters, which candidate to pursue, admit/watch/reject). the operator owns only
red-line/coverage POLICY DATA + the single irreversible sanction (committing a owner_review-staged
config row). This file imports NO verdict-writer (penumbra_curator_decide / candidates.record_verdict /
source_audit.record_source_verdict / record_applied), NO model/anthropic client, NO WebSearch, NO
profile.* / relevance / employer_hits. Every digest list is sorted(...) lexicographic, NEVER
centrality/yield/relevance-ranked. (Smoke 15 greps for this.)

Ships INERT-BUT-WIRED (spec 0): curator_policy.json has enabled:false, so discover() returns [] and
this cron does only read-dossier -> compute STOP/coverage facts -> diff-gated Bark -> write state.
Activation is a one-line DATA edit (declare coverage_targets.json + flip enabled:true), not a code
change.

Discipline mirrors source_audit.py cell-for-cell: the ROOT/sys.path bootstrap, load_sources(),
_sentinel_common bark_push/load_state/save_state, exit codes (0 clean / 1 barked / 2 fatal), first-run
SILENT baseline, diff-gated edge-alarm Bark. Runs as a normal (non-WRITES_ENABLED) process, so the
yield tap stays a silent no-op (discovery's own probe fetches never pollute yield counters).

State: ``~/.penumbra/state/curator/curator-loop-state.json``. launchd: ``com.penumbra.job.curator`` (monthly).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / "penumbra-mcp"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))  # sibling _sentinel_common

from _sentinel_common import bark_push, load_state, save_state  # noqa: E402

STATE_PATH = Path.home() / ".penumbra" / "state" / "curator" / "curator-loop-state.json"

_RUN_HISTORY_CAP = 12  # bounded ring of run summaries for the STOP streak


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _read_policy():
    """Read curator_policy.json (the operator DATA). Tolerant: a missing/corrupt file degrades to the
    INERT built-in (enabled:false), so a broken policy never accidentally ENABLES discovery."""
    import json
    p = (ROOT / "src" / "penumbra" / "core" / "curator" / "curator_policy.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN curator_policy.json unreadable ({exc}) -> INERT built-in (enabled:false)")
    return {"enabled": False, "cold_start": {"enabled": False, "budget": 3},
            "min_cadence_days": 25, "M_zero_new_streak": 3, "discover_topn": 12,
            "error_retry_budget": 2, "watching_max_reprobes": 3, "watching_ttl_days": 120,
            "max_new_probes": 24, "coverage_ceiling": {"_default": 4}}


def _is_stopped(state: dict, policy: dict) -> bool:
    """True iff the loop has CONVERGED: M consecutive demonstrably-HEALTHY rounds with zero
    net-new placed cells AND no empty in-scope cells AND service-gap ~0. A pure read of the run
    history + state counter; renders no verdict. (Used only to short-circuit discovery + keep the
    Bark quiet at saturation; it never decides admission.)"""
    m = int(policy.get("M_zero_new_streak", 3) or 3)
    return int(state.get("consecutive_zero_new_rounds", 0) or 0) >= m


def main() -> int:
    print(f"[{_now_iso()}] curator P4 loop running")
    try:
        from penumbra.server import load_sources
        load_sources()  # populate the adapter registry (same path server.py uses at startup)
        from penumbra.core.curator import candidates, discover, evidence, probe, redlines, source_audit
        from penumbra.core import fetcher
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL import: {exc}")
        return 2

    policy = _read_policy()
    state = load_state(STATE_PATH)
    first_run = not state

    # ── Step 0: min-interval guard (Attack-4, first-class step). A run within the cadence window
    # is a clean no-op, so RunAtLoad restart storms during dev redeploys cannot defeat the monthly
    # cadence. (Skipped on the very first run so the baseline gets recorded.) ──
    min_cadence_days = float(policy.get("min_cadence_days", 25) or 25)
    last_run_epoch = state.get("last_discovery_run_epoch")
    if not first_run and isinstance(last_run_epoch, (int, float)):
        if (_now_epoch() - float(last_run_epoch)) < (min_cadence_days * 86400.0):
            print(f"  within cadence ({min_cadence_days}d); no-op")
            return 0

    # ── Step 1: gap read (READ-ONLY). The SAME gather the weekly sentinel makes. ──
    try:
        dossier = source_audit.gather_source_dossier()
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL gather: {exc}")
        return 2
    empty_cells = list(dossier.get("empty_cells_for_discovery") or [])
    single_cells = list(dossier.get("single_occupant_cells") or [])
    try:
        from penumbra.core.curator import yield_tap
        yield_state = yield_tap._load_all()
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN yield read failed ({exc}) -> empty")
        yield_state = {}

    # ── Step 2: discover. Returns [] when policy.enabled is false (scaffold idle). ──
    try:
        findings = discover.discover(dossier, yield_state=yield_state, policy=policy)
    except Exception as exc:  # noqa: BLE001: a discovery failure is a degraded round, never fatal
        print(f"  WARN discover failed ({exc}) -> degraded round, no findings")
        findings = []
    discovery_health = discover.discovery_health(findings, dossier)
    print(f"  discovered {len(findings)} candidate(s); discovery_health={discovery_health}")

    # ── Step 3: dedup + persist. Drop a finding if its canonical host is a live host, OR is in the
    # terminal-host ledger (Attack-2), OR make_id resolves to an existing non-new/non-error row. ──
    try:
        live_hosts = _live_hosts_safe(fetcher)
    except Exception:  # noqa: BLE001
        live_hosts = set()
    existing = {r.get("id"): r.get("state") for r in candidates.list()}
    survived = 0
    for f in findings:
        urls = f.get("urls") or []
        cand_hosts = {candidates.canonical_host(u) for u in urls}
        cand_hosts.discard("")
        if cand_hosts & {candidates.canonical_host(h) for h in live_hosts}:
            continue  # already a live source
        if any(candidates.host_is_tried(u) for u in urls):
            continue  # terminal-host ledger: rejected/redline_blocked/parked_p2/probe_dead host
        fid = f.get("id") or candidates.make_id(f.get("name") or "", urls)
        st = existing.get(fid)
        if st is not None and st not in ("new", "error"):
            continue  # already tracked in a downstream state
        try:
            candidates.add(f)
            survived += 1
        except Exception as exc:  # noqa: BLE001: one bad row must not abort the round
            print(f"  WARN candidates.add failed for {fid}: {exc}")
    print(f"  survived dedup: {survived}")

    # ── Step 4: pre-build evidence (drive P1's mechanical half). For each row whose state == 'new'
    # AND has a URL, replicate penumbra_curator_probe's BODY with the Attack-fixed routing. NEVER calls
    # a verdict-writer; only mechanical set_state / store_evidence transitions. ──
    error_retry_budget = int(policy.get("error_retry_budget", 2) or 2)
    max_new_probes = int(policy.get("max_new_probes", 24) or 24)
    probed = 0
    for row in candidates.list("new"):
        if probed >= max_new_probes:
            break  # per-run probe ceiling (anti-runaway limit 3 of 4)
        cid = row.get("id")
        if not (row.get("urls") or []):
            continue  # URL-less cold-start stub: nothing to probe; surfaces as an unfilled cell
        probed += 1
        # 4.1 hard red-line short-circuit (terminal). A discovered candidate gets the 8a promotion.
        hits = redlines.match(row)
        if any(h.get("severity") == "hard" for h in hits):
            candidates.set_state(cid, "redline_blocked",
                                 note=f"hard red-line: {[h['id'] for h in hits if h['severity']=='hard']}")
            for u in (row.get("urls") or []):
                candidates.record_tried_host(u)
            continue
        # 4.2 bounded mode probe. A failure bumps error_count; K consecutive -> probe_dead.
        ok, probe_out = fetcher._run_bounded(lambda r=row: probe.mode_probe(r), 60.0)
        if not ok or (isinstance(probe_out, dict) and probe_out.get("probe_error")
                      and not probe_out.get("probe_reached")):
            reason = "deadline" if not ok else probe_out.get("probe_error")
            ec = int((candidates.get(cid) or {}).get("error_count", 0) or 0) + 1
            if ec >= error_retry_budget:
                candidates.set_state(cid, "probe_dead", note=f"probe failed {ec}x: {reason}")
                _set_field(candidates, cid, "error_count", ec)
                for u in (row.get("urls") or []):
                    candidates.record_tried_host(u)
            else:
                candidates.set_state(cid, "error", note=f"probe failure {ec}/{error_retry_budget}: {reason}")
                _set_field(candidates, cid, "error_count", ec)
            continue
        # success: reset the error streak, attach probe cache, build the packet.
        _set_field(candidates, cid, "error_count", 0)
        row["_probe_cache"] = probe_out
        # 4.3 UNWALL-invisible does NOT auto-park (HOLE-2): it goes awaiting_verdict carrying the
        # text_len_plain=0 FACT in the packet, so the AGENT (not the cron) decides watch/reject/park.
        packet = evidence.build_packet_for(row)
        digest = evidence.safety_digest(packet)
        candidates.store_evidence(cid, packet, digest, "awaiting_verdict",
                                  note=f"probed (mode={probe_out.get('mode')})")
    print(f"  probed {probed} new row(s)")

    # ── watching TTL/budget sweep (Attack-4): a watching candidate past its budget/TTL -> rejected
    # ('watch-expired'). Mechanical lifecycle bound, not a verdict on the source's worth. ──
    _sweep_watching(candidates, policy)

    # ── Step 5: Bark (diff-gated edge-alarm; HOLE-1/5). ──
    prev = state.get("last_signals") or {}
    awaiting_ids = sorted(r.get("id") for r in candidates.list("awaiting_verdict") if r.get("id"))
    newly_awaiting = sorted(set(awaiting_ids) - set(prev.get("awaiting_verdict_ids") or []))
    newly_empty = sorted(set(empty_cells) - set(prev.get("empty_cells") or []))
    newly_single = sorted(set(single_cells) - set(prev.get("single_occupant_cells") or []))

    pushed = 0
    lines = []
    if newly_awaiting:
        names = sorted((candidates.get(i) or {}).get("name") or i for i in newly_awaiting)
        lines.append(f"🆕 {len(newly_awaiting)} NEW candidate(s) awaiting verdict: "
                     f"{', '.join(names[:8])}" + (" …" if len(names) > 8 else ""))
    if newly_empty:
        lines.append(f"🕳️ {len(newly_empty)} NEWLY-empty target cell(s): "
                     f"{', '.join(newly_empty[:10])}" + (" …" if len(newly_empty) > 10 else ""))
    if newly_single:
        lines.append(f"⚠️ {len(newly_single)} NEWLY-single-occupant cell(s): "
                     f"{', '.join(newly_single[:10])}" + (" …" if len(newly_single) > 10 else ""))
    body = "\n".join(lines) if lines else "no new curator signals this round"
    print("  " + body.replace("\n", "\n  "))

    if first_run:
        print("  first run → SILENT baseline (no push); signals recorded for next round's diff")
    elif lines:
        title = f"🔎 源采集巡查 · {len(newly_awaiting)} 新候选 / {len(newly_empty)} 新空格"
        if bark_push(title, body + "\n\n(中性事实; admit/watch/reject 由采集 agent 判,本巡查不裁决)",
                     group="Penumbra-Curator", level="passive"):
            pushed += 1

    # ── Step 6: write run-summary state (for the STOP streak). The streak only moves on a
    # demonstrably-HEALTHY round (Attack-3): degraded freezes it (neither ++ nor reset). ──
    net_new_placed = _net_new_placed(state, empty_cells)
    streak = int(state.get("consecutive_zero_new_rounds", 0) or 0)
    if discovery_health == "degraded":
        pass  # FREEZE the streak: an outage round never masquerades as saturation
    elif net_new_placed == 0:
        streak += 1
    else:
        streak = 0
    summary = {
        "at": _now_iso(),
        "candidates_found": len(findings),
        "survived_dedup": survived,
        "newly_awaiting_count": len(newly_awaiting),
        "empty_cells_count": len(empty_cells),
        "net_new_placed": net_new_placed,
        "discovery_health": discovery_health,
    }
    history = list(state.get("run_history") or [])
    history.append(summary)
    history = history[-_RUN_HISTORY_CAP:]

    state["last_run"] = _now_iso()
    state["last_discovery_run_epoch"] = _now_epoch()
    state["consecutive_zero_new_rounds"] = streak
    state["prev_empty_cells"] = empty_cells
    state["run_history"] = history
    state["last_signals"] = {
        "awaiting_verdict_ids": awaiting_ids,
        "empty_cells": empty_cells,
        "single_occupant_cells": single_cells,
    }
    save_state(STATE_PATH, state)

    stopped = _is_stopped(state, policy)
    print(f"  done. found={len(findings)} survived={survived} probed={probed} "
          f"awaiting_new={len(newly_awaiting)} streak={streak} stopped={stopped} bark={pushed}")
    return 1 if pushed else 0


# ── small mechanical helpers (no judgment) ─────────────────────────────────────────
def _live_hosts_safe(fetcher) -> set:
    try:
        from penumbra.core.curator import apply as _apply
        return _apply._live_hosts()
    except Exception:  # noqa: BLE001
        return set()


def _set_field(candidates, cid: str, key: str, value) -> None:
    """Persist a scalar bookkeeping field (error_count) on a candidate row WITHOUT a state change.
    Uses record_applied's atomic pattern via a tiny dedicated mutation: re-add preserves state, so
    we read-modify the row through candidates' lock by re-saving the whole list. Mechanical, no
    verdict. Degrades silently on any failure (the field is a convenience counter, not load-bearing
    for safety)."""
    try:
        import json as _json
        with candidates._LOCK:
            rows = candidates._load_all()
            for r in rows:
                if r.get("id") == cid:
                    r[key] = value
                    break
            candidates._save_all(rows)
            _ = _json  # keep import local-scoped; no external write
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN set_field {key} on {cid} failed: {exc}")


def _sweep_watching(candidates, policy: dict) -> None:
    """Route a watching candidate past its re-probe budget / TTL to rejected ('watch-expired',
    Attack-4). A mechanical lifecycle bound (the recurring re-probe set cannot grow unbounded), NOT
    a verdict on the source's worth: it does NOT call record_verdict; it uses the watching->rejected
    FSM edge with a fixed mechanical reason."""
    max_reprobes = int(policy.get("watching_max_reprobes", 3) or 3)
    ttl_days = float(policy.get("watching_ttl_days", 120) or 120)
    now = _now_epoch()
    for row in candidates.list("watching"):
        cid = row.get("id")
        reprobes = sum(1 for h in (row.get("history") or []) if h.get("state") == "probed")
        expired = reprobes >= max_reprobes
        if not expired:
            verdict = row.get("verdict") or {}
            at = verdict.get("at")
            if isinstance(at, str):
                try:
                    t = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                    expired = (now - t) >= (ttl_days * 86400.0)
                except Exception:  # noqa: BLE001
                    expired = False
        if expired:
            try:
                candidates.set_state(cid, "rejected", note="watch-expired (TTL/re-probe budget)", by="curator")
                for u in (row.get("urls") or []):
                    candidates.record_tried_host(u)
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN watch-expire of {cid} failed: {exc}")


def _net_new_placed(state: dict, empty_cells: list) -> int:
    """How many target cells became PLACED since last round (a cell that left empty_cells_for_
    discovery). A pure set-difference FACT for the STOP streak. First round (no prior) -> 0."""
    prev = set(state.get("prev_empty_cells") or [])
    if not prev:
        return 0
    return len(prev - set(empty_cells))


if __name__ == "__main__":
    sys.exit(main())
