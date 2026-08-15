"""The QUARANTINED live-mutation core of the curator (apply.py stays pure / gate-only).

THE CORE SPLIT (the load-bearing safety property):
  * LIVE EFFECT (reversible)  = an overlay row under ~/.omniseek/state/curator/overlays/ + a live
    re-register into the RUNNING worker's fetcher._adapters. NO git, NO restart, NO in-tree write.
    A single operator tap (server.omniseek_curator_apply_live) applies exactly this.
  * DURABLE TRUTH (irreversible) = the in-tree config JSON commit + redeploy. THE OPERATOR'S HAND
    ONLY; NOTHING in this module (or anywhere in code) runs git / deploy.sh / launchctl.

This module owns:
  * the per-family overlay files (atomic via cache._atomic_write_text under a _LOCK, mirroring
    candidates.py discipline): load_overlay / overlay_rows / append / drop / mark_committed
  * register_one(family, row) -> the family adapter instance for a row (the same classes the import
    loaders build), used by both apply_overlay_row AND the overlay-aware loaders
  * apply_overlay_row(cand) -> the ONE load-bearing ordering: validate -> re-gate -> live-register
    BEFORE the overlay write -> append -> recall-cache-invalidate -> record_applied
  * rollback_overlay_row(name) -> the FULL revert: unregister the live adapter AND drop the overlay
  * the runtime retire overlay (explicit_only_overrides.json) read/write for omniseek_curator_retire_live

append() REFUSES (raises) if the row name already exists in the BASE config JSON OR in the overlay.
The 4 family loaders read base + overlay_rows(family), appending the overlay AFTER base and SKIPPING
an overlay row whose name is already in base (base wins: a deploy that promoted a row into the tree
wins; an overlay row can never collide-replace a base adapter).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from omniseek.core import cache

logger = logging.getLogger(__name__)

# Same state tree as candidates.json / source_verdicts.json (survives redeploys, rides the weekly
# state-backup launchd, keeps the read-only deploy tree pristine). Created on first append.
OVERLAY_DIR = Path.home() / ".omniseek" / "state" / "curator" / "overlays"

# Guards every overlay write (mirrors candidates._LOCK / source_audit._LOCK). All mutations go
# through append/drop/mark_committed under this lock; an atomic cache._atomic_write_text means a
# kill mid-write leaves only a .tmp, never a corrupt final file.
_LOCK = threading.Lock()

_KNOWN_FAMILIES = frozenset({"rss", "org_watch", "page_watch", "news_scraper", "search_index"})

# Families whose live source enters the recall index (so a live register must invalidate the
# indexable cache). rss qualifies; the never-auto families also index but never reach this lane.
_RECALL_CAPABLE_FAMILIES = frozenset({"rss", "org_watch", "page_watch", "news_scraper"})

# The in-tree config file each family's base rows live in (relative to .../eye/sources/). Reused to
# read the BASE names for the collision check in append() (an overlay row may NEVER shadow a base
# adapter). Mirrors apply._FAMILY_CONFIG_FILE / candidates evidence file mapping.
_FAMILY_CONFIG_FILE = {
    "rss": "scrape/rss_bundles.json",
    "news_scraper": "scrape/scrape_sites.json",
    "org_watch": "api/org_watch.json",
    "search_index": "api/search_index_sites.json",
    "page_watch": "scrape/page_watch.json",
}
_SOURCES_DIR = (Path(__file__).resolve().parents[1] / "sources")  # .../omniseek/eye/sources


# ── overlay IO (atomic, lock-guarded, corrupt-tolerant; mirrors candidates._load_all) ────────────
def _path(family: str) -> Path:
    return OVERLAY_DIR / f"{family}_overlay.json"


def load_overlay(family: str) -> list:
    """Read a family's overlay file -> a list of records. Tolerant: missing / corrupt / not-a-list
    -> [] (logged, NEVER raises into a loader). Filters to records whose ``row`` is a dict with a
    truthy ``name`` (a hand-edited / fuzzed overlay can never feed a loader a junk record)."""
    p = _path(family)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator %s_overlay.json unreadable (%s) -> treating as empty []",
                       family, exc)
        return []
    if not isinstance(data, list):
        logger.warning("curator %s_overlay.json is not a list -> treating as empty []", family)
        return []
    out = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        row = rec.get("row")
        if isinstance(row, dict) and row.get("name"):
            out.append(rec)
    return out


def overlay_rows(family: str) -> list:
    """Just the row dicts (what the family loaders concat after their base rows)."""
    return [rec["row"] for rec in load_overlay(family)]


def _base_names(family: str) -> set:
    """The set of row NAMES in the in-tree base config for a family (read fresh; tolerant)."""
    rel = _FAMILY_CONFIG_FILE.get(family)
    if not rel:
        return set()
    p = _SOURCES_DIR / rel
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001: a missing/corrupt base just yields no base names
        logger.debug("curator base-names read failed for %s: %s", family, exc)
        return set()
    if not isinstance(rows, list):
        return set()
    return {str(r.get("name")) for r in rows if isinstance(r, dict) and r.get("name")}


def append(family: str, row: dict, candidate_id: str, by: str) -> dict:
    """Append an overlay record under _LOCK (atomic). REFUSES (raises ValueError) if the row name
    already exists in the BASE config JSON OR the overlay: an overlay row may never shadow a base
    adapter, and a double-append is rejected. Returns the persisted record. NOT a live register:
    apply_overlay_row registers FIRST, then calls this (register-before-append ordering)."""
    if family not in _KNOWN_FAMILIES:
        raise ValueError(f"unknown family {family!r}; valid: {sorted(_KNOWN_FAMILIES)}")
    if not isinstance(row, dict) or not row.get("name"):
        raise ValueError("row must be a dict with a truthy 'name'")
    name = str(row["name"])
    with _LOCK:
        if name in _base_names(family):
            raise ValueError(
                f"refuse overlay append: {name!r} already a base {family} adapter (base wins)")
        existing = load_overlay(family)
        if any(rec["row"].get("name") == name for rec in existing):
            raise ValueError(f"refuse overlay append: {name!r} already in the {family} overlay")
        rec = {
            "row": row,
            "candidate_id": candidate_id,
            "admitted_at": _now_iso(),
            "admitted_by": by,
            "git_committed": False,
        }
        existing.append(rec)
        OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        cache._atomic_write_text(
            _path(family), json.dumps(existing, default=str, ensure_ascii=False, indent=1))
        return rec


def drop(family: str, name: str) -> bool:
    """Rollback primitive: atomic-rewrite the overlay minus the named row. Idempotent (a name
    already gone -> False, a no-op success). Returns True iff a row was removed."""
    if family not in _KNOWN_FAMILIES:
        raise ValueError(f"unknown family {family!r}")
    with _LOCK:
        existing = load_overlay(family)
        kept = [rec for rec in existing if rec["row"].get("name") != name]
        if len(kept) == len(existing):
            return False
        OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        cache._atomic_write_text(
            _path(family), json.dumps(kept, default=str, ensure_ascii=False, indent=1))
        return True


def mark_committed(family: str, name: str) -> bool:
    """Reconciler: flip git_committed True on an overlay row (a DELIBERATE operator-promoted-it
    step, never auto). Returns True iff the row was found. The loader's base-wins de-dup then makes
    the double-register safe once the operator commits the in-tree sibling."""
    with _LOCK:
        existing = load_overlay(family)
        hit = False
        for rec in existing:
            if rec["row"].get("name") == name:
                rec["git_committed"] = True
                hit = True
        if not hit:
            return False
        OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        cache._atomic_write_text(
            _path(family), json.dumps(existing, default=str, ensure_ascii=False, indent=1))
        return True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── per-family adapter construction (the SAME classes the import loaders build) ──────────────────
def register_one(family: str, row: dict):
    """Build the family adapter instance for a config row (the same class the import-time loader
    uses). Returns the adapter, or None for page_watch (a page_watch row is an extra ROW under the
    single PageWatchAdapter, never a new adapter: the loader handles it via _rows()). Used by BOTH
    apply_overlay_row (to register live) and the overlay-aware loaders (to register at import)."""
    if family == "rss":
        from omniseek.core.sources.scrape.rss_bundles_source import _RSSBundle
        return _RSSBundle(
            name=row["name"], description=row["description"], feeds=row["feeds"],
            cache_ttl=row.get("cache_ttl", 1800), url_pattern=row.get("url_pattern"),
            explicit_only=row.get("explicit_only", False), guard_ip=True)  # overlay-origin: SSRF guard
    if family == "org_watch":
        from omniseek.core.sources.api.org_watch_source import _OrgWatchAdapter
        return _OrgWatchAdapter(
            name=row["name"], affiliations=row["affiliations"],
            description=row["description"], regions=row.get("regions"))
    if family == "news_scraper":
        from omniseek.core.sources.scrape.news_scraper_source import _ScrapeSite
        return _ScrapeSite(
            name=row["name"], description=row["description"], sites=row["sites"],
            cache_ttl=row.get("cache_ttl", 10800), url_pattern=row.get("url_pattern"),
            explicit_only=row.get("explicit_only", False))
    if family == "page_watch":
        return None  # an extra ROW, not a new adapter (the single PageWatchAdapter owns all rows)
    if family == "search_index":
        from omniseek.core.sources.api.search_index_source import _SearchVenue
        return _SearchVenue(
            name=row["name"], description=row["description"], site=row["site"],
            extra=row.get("extra", ""), url_filter=row.get("url_filter", ""),
            explicit_only=row.get("explicit_only", False),
            domains=row.get("domains"), regions=row.get("regions"))
    raise ValueError(f"register_one: unknown family {family!r}")


# ── the live-apply sequence (the ONE load-bearing ordering invariant; spec §1.7) ─────────────────
def apply_overlay_row(cand: dict) -> dict:
    """Apply an admitted candidate's row LIVE + reversibly. Abort-on-fail BEFORE any side effect,
    in this exact order (any reordering reintroduces a documented break):

      1. validate the row TYPED (apply.validate_row_typed): abort if non-empty (never register junk)
      2. re-derive the gate FRESH (apply._auto_apply_ok re-reads live roster/policy; TOCTOU guard) :
         abort if False; defensively also abort if family in _NEVER_AUTO_FAMILIES
      3. fetcher.register_adapter_live(adapter): register BEFORE the overlay write; an
         AdapterCollision aborts with NO overlay row written. page_watch: no register (an extra row),
         so the overlay append IS the live effect (PageWatchAdapter._rows reads it next call)
      4. apply_live.append(family, row, ...): atomic overlay append under _LOCK
      5. recall.invalidate_indexable_cache() if family is recall-capable (fail-open)
      6. candidates.record_applied(...): the durable history stamp (via:one_tap_overlay)

    Returns a receipt dict; raises on any abort condition (the caller surfaces it to the operator)."""
    from omniseek.core import fetcher
    from omniseek.core.curator import apply as _apply
    from omniseek.core.curator import candidates as _candidates

    family = (cand.get("proposed_family") or "other").lower()
    cid = cand.get("id")
    by = "one_tap_overlay"

    # The exact in-tree row the family loader would consume (evidence._proposed_config_row verbatim).
    evidence = cand.get("evidence") or {}
    rev = evidence.get("reversibility") or {}
    row = rev.get("proposed_config_row") or cand.get("proposed_config_row")
    if not isinstance(row, dict):
        raise ValueError("no proposed_config_row to apply (run omniseek_curator_probe first)")

    # 1. typed validation (network-free; never tests/smoke.py which sys.exits + reloads all 143).
    problems = _apply.validate_row_typed(family, row)
    if problems:
        raise ValueError(f"row failed typed validation, NOT applied: {problems}")

    # 2. re-derive the gate FRESH against the live roster/policy (TOCTOU guard).
    if family in _apply._NEVER_AUTO_FAMILIES:
        raise ValueError(
            f"refuse live apply: family {family!r} is in _NEVER_AUTO_FAMILIES "
            "(recurring fetch bypasses safe_fetch); use omniseek_curator_stage_commit")
    if not _apply._auto_apply_ok(cand):
        raise ValueError(
            "refuse live apply: the auto-apply gate is not satisfied (family/mode/redline/evidence/"
            "render/classification). Use the git-commit path (omniseek_curator_stage_commit).")

    name = str(row["name"])
    # 3. register live BEFORE the overlay write (a collision aborts with nothing persisted).
    registered = False
    if family != "page_watch":
        adapter = register_one(family, row)
        fetcher.register_adapter_live(adapter)  # raises AdapterCollision -> abort, no overlay row
        registered = True

    try:
        # 4. atomic overlay append (also raises if name already present: register-before-append
        #    means a live adapter is in place first; the append refusal then rolls the register back).
        rec = append(family, row, candidate_id=cid, by=by)
    except Exception:
        if registered:
            fetcher.unregister_adapter(name)  # undo the live register so we never half-apply
        raise

    # 5. recall cache invalidation (fail-open: a recall import error must not fail the apply).
    if family in _RECALL_CAPABLE_FAMILIES:
        try:
            from omniseek.core import recall
            recall.invalidate_indexable_cache()
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall.invalidate_indexable_cache failed (non-fatal): %s", exc)

    # 6. durable history stamp (the dormant candidates.record_applied hook, now its moment).
    applied = {
        "via": "one_tap_overlay",
        "family": family,
        "name": name,
        "overlay_path": str(_path(family)),
        "git_committed": False,
    }
    try:
        _candidates.record_applied(cid, applied, note="applied live (overlay)")
    except Exception as exc:  # noqa: BLE001: the live effect already landed; a stamp failure is logged
        logger.warning("record_applied stamp failed for %s (live effect already applied): %s",
                       cid, exc)

    return {
        "applied": True,
        "family": family,
        "name": name,
        "row": row,
        "overlay_record": rec,
        "git_committed": False,
        "live_registered": registered,
    }


def rollback_overlay_row(family: str, name: str) -> dict:
    """FULL revert of a live-applied overlay row (spec §3.2): unregister the live adapter AND drop
    the overlay row, under the registry lock semantics. A rollback that only dropped the overlay
    would leave the adapter in _adapters (still omniseek_fetch-able): a half-applied state. Idempotent:
    a double-rollback with the name already gone is a no-op success."""
    from omniseek.core import fetcher

    if family not in _KNOWN_FAMILIES:
        raise ValueError(f"unknown family {family!r}")
    if family != "page_watch":
        fetcher.unregister_adapter(name)  # leaves _adapters immediately; get_adapter -> None
    dropped = drop(family, name)
    if family in _RECALL_CAPABLE_FAMILIES:
        try:
            from omniseek.core import recall
            recall.invalidate_indexable_cache()
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall.invalidate_indexable_cache failed (non-fatal): %s", exc)
    return {"rolled_back": True, "family": family, "name": name, "overlay_dropped": dropped}


# ── the runtime retire overlay (omniseek_curator_retire_live; the prune live-half) ────────────────────
_RETIRE_PATH = (Path.home() / ".omniseek" / "state" / "curator" / "explicit_only_overrides.json")


def load_retire_overlay() -> dict:
    """Read the runtime retire overlay -> {name: "retired:<reason> <date>"}. Tolerant: missing /
    corrupt / not-a-dict -> {} (logged). The fetcher reads the SAME file (cached) via
    _explicit_only_overrides; this module owns the WRITE side."""
    if not _RETIRE_PATH.exists():
        return {}
    try:
        data = json.loads(_RETIRE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator explicit_only_overrides.json unreadable (%s) -> {}", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


def retire_live(name: str, reason: str) -> dict:
    """Add a reversible runtime retire for a source: write name -> "retired:<reason> <date>" into
    the retire overlay (atomic, under _LOCK) so _explicit_only_reason returns it and the source
    leaves the broad fan-out immediately (no restart, no git). Invalidates the fetcher cache + the
    recall cache. The DURABLE half (the in-tree explicit_only edit + the smoke frozen-list line) is
    staged for the operator separately (source_audit.prepare_source_prune_case)."""
    from omniseek.core import fetcher

    retire_value = f"retired:{reason} {time.strftime('%Y-%m-%d', time.gmtime())}"
    with _LOCK:
        overlay = load_retire_overlay()
        overlay[name] = retire_value
        _RETIRE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cache._atomic_write_text(
            _RETIRE_PATH, json.dumps(overlay, default=str, ensure_ascii=False, indent=1))
    fetcher.invalidate_explicit_only_overrides()
    _invalidate_recall()
    return {"retired": True, "source": name, "explicit_only_value": retire_value}


def unretire_live(name: str) -> dict:
    """Rollback a runtime retire: drop the overlay entry so the source rejoins the broad fan-out
    live. Idempotent (a name already gone -> a no-op success)."""
    from omniseek.core import fetcher

    with _LOCK:
        overlay = load_retire_overlay()
        existed = name in overlay
        if existed:
            overlay.pop(name, None)
            _RETIRE_PATH.parent.mkdir(parents=True, exist_ok=True)
            cache._atomic_write_text(
                _RETIRE_PATH, json.dumps(overlay, default=str, ensure_ascii=False, indent=1))
    fetcher.invalidate_explicit_only_overrides()
    _invalidate_recall()
    return {"unretired": True, "source": name, "was_retired": existed}


def _invalidate_recall() -> None:
    try:
        from omniseek.core import recall
        recall.invalidate_indexable_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall.invalidate_indexable_cache failed (non-fatal): %s", exc)
