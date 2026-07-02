"""Paper enrichment — a THIN, keyless, mechanical primitive. No judgment.

Signals a per-paper lookup wants but the field-MAP tools (field_skeleton) do not give cleanly
on a SINGLE id (verified gaps):
  • Open-access FULL-TEXT PDF: arXiv is always OA (build the pdf url directly); real DOIs are
    resolved via Unpaywall (best_oa_location.url_for_pdf). This turns a citation node from
    "abstract only" into "readable full text" — which is why we did NOT bolt on a synthesis
    engine (e.g. PaperQA2): the AGENT fetches the PDF and synthesizes. Minimal code, max agent.
  • INTEGRITY: is the paper retracted / flagged? Crossref carries the Retraction Watch feed in
    a work's ``updated-by`` array (types: retraction / expression_of_concern / correction / …).
  • CITATION COUNT for ONE paper: read from the call already made. DOI from the Crossref message
    (is-referenced-by-count, free), arXiv from one bounded cached S2 call (citationCount). This is
    the paper's natural home for the count, so an agent need not repurpose the field-map tool for it.

This reports facts only. The AGENT decides when to enrich (before reading → get the PDF;
before trusting a high-stakes cite → check integrity) and what to do with the result.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import feedparser

from penumbra.core import auth, cache, http

logger = logging.getLogger(__name__)

# Contact for the polite pools (Unpaywall ?email=, Crossref ?mailto=, our UA). Host-injected;
# never a hardcoded personal address (see auth.contact_email).
_MAIL = auth.contact_email()
_UA = f"penumbra/0.1 (mailto:{_MAIL}; paper enrichment)"
_TIMEOUT = 20
_MAX_IDS = 50

# arXiv Atom API (the SAME endpoint arxiv_source uses): one id_list lookup yields both the
# published-journal DOI (arxiv_doi) and the withdrawal marker (arxiv_comment / title / abstract).
_ARXIV_API = "https://export.arxiv.org/api/query"
# arXiv has no machine retraction feed; an author withdrawal is announced in prose, by convention
# the word "withdrawn" in the comment / title / abstract (e.g. "This paper has been withdrawn by
# the author"). Word-boundary anchored so it never trips on "withdrawnness"-style substrings.
_ARXIV_WITHDRAWN_RE = re.compile(r"\bwithdrawn\b", re.I)

# arXiv id with explicit context (arxiv:/abs/pdf//10.48550/arxiv.) OR a bare YYMM.NNNNN.
# A DOI always starts with "10." so it can never hit the bare branch → no false positives.
_ARXIV_CTX_RE = re.compile(r"(?:arxiv[:/.]|abs/|pdf/|10\.48550/arxiv\.)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_ARXIV_BARE_RE = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")

# Crossref ``updated-by`` notice types that signal an integrity event.
_INTEGRITY_TYPES = {"retraction", "expression_of_concern", "correction",
                    "erratum", "removal", "withdrawal"}
_RETRACTED_TYPES = {"retraction", "removal", "withdrawal"}

# FIXED-API host allowlist (the SSRF hardening for the STRUCTURE resolver route, attack-3): these
# resolvers are reached with a candidate-page-parsed DOI / arXiv id, so the id is attacker-
# influenceable. _get_json pins the request to one of these trusted hosts AND refuses redirects (a
# 3xx is the attack signal — these APIs return 200 JSON; a redirect -> resolution failure). Three
# independent constraints hold together: the id regex constrains the path, the allowlist constrains
# the host, follow_redirects=False constrains the redirect. (Not full safe_fetch: these are fixed
# trusted hosts with pooled keep-alive, not arbitrary attacker hosts.)
_API_HOSTS = frozenset({"api.unpaywall.org", "api.crossref.org", "arxiv.org"})


class _OffAllowlistHost(ValueError):
    """A resolver URL whose host is not in _API_HOSTS (a candidate-influenced id redirected /
    pointed off-host). _get_json raises this; the callers' except returns the empty record."""


def _arxiv_id(s: str) -> str | None:
    s = (s or "").strip()
    m = _ARXIV_CTX_RE.search(s) or _ARXIV_BARE_RE.match(s)
    return m.group(1) if m else None


def _doi(s: str) -> str | None:
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (s or "").strip(), flags=re.I)
    return s if s.startswith("10.") else None


def _get_json(url: str) -> dict:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in _API_HOSTS:
        raise _OffAllowlistHost(f"resolver host {host!r} not in the fixed-API allowlist")
    # Route through the shared pooled http client (keep-alive reuse to api.unpaywall.org /
    # api.crossref.org instead of a fresh TCP+TLS handshake per id; behind the size cap +
    # cache_only egress guard). follow_redirects=False is passed PER CALL to override the pooled
    # client's client-level follow_redirects=True: these APIs answer 200 JSON, so a 3xx is the
    # off-host redirect attack (attack-3) — refuse it, keeping the host pin + id regex + redirect
    # refusal as three independent SSRF constraints. The enrich _UA carries the mailto courtesy
    # contact unpaywall/crossref expect (their mailto/email params already ride the url query).
    # http.get_json returns None on failure; re-raise so the callers' except returns the empty record.
    d = http.get_json(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=False)
    if d is None:
        raise RuntimeError(f"resolver request failed or returned no JSON: {url}")
    return d


def _unpaywall(doi: str) -> dict:
    try:
        d = _get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={_MAIL}")
        loc = d.get("best_oa_location") or {}
        return {"is_oa": bool(d.get("is_oa")), "pdf_url": loc.get("url_for_pdf"),
                "oa_url": loc.get("url"), "oa_host": loc.get("host_type")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("unpaywall %s: %s", doi, exc)
        return {"is_oa": None, "pdf_url": None}


def _integrity(doi: str) -> dict:
    try:
        d = _get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?mailto={_MAIL}")["message"]
        notices = sorted({u.get("type") for u in (d.get("updated-by") or []) if u.get("type")}
                         & _INTEGRITY_TYPES)
        # The citation count rides the SAME Crossref message we already fetched (no extra HTTP):
        # is-referenced-by-count is Crossref's citation count, used identically by crossref_source.
        return {"retracted": bool(set(notices) & _RETRACTED_TYPES), "notices": notices,
                "citation_count": d.get("is-referenced-by-count")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("crossref integrity %s: %s", doi, exc)
        return {"retracted": None, "notices": [], "citation_count": None}


def _arxiv_integrity(ax: str) -> dict:
    """arXiv integrity from ONE Atom lookup: a withdrawal marker (author-announced in prose) and,
    when the preprint carries a published-journal DOI, that DOI routed through the SAME Crossref /
    Retraction-Watch path the DOI branch uses. arXiv has no machine retraction feed, so a withdrawal
    is detected from the comment / title / abstract. ``ax`` is already regex-constrained by
    _arxiv_id() (\\d{4}\\.\\d{4,5}), so the id_list value is injection-safe; degrades to the
    honest "not checked" record on any failure (network / parse / Crossref down)."""
    try:
        xml = http.get_text(_ARXIV_API, headers={"User-Agent": _UA}, timeout=_TIMEOUT,
                            params={"id_list": ax, "max_results": 1})
        if not xml:
            return {"retracted": None, "notices": [], "note": "arxiv: not checked"}
        entries = feedparser.parse(xml).entries
        if not entries:
            return {"retracted": None, "notices": [], "note": "arxiv: not checked"}
        e = entries[0]
        # Withdrawal is announced in prose (comment is the canonical home; title/abstract as backup).
        blob = " ".join(filter(None, (e.get("arxiv_comment"), e.get("title"), e.get("summary"))))
        # The prose withdrawal marker is a FACT surfaced as a NOTICE for the agent to judge, NOT a
        # retraction verdict: a regex on prose would false-positive on "not withdrawn" or a paper
        # that merely discusses withdrawal. The authoritative ``retracted`` boolean comes ONLY from
        # the published-journal DOI's Crossref / Retraction-Watch record when the preprint has one;
        # arXiv has no machine retraction feed, so an arXiv-only paper stays retracted=None (the
        # notice still carries the marker, so a genuinely withdrawn arXiv-only paper is visible).
        notices = ["arxiv_withdrawn"] if _ARXIV_WITHDRAWN_RE.search(blob) else []
        journal_doi = _doi(e.get("arxiv_doi") or "")
        if journal_doi:
            di = _integrity(journal_doi)
            notices = sorted(set(notices) | set(di.get("notices") or []))
            return {"retracted": di.get("retracted"), "notices": notices,
                    "note": f"arxiv: marker noted + journal-doi {journal_doi} checked",
                    "journal_doi": journal_doi}
        return {"retracted": None, "notices": notices,
                "note": "arxiv: withdrawal marker noted (arxiv-only; no authoritative retraction feed)"
                        if notices else "arxiv: checked (arxiv-only; no journal DOI, no marker)"}
    except Exception as exc:  # noqa: BLE001 — failure → honest "not checked", never raise
        logger.warning("arxiv integrity %s: %s", ax, exc)
        return {"retracted": None, "notices": [], "note": "arxiv: not checked"}


def enrich(ids: list[str]) -> list[dict]:
    """For each DOI / arXiv id: open-access PDF + integrity. Keyless, cached 24h."""
    out: list[dict] = []
    for raw in (ids or [])[:_MAX_IDS]:
        key = cache.make_key("enrich", raw)
        cached = cache.get(key)
        if cached is not None:
            out.append(cached)
            continue
        ax = _arxiv_id(raw)
        if ax:
            # ONE bounded, cached S2 call for the arXiv citation count (the arXiv branch makes no
            # other external call). _s2.get_paper normalizes the bare id to ArXiv:<id> and carries
            # the breaker + semaphore; it degrades to None, so the count is None when S2 is unreachable.
            from penumbra.core import _s2
            p = _s2.get_paper(ax, fields=["citationCount"])
            rec = {"id": raw, "kind": "arxiv", "doi": f"10.48550/arXiv.{ax}",
                   # .pdf suffix so penumbra_read_document/_fmt_of routes it to the PDF reader (a bare
                   # /pdf/<id> has no extension → was rejected as "unsupported"). arXiv serves both.
                   "is_oa": True, "pdf_url": f"https://arxiv.org/pdf/{ax}.pdf",
                   "oa_url": f"https://arxiv.org/abs/{ax}",
                   "citation_count": getattr(p, "citationCount", None) if p else None,
                   # Withdrawal marker (author-announced on arXiv) + the journal DOI, if any,
                   # routed through the SAME Crossref / Retraction-Watch path the DOI branch uses.
                   "integrity": _arxiv_integrity(ax)}
        else:
            doi = _doi(raw)
            if not doi:
                rec = {"id": raw, "error": "not a DOI or arXiv id"}
            else:
                integrity = _integrity(doi)
                # Surface the count Crossref returned inside _integrity at the record top level.
                rec = {"id": raw, "kind": "doi", "doi": doi,
                       **_unpaywall(doi), "citation_count": integrity.get("citation_count"),
                       "integrity": integrity}
        cache.set(key, rec, ttl=24 * 3600)
        out.append(rec)
    return out
