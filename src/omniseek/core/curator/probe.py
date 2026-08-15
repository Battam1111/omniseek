"""Mechanical mode-probes over the SSRF-hardened fetcher for attacker-influenceable candidate URLs.

This is the eye's FIRST pass pointed at hosts an adversary chose (a candidate-source URL).
Everything here is MECHANICAL: it fetches / measures / counts / resolves / matches and returns
FACTS. It renders NO verdict. There is no key or string-value naming score/verdict/passes/
recommend/admit/reject/good/quality/rating/confidence/decision/beats_web_search anywhere in a
probe's output: only counts, lists, lengths, dates, booleans-of-fact, each with a provenance
tag. The AGENT reads these facts and judges; the code never does.

``safe_fetch`` (the crux of safety: IP-pinned, redirect-revalidated, decode-capped) was PROMOTED
to the core-leaf module ``omniseek.core.safeurl`` so the untrusted web_fallback read can share the
EXACT same pinned-per-hop fetcher. It is RE-EXPORTED at the top of this module (with its helpers)
so every probe below + the smoke goldens exercise the moved impl through the same names; the
mechanics + rationale now live in safeurl's docstring.
"""

from __future__ import annotations

import logging
import os
import re
import socket  # kept so the moved-fetcher goldens' probe.socket.getaddrinfo monkeypatch resolves
import subprocess  # P2 wall-probe: cold-start the jail launcher (scripts/probe_jail.sh up) on demand
from pathlib import Path
from typing import Callable, Optional

import httpx  # kept so the moved-fetcher goldens' probe.httpx.Client / .MockTransport patch resolves

# safe_fetch + its SSRF helpers were PROMOTED to the core-leaf module omniseek.core.safeurl so the
# untrusted web_fallback read can share the EXACT pinned-per-hop fetcher (safeurl imports only core,
# so no curator import cycle). They are RE-EXPORTED here so probe's callers (mode_probe + the mode
# probes below) and its smoke goldens stay byte-identical: they exercise the MOVED impl through
# these names (curator.probe.safe_fetch IS safeurl.safe_fetch).
from omniseek.core.safeurl import (  # noqa: F401
    _DEFAULT_MAX_BYTES, _blocked, _host_suffix_blocked, _ip_is_blocked, _read_capped,
    _resolve_safe_ip, _validate_url_shape, safe_fetch,
)

logger = logging.getLogger(__name__)

# The §11 frozen acquisition-mode vocabulary (reused; smoke §12 asserts _PROBES keys == this).
MODE_VOCAB = {"STRUCTURE", "UNWALL", "TRANSCRIBE", "RECALL", "MONITOR"}

# ─────────────────────────────────────────────────────────────────────────────────
# Mode probes: each FETCHES candidate URLs via safe_fetch (re-exported from safeurl) and returns
# provenance-tagged FACTS only. Provenance tags: 'verified' (eye independently confirmed), 'claimed'
# (parsed straight from publisher bytes), 'derived' (computed by the eye over fetched content).
# ─────────────────────────────────────────────────────────────────────────────────

_PASSWORD_INPUT_RE = re.compile(r'<input[^>]*type=["\']?password', re.IGNORECASE)
_NEXTDATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
# Reuse the search_index tombstone phrases (the PHRASES, not the _is_tombstone boolean verdict).
try:
    from omniseek.core.sources.api.search_index_source import _TOMBSTONE_RE as _SI_TOMBSTONE_RE
except Exception:  # noqa: BLE001: degrade to a local copy if the import path moves
    _SI_TOMBSTONE_RE = re.compile(
        r"this page has moved|page not found|automatic redirect", re.IGNORECASE)

# DOI / arXiv / OpenAlex / S2 id syntax (for STRUCTURE presence detection; resolution follows).
_DOI_SYNTAX_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_ARXIV_SYNTAX_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
_OA_ID_RE = re.compile(r"\bW\d{6,}\b")


def _first_url(candidate: dict) -> Optional[str]:
    for u in (candidate.get("urls") or []):
        if isinstance(u, str) and u.strip():
            return u.strip()
    return None


def _baseline_queries(sample_titles: list) -> list:
    """Probe-DERIVED web-search baseline: up to 3 of the candidate's OWN real item titles, so
    the agent can check 'does plain web search already surface these exact items?'. The
    submitter cannot replace these (origin tagged); they cannot be pre-rigged by the adversary
    because they are the source's own parsed titles."""
    out = []
    for t in (sample_titles or [])[:3]:
        if isinstance(t, str) and t.strip():
            out.append({"q": t.strip()[:160], "origin": "probe-derived"})
    if not out:
        out.append({"q": "(no item titles parsed: agent must derive queries)",
                    "origin": "probe-derived"})
    return out


def _probe_structure(candidate: dict, fetch: dict) -> dict:
    """STRUCTURE: report RESOLVED ids, not regex hits (a syntax match is fabrication-prone).
    structured_fields_present = what the page CLAIMS; structured_fields_resolved = what the eye
    VERIFIED against OpenAlex/S2/Crossref. The divergence is the fabrication tell."""
    text = fetch.get("text") or ""
    present: list = []
    resolved: list = []

    dois = list({m.group(0) for m in _DOI_SYNTAX_RE.finditer(text)})[:5]
    arxiv = list({m.group(1) for m in _ARXIV_SYNTAX_RE.finditer(text)})[:5]
    oa_ids = list({m.group(0) for m in _OA_ID_RE.finditer(text)})[:5]
    if dois or arxiv:
        present.append("doi")
    if oa_ids:
        present.append("author_ids")

    doi_resolves = False
    author_ids_resolved = 0
    citation_count = None
    venue = None
    retracted = None

    # Resolve a DOI / arXiv id through the SAME enrich path the eye already trusts (Crossref/OA).
    for ident in (dois + arxiv)[:3]:
        try:
            from omniseek.core import enrich
            recs = enrich.enrich([ident])
        except Exception as exc:  # noqa: BLE001: resolution failure -> not resolved, never raise
            logger.debug("STRUCTURE enrich resolve failed for %r: %s", ident, exc)
            recs = []
        for rec in recs:
            if rec.get("error"):
                continue
            if rec.get("doi"):
                doi_resolves = True
                integ = rec.get("integrity") or {}
                if integ.get("retracted") is not None:
                    retracted = integ.get("retracted")
        if doi_resolves:
            break

    # Resolve an OpenAlex work-id (verifies citation_count/venue ONLY from the resolved record).
    for wid in oa_ids[:2]:
        try:
            from omniseek.core import _openalex as oa
            work = oa.get_json(f"/works/{wid}")
            if work and work.get("id"):
                author_ids_resolved += len(work.get("authorships") or [])
                if citation_count is None:
                    citation_count = work.get("cited_by_count")
                if venue is None:
                    loc = (work.get("primary_location") or {}).get("source") or {}
                    venue = loc.get("display_name")
        except Exception as exc:  # noqa: BLE001
            logger.debug("STRUCTURE OpenAlex resolve failed for %r: %s", wid, exc)

    if doi_resolves:
        resolved.append("doi")
    if author_ids_resolved:
        resolved.append("author_ids")

    diff = {
        "structured_fields_present": present,
        "structured_fields_resolved": resolved,
        "doi_resolves": doi_resolves,
        "author_ids_resolved": author_ids_resolved,
        "citation_count": citation_count,
        "venue": venue,
        "retracted": retracted,
    }
    provenance = {
        "structured_fields_present": "claimed",
        "structured_fields_resolved": "verified",
        "doi_resolves": "verified",
        "author_ids_resolved": "verified",
        "citation_count": "verified",
        "venue": "verified",
        "retracted": "verified",
    }
    return {"diff": diff, "diff_provenance": provenance,
            "web_baseline_request": {"suggested_queries": _baseline_queries([])}}


def _probe_unwall(candidate: dict, fetch: dict) -> dict:
    """UNWALL: anonymous-stranger fetch (NEVER a logged-in/CDP session). NO looks_walled
    boolean (that is a thresholded quality verdict); emit raw facts only. A near-empty
    text_len means the candidate is structurally invisible to P1 -> parked_p2 (handled by the
    caller), never a verdict."""
    text = fetch.get("text") or ""
    stripped = text.strip()
    tombstone_hits = sorted({m.group(0).lower() for m in _SI_TOMBSTONE_RE.finditer(text)})
    wall_marker_whole = bool(tombstone_hits) and len(stripped) < 400
    diff = {
        "text_len_plain": len(stripped),
        "bytes_plain": fetch.get("bytes") or 0,
        "has_login_form": bool(_PASSWORD_INPUT_RE.search(text)),
        "nextdata_empty": _nextdata_empty(text),
        "tombstone_phrase_hits": tombstone_hits,
        "wall_marker_is_whole_page": wall_marker_whole,
        "cdp_path_exists": True,  # a CDP wall-aware probe EXISTS; P1 does not drive it (P2 does)
    }
    provenance = {k: "derived" for k in diff}
    provenance["cdp_path_exists"] = "claimed"
    return {"diff": diff, "diff_provenance": provenance,
            "web_baseline_request": {"suggested_queries": _baseline_queries([])}}


def _nextdata_empty(text: str) -> bool:
    """True iff a __NEXT_DATA__ blob is present but carries (essentially) no payload: a
    JS-shell wall tell. Pure fact."""
    m = _NEXTDATA_RE.search(text or "")
    if not m:
        return False
    blob = (m.group(1) or "").strip()
    return len(blob) < 40


def _probe_transcribe(candidate: dict, fetch: dict) -> dict:
    """TRANSCRIBE: does NOT transcribe in P1 (cost). Parse the feed/page for media, then HEAD
    the media URL (declared != real). Hollowness (404 / length 0) becomes a visible fact."""
    text = fetch.get("text") or ""
    media_url = None
    duration_declared = None
    has_enclosure = False
    has_transcript = False

    # enclosure / audio/video tags in a feed or page.
    enc = re.search(r'<enclosure[^>]*url=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if enc:
        has_enclosure = True
        media_url = enc.group(1)
    if media_url is None:
        m = re.search(r'<(?:audio|video|source)[^>]*src=["\']([^"\']+)["\']', text, re.IGNORECASE)
        if m:
            media_url = m.group(1)
    dur = re.search(r'<itunes:duration>([^<]+)</itunes:duration>', text, re.IGNORECASE)
    if dur:
        duration_declared = dur.group(1).strip()[:32]
    if re.search(r'<track[^>]*kind=["\']?captions|<itunes:?\s*transcript|\.vtt|\.srt',
                 text, re.IGNORECASE):
        has_transcript = True

    media_reachable = None
    media_content_length = None
    media_content_type = None
    if media_url:
        hf = safe_fetch(media_url, method="HEAD")
        if hf.get("ok"):
            media_reachable = True
            media_content_type = hf.get("content_type")
            # content-length not in our facts dict; re-derive from bytes for a HEAD it is 0, so
            # rely on the content-type + reachability as the verified facts.
            media_content_length = hf.get("bytes")
        elif hf.get("blocked_reason"):
            media_reachable = False

    diff = {
        "has_media_enclosure": has_enclosure,
        "media_url": (media_url[:300] if media_url else None),
        "duration_declared": duration_declared,
        "has_existing_transcript": has_transcript,
        "media_reachable": media_reachable,
        "media_content_length": media_content_length,
        "media_content_type": media_content_type,
    }
    provenance = {
        "has_media_enclosure": "claimed",
        "media_url": "claimed",
        "duration_declared": "claimed",
        "has_existing_transcript": "claimed",
        "media_reachable": "verified",
        "media_content_length": "verified",
        "media_content_type": "verified",
    }
    return {"diff": diff, "diff_provenance": provenance,
            "web_baseline_request": {"suggested_queries": _baseline_queries([])}}


def _parse_feed_titles(text: str) -> "tuple[list, list, Optional[str], Optional[str]]":
    """Best-effort parse of a feed for (titles, bodies, oldest_date, newest_date). Uses
    feedparser if available, else a lightweight regex. Pure derivation."""
    titles: list = []
    bodies: list = []
    dates: list = []
    try:
        import feedparser
        parsed = feedparser.parse(text)
        for e in (parsed.entries or []):
            t = (e.get("title") or "").strip()
            if t:
                titles.append(t)
            body = e.get("summary") or e.get("description") or ""
            bodies.append(re.sub(r"<[^>]+>", "", body).strip())
            for k in ("published", "updated"):
                if e.get(k):
                    dates.append(str(e.get(k)))
                    break
    except Exception:  # noqa: BLE001: degrade to regex
        for m in re.finditer(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL):
            t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if t:
                titles.append(t)
    oldest = min(dates) if dates else None
    newest = max(dates) if dates else None
    return titles, bodies, oldest, newest


def _probe_recall(candidate: dict, fetch: dict) -> dict:
    """RECALL: parse the feed and report BOTH lexical AND semantic overlap vs the recall
    index, plus the actual matched rows. Their DIVERGENCE is the tell (high semantic + low
    lexical = already covered, other language). Never a bare overlap integer alone."""
    text = fetch.get("text") or ""
    titles, bodies, oldest, newest = _parse_feed_titles(text)
    sample_titles = titles[:10]
    body_lens = [len(b) for b in bodies if b]
    mean_body_len = round(sum(body_lens) / len(body_lens), 1) if body_lens else 0.0

    lexical = 0
    semantic = 0
    recall_hits: list = []
    try:
        from omniseek.core import recall
        for t in sample_titles[:5]:
            lex = recall.search(t, k=3)
            if lex:
                lexical += 1
                for d in lex[:1]:
                    recall_hits.append({"existing_title": (d.title or "")[:120],
                                        "source": d.source})
            try:
                hyb, _info = recall.hybrid(t, k=3)
            except Exception:  # noqa: BLE001
                hyb = []
            if hyb:
                semantic += 1
    except Exception as exc:  # noqa: BLE001: index unavailable -> 0 overlap, never raise
        logger.debug("RECALL probe index overlap failed: %s", exc)

    diff = {
        "item_count": len(titles),
        "oldest_item_date": oldest,
        "newest_item_date": newest,
        "sample_titles": sample_titles,
        "mean_body_len": mean_body_len,
        "sample_bodies_present": bool(body_lens),
        "recall_overlap_lexical": lexical,
        "recall_overlap_semantic": semantic,
        "recall_hits": recall_hits[:3],
    }
    provenance = {k: "derived" for k in diff}
    return {"diff": diff, "diff_provenance": provenance,
            "web_baseline_request": {"suggested_queries": _baseline_queries(sample_titles)}}


def _probe_monitor(candidate: dict, fetch: dict) -> dict:
    """MONITOR: single-sample change-signal affordances. A true monitor judgment needs TWO
    samples; P1 records only the affordance + a mandatory single-sample fact."""
    text = fetch.get("text") or ""
    titles, _bodies, oldest, newest = _parse_feed_titles(text)
    # last_modified / etag would come from response headers; safe_fetch returns content_type but
    # not arbitrary headers, so report what we can derive from the body + a single-sample note.
    has_stable_ids = bool(re.search(r"<guid|<id>", text, re.IGNORECASE))
    diff = {
        "last_modified": None,             # header-derived, not surfaced by safe_fetch facts
        "etag": None,
        "pubdate_spread": {"oldest": oldest, "newest": newest},
        "has_stable_item_ids": has_stable_ids,
        "item_count": len(titles),
        "cadence_confidence": "single-sample",
    }
    provenance = {
        "last_modified": "claimed",
        "etag": "claimed",
        "pubdate_spread": "derived",
        "has_stable_item_ids": "derived",
        "item_count": "derived",
        "cadence_confidence": "derived",
    }
    return {"diff": diff, "diff_provenance": provenance,
            "web_baseline_request": {"suggested_queries": _baseline_queries(titles[:10])}}


# Frozen dispatch table; keys == MODE_VOCAB (smoke §12 asserts equality).
_PROBES: "dict[str, Callable]" = {
    "STRUCTURE": _probe_structure,
    "UNWALL": _probe_unwall,
    "TRANSCRIBE": _probe_transcribe,
    "RECALL": _probe_recall,
    "MONITOR": _probe_monitor,
}


def mode_probe(candidate: dict, *, deadline_s: float = 25.0, walled: bool = False) -> dict:
    """Dispatch on candidate['mode'] -> the per-mode probe over a fetch of the candidate's first URL.
    Returns provenance-tagged FACTS + reachability. NO verdict key/value. Fail-closed: an unfetchable
    list / unknown mode / probe exception -> probe_reached=False + probe_error, never an admit-shaped
    result.

    ``walled`` selects the FETCH: False (P1) uses the anonymous ``safe_fetch`` (IP-pinned plain HTTP);
    True (P2, the wall-aware re-probe) uses ``render_walled`` (the JAILED browser render), so a
    candidate that was ``parked_p2`` because it is structurally invisible to plain HTTP (anti-bot /
    SPA / soft-login-wall) is measured on its REAL rendered content by the SAME per-mode probes. The
    wall-rendered facts are DERIVED FROM ATTACKER BYTES (a honeypot could author the DOM), so a single
    render NEVER admits: probe_via='wall_probe_jail' flags this so the judge weights it as such (M7)."""
    mode = (candidate.get("mode") or candidate.get("proposed_mode") or "").strip().upper()
    url = _first_url(candidate)
    base = {
        "mode": mode,
        "diff": {},
        "diff_provenance": {},
        "probe_reached": False,
        "probe_fetch_meta": {},
        "probe_error": None,
        "web_baseline_request": {"suggested_queries": _baseline_queries([])},
    }
    if mode not in _PROBES:
        base["probe_error"] = f"unknown mode {mode!r}"
        return base
    if not url:
        base["probe_error"] = "no candidate url to probe"
        return base

    if walled:
        fetch = render_walled(url, deadline_s=max(deadline_s, 45.0))
        base["probe_via"] = "wall_probe_jail"  # facts are render-derived (attacker bytes): weight per M7
    else:
        fetch = safe_fetch(url, timeout_total=min(deadline_s, 20.0))
    base["probe_fetch_meta"] = {
        "ok": fetch.get("ok"),
        "status": fetch.get("status"),
        "blocked_reason": fetch.get("blocked_reason"),
        "final_url": fetch.get("final_url"),
        "redirect_chain": fetch.get("redirect_chain"),
        "content_type": fetch.get("content_type"),
        "bytes": fetch.get("bytes"),
    }
    # probe_reached is True iff safe_fetch.ok (a blocked/failed fetch is not "reached").
    base["probe_reached"] = bool(fetch.get("ok"))
    if not fetch.get("ok"):
        base["probe_error"] = f"fetch blocked/failed: {fetch.get('blocked_reason')}"
        return base
    try:
        result = _PROBES[mode](candidate, fetch)
    except Exception as exc:  # noqa: BLE001: a probe exception is recorded, never raised out
        base["probe_error"] = f"{type(exc).__name__}: {exc}"[:160]
        return base
    base["diff"] = result.get("diff", {})
    base["diff_provenance"] = result.get("diff_provenance", {})
    base["web_baseline_request"] = result.get(
        "web_baseline_request", {"suggested_queries": _baseline_queries([])})
    return base


# ── P2: the wall-aware render (jailed browser) ─────────────────────────────────────
# render_walled is the P2 FETCH: it swaps the anonymous plain-HTTP safe_fetch for a render in the
# network-isolated jail (a colima container whose ONLY egress is the SSRF-pin proxy), so a candidate
# structurally invisible to plain HTTP yields its REAL rendered content. It returns a safe_fetch-
# SHAPED dict so the per-mode probes + build_packet consume it byte-identically.

def _jail_cdp_url() -> str:
    """Host-side CDP endpoint of the wall-probe jail (the socat bridge port colima forwards)."""
    return f"http://127.0.0.1:{os.environ.get('OMNISEEK_PROBE_CDP_PORT', '9444')}"


def _jail_script() -> Optional[Path]:
    s = Path(__file__).resolve().parents[4] / "scripts" / "probe_jail.sh"
    return s if s.exists() else None


def _ensure_jail_up() -> bool:
    """Idempotently ensure the wall-probe jail is running; return True once its CDP endpoint answers.
    The rare parked_p2 case pays a few-seconds cold start (``probe_jail.sh up``) rather than keeping
    three containers idle on the 16GB mini. Best-effort: any failure returns False (render_walled then
    fails closed), never raises."""
    cdp = _jail_cdp_url()
    try:
        if httpx.get(f"{cdp}/json/version", timeout=4).status_code == 200:
            return True
    except Exception:  # noqa: BLE001
        pass
    script = _jail_script()
    if script is None:
        logger.warning("wall-probe jail launcher not found (scripts/probe_jail.sh)")
        return False
    try:
        subprocess.run(["bash", str(script), "up"], timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return httpx.get(f"{cdp}/json/version", timeout=6).status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.warning("wall-probe jail cold-start failed: %s", exc)
        return False


def render_walled(url: str, *, deadline_s: float = 45.0) -> dict:
    """Render a WALLED candidate url in the jailed Chromium and return a ``safe_fetch``-SHAPED dict.

    The P2 fetch: it executes the candidate's JS (passing anti-bot / SPA / soft-login-wall) in a
    FRESH incognito context whose ONLY egress is the SSRF-pin proxy, so a page near-empty to
    ``safe_fetch`` yields its real rendered HTML here. Fail-closed: bad-shape url / jail down / render
    error / timeout -> a ``_blocked()`` dict (never a fabricated body). ``fetch['text']`` is the
    RENDERED HTML (``page.content()``) so the per-mode probes (which regex the HTML for __NEXT_DATA__ /
    DOI / password-input / tombstone and measure text_len) read it exactly as a safe_fetch body."""
    _parts, reason = _validate_url_shape(url)   # refuse a bad-shape / nonstandard-port target up front
    if reason is not None:
        return _blocked(reason)
    if not _ensure_jail_up():
        return _blocked("jail_unavailable")
    from omniseek.core.sources.walled import _cdp

    def _extract(page) -> dict:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001: networkidle can time out on a chatty page; the render is still usable
            pass
        page.wait_for_timeout(1200)   # let late client-render settle
        return {"html": page.content() or "", "url": page.url}

    try:
        r = _cdp.cdp_render(_extract, initial_url=url, cdp_url=_jail_cdp_url(),
                            timeout=int(min(max(deadline_s, 20.0), 90.0)))
    except Exception as exc:  # noqa: BLE001: a render failure is a blocked fetch, not a body
        logger.info("wall-probe render failed for %s: %s", url[:120], type(exc).__name__)
        return _blocked("render_error")
    html = (r.get("html") or "")[:_DEFAULT_MAX_BYTES]
    return {
        "ok": bool(html),
        "status": 200 if html else None,
        "bytes": len(html.encode("utf-8", "ignore")),
        "text": html,                       # per-mode probes read fetch['text']: hand them the RENDERED HTML
        "final_url": (r.get("url") or url)[:2048],
        "redirect_chain": [],
        "content_type": "text/html",
        "blocked_reason": None if html else "render_empty",
        "rendered_via": "wall_probe_jail",  # provenance: DERIVED FROM ATTACKER BYTES via a jailed render
    }
