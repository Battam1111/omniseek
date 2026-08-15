"""Red-lines as operator DATA: pure host/suffix/regex/query-term matching, NO admission verdict.

A red line is an operator-owned rule that a candidate source's URLs or query-bearing fields
touch a forbidden pattern. The MECHANISM lives here (ship + a seed); the CONTENTS of
``redlines.json`` (which lines exist, each line's hard/soft severity) are operator policy
data, reviewed/edited at install. Smoke freezes the shape + an EXPECTED_REDLINES id set so a
silent loosening fails the deploy, and asserts NO national-origin term appears.

``match(candidate)`` reports WHICH lines a candidate touches. It does NOT decide admission.
The ONE place severity bites mechanically is downstream (omniseek_curator_decide refuses an admit
when any HARD line hit): and that refusal lives in the decide tool, not here. This module
only reports facts.

National-origin is NOT a red line (operator policy, 2026-06-10): the sources carry no
national-origin field and we never touch LinkedIn profiles, so a national-origin "guardrail" is
a cargo-cult pseudo-rail; national origin is a legitimate, core dimension of immigration
research. Smoke asserts no national-origin term appears in this data file.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("redlines.json")

# Frozen id set (smoke asserts equality). Adding/removing a line is a deliberate operator edit
# that must update BOTH redlines.json AND this constant.
EXPECTED_REDLINES = frozenset({
    "linkedin_trackb", "pii_email_query", "pii_employment_tracker", "scraper_data_broker",
})

# P4 (Attack-2 / spec 8a): for a CURATOR-DISCOVERED candidate (no human submitter to vet it),
# these otherwise-soft lines are PROMOTED to hard so the cron cannot auto-route a privacy-adjacent
# discovered candidate to awaiting_verdict without an agent's deliberate review. A human-submitted
# candidate keeps the soft severity (a person consciously vetted it).
_PROMOTE_HARD_IF_DISCOVERED = frozenset({"pii_employment_tracker"})

_VALID_SEVERITY = frozenset({"hard", "soft"})
_VALID_KIND = frozenset({"host", "host_suffix", "path_regex", "query_term"})


def load_rules() -> list:
    """Parse redlines.json. Returns [] on missing/corrupt (logged): fail-closed posture is
    enforced at the decide gate, not by silently dropping rules here, but a missing file
    means no lines matched, which is the conservative report (the gate still defaults reject
    on thin evidence)."""
    if not _DATA.exists():
        logger.warning("curator redlines.json missing -> no red-lines loaded")
        return []
    try:
        rows = json.loads(_DATA.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator redlines.json unreadable: %s", exc)
        return []
    return rows if isinstance(rows, list) else []


def _query_bearing_strings(candidate: dict) -> list:
    """The query-bearing fields a query_term line is checked against (NOT only urls): a
    personal-data-harvesting scrape rides a whitelisted host's QUERY parameter (org_watch
    affiliation = an email or 'former <Org>'; an rss feed query param; search_index extra)."""
    out: list = []
    for aff in (candidate.get("affiliations") or []):
        if isinstance(aff, str):
            out.append(aff)
    # org_watch-style proposed config carries affiliations under the proposed row too.
    row = candidate.get("proposed_config_row") or {}
    for aff in (row.get("affiliations") or []):
        if isinstance(aff, str):
            out.append(aff)
    extra = candidate.get("extra") or row.get("extra")
    if isinstance(extra, str) and extra:
        out.append(extra)
    # An RSS feed URL's query string is itself query-bearing.
    for u in (candidate.get("urls") or []):
        if isinstance(u, str):
            q = urlparse(u).query
            if q:
                out.append(q)
    return out


def _is_discovered(candidate: dict) -> bool:
    """True iff this candidate came from the P4 discovery loop (no human submitter): it carries a
    ``_discovery`` provenance sub-dict OR was submitted_by the curator-loop. Used to PROMOTE the
    privacy-adjacent soft lines to hard for discovered candidates (spec 8a)."""
    if candidate.get("_discovery") or candidate.get("discovery"):
        return True
    return (candidate.get("submitted_by") or "") == "curator-loop"


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _match_one(rule: dict, candidate: dict) -> Optional[dict]:
    """Return a hit dict if this rule matches the candidate, else None. Pure matching."""
    kind = rule.get("kind")
    value = rule.get("value") or ""
    urls = [u for u in (candidate.get("urls") or []) if isinstance(u, str)]

    matched_value = None
    if kind == "host":
        for u in urls:
            if _host_of(u) == value.lower():
                matched_value = u
                break
    elif kind == "host_suffix":
        suf = value.lower().lstrip(".")
        for u in urls:
            h = _host_of(u)
            if h == suf or h.endswith("." + suf):
                matched_value = u
                break
    elif kind == "path_regex":
        try:
            pat = re.compile(value)
        except re.error:
            return None
        for u in urls:
            path = urlparse(u).path
            if pat.search(path):
                matched_value = u
                break
    elif kind == "query_term":
        try:
            pat = re.compile(value)
        except re.error:
            return None
        # query_term applies to BOTH query-bearing fields AND the raw urls (a PII email could
        # sit in a path or query of a submitted url too).
        for s in (_query_bearing_strings(candidate) + urls):
            if isinstance(s, str) and pat.search(s):
                matched_value = s[:200]
                break
    if matched_value is None:
        return None
    return {
        "id": rule.get("id"),
        "severity": rule.get("severity"),
        "kind": kind,
        "reason": rule.get("reason"),
        "matched_value": matched_value,
    }


def match(candidate: dict) -> list:
    """Return the list of red-line hits a candidate touches (over its urls + query-bearing
    fields). PURE matching: reports facts, decides nothing. Each hit carries severity so the
    downstream decide gate can refuse an admit on any HARD hit."""
    hits: list = []
    discovered = _is_discovered(candidate)
    for rule in load_rules():
        if not all(rule.get(k) for k in ("id", "severity", "kind", "value")):
            continue
        hit = _match_one(rule, candidate)
        if hit is None:
            continue
        # 8a: promote a privacy-adjacent soft line to hard for a discovered candidate (no human
        # submitter vetted it). The on-disk rule stays soft; the promotion is per-candidate.
        if discovered and hit.get("id") in _PROMOTE_HARD_IF_DISCOVERED and hit.get("severity") == "soft":
            hit = {**hit, "severity": "hard", "promoted_for_discovered": True}
        hits.append(hit)
    return hits


def has_hard_hit(candidate_or_hits) -> bool:
    """Convenience: True iff any HARD-severity line is hit. Accepts a candidate dict or a
    pre-computed hits list."""
    hits = candidate_or_hits if isinstance(candidate_or_hits, list) else match(candidate_or_hits)
    return any(h.get("severity") == "hard" for h in hits)
