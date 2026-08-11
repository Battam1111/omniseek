"""Curator candidate backlog: the durable, atomically-persisted source-admission state.

The eye otherwise forgets (fetch-live, TTL-cache). The Curator must NOT: a candidate
source submitted, probed, and awaiting a verdict has to survive a redeploy / restart /
crash, or the work is lost (a prior bug lost candidates held only in transient task
output). So this module owns ONE JSON file under ``~/.penumbra/state/curator/`` (the same
tree as ``health-watchdog-state.json``; survives redeploys, rides the weekly state-backup
launchd, keeps the read-only deploy tree pristine).

THE RAZOR holds here too: this module is pure STATE. It stores what was decided and hands
it back. It NEVER computes a verdict: ``record_verdict`` is the AGENT's write-back; nothing
in this file maps evidence to admit/watch/reject. The only logic is the FSM (which state
transitions are legal) and atomic, lock-guarded persistence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from penumbra.core import cache

logger = logging.getLogger(__name__)

# The public helper ``list(state=...)`` (below) shadows the builtin at module scope, so capture
# the builtin under a private alias for the type checks inside this module.
_builtin_list = list

# Runtime state lives under ~/.penumbra/state/curator/ (NOT in the repo). Created on first
# write. Survives redeploys + rides the state-backup launchd. The deploy SMOKE must touch
# NO write path here (smoke imports the module read-only; see smoke §12 invariant 5).
STATE_DIR = Path.home() / ".penumbra" / "state" / "curator"
CANDIDATES_PATH = STATE_DIR / "candidates.json"
SEEN_HOSTS_PATH = STATE_DIR / "seen_hosts.json"
# P4 anti-rediscovery: the canonical-host TERMINAL ledger. A host that ever reached a terminal
# negative state (rejected / redline_blocked / parked_p2 / probe_dead) is recorded here by its
# CANONICAL host (Attack-2), so the monthly discovery loop never re-surfaces it under a name/URL
# variant (make_id is unstable across venue-name / www. / scheme / trailing-slash drift).
TRIED_HOSTS_PATH = STATE_DIR / "tried_hosts.json"

# One module-level lock so a future loop thread (P4) + an interactive MCP call can never
# interleave a lost update. All mutations go through _save_all() under this lock.
_LOCK = threading.Lock()


# ── the lifecycle FSM (frozen edge set) ───────────────────────────────────────
# States are mechanical; the transition INTO admit/watch/reject is always AGENT-driven
# (record_verdict). An illegal jump RAISES. Transitions are idempotent (set_state to the
# current state is a no-op edge below) so the loop resumes after a crash from any state.
STATES = frozenset({
    "new", "probed", "awaiting_verdict", "admitted", "watching", "rejected",
    "owner_review", "redline_blocked", "parked_p2", "error", "probe_dead",
})

# Terminal states: no FORWARD recovery edge (rejected/redline_blocked/parked_p2/probe_dead keep their
# rationale so the loop never re-surfaces them; applied is a P1.5 terminal, unused in P1). parked_p2
# is terminal for ANTI-REDISCOVERY (its host joins tried_hosts) yet has ONE forward edge, the P2
# wall-aware revival (parked_p2 -> awaiting_verdict) that re-judges the existing row after a jailed
# render; the others keep only the universal "-> error" escape.
# probe_dead (P4): a candidate whose probe failed K consecutive times (dead host / persistent
# SSRF block / always-timeout). Distinct from rejected (no agent judged it) and from an error
# zombie re-probed forever; its canonical host joins tried_hosts.json.
TERMINAL_STATES = frozenset({"rejected", "redline_blocked", "parked_p2", "probe_dead"})

# Frozen ALLOWED_TRANSITIONS edge set. (from_state, to_state).
#   new -> probed | redline_blocked | parked_p2 | error
#   probed -> awaiting_verdict | error
#   awaiting_verdict -> admitted | watching | rejected | error
#   admitted -> owner_review        (P1: an admit ALWAYS stages to the operator; no live apply)
#   watching -> probed                (re-probe on cadence: P4)
#   error -> probed                   (a transient probe exception must NOT strand a candidate
#                                       forever: the loss-of-work fix)
# "any state -> error" is allowed (a probe/fetch threw): recorded, never silently dropped.
ALLOWED_TRANSITIONS = frozenset({
    ("new", "probed"),
    # The single mechanical probe step (mode_probe + build_packet) does the probed-WORK and the
    # packet-build atomically, so it lands a fresh candidate directly in awaiting_verdict. The
    # intermediate "probed" state exists for a future split (probe vs build) + crash-resume.
    ("new", "awaiting_verdict"),
    ("new", "redline_blocked"),
    ("new", "parked_p2"),
    ("new", "error"),
    ("probed", "awaiting_verdict"),
    ("probed", "redline_blocked"),
    ("probed", "parked_p2"),
    ("probed", "error"),
    ("awaiting_verdict", "admitted"),
    ("awaiting_verdict", "watching"),
    ("awaiting_verdict", "rejected"),
    ("awaiting_verdict", "error"),
    ("admitted", "owner_review"),
    ("admitted", "error"),
    ("watching", "probed"),
    ("watching", "error"),
    # P4 (Attack-4): a watching candidate past its TTL / re-probe budget routes to rejected
    # ("watch-expired"), so the recurring re-probe set cannot grow unbounded.
    ("watching", "rejected"),
    ("owner_review", "error"),
    # any -> error (a thrown probe/fetch is recorded). Enumerated for the remaining states
    # so the frozen set is total and smoke can assert it exactly.
    ("redline_blocked", "error"),
    ("parked_p2", "error"),
    # P2 wall-aware probe: a parked_p2 candidate (structurally invisible to the plain-HTTP probe)
    # is REVIVED to awaiting_verdict once the jailed-browser render (mode_probe walled=True) surfaces
    # its real content. parked_p2 stays the terminal anti-rediscovery marker (its canonical host joins
    # tried_hosts so discovery never re-ADDS it); this edge revives the EXISTING row, not a re-find.
    ("parked_p2", "awaiting_verdict"),
    ("rejected", "error"),
    # error recovery: a re-probe lifts a stranded candidate back into the pipeline.
    ("error", "probed"),
    # P4 probe_dead (Attack-4): after K consecutive probe failures a candidate dies to the
    # terminal probe_dead state (NOT rejected: no agent judged it). Reachable from error (the
    # K-th retry) and directly from new/probed (the K-th attempt was made in that state). Like the
    # other terminals it keeps the universal "-> error" escape hatch (a recorded thrown op), but no
    # forward recovery edge (it never re-enters the pipeline).
    ("error", "probe_dead"),
    ("new", "probe_dead"),
    ("probed", "probe_dead"),
    ("probe_dead", "error"),
})


def _can_transition(frm: str, to: str) -> bool:
    if to not in STATES:
        return False
    if frm == to:
        return True  # idempotent self-edge (crash-resume safety)
    return (frm, to) in ALLOWED_TRANSITIONS


# ── id derivation ──────────────────────────────────────────────────────────────
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or "candidate"


def make_id(name: str, urls: Optional[list] = None) -> str:
    """A stable, name-derived id used as the dedup key on resubmit: same (name, urls) ->
    same id, so re-submitting the same candidate updates the row rather than forking it."""
    basis = (name or "") + "|" + "|".join(sorted(urls or []))
    h = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(name)}-{h}"


# ── persistence (atomic, lock-guarded, corrupt-tolerant) ─────────────────────────
def _load_all() -> list:
    """Read the candidates file. Tolerates missing/corrupt -> [] (mirroring cache.get),
    logged loudly, never silently misleading. NEVER raises into the caller."""
    if not CANDIDATES_PATH.exists():
        return []
    try:
        data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator candidates.json unreadable (%s) -> treating as empty []", exc)
        return []
    if not isinstance(data, _builtin_list):
        logger.warning("curator candidates.json is not a list -> treating as empty []")
        return []
    # Legacy-state migration: the admit-staging state was renamed captain_review -> owner_review
    # (open-source de-identification). Normalize any persisted candidate so an in-flight one is not
    # orphaned by the new STATES / ALLOWED_TRANSITIONS.
    for _r in data:
        if isinstance(_r, dict) and _r.get("state") == "captain_review":
            _r["state"] = "owner_review"
    return data


def _save_all(rows: list) -> None:
    """Atomic write of the WHOLE backlog via cache._atomic_write_text (tmp-in-same-dir +
    os.replace). A kill mid-write leaves only a .tmp, never a corrupt final file; concurrent
    writers -> last rename wins. MUST be called under _LOCK."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cache._atomic_write_text(
        CANDIDATES_PATH, json.dumps(rows, default=str, ensure_ascii=False, indent=1))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_history(row: dict, state: str, by: str, note: str) -> None:
    row.setdefault("history", []).append(
        {"state": state, "at": _now_iso(), "by": by, "note": note})


# ── helper API (pure state ops, no judgment) ─────────────────────────────────────
def add(candidate: dict) -> str:
    """Add (or update on resubmit) a candidate row; persist immediately (the loss-of-work
    fix). Returns the row id. A resubmit of the same (name, urls) refreshes the submitted
    fields but PRESERVES any existing state/evidence/verdict/history (idempotent).

    A ``draft`` (foundry-grade, P10) is an optional WORKING artifact the submitter built
    ({"row": <sources.json-shape dict>, "fixture": {"raw", "expect"}, "probe_summary": str}),
    stored verbatim as a submitted field (additive; absent = today's behavior). The packet
    surfaces it for the judge and stage_commit prefers its row as the ready-to-paste block."""
    name = candidate.get("name") or ""
    urls = candidate.get("urls") or []
    cid = candidate.get("id") or make_id(name, urls)
    with _LOCK:
        rows = _load_all()
        idx = next((i for i, r in enumerate(rows) if r.get("id") == cid), None)
        submitted = {
            "id": cid,
            "name": name,
            "urls": urls,
            "proposed_mode": candidate.get("proposed_mode"),
            "proposed_domain": candidate.get("proposed_domain"),
            "proposed_family": candidate.get("proposed_family") or "other",
            "proposed_kind": candidate.get("proposed_kind"),
            "proposed_regions": candidate.get("proposed_regions") or [],
            "rationale_text": candidate.get("rationale_text") or "",
            "submitted_by": candidate.get("submitted_by") or "agent",
            "submitted_at": candidate.get("submitted_at") or _now_iso(),
            # foundry draft artifact (verbatim, UNTRUSTED submitter input like rationale_text);
            # stored only when present so a plain submit's row shape is unchanged.
            "draft": candidate.get("draft") if isinstance(candidate.get("draft"), dict) else None,
        }
        if idx is None:
            row = {
                **submitted,
                "state": "new",
                "evidence": None,
                "evidence_built_at": None,
                "evidence_safety_digest": None,
                "verdict": None,
                "applied": None,
                "history": [],
            }
            _append_history(row, "new", submitted["submitted_by"], "submitted")
            rows.append(row)
        else:
            row = rows[idx]
            # A resubmit with NO new draft must not wipe a draft an earlier submit stored (preserve
            # it like state/evidence/verdict); a resubmit WITH a draft replaces it.
            if submitted["draft"] is None and row.get("draft") is not None:
                submitted["draft"] = row["draft"]
            row.update(submitted)  # refresh the SUBMITTED fields only; keep state/evidence/verdict
            _append_history(row, row.get("state", "new"), submitted["submitted_by"], "resubmitted")
            rows[idx] = row
        _save_all(rows)
    return cid


def get(cid: str) -> Optional[dict]:
    for r in _load_all():
        if r.get("id") == cid:
            return r
    return None


def list(state: Optional[str] = None) -> list:  # noqa: A001 (shadows built-in by design, mirrors list_sources)
    rows = _load_all()
    if state is None:
        return rows
    return [r for r in rows if r.get("state") == state]


def set_state(cid: str, state: str, note: str = "", by: str = "curator") -> dict:
    """Move a candidate to ``state``. RAISES on an unknown id or an illegal FSM edge."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; valid: {sorted(STATES)}")
    with _LOCK:
        rows = _load_all()
        idx = next((i for i, r in enumerate(rows) if r.get("id") == cid), None)
        if idx is None:
            raise KeyError(f"unknown candidate id {cid!r}")
        row = rows[idx]
        frm = row.get("state", "new")
        if not _can_transition(frm, state):
            raise ValueError(f"illegal transition {frm!r} -> {state!r} for {cid!r}")
        row["state"] = state
        _append_history(row, state, by, note)
        rows[idx] = row
        _save_all(rows)
        return row


def store_evidence(cid: str, packet: dict, safety_digest: dict, state: str,
                   note: str = "", by: str = "curator") -> dict:
    """Persist a freshly-built evidence packet + its safety digest AND set state in ONE
    atomic _save_all (so a crash can't split evidence from state). RAISES on illegal edge."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    with _LOCK:
        rows = _load_all()
        idx = next((i for i, r in enumerate(rows) if r.get("id") == cid), None)
        if idx is None:
            raise KeyError(f"unknown candidate id {cid!r}")
        row = rows[idx]
        frm = row.get("state", "new")
        if not _can_transition(frm, state):
            raise ValueError(f"illegal transition {frm!r} -> {state!r} for {cid!r}")
        row["evidence"] = packet
        row["evidence_built_at"] = _now_iso()
        row["evidence_safety_digest"] = safety_digest
        row["state"] = state
        _append_history(row, state, by, note or "evidence built")
        rows[idx] = row
        _save_all(rows)
        return row


def record_verdict(cid: str, verdict: dict, state: str, note: str = "") -> dict:
    """The AGENT's write-back: store the agent-rendered verdict AND set the resulting state
    in ONE atomic _save_all under the lock (no crash can split verdict from state). This
    module computes NOTHING about the verdict: it persists what the agent decided. RAISES
    on illegal FSM edge so a bad decision can't corrupt the lifecycle."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    with _LOCK:
        rows = _load_all()
        idx = next((i for i, r in enumerate(rows) if r.get("id") == cid), None)
        if idx is None:
            raise KeyError(f"unknown candidate id {cid!r}")
        row = rows[idx]
        frm = row.get("state", "new")
        if not _can_transition(frm, state):
            raise ValueError(f"illegal transition {frm!r} -> {state!r} for {cid!r}")
        row["verdict"] = {**verdict, "by": "agent", "at": _now_iso()}
        row["state"] = state
        _append_history(row, state, "agent", note or f"verdict={verdict.get('decision')}")
        rows[idx] = row
        _save_all(rows)
        return row


def record_applied(cid: str, applied: dict, note: str = "") -> dict:
    """P1.5 ONLY: record that a config row was written live. In P1 nothing calls this with a
    real live write: admits stage to owner_review and ``applied`` stays null."""
    with _LOCK:
        rows = _load_all()
        idx = next((i for i, r in enumerate(rows) if r.get("id") == cid), None)
        if idx is None:
            raise KeyError(f"unknown candidate id {cid!r}")
        row = rows[idx]
        row["applied"] = {**applied, "at": _now_iso()}
        _append_history(row, row.get("state", "new"), "curator", note or "applied")
        rows[idx] = row
        _save_all(rows)
        return row


# ── first-seen-host ledger ───────────────────────────────────────────────────────
def _load_seen_hosts() -> set:
    if not SEEN_HOSTS_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_HOSTS_PATH.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, _builtin_list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def host_seen(host: str) -> bool:
    """True iff this EXACT FQDN is already known to the eye: present in the FRESH live
    roster's derived hosts (via apply._live_hosts: never a stale/passed-in list) OR in the
    seen_hosts.json ledger. Matching is exact-FQDN: a shared-suffix host (github.io,
    substack.com, ...) NEVER confers 'seen' to a sibling subdomain (each is its own first-seen
    decision). This is a FACT the agent weighs; it never decides admission by itself."""
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in _load_seen_hosts():
        return True
    try:
        from penumbra.core.curator import apply as _apply
        return host in _apply._live_hosts()
    except Exception as exc:  # noqa: BLE001: a roster-derivation failure must not crash host_seen
        logger.debug("curator host_seen live-roster derivation failed: %s", exc)
        return False


# ── canonical host + the terminal-host ledger (P4 anti-rediscovery, Attack-2) ─────
# Tracking query-param prefixes stripped during canonicalization (a re-emitted URL must collapse
# to the same canonical host regardless of campaign/referrer noise; we only key on the HOST, but
# canonicalize defensively so a future URL-level key stays stable).
def canonical_host(url_or_host: str) -> str:
    """Canonicalize a URL or bare host to a stable comparison key: lowercase host, drop scheme +
    userinfo + port, strip a leading ``www.``. A rejected/redline_blocked/parked_p2/probe_dead
    host must collapse to the SAME key whether discovery re-guesses it as ``https://www.x.com/``,
    ``http://x.com``, or ``x.com`` next month (make_id hashes name+urls, which drifts; the
    canonical host is the durable anti-rediscovery key). Returns '' on an unparseable input."""
    s = (url_or_host or "").strip().lower()
    if not s:
        return ""
    host = s
    if "://" in s or s.startswith("//"):
        from urllib.parse import urlparse
        host = (urlparse(s if "://" in s else "http:" + s).hostname or "")
    else:
        # bare host (possibly with a path / port / userinfo glued on): take the authority head.
        host = s.split("/", 1)[0]
        if "@" in host:
            host = host.rsplit("@", 1)[-1]
        host = host.split(":", 1)[0]
    host = host.strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _load_tried_hosts() -> set:
    """Read the terminal-host ledger -> a set of canonical hosts. Tolerant: missing/corrupt ->
    set() (mirrors _load_seen_hosts), logged, never raised into the caller."""
    if not TRIED_HOSTS_PATH.exists():
        return set()
    try:
        data = json.loads(TRIED_HOSTS_PATH.read_text(encoding="utf-8"))
        return {h for h in data if isinstance(h, str)} if isinstance(data, _builtin_list) else set()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator tried_hosts.json unreadable (%s) -> treating as empty set", exc)
        return set()


def host_is_tried(url_or_host: str) -> bool:
    """True iff a candidate's canonical host ever reached a terminal NEGATIVE state. The durable
    anti-rediscovery guarantee (Attack-2): the discovery loop drops such a host BEFORE re-adding
    it, so the agent is never re-asked to judge a known-dead candidate."""
    h = canonical_host(url_or_host)
    return bool(h) and h in _load_tried_hosts()


def record_tried_host(url_or_host: str) -> None:
    """Append a candidate's canonical host to the terminal-host ledger (atomic, lock-guarded,
    idempotent). Called when a candidate reaches rejected / redline_blocked / parked_p2 /
    probe_dead. A no-op for an unparseable host."""
    h = canonical_host(url_or_host)
    if not h:
        return
    with _LOCK:
        hosts = _load_tried_hosts()
        if h in hosts:
            return
        hosts.add(h)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache._atomic_write_text(
            TRIED_HOSTS_PATH, json.dumps(sorted(hosts), ensure_ascii=False, indent=1))
