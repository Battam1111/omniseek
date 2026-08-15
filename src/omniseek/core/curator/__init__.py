"""Curator P1: the source-admission gate of the self-iterating eye.

Given a CANDIDATE source (name + URL(s) + a proposed mode/domain/family), the Curator gathers
MECHANICAL evidence (safety / coverage / dedup / mode probe / live parse), persists a durable
backlog, assembles a NEUTRAL evidence packet, and stages every admit for the operator. It renders
NO verdict in code: the admit/watch/reject decision is the spawned AGENT writing record_verdict
after reading the packet + running the probe-derived web-search baseline.

Sub-modules (all mechanical):
  candidates : durable, atomic, lock-guarded backlog + the lifecycle FSM
  redlines   : operator-data red-line matching over urls + query-bearing fields
  probe      : safe_fetch (SSRF/redirect/decode-hardened) + the 5 mode probes
  apply      : gate-only: _live_hosts / _validate_row / _auto_apply_ok + operator-case prep
  apply_live : the live-mutation core (reversible overlay register + runtime retire)
  evidence   : build_packet (the no-verdict contract)
  source_audit : P3 existing-source dossier + the KEEP/WATCH/PRUNE verdict chokepoint

P4 shipped the live-apply lane (apply_live.py): an operator one-tap registers a REVERSIBLE overlay
row into the RUNNING worker (no git, no restart, no in-tree write); the never-auto families stage
a git commit instead, and the in-tree commit + redeploy stays the operator's hand only (code never
runs git). See apply_live.py + apply.py for the live/durable split.
"""

from __future__ import annotations

from omniseek.core.curator import (  # noqa: F401
    apply, apply_live, candidates, evidence, probe, redlines,
)
