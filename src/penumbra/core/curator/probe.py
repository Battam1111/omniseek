"""Mechanical mode-probes + the SSRF-hardened fetcher for attacker-influenceable candidate URLs.

This is the eye's FIRST fetcher pointed at hosts an adversary chose (a candidate-source URL).
Everything here is MECHANICAL: it fetches / measures / counts / resolves / matches and returns
FACTS. It renders NO verdict. There is no key or string-value naming score/verdict/passes/
recommend/admit/reject/good/quality/rating/confidence/decision/beats_web_search anywhere in a
probe's output: only counts, lists, lengths, dates, booleans-of-fact, each with a provenance
tag. The AGENT reads these facts and judges; the code never does.

``safe_fetch`` is the crux of safety (build + test it FIRST). It OWNS its httpx client
(cookieless, redirect-disabled, trust_env=False) instead of wrapping http._request_capped
(which forces the shared pool + follow_redirects=True + a COMPRESSED-bytes cap: all wrong for
an attacker host). It scheme-allowlists http/https, rejects userinfo + non-80/443 ports on the
FINAL pinned connection, resolves the host and validates EVERY resolved IP, then CONNECTS TO
THE PINNED IP literal (defeating DNS-rebind), walks redirects manually re-validating each hop,
caps on DECODED bytes (defeating a gzip bomb) AND a raw cap, and honors cache.cache_only().
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, urlsplit

import httpx

from penumbra.core import cache, http

logger = logging.getLogger(__name__)

# The §11 frozen acquisition-mode vocabulary (reused; smoke §12 asserts _PROBES keys == this).
MODE_VOCAB = {"STRUCTURE", "UNWALL", "TRANSCRIBE", "RECALL", "MONITOR"}

# ── SSRF guard DATA (declared, not hard-coded in logic) ──────────────────────────
# Host-suffix denylist: literal hostnames that must never be fetched even if they happen to
# resolve to something that looks public (defense in depth alongside the IP checks).
_BLOCKED_HOST_SUFFIXES = (
    "localhost", ".localhost", ".local", ".internal", ".lan",
    "metadata.google.internal",
)
_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = (80, 443)

# This eye runs on a host whose resolver uses FAKE-IP proxying (Clash-style): PUBLIC domains
# resolve into 198.18.0.0/15 (a reserved range is_private flags) which the local proxy maps back
# to the real domain, so CONNECTING to the fake IP routes to the genuine public site. These are
# NOT real internal hosts, so they are ALLOWED. Every other private/loopback/link-local/reserved
# range stays blocked, and internal hosts/IPs are NEVER faked (direct IPs + local domains resolve
# really, so 127/10/169.254/192.168/::1/metadata still hit the block) -> SSRF stays closed.
_PROXY_FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)

# Caps. max_bytes is a DECODED cap (gzip-bomb defense); a separate raw cap refuses a body
# whose COMPRESSED size already exceeds the budget before we ever decode it.
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def _ip_is_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True iff this resolved IP is in a range we must never connect to. Rejects IPv4-mapped
    IPv6 (e.g. ::ffff:169.254.169.254) by unwrapping it first."""
    if getattr(ip, "ipv4_mapped", None) is not None:
        return True  # ::ffff:a.b.c.d: refuse outright (the mapped-address rebind trick)
    if any(ip in net for net in _PROXY_FAKE_IP_NETS):
        return False  # the deployment's fake-IP proxy pool = public domains (see _PROXY_FAKE_IP_NETS)
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _host_suffix_blocked(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return True
    for suf in _BLOCKED_HOST_SUFFIXES:
        if h == suf.lstrip(".") or h.endswith(suf):
            return True
    return False


def _resolve_safe_ip(host: str) -> "tuple[Optional[str], Optional[int], Optional[str]]":
    """Resolve ``host`` and validate EVERY returned IP. Returns (safe_ip_literal, family, None)
    on success, or (None, None, blocked_reason) on any failure / a blocked IP. Resolve-once,
    pin-the-IP: the caller CONNECTS to the returned literal IP (not the hostname), which closes
    the DNS-rebind/TOCTOU window (a 2nd lookup at connect time can't swap in a private IP)."""
    if _host_suffix_blocked(host):
        return None, None, "private_ip"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError):
        return None, None, "dns"
    if not infos:
        return None, None, "dns"
    safe_ip = None
    safe_family = None
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None, None, "private_ip"  # unparseable address -> fail closed
        if _ip_is_blocked(ip):
            return None, None, "private_ip"  # ANY blocked IP in the set aborts (no cherry-pick)
        if safe_ip is None:
            safe_ip = addr
            safe_family = family
    if safe_ip is None:
        return None, None, "dns"
    return safe_ip, safe_family, None


def _validate_url_shape(url: str) -> "tuple[Optional[dict], Optional[str]]":
    """Validate scheme / userinfo / port on a single URL. Returns (parsed_parts, None) or
    (None, blocked_reason). Checked on EVERY hop (input + each redirect target)."""
    try:
        sp = urlsplit(url)
    except ValueError:
        return None, "bad_scheme"
    if sp.scheme.lower() not in _ALLOWED_SCHEMES:
        return None, "bad_scheme"
    if sp.username or sp.password:
        return None, "userinfo"
    host = sp.hostname
    if not host:
        return None, "dns"
    port = sp.port
    if port is None:
        port = 443 if sp.scheme.lower() == "https" else 80
    if port not in _ALLOWED_PORTS:
        return None, "bad_port"
    return {"scheme": sp.scheme.lower(), "host": host, "port": port,
            "path": sp.path, "query": sp.query}, None


def _blocked(reason: str, status: Optional[int] = None, chain: Optional[list] = None) -> dict:
    return {"ok": False, "status": status, "bytes": 0, "text": "", "final_url": "",
            "redirect_chain": chain or [], "content_type": None, "blocked_reason": reason}


def _read_capped(resp: httpx.Response, max_bytes: int) -> "tuple[Optional[bytes], Optional[str]]":
    """Read the body, aborting on EITHER cap: DECODED bytes (iter_bytes handles gzip/deflate/
    br/zstd) > max_bytes (gzip-bomb defense), OR raw compressed bytes > max_bytes (refuse a body
    whose compressed size alone already blows the budget). Returns (decoded_bytes, None) or
    (None, 'oversize')."""
    decoded = bytearray()
    raw_total = 0
    # raw_total tracks compressed wire bytes; if the COMPRESSED stream already exceeds the cap we
    # never finish decoding. httpx exposes the raw stream length via num_bytes_downloaded.
    try:
        for chunk in resp.iter_bytes():
            decoded += chunk
            if len(decoded) > max_bytes:
                return None, "oversize"
            raw_total = resp.num_bytes_downloaded
            if raw_total > max_bytes:
                return None, "oversize"
    except Exception:  # noqa: BLE001: a stream error mid-read is a failed fetch, not a body
        return None, "oversize"
    return bytes(decoded), None


def safe_fetch(url: str, *, method: str = "GET", render: bool = False,
               timeout_total: float = 20.0, max_bytes: int = _DEFAULT_MAX_BYTES,
               max_redirects: int = 5) -> dict:
    """Fetch an ATTACKER-INFLUENCEABLE candidate URL and return FACTS, fail-closed.

    Owns its client (NOT http._request_capped/_get_client). Returns:
        {"ok", "status", "bytes", "text", "final_url", "redirect_chain", "content_type",
         "blocked_reason": one of private_ip|bad_scheme|oversize|timeout|dns|userinfo|
                           bad_port|redirect_loop|cache_only|fetch_error|None}
    A blocked fetch is RECORDED; it never silently judges the candidate. ``render`` is accepted
    for signature parity but P1 NEVER drives a browser here (anonymous stranger only, no CDP).

    SAFE_FETCH BOUNDARY (spec 8c, SSRF pass): safe_fetch hardens the PROBE-TIME fetch ONLY -- the
    one fetch the eye makes at a candidate URL before any verdict. The POST-ADMISSION recurring
    fetch a family adapter makes once a source is live (org_watch / page_watch / news_scraper /
    render) goes through the NORMAL fetcher and is NOT IP-pinned by this guard. That is exactly why
    those families are in apply._NEVER_AUTO_FAMILIES (never auto-applied) and why an admit of one
    must consciously acknowledge the unguarded recurring fetch (server.eye_curator_decide requires
    baseline_ref.recurring_fetch_acknowledged for a first-seen host in that subclass). Routing the
    enrich/OpenAlex resolution hosts through a fixed-API host allowlist is a flagged follow-up
    hardening (the id regexes constrain path injection today), not a P4 blocker.
    """
    # cache.cache_only() (cache_only=True): do ZERO live egress.
    if cache.cache_only():
        return _blocked("cache_only")

    parts, reason = _validate_url_shape(url)
    if reason is not None:
        return _blocked(reason)

    chain: list = []
    current_url = url
    seen: set = set()

    client = httpx.Client(
        follow_redirects=False,           # we walk redirects MANUALLY, re-validating each hop
        cookies={},                       # no cookies, ever
        headers={"User-Agent": http.USER_AGENT},
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        trust_env=False,                  # no env proxies / no Authorization from netrc / etc.
        max_redirects=0,
    )
    try:
        for _hop in range(max_redirects + 1):
            parts, reason = _validate_url_shape(current_url)
            if reason is not None:
                return _blocked(reason, chain=chain)
            host = parts["host"]
            port = parts["port"]
            scheme = parts["scheme"]

            safe_ip, _family, reason = _resolve_safe_ip(host)
            if reason is not None:
                return _blocked(reason, chain=chain)

            # Pin the connection to the validated IP literal; carry the Host header + (for TLS)
            # the SNI hostname so the server still routes correctly. This defeats DNS-rebind:
            # the guard validated THIS ip, and THIS ip is exactly what we connect to.
            if ":" in safe_ip:  # IPv6 literal needs brackets in a URL authority
                authority = f"[{safe_ip}]:{port}"
            else:
                authority = f"{safe_ip}:{port}"
            pinned_url = f"{scheme}://{authority}{parts['path'] or '/'}"
            if parts["query"]:
                pinned_url += "?" + parts["query"]

            req_headers = {"Host": host}
            extensions = {}
            if scheme == "https":
                # sni_hostname makes the TLS handshake present the real host while we connect to
                # the pinned IP: required for SNI-based virtual hosts + cert validation.
                extensions = {"sni_hostname": host}

            try:
                with client.stream(method, pinned_url, headers=req_headers,
                                   extensions=extensions, timeout=timeout_total) as resp:
                    status = resp.status_code
                    # Redirect? re-validate the Location target as a brand-new untrusted URL.
                    if status in (301, 302, 303, 307, 308) and "location" in resp.headers:
                        loc = resp.headers["location"]
                        nxt = urljoin(current_url, loc)  # resolve relative against the CURRENT url
                        if nxt in seen:
                            return _blocked("redirect_loop", status=status, chain=chain)
                        seen.add(nxt)
                        chain.append({"from": current_url, "to": nxt, "status": status})
                        current_url = nxt
                        continue  # next hop re-runs the FULL guard at the top of the loop
                    # Terminal response: read the body under both caps.
                    body, oversize = _read_capped(resp, max_bytes)
                    if oversize is not None:
                        return _blocked(oversize, status=status, chain=chain)
                    ctype = resp.headers.get("content-type")
                    text = ""
                    if method.upper() != "HEAD" and body:
                        try:
                            enc = resp.encoding or "utf-8"
                            text = body.decode(enc, errors="replace")
                        except (LookupError, ValueError):
                            text = body.decode("utf-8", errors="replace")
                    return {
                        "ok": True,
                        "status": status,
                        "bytes": len(body or b""),
                        "text": text[:max_bytes],         # length-bounded attacker string
                        "final_url": current_url[:2048],
                        "redirect_chain": chain,
                        "content_type": (ctype[:200] if ctype else None),
                        "blocked_reason": None,
                    }
            except httpx.TimeoutException:
                return _blocked("timeout", chain=chain)
            except httpx.HTTPError:
                return _blocked("fetch_error", chain=chain)
        # Exhausted max_redirects without a terminal response.
        return _blocked("redirect_loop", chain=chain)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────────
# Mode probes: each FETCHES candidate URLs via safe_fetch and returns provenance-tagged
# FACTS only. Provenance tags: 'verified' (eye independently confirmed), 'claimed' (parsed
# straight from publisher bytes), 'derived' (computed by the eye over fetched content).
# ─────────────────────────────────────────────────────────────────────────────────

_PASSWORD_INPUT_RE = re.compile(r'<input[^>]*type=["\']?password', re.IGNORECASE)
_NEXTDATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
# Reuse the search_index tombstone phrases (the PHRASES, not the _is_tombstone boolean verdict).
try:
    from penumbra.core.sources.api.search_index_source import _TOMBSTONE_RE as _SI_TOMBSTONE_RE
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
            from penumbra.core import enrich
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
            from penumbra.core import _openalex as oa
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
        from penumbra.core import recall
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


def mode_probe(candidate: dict, *, deadline_s: float = 25.0) -> dict:
    """Dispatch on candidate['mode'] -> the per-mode probe over a safe_fetch of the candidate's
    first URL. Returns provenance-tagged FACTS + reachability. NO verdict key/value. Fail-closed:
    an unfetchable list / unknown mode / probe exception -> probe_reached=False + probe_error,
    never an admit-shaped result."""
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
