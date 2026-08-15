"""Apply gate: P1 ships NO live auto-apply; this is the gate-only + operator-case-prep layer.

WHY no live apply in P1 (confirmed against the real code): every config family registers its
rows ONCE at import (rss_bundles_source._load_and_register, org_watch_source._load,
page_watch_source, news_scraper_source._load), reading the in-tree JSON via
Path(__file__).with_name(...). There is NO runtime overlay path, no importlib.reload, no
file-watch. So a row appended to a ~/.omniseek/state overlay is read by NOTHING (a silent no-op
marked 'applied' would be a lie smoke can't catch), and appending to the in-tree file violates
the read-only-deploy invariant and vanishes on the next deploy. THEREFORE: every admit stages
to owner_review with a ready-to-paste config row + a git-patch note; the operator commits, the
deploy restart picks it up. ``reversibility.auto_appliable`` is ALWAYS False in P1.

This module provides:
  * _live_hosts()   : the FRESH live-roster host set for dedup + first-seen (never stale)
  * _validate_row() : pure, network-free per-family required-field check (mirrors smoke's)
  * _auto_apply_ok(): the GATE (reads policy DATA); built for P1.5 but NEVER drives a mutation
                       in P1 (nothing calls it to write live config)
  * prepare_owner_case(): what omniseek_curator_stage_commit returns (render the row + patch note)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).with_name("admission_policy.json")

# Per-family required fields for a network-free row validation (mirrors tests/smoke.py CONFIGS).
_FAMILY_REQUIRED = {
    "rss": ("name", "description", "feeds"),
    "news_scraper": ("name", "description", "sites"),
    "org_watch": ("name", "affiliations", "description"),
    "search_index": ("name", "description", "site"),
    "page_watch": ("name", "label", "url"),
}

# Map a proposed family to the config file it would be pasted into (for the operator case).
_FAMILY_CONFIG_FILE = {
    "rss": "scrape/rss_bundles.json",
    "news_scraper": "scrape/scrape_sites.json",
    "org_watch": "api/org_watch.json",
    "search_index": "api/search_index_sites.json",
    "page_watch": "scrape/page_watch.json",
}


def _load_policy() -> dict:
    try:
        return json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("curator admission_policy.json unreadable: %s", exc)
        return {}


def default_posture() -> str:
    """The operator-set decision posture, read from policy DATA (NOT a code literal)."""
    return _load_policy().get("default_posture", "reject_if_thin")


def _auto_policy() -> dict:
    return _load_policy().get("auto_apply", {}) or {}


# ── live-roster host derivation (forced fresh; encodes the P31 over-count fix) ────
def _hosts_of_adapter(adapter) -> set:
    """Derive the host FQDNs an adapter serves. Host-LESS families (org_watch queries OpenAlex
    by free-text affiliation; search_index queries a search engine) contribute NO host. RSS
    bundles -> the feed hosts; page_watch -> the watched page hosts; scrape sites -> the index
    hosts. Best-effort + defensive (a new adapter shape just yields no host)."""
    hosts: set = set()
    # RSS bundle: feeds attribute is a list of feed URLs.
    feeds = getattr(adapter, "feeds", None)
    if isinstance(feeds, list):
        for f in feeds:
            h = urlparse(f).hostname if isinstance(f, str) else None
            if h:
                hosts.add(h.lower())
    # news_scraper: sites is a list of {"url": ...}.
    sites = getattr(adapter, "sites", None)
    if isinstance(sites, list):
        for s in sites:
            u = s.get("url") if isinstance(s, dict) else None
            h = urlparse(u).hostname if isinstance(u, str) else None
            if h:
                hosts.add(h.lower())
    # page_watch: rows carry per-page urls (read via the adapter's _rows()).
    rows_fn = getattr(adapter, "_rows", None)
    if getattr(adapter, "name", None) == "page_watch" and callable(rows_fn):
        try:
            for r in rows_fn():
                u = r.get("url") if isinstance(r, dict) else None
                h = urlparse(u).hostname if isinstance(u, str) else None
                if h:
                    hosts.add(h.lower())
        except Exception:  # noqa: BLE001
            pass
    return hosts


def _live_hosts() -> set:
    """The set of host FQDNs the LIVE roster currently serves. Forced FRESH from
    fetcher.list_sources / get_adapter (NEVER a passed-in or cached list: the P31 stale-list
    over-count fix). Host-less families contribute nothing. Used for dedup + first-seen."""
    hosts: set = set()
    try:
        from omniseek.core import fetcher
        for name in fetcher.all_adapter_names():
            try:
                adapter = fetcher.get_adapter(name)
                hosts |= _hosts_of_adapter(adapter)
            except Exception:  # noqa: BLE001: one bad adapter must not break derivation
                continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("curator _live_hosts roster derivation failed: %s", exc)
    return hosts


# ── per-family row validation (pure, network-free; mirrors smoke) ─────────────────
def _validate_row(family: str, row: dict) -> list:
    """Return a list of problems (empty == valid). Pure: checks required fields are present +
    truthy for the family, mirroring tests/smoke.py's per-family check. Does NOT run smoke
    (which sys.exits + re-load_sources()), does NOT touch the network."""
    problems: list = []
    required = _FAMILY_REQUIRED.get(family)
    if required is None:
        return [f"unknown family {family!r}"]
    if not isinstance(row, dict):
        return ["row is not an object"]
    for k in required:
        if not row.get(k):
            problems.append(f"missing required field {k!r}")
    return problems


def validate_row_typed(family: str, row: dict) -> list:
    """The TYPED companion to _validate_row: beyond required-field PRESENCE, check element SHAPE so
    a hand-edited / fuzzed overlay row can never register a junk adapter (or crash a loader at
    import). Pure, network-free. Returns a list of problems (empty == valid). The import-time
    overlay loaders + the live-apply sequence + the deploy smoke all call this.

      rss          : feeds is a NON-EMPTY list[str] of URLs (a bare str is rejected, else
                     RSSAdapterBase iterates the string's characters as if each were a feed).
      news_scraper : sites is a NON-EMPTY list[dict] each with a truthy 'url'; and render ABSENT on
                     every site for the auto-apply subclass (a render:true site is a CDP scrape).
      org_watch    : affiliations is a NON-EMPTY list of NON-EMPTY str (rejects [""], an all-empty
                     needle matches everything/nothing).
      page_watch / search_index : required fields present + non-empty (the presence floor suffices;
                     they carry no list-of-elements field to type-check).
    """
    problems = _validate_row(family, row)  # presence floor first (also catches unknown family)
    if problems:
        return problems
    if family == "rss":
        feeds = row.get("feeds")
        if not isinstance(feeds, list) or not feeds:
            problems.append("feeds must be a non-empty list")
        elif not all(isinstance(f, str) and f.strip() for f in feeds):
            problems.append("every feed must be a non-empty string URL")
    elif family == "news_scraper":
        sites = row.get("sites")
        if not isinstance(sites, list) or not sites:
            problems.append("sites must be a non-empty list")
        else:
            if not all(isinstance(s, dict) and s.get("url") for s in sites):
                problems.append("every site must be a dict with a truthy 'url'")
            if any(isinstance(s, dict) and s.get("render") for s in sites):
                problems.append("render:true site is a CDP scrape, not allowed in this subclass")
    elif family == "org_watch":
        affs = row.get("affiliations")
        if not isinstance(affs, list) or not affs:
            problems.append("affiliations must be a non-empty list")
        elif not all(isinstance(a, str) and a.strip() for a in affs):
            problems.append("every affiliation must be a non-empty string")
    return problems


# ── the auto-apply GATE (reads policy DATA; gate-only in P1) ──────────────────────
_NEVER_AUTO_FAMILIES = frozenset({"org_watch", "page_watch", "news_scraper", "search_index"})

# Fixed API-resolver hosts: an rss row whose feed host is one of these is a relabeled people/paper
# query (an OpenAlex/S2/Crossref API), NOT a real RSS feed. Used by the family/mode derivation in
# _auto_apply_ok to reject a relabel. (The enrich/_openalex host allowlists are the egress-time
# counterpart; this is the admission-time classification check.)
_API_RESOLVER_HOSTS = frozenset({
    "api.openalex.org", "api.semanticscholar.org", "api.crossref.org",
    "api.unpaywall.org",
})

# Content-types that mark a real feed (vs an API JSON / an HTML people page).
_FEED_CONTENT_TYPES = ("rss+xml", "atom+xml", "application/xml", "text/xml", "/xml")


def _derived_family_disagrees(family: str, fetch: dict) -> bool:
    """A classification guard against a relabeled candidate (the submitter-supplied
    proposed_family is UNTRUSTED). Derive a family from the PROBED reality (the resolved
    POST-REDIRECT final_url host + the terminal content_type) and return True iff it CONTRADICTS
    the declared family. Fail-OPEN when there is no probe artifact to derive from (the real
    auto-apply path always has one because evidence_complete requires probe_reached; a hand-built
    fixture with no fetch block simply isn't contradicted here, the other gates still bind).

    For family 'rss': the resolved final-URL host must NOT be an API-resolver host (else it is a
    relabeled people/paper query), AND when a content_type is present it must be feed-shaped (an
    rss row whose terminal type is application/json off an API host is a relabel)."""
    final_url = (fetch.get("final_url") or "")
    ctype = (fetch.get("content_type") or "").lower()
    final_host = (urlparse(final_url).hostname or "").lower() if final_url else ""

    # An API-resolver host is org_watch-class regardless of the label; an rss/feed declaration over
    # one is a fabrication.
    if final_host and final_host in _API_RESOLVER_HOSTS:
        if family == "rss":
            return True  # rss declared, but the feed resolves to an API resolver -> relabel
    if family == "rss" and ctype:
        # a real feed serves an xml/rss/atom content-type; a JSON terminal type is not a feed.
        if not any(tok in ctype for tok in _FEED_CONTENT_TYPES):
            return True
    return False


def _verified_mode(stage3: dict) -> str:
    """The mode the probe VERIFIED (from stage3 provenance), upper-cased. Falls back to the declared
    probe mode when no verified-provenance field is present. '' when no probe artifact."""
    if not stage3:
        return ""
    return (stage3.get("mode") or "").upper()


def _auto_apply_ok(cand: dict) -> bool:
    """The gate the live auto-apply lane consults (apply_live.apply_overlay_row). Returns True ONLY
    when family is in the policy auto_apply families AND mode is in the policy auto_apply modes AND
    no render signal AND the PROBED reality does not contradict the declared family/mode AND the
    agent's verdict is admit AND evidence_complete AND no red-line hit. org_watch / page_watch /
    news_scraper / search_index / render-scrapers are structurally excluded. Reads policy DATA, not
    code constants.

    THE RAZOR (corrected 2026-06-15): the AGENT's admit verdict IS the editorial + host-trust
    judgment (it read the evidence packet, incl. the stage0 safe_fetch result + the stage3 mode
    probe). This gate adds ONLY mechanical, non-judgment safety: the operator-owned auto_apply
    family/mode POLICY (the reversible-harvest boundary, a legitimate operator value), the anti-relabel
    classification check, and the no-render / no-red-line floors. It deliberately does NOT consult a
    operator-curated host allowlist or a first-seen-host gate: those routed a JUDGMENT ('is this host
    trustworthy') to a human, which the razor forbids. The one genuine safety reason the allowlist
    existed (the overlay rss recurring fetch bypasses safe_fetch) is closed MECHANICALLY instead, by
    the guard_ip IP-revalidation on overlay-origin _RSSBundle feeds (rss_bundles_source), so an
    agent-admitted feed cannot become an unguarded SSRF on its recurring poll.

    HARDENED (the classification hole): proposed_family / proposed_mode / urls are submitter-
    supplied + UNTRUSTED. The gate derives family + mode from the probe artifact (the resolved
    post-redirect final_url host + terminal content_type; the verified-provenance mode) and requires
    agreement, so a people-tracker relabeled family='rss' over an OpenAlex query cannot pass.

    Fires only when the agent has ADMITTED a candidate within the narrow auto_apply family/mode lane;
    every other admit stages to owner_review for the durable git commit (the operator's irreversible
    step). Inert until the loop is enabled AND an agent renders an admit.
    """
    policy = _auto_policy()
    fams = set(policy.get("families") or [])
    modes = set(policy.get("modes") or [])

    family = (cand.get("proposed_family") or "other").lower()
    mode = (cand.get("proposed_mode") or "").upper()

    if family in _NEVER_AUTO_FAMILIES:
        return False
    if family not in fams:
        return False
    if mode not in modes:
        return False

    evidence = cand.get("evidence") or {}
    safety = evidence.get("stage0_safety") or {}
    fetch = safety.get("fetch") or {}
    stage3 = evidence.get("stage3_mode_probe") or {}

    # family-agnostic render / credential signal: inspect BOTH the declared row's sites AND the
    # probed fetch, regardless of declared family (a render scrape never auto-applies).
    row = (evidence.get("reversibility") or {}).get("proposed_config_row") \
        or cand.get("proposed_config_row") or {}
    sites = row.get("sites") or []
    if any(isinstance(s, dict) and s.get("render") for s in sites):
        return False

    # the PROBED reality must not contradict the declared family (anti-relabel, the classification
    # hardening), and when a verified mode is present it must equal the declared mode in policy.
    if _derived_family_disagrees(family, fetch):
        return False
    vmode = _verified_mode(stage3)
    if vmode and vmode != mode:
        return False

    # verdict must be an admit, evidence must be complete, no hard/soft red-line.
    verdict = cand.get("verdict") or {}
    if verdict.get("decision") != "admit":
        return False
    if not evidence.get("evidence_complete"):
        return False
    if safety.get("redline_hits"):
        return False
    if safety.get("hard_redline_blocked"):
        return False
    return True


# ── operator-case preparation (what omniseek_curator_stage_commit returns) ───────────────
# A draft row that walks a declarative shape (transport / endpoint / field_map) belongs in
# sources.json (the declarative table), NOT one of the 5 legacy family files.
_DECLARATIVE_MARKERS = ("transport", "endpoint", "field_map", "results_path", "tool")
_DECLARATIVE_CONFIG_FILE = "sources.json"


def _looks_declarative(row: dict) -> bool:
    return isinstance(row, dict) and any(k in row for k in _DECLARATIVE_MARKERS)


def prepare_owner_case(cand: dict) -> dict:
    """P1: PREPARE the operator case (render the ready-to-paste config row + a git-patch note).
    Does NOT mutate live config. Re-checks the row validity. Never auto-applies.

    FOUNDRY-GRADE (P10): when the candidate carries a ``draft`` (a WORKING artifact the submitter
    built), the ready-to-paste block IS the draft's ``row`` verbatim (with a provenance line naming
    the submitting session), NOT the thin row re-derived from the candidate fields. A draft
    declarative row (transport mcp/http) targets sources.json; the rss-safe auto-apply lane is NOT
    widened here (a draft always stages to the operator's git commit)."""
    evidence = cand.get("evidence") or {}
    rev = evidence.get("reversibility") or {}
    family = (cand.get("proposed_family") or "other").lower()

    draft = cand.get("draft") if isinstance(cand.get("draft"), dict) else None
    draft_row = draft.get("row") if isinstance(draft, dict) and isinstance(draft.get("row"), dict) else None

    if draft_row is not None:
        # The draft row is the ready-to-paste block. A declarative row goes to sources.json; a
        # legacy-family draft row is validated against that family; otherwise presence-only.
        row = draft_row
        provenance = (f"Drafted by the submitting session ({cand.get('submitted_by') or 'agent'}) "
                      f"and staged verbatim; the fixture + probe_summary rode the packet.")
        if _looks_declarative(row):
            config_file = _DECLARATIVE_CONFIG_FILE
            # A declarative row is valid if it carries at least name + a title/url field_map (the
            # DeclarativeAPIAdapter's own floor); we do a light presence check, not a full load.
            problems = []
            if not row.get("name"):
                problems.append("missing required field 'name'")
            fm = row.get("field_map")
            if not isinstance(fm, dict) or "title" not in fm or "url" not in fm:
                problems.append("field_map must map at least 'title' and 'url'")
            if not row.get("endpoint"):
                problems.append("missing required field 'endpoint'")
            if (row.get("transport") or "http").lower() == "mcp" and not row.get("tool"):
                problems.append("transport 'mcp' requires a 'tool' name")
        else:
            config_file = _FAMILY_CONFIG_FILE.get(family)
            problems = _validate_row(family, row)
    else:
        row = rev.get("proposed_config_row")
        config_file = rev.get("config_file") or _FAMILY_CONFIG_FILE.get(family)
        problems = _validate_row(family, row) if row else ["no proposed_config_row in evidence"]
        provenance = None

    patch_note = ""
    if row and config_file:
        loader_desc = ("the declarative loader" if config_file == _DECLARATIVE_CONFIG_FILE
                       else f"the {family} family loader")
        patch_note = (
            f"Append this row to organs/eye/src/omniseek/eye/sources/{config_file} "
            f"({loader_desc} registers it at the next service restart), commit, "
            f"then redeploy. P1 stages this for the operator; it is NEVER applied automatically."
        )
    case = {
        "auto_appliable": False,          # ALWAYS False in P1 (no live apply path exists)
        "family": family,
        "config_file": config_file,
        "proposed_config_row": row,
        "row_valid": not problems,
        "row_problems": problems,
        "git_patch_note": patch_note,
        "note": ("P1 has no live auto-apply: config families register once at import. This "
                 "prepares an operator case; the operator commits + the deploy restart picks it up."),
    }
    if provenance is not None:
        case["draft_provenance"] = provenance
        case["from_draft"] = True
    # spec 2 / 8c (Attack-2): a family whose recurring POST-admission fetch BYPASSES safe_fetch
    # carries an explicit harm block in the operator case, so the irreversible commit is made with
    # eyes open: deleting the config row later cannot un-harvest data already indexed.
    if family in _NEVER_AUTO_FAMILIES:
        case["recurring_fetch_harm"] = {
            "family": family,
            "safe_fetch_bypassed": True,
            "harm": ("Once live, this family's adapter fetches the source on a recurring cadence "
                     "through the NORMAL fetcher (NOT the SSRF-pinned probe-time safe_fetch). "
                     "org_watch in particular is a turnkey people-tracker. Row deletion stops "
                     "FUTURE fetches but cannot un-harvest / un-index data already collected."),
            "mitigation": ("Verify the host is a stable, reputable, non-people-broker source "
                           "before committing. Prefer a narrower domain/affiliation scope. The "
                           "commit is the single irreversible sanction; weigh it accordingly."),
        }
    return case
