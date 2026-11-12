"""Upstream declarations: what each upstream allows, what OmniSeek enforces, what it last observed.

Before this module every upstream's terms lived in the head of whoever wrote the adapter: arXiv ran
9x over its published rate for weeks (the 0.35 s pacer, 2026-08-19), then 4 requests in flight
against "a single connection at a time"; the OpenAlex comments kept a two-bucket "~2x capacity"
belief after the keyless budget became a tenth of the keyed one. The cure is one place per upstream
where the terms are DATA, the shared limiter READS them, and the health check COMPARES them with
what the upstream actually reports:

  upstreams.json      one entry per independent upstream (an operator's API or site, not a source
                      name): the published terms with their source URL and check date ("未公布"
                      where nothing is published, plus where we looked), OmniSeek's gate, the hosts
                      and the sources that use it.
  hosts               exact host names, or a domain suffix written with a leading dot: ".wikipedia.org"
                      matches every subdomain ("zh.wikipedia.org"), never the bare domain (listed on
                      its own) and never a look-alike ("notwikipedia.org"). For one host a gated
                      entry wins, then an exact entry, then the longest suffix (design decision,
                      2026-09-29: the whole Wikimedia family is one entry).
  paths               an entry may also list path prefixes ("paths": ["/pdf/", "/html/"]): it then owns
                      only the URLs on its hosts whose path starts with one of them, and wins those over
                      the host's other entries (2026-10-24: export.arxiv.org serves the API
                      under /api/ with one request every 3 s, and full text under /pdf/ and /html/,
                      which arXiv sets aside for programs with its own rate). A bare host (no path) and
                      every other path stay with the host's entry. Host-wide things (a robots.txt
                      Crawl-delay, a declared User-Agent, the one-connection transport) cannot be
                      scoped to a path, so a path entry may not declare them.
  guard(uid)          THE shared _guard.BackendGuard for that upstream, built from its gate. Every
                      egress path to the upstream (the owning module, the shared http client's host
                      gate, a health probe) takes the same object, so the limit holds across sources,
                      tools, threads and event loops.
  aegress(url)        the host gate for any direct egress: a no-op for an ungated host or when the
                      current holder already holds that upstream (the owning module's own hold).
                      The sync ``egress`` is a thin wrapper over the same async gates,
                      for egress that blocks in a thread OmniSeek cannot hand to the event loop
                      mid-request: xianyu's CDP read (entered on its calling thread, around the whole
                      browser call) and the system push outlet's sync ``send`` (notify). It holds the
                      gates by a credential of its own (_guard.Holder), entered and let go through two
                      portal calls. A CDP render's page load takes its host's gates with
                      ``abrowser_turn``, on the loop (_cdp.acdp_call), once it has the browser's turn.
  budget              a full gate is a line: a request's permit wait and start wait, over every gate
                      it passes, run until the caller's own deadline and only then is the request
                      turned away unsent (2026-11-11: the shorter declared
                      max_wait_s was removed). A caller with no deadline waits until served. See
                      _guard.deadline_refusal.
  redirects           AsyncHopGates holds the gates of the host a request is on: a redirect to the same
                      host continues the same visit; a redirect to another host lets go of the first
                      host's gates, then takes the new host's within the time left; never two hosts'
                      gates at once (design decision 1). Every redirect-following path uses it.
  host gates          a host whose robots.txt sets a Crawl-delay (terms.robots_crawl_delay_s) gets
                      its own gate, one request at a time and starts at least that delay apart.
                      egress(url) takes it after the upstream's gate, so the stricter of the two
                      always holds (design decision, 2026-09-29).
  single connection   a gated upstream whose terms publish max_concurrency 1 (arXiv: "a single
                      connection at a time") is listed by single_connection_hosts(); the shared http
                      client gives those hosts a no-keep-alive HTTP/1.1 transport and send
                      "Connection: close", so no idle connection outlives a request.
  user agent          an upstream may declare the User-Agent it requires (Wikimedia's policy): every
                      shared egress layer sends that string to the upstream's hosts and nothing else
                      does (user_agent_for / with_declared_user_agent). The shared browser (CDP)
                      cannot send it, so egress(url, browser=True) refuses such a host.
  disabled            an upstream OmniSeek must not send to, with the reason and its source
                      (disabled_reason): the code that would send reads it, sends nothing and
                      reports the reason. The web-search backend reads it for its keyless fallback.
  observe(...)        records rate-limit headers / quota fields as the upstream reports them; any
                      response carrying Retry-After defers the whole upstream for every caller, unless
                      the calling module is in SELF_BACKOFF (design decision 3). Any answer also tells
                      the host's liveness record that the host is reachable.
  host liveness       HOST_DOWN_AFTER connections in a row that were not made (refused, handshake
                      timeout, EOF) open the host's breaker for HOST_DOWN_FOR_S: requests to it fail at
                      once with HostDown (an UpstreamBusy, nothing sent, no permit taken), and the health
                      check reads that as down (2026-10-20). A run of failures opens it
                      only when some host answered after the run began: when nothing answers at all,
                      this machine's network is down, not the host. Every refusal names
                      its URL (credentials masked) and the initiator (_guard.initiator).
  health_block(...)   declared vs gate vs observed, mismatches flagged, undeclared sources listed.

Judgment-free plumbing: it stores the declaration, enforces the numbers it is given and reports
differences. Deciding what an upstream's terms ARE is the job of whoever edits upstreams.json.
"""

from __future__ import annotations

import contextlib
import email.utils
import itertools
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from omniseek.core import _guard, _probe, portal
from omniseek.core._guard import BackendGuard, deadline_after, deadline_until  # noqa: F401

logger = logging.getLogger(__name__)

DECL_PATH = Path(__file__).with_name("upstreams.json")
UNPUBLISHED = "未公布"
# A declaration older than this is flagged for re-checking (the terms of several upstreams changed
# within months in 2025-2026: OpenAlex, Crossref, Wikimedia, Brave, Context7).
RECHECK_AFTER_DAYS = 90
# A Retry-After longer than this still sheds callers (they fail fast), it just is not waited out.
_DEFER_CAP_S = 600.0

_TERM_NUMERIC = ("max_concurrency", "min_interval_s")
_RATE_HEADER = re.compile(r"^(x-)?(rate-?limit|ratelimit)|^x-concurrency-limit$|^x-api-pool$"
                          r"|^retry-after$|^quota_(max|remaining)$", re.I)


class UpstreamBusy(RuntimeError):
    """A declared upstream's gate could not start the request before the caller's deadline: the
    request was NOT sent. Callers map it to their failure contract (None / [] / degrade)."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        _probe.note_held(str(self))   # a running health check records that nothing was sent


class HostDown(UpstreamBusy):
    """The host's breaker is open (``HOST_DOWN_AFTER`` connections in a row were not made): the request
    was NOT sent and took no permit. An ``UpstreamBusy`` so every caller's existing failure mapping
    holds, but recorded as evidence the host is down, not as a gate holding a request back: a health
    check that meets it reads False, and the search _meta names the reason 主机断路."""

    def __init__(self, *args) -> None:
        RuntimeError.__init__(self, *args)
        _probe.note_host_down(str(self))


class DeclaredUserAgentRefused(RuntimeError):
    """The host's upstream declares a User-Agent and the caller is the shared browser, which sends its
    own: the page was NOT rendered (sending a browser's User-Agent there would break the upstream's
    User-Agent policy). Callers map it to their failure contract."""


class DeclarationUnreadable(RuntimeError):
    """upstreams.json is missing or unreadable. OmniSeek does not run without it (design decision 4,
    2026-09-29): no built-in defaults, the server refuses to start and names the file and the reason."""


# Both mean the request inside a hold was never sent, so its reserved start is handed back.
_guard.register_unsent(UpstreamBusy, DeclaredUserAgentRefused)

# Modules that honour an upstream's Retry-After themselves, and therefore record responses with
# defer_on_429=False (design decision 3, 2026-09-29). Everywhere else the default holds: a response that
# carries Retry-After defers the whole upstream for every caller. The exemption covers the upstream
# gate only; the host's robots.txt Crawl-delay gate is deferred regardless (see observe).
# Only the modules listed here may switch the deferral off (the test that enforced it was deleted).
SELF_BACKOFF = {
    "omniseek.core._openalex": (
        "per-lane budgets: a budget 429 marks that lane dry until the reset OpenAlex reports and the "
        "call spills to the other lane; deferring the shared gate would also stop the lane that still "
        "has budget"),
    "omniseek.core._github": (
        "the module's retry loop reads Retry-After and X-RateLimit-Reset (primary and secondary "
        "limits) and opens its own breaker for the reported time"),
    "omniseek.core.sources.api._search_backend": (
        "Brave's 429 opens the backend's own cooldown from Retry-After, and the backend paces with its "
        "own pacer (gate.http_gate false), so the registry guard is not the limiter there"),
}


_lock = threading.Lock()
_decl: Optional[dict] = None
_load_error: Optional[str] = None
_host_index: dict[str, str] = {}     # exact host -> owning upstream id
_suffix_index: dict[str, str] = {}   # ".domain" suffix -> owning upstream id
_host_delay: dict[str, float] = {}   # host -> robots.txt Crawl-delay seconds (max over declarations)
_host_ua: dict[str, str] = {}        # exact host -> the User-Agent a declaration listing it requires
_suffix_ua: dict[str, str] = {}      # ".domain" suffix -> the User-Agent its declaration requires
_path_index: dict[str, list] = {}    # exact host -> [(path prefix, upstream id)], longest prefix first
_HOST_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_SUFFIX_RE = re.compile(r"\.[a-z0-9-]+(\.[a-z0-9-]+)+")   # at least two labels: never a bare ".org"
_optional: set = set()
_guards: dict[str, BackendGuard] = {}
_host_guards: dict[str, BackendGuard] = {}
_readings: dict[str, dict] = {}


# ── loading ────────────────────────────────────────────────────────────────────────────────────
def _load() -> dict:
    global _decl, _load_error, _host_index, _suffix_index, _host_delay, _host_ua, _suffix_ua, _optional
    global _path_index
    if _decl is not None:
        return _decl
    with _lock:
        if _decl is not None:
            return _decl
        try:
            raw = json.loads(DECL_PATH.read_text(encoding="utf-8"))
            ups = raw.get("upstreams") or {}
            if not isinstance(ups, dict):
                raise ValueError("'upstreams' must be an object keyed by upstream id")
            _optional = set((raw.get("optional_sources") or {}).get("names") or ())
        except Exception as exc:  # noqa: BLE001 (re-raised as DeclarationUnreadable just below)
            # No built-in defaults (design decision 4): a silently empty declaration dropped every
            # source that builds its gate at import and left the shared client ungated.
            _load_error = f"{DECL_PATH}: {type(exc).__name__}: {exc}"
            logger.error("upstreams.json unreadable: %s", _load_error)
            raise DeclarationUnreadable(
                f"upstream declarations unreadable, OmniSeek will not run without them: {_load_error}"
            ) from exc
        index: dict[str, str] = {}
        sfx_index: dict[str, str] = {}
        path_index: dict[str, list] = {}
        # Gated entries win a shared host (the gate is what must see the request); otherwise the
        # first declaration of a host is its owner for header readings. A host entry starting with
        # "." is a domain suffix (see uid_for_host for how the two kinds meet). An entry with "paths"
        # owns only those path prefixes of its hosts, so it stays out of the host-wide indexes.
        for uid, e in sorted(ups.items(), key=lambda kv: (not kv[1].get("gate"), kv[0])):
            prefixes = [str(p) for p in (e.get("paths") or ()) if str(p).startswith("/")]
            for h in e.get("hosts") or ():
                h = str(h).lower()
                if prefixes:
                    path_index.setdefault(h, []).extend((p, uid) for p in prefixes)
                    continue
                (sfx_index if h.startswith(".") else index).setdefault(h, uid)
        for h in path_index:
            path_index[h].sort(key=lambda pu: -len(pu[0]))
        delays: dict[str, float] = {}
        for e in ups.values():
            rcd = (e.get("terms") or {}).get("robots_crawl_delay_s")
            for h, secs in (rcd.items() if isinstance(rcd, dict) else ()):
                if _is_num(secs) and secs > 0:
                    delays[str(h).lower()] = max(float(secs), delays.get(str(h).lower(), 0.0))
        # A declared User-Agent belongs to every host its entry lists, whichever entry owns the host
        # in the index (a test keeps two declarations from giving one host different strings).
        uas: dict[str, str] = {}
        sfx_uas: dict[str, str] = {}
        for uid, e in sorted(ups.items()):
            ua = e.get("user_agent")
            if isinstance(ua, str) and ua.strip():
                for h in e.get("hosts") or ():
                    h = str(h).lower()
                    (sfx_uas if h.startswith(".") else uas).setdefault(h, ua)
        _host_index = index
        _suffix_index = sfx_index
        _path_index = path_index
        _host_delay = delays
        _host_ua = uas
        _suffix_ua = sfx_uas
        _decl = ups
        return _decl


def declarations() -> dict:
    """{uid: entry} as declared (read-only use)."""
    return _load()


def entry(uid: str) -> dict:
    d = _load()
    if uid not in d:
        raise KeyError(f"upstream {uid!r} is not declared in {DECL_PATH.name}")
    return d[uid]


def load_error() -> Optional[str]:
    """None when upstreams.json loaded; else the path and the reason (never raises)."""
    try:
        _load()
    except DeclarationUnreadable:
        pass
    return _load_error


def _host(target: str) -> str:
    # lower-cased, trailing dot dropped: "en.wikipedia.org." is the same host (and must get the
    # same gates and the same declared User-Agent)
    try:
        h = (urlsplit(target).hostname or "") if "://" in target else target
        return h.lower().rstrip(".")
    except Exception:  # noqa: BLE001
        return ""


def _suffixes(host: str) -> list[str]:
    """The domain suffixes of ``host`` a leading-dot host entry can match, longest first:
    "a.b.wikipedia.org" -> [".b.wikipedia.org", ".wikipedia.org"]; never a bare ".org"."""
    labels = host.split(".")
    return ["." + ".".join(labels[i:]) for i in range(1, len(labels) - 1)]


def _path_of(target: str) -> Optional[str]:
    """The path of ``target`` when it is a URL ("/" for an empty one), else None (a bare host)."""
    if "://" not in target:
        return None
    try:
        return urlsplit(target).path or "/"
    except Exception:  # noqa: BLE001
        return None


def uid_for_host(target: str) -> Optional[str]:
    """The upstream id owning ``target`` (a URL or a bare host), or None if undeclared. A URL whose
    path starts with a prefix an entry declares for its host ("paths") belongs to that entry, the
    longest prefix first. Otherwise candidates are the entry listing the exact host and every entry
    whose domain suffix matches it; a gated one wins (the gate must see the request), then the exact
    one, then the longest suffix."""
    d = _load()
    h = _host(target)
    if not h:
        return None
    path = _path_of(target)
    if path is not None:
        for prefix, uid in _path_index.get(h, ()):
            if path.startswith(prefix):
                return uid
    cands = [(0, 0, _host_index[h])] if h in _host_index else []
    cands += [(1, -len(sfx), _suffix_index[sfx]) for sfx in _suffixes(h) if sfx in _suffix_index]
    if not cands:
        return None
    cands.sort(key=lambda c: (not (d.get(c[2]) or {}).get("gate"), c[0], c[1]))
    return cands[0][2]


def crawl_delay_for(target: str) -> Optional[float]:
    """The robots.txt Crawl-delay declared for ``target``'s host (a URL or a bare host), or None."""
    _load()
    return _host_delay.get(_host(target))


def cache_ttl_for(ttl: float, urls: Iterable[str]) -> float:
    """A cache time for a fetch of ``urls`` that is never shorter than the longest Crawl-delay among
    their hosts: a cache that runs out before the host may be asked again only buys
    a refused request (statmodeling: Crawl-delay 3600 s against a 1800 s feed cache, 68 of 85 refused)."""
    delays = [d for d in (crawl_delay_for(str(u)) for u in urls) if d]
    return max([ttl, *delays]) if delays else ttl


def single_connection_hosts() -> list[str]:
    """Hosts of gated upstreams whose published terms allow ONE connection at a time
    (terms.max_concurrency == 1). The shared http clients mount a no-keep-alive transport for them
    and send ``Connection: close``."""
    out = []
    for e in _load().values():
        if e.get("gate") and (e.get("terms") or {}).get("max_concurrency") == 1:
            # exact hosts only: the transport is mounted per host (schema_problems refuses a suffix)
            out.extend(str(h).lower() for h in e.get("hosts") or () if not str(h).startswith("."))
    return sorted(set(out))


def is_single_connection_host(target: str) -> bool:
    return _host(target) in single_connection_hosts()


def user_agent_for(target: str) -> Optional[str]:
    """The User-Agent (``user_agent``) declared for ``target``'s host (a URL or a bare host): an exact
    host entry first, else the longest matching domain suffix; None when no declaration gives one."""
    _load()
    h = _host(target)
    if not h:
        return None
    if h in _host_ua:
        return _host_ua[h]
    return next((_suffix_ua[sfx] for sfx in _suffixes(h) if sfx in _suffix_ua), None)


def declared_user_agents() -> set:
    """Every declared User-Agent string (to recognise one that followed a redirect off its host)."""
    _load()
    return set(_host_ua.values()) | set(_suffix_ua.values())


def disabled_reason(uid: str) -> Optional[str]:
    """Why OmniSeek must not send to upstream ``uid`` (its declaration's ``disabled.reason``), or None
    when it is in service. The reason and its source live in the declaration; the code that would
    send reads this, sends nothing and reports the reason."""
    dis = (_load().get(uid) or {}).get("disabled")
    return (str(dis.get("reason") or "").strip() or None) if isinstance(dis, dict) else None


def with_declared_user_agent(target: str, headers: Optional[dict]) -> dict:
    """``headers`` with the User-Agent replaced by the one declared for ``target``'s host; unchanged
    (a copy) for a host with none. Case-insensitive on the header name."""
    out = dict(headers or {})
    ua = user_agent_for(target)
    if ua:
        for k in [k for k in out if str(k).lower() == "user-agent"]:
            del out[k]
        out["User-Agent"] = ua
    return out


def gated_uid_for_url(url: str) -> Optional[str]:
    """The upstream whose declared gate the shared egress paths must take for ``url``, or None.
    ``gate.http_gate: false`` marks an upstream enforced by its module's own limiter (the web-search
    backend's pacers); a second, separate limiter for the same upstream would not share its state."""
    uid = uid_for_host(url)
    if not uid:
        return None
    g = _load()[uid].get("gate")
    return uid if g and g.get("http_gate", True) else None


# ── the shared guard per upstream ──────────────────────────────────────────────────────────────
def gate_value(uid: str, key: str, default: Any = None) -> Any:
    """One number from an upstream's gate (for a module whose own limiter reads the declaration)."""
    return (entry(uid).get("gate") or {}).get(key, default)


def guard(uid: str, *, extra_state: Optional[dict] = None,
          log: Optional[logging.Logger] = None) -> BackendGuard:
    """THE BackendGuard for ``uid``, built once from its declared gate. Every caller gets the same
    object. ``extra_state`` keys are added if missing (a module's breaker-adjacent fields), ``log``
    sets the breaker-open logger; both are safe whichever caller builds the guard first."""
    g = _guards.get(uid)
    if g is None:
        gate = entry(uid).get("gate")
        if not gate:
            raise KeyError(f"upstream {uid!r} declares no gate")
        with _lock:
            g = _guards.get(uid)
            if g is None:
                g = BackendGuard(
                    uid, int(gate["max_inflight"]),
                    break_after=int(gate.get("break_after", 5)),
                    break_for_s=float(gate.get("break_for_s", 120.0)),
                    min_interval_s=float(gate.get("min_interval_s", 0.0)),
                    windows=[tuple(w) for w in gate.get("windows") or ()],
                )
                _guards[uid] = g
    if extra_state:
        with g.lock:
            for k, v in extra_state.items():
                g.state.setdefault(k, v)
    if log is not None:
        g._log = log
    return g


def host_guard(host: str) -> BackendGuard:
    """THE gate for a host whose robots.txt sets a Crawl-delay: one request at a time, request
    starts at least the delay apart. Named by the host itself (upstream ids never contain a dot,
    see schema_problems), shared by every declaration that lists the host."""
    host = host.lower()
    g = _host_guards.get(host)
    if g is None:
        delay = crawl_delay_for(host)
        if not delay:
            raise KeyError(f"host {host!r} declares no robots.txt Crawl-delay")
        with _lock:
            g = _host_guards.get(host)
            if g is None:
                g = BackendGuard(host, 1, min_interval_s=float(delay))
                _host_guards[host] = g
    return g


def _gates_for_url(url: str) -> list:
    """The gates one request to ``url`` must pass, in the fixed order (upstream gate, then the
    host's Crawl-delay gate): [(key, guard)]. Taking both is the stricter of the two."""
    out = []
    uid = gated_uid_for_url(url)
    if uid is not None:
        out.append((uid, guard(uid)))
    host = _host(url)
    if crawl_delay_for(host):
        out.append((host, host_guard(host)))
    return out


def _refusal(key: str, url: Optional[str] = None) -> Callable[[float], UpstreamBusy]:
    """The one way a gate turns a request away, raised BEFORE anything is sent: the caller's deadline
    came before the request could start (``_guard.deadline_refusal``). The message names the gate, how
    long the request waited at it, the request's URL (credentials masked) and who asked,
    so every log line that prints the refusal can be counted by URL and initiator."""
    tail = _refusal_tail(url)

    def on_refuse(waited: float) -> UpstreamBusy:
        return UpstreamBusy(_guard.deadline_refusal(key, waited, tail))
    return on_refuse


def loggable_url(url: str) -> str:
    """``url`` with the value of every credential-shaped query parameter masked (omniseek.redact)."""
    try:
        from omniseek import redact as _redact
        return str(_redact.redact(str(url)))[:300]
    except Exception:  # noqa: BLE001 (masking must never break a refusal)
        return urlsplit(str(url))._replace(query="", fragment="").geturl()[:300]


def _refusal_tail(url: Optional[str]) -> str:
    who = _guard.current_initiator() or "?"
    return f" [url={loggable_url(url)} by={who}]" if url else f" [by={who}]"


# ── host liveness: a host that stopped answering opens its breaker ─────────────────
# lobste.rs stopped completing TLS handshakes on 2026-10-18; for two days every request to it held the
# host's one permit for the full 15 s timeout, the other two feed requests of the same bundle waited
# 15 s and were refused, and the health check re-read that as "not verified". The ONE record of
# whether a host answers lives here, per host (every host, gated or not), and is read by every egress
# path that passes the AsyncHopGates (the shared http helpers, http.direct, the curl tier, CDP renders) and,
# through the HostDown it raises, by the health check and the search _meta.
#
# PROVISIONAL (both numbers, 2026-10-20): HOST_DOWN_AFTER unreachable answers in a
# row open the breaker; it stays open HOST_DOWN_FOR_S seconds, then the count starts again from zero.
# Calibrate from the first week of logs: count the "host breaker OPEN" lines, and for each open window
# check whether another path (a browser render, the curl tier, a later request) reached the host while
# it was open (a breaker that opens on a host that did answer means the count is too low or the window
# too long); a host that keeps reopening every window is the case the window is for.
HOST_DOWN_AFTER = 3
HOST_DOWN_FOR_S = 600

_host_live: dict[str, dict] = {}   # host -> {"fails", "since", "open_until" (wall clock), "last"}
_host_live_lock = threading.Lock()

# The last HTTP status each host answered and when (wall clock), whoever asked (observe records it): what
# a check whose own request that host's gate held back can still say about the host. A feed host with a
# one-hour Crawl-delay is asked by the hourly ingest and almost never by the health check, so without
# this the check could only read it as dead without an address, or never see its 403.
_last_answer: dict[str, tuple[int, float]] = {}


def last_answer(host: str) -> Optional[tuple[int, float]]:
    """``(HTTP status, wall-clock time)`` of the last answer ``host`` gave this process, or None."""
    return _last_answer.get(_host(host)) if host else None

# A run of failures says the HOST is down only if the network worked meanwhile (
# 2026-10-20): when the host machine's own egress drops (it has flapped, brain
# finding-egress-flap-investigation-2026-10-19) every host asked in that minute fails at once, and
# without this each would sit behind a 600 s breaker after the network came back. ``_answered`` is the
# number of answers from any host so far; a run remembers it in "since" and opens the breaker only
# when it has grown. A count, not a clock, so two calls in one clock tick still order.
_answers = itertools.count(1)
_answered = 0

_UNREACHABLE_TYPES = ("ConnectTimeout", "ConnectError", "ConnectionRefusedError")
_UNREACHABLE_MARKERS = (
    "UNEXPECTED_EOF_WHILE_READING", "EOF occurred in violation of protocol",
    "Server disconnected without sending a response",
    "net::ERR_CONNECTION_REFUSED", "net::ERR_CONNECTION_TIMED_OUT", "net::ERR_CONNECTION_CLOSED",
    "net::ERR_EMPTY_RESPONSE",
    "curl: (7)", "curl: (35)", "curl: (52)", "Connection refused", "Connection timed out",
)


def is_unreachable(exc: BaseException) -> bool:
    """True when ``exc`` says no connection was made: refused, the TCP or TLS handshake timed out, or
    the host closed before any answer (EOF). A read timeout, an HTTP error status, an SSRF refusal or a
    gate refusal is not this."""
    if isinstance(exc, UpstreamBusy):
        return False
    text = str(exc)
    if text.startswith("refused SSRF-class url"):
        return False
    if any(t.__name__ in _UNREACHABLE_TYPES for t in type(exc).__mro__):
        return True
    return any(m in text for m in _UNREACHABLE_MARKERS)


def note_host_failure(host: str, exc: BaseException) -> None:
    """One connection to ``host`` was not made (see ``is_unreachable``). Opens the host's breaker at
    the ``HOST_DOWN_AFTER``-th in a row, if some host answered after the run began. Never raises."""
    try:
        h = _host(host)
        if not h:
            return
        _probe.note_unreachable(h, f"{type(exc).__name__}: {exc}")
        opened = None
        with _host_live_lock:
            rec = _host_live.setdefault(h, {"fails": 0, "since": 0, "open_until": 0.0, "last": ""})
            if rec["fails"] == 0:
                rec["since"] = _answered
            rec["fails"] += 1
            rec["last"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            if rec["fails"] >= HOST_DOWN_AFTER and _answered > rec["since"]:
                rec["open_until"] = time.time() + float(HOST_DOWN_FOR_S)
                opened, rec["fails"] = rec["fails"], 0
        if opened:
            logger.warning("host breaker OPEN: %s did not answer %d connections in a row (last: %s); "
                           "requests to it fail at once until %s", h, opened, rec["last"],
                           _clock(rec["open_until"]))
    except Exception as e:  # noqa: BLE001 (bookkeeping must never break an egress)
        logger.debug("note_host_failure failed: %s", e)


def note_host_reached(host: str) -> None:
    """``host`` answered (any status): its count of unreachable connections starts again, and the
    network is known to work (a run of failures elsewhere that began before now may open a breaker)."""
    global _answered
    _answered = next(_answers)
    h = _host(host) if host else ""
    if h and h in _host_live:
        with _host_live_lock:
            rec = _host_live.get(h)
            if rec is not None:
                rec["fails"] = 0
                rec["open_until"] = 0.0


def note_host_absent(host: str) -> None:
    """Public DNS says ``host`` does not exist (eye.dns_absence): its unreachable connections were an
    answer, not a host that is down, so they are dropped and never open its breaker."""
    h = _host(host) if host else ""
    if h and h in _host_live:
        with _host_live_lock:
            _host_live.pop(h, None)


def host_down_until(host: str) -> Optional[float]:
    """The wall-clock time ``host``'s breaker stays open until, or None when it is closed."""
    rec = _host_live.get(_host(host))
    if rec and rec["open_until"] > time.time():
        return rec["open_until"]
    return None


def host_breakers() -> dict:
    """{host: {"open_until", "last"}} for every open breaker (the health block shows it)."""
    now = time.time()
    with _host_live_lock:
        return {h: {"open_until": _clock(r["open_until"]), "last": r["last"]}
                for h, r in _host_live.items() if r["open_until"] > now}


def _clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def _check_host(url: str) -> None:
    """Raise ``HostDown`` (nothing sent, no permit taken) when ``url``'s host has an open breaker."""
    h = _host(url)
    until = host_down_until(h) if h else None
    if until is not None:
        rec = _host_live.get(h) or {}
        raise HostDown(f"{h}: 主机连不上，断路到 {_clock(until)} (last: {rec.get('last', '')}); "
                       f"not sent{_refusal_tail(url)}")


@contextlib.asynccontextmanager
async def ahold(uid: Optional[str], *, request_s: Optional[float] = None):
    """Hold upstream ``uid``'s declared gate around ONE request: a permit, then the start slot, waited
    for until the caller's deadline (none: until served). For egress that is
    not addressed by URL (a client library such as atproto's ``AsyncClient``, awaited inside this
    block). Raises ``UpstreamBusy`` without sending when the gate cannot admit the
    request in time; ``uid=None`` is a no-op. ``request_s``: the request's own timeout, for the
    permit's lease (see ``BackendGuard.ahold``)."""
    if uid is None:
        yield None
        return
    async with guard(uid).ahold(_refusal(uid), request_s=request_s):
        yield uid


def budget_until(url: str, default: Optional[float] = None) -> Optional[float]:
    """The monotonic moment one request to ``url`` must stop waiting by: the caller's deadline, else
    ``default`` seconds from now (the path's own timeout), else no bound. For a path that waits
    somewhere else first (a render's Chrome turn), so that wait and the gate wait share one budget."""
    d = _guard.current_deadline()
    if d is not None or default is None:
        return d
    return time.monotonic() + max(0.0, float(default))


def check_browser(url: str) -> None:
    """Raise ``DeclaredUserAgentRefused`` when ``url``'s host declares a User-Agent: the shared browser
    sends its own, so it must not load that host (checked before queueing for the browser)."""
    if user_agent_for(url):
        raise DeclaredUserAgentRefused(
            f"{_host(url)}: its upstream declares a User-Agent the shared browser cannot send; "
            "not rendered")


class AsyncHopGates:
    """The declared gates of the host one request is on right now, carried across its redirects: the
    ONE place the redirect rule lives (design decision 1, 2026-09-29).

    ``enter(url)`` before every hop. A hop to the SAME host continues the same visit: no second
    permit, no second start slot (a same-host 301 used to wait a whole Crawl-delay, or be refused
    outright past 15 s). A hop to ANOTHER host first lets go of the current host's gates,
    then takes the new host's gates within the time left (a redirect used to land on a
    gated host without its gate). Two hosts' gates are never held at once, so two redirect chains can
    never wait on each other.

    ONE bound for the whole chain (a chain is one read; design decisions of 2026-09-29 on section 16.5,
    item 1, and on the objection in 16.8): the caller's deadline is the chain's absolute end, and the
    gates of every hop (the upstream's, then the host's Crawl-delay gate, in that fixed order) wait
    in line until it (no shorter per-gate ceiling); without a deadline a hop waits
    until served. One AsyncHopGates per request. ``request_s`` is the request's own timeout: each
    permit's lease ends that long after the chain's deadline (with none, after its hop's reserved
    start; None: no lease, the permit is held until given back). ``enter(url, wait=False)``
    tries each gate once without waiting. ``close()`` lets go; ``close(unsent=True)``
    also hands back the reserved start, for a request that was never sent, unless a
    response already arrived for it (see ``_guard.mark_sent``).

    ``holder``: the credential every gate of the chain is held by (``_guard.Holder``);
    None, the task that enters each hop. ``egress`` hands one, so the chain may be entered and let go
    of in different tasks. ``handles`` are the holds of the current hop (``egress`` registers them in
    its thread's context, ``_guard.holding``)."""

    def __init__(self, request_s: Optional[float] = None, check_host: bool = True,
                 holder: Optional[_guard.Holder] = None) -> None:
        self._host: Optional[str] = None
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self.key: Optional[str] = None
        self.handles: tuple = ()
        self._request_s = request_s
        self._check_host = check_host   # False only for the curl tier's retry of a request that just failed
        self._holder = holder
        self._deadline: Optional[float] = _guard.current_deadline()   # the chain's absolute end

    def _hop_end(self, until: Optional[float]) -> Optional[float]:
        """The earliest of the chain's deadline, the caller's deadline in force now and ``until``;
        None when there is none (wait until served)."""
        ends = [t for t in (self._deadline, _guard.current_deadline(), until) if t is not None]
        return min(ends) if ends else None

    async def enter(self, url: str, *, until: Optional[float] = None, wait: bool = True) -> None:
        host = _host(url)
        if host == self._host:
            return
        await self.close()
        if self._check_host:
            _check_host(url)   # a host whose breaker is open: refused here, before any gate or permit
        gates = _gates_for_url(url)
        if gates:
            end = self._hop_end(until)
            stack = contextlib.AsyncExitStack()
            handles = []
            try:
                for key, g in gates:
                    handles.append(await stack.enter_async_context(
                        g.ahold(_refusal(key, url), until=end, request_s=self._request_s, wait=wait,
                                holder=self._holder)))
            except BaseException as exc:
                await stack.__aexit__(type(exc), exc, exc.__traceback__)  # the gates taken see why (refund)
                raise
            self._stack, self.key, self.handles = stack, gates[0][0], tuple(handles)
        self._host = host

    def _detach(self):
        host, stack = self._host, self._stack
        self._stack, self._host, self.key, self.handles = None, None, None, ()
        return host, stack

    async def close(self, unsent: bool = False) -> None:
        _host, stack = self._detach()
        if stack is None:
            return
        if unsent:
            exc = _guard.RequestNotSent()
            await stack.__aexit__(type(exc), exc, None)
        else:
            await stack.__aexit__(None, None, None)

    async def _abort(self, exc: BaseException, *, count: bool = True) -> Optional[str]:
        host, stack = self._detach()
        unreached = host if host and is_unreachable(exc) else None
        if unreached and count:
            note_host_failure(unreached, exc)   # the hop's connection was not made (see host liveness)
        if stack is not None:
            await stack.__aexit__(type(exc), exc, exc.__traceback__)
        return unreached

    async def let_go(self, exc: BaseException) -> Optional[str]:
        """Let go of this hop's gates after ``exc`` ended it, exactly as a failure leaving the block
        does (a request that never left hands its start back), but WITHOUT counting it against the
        host: the caller is about to try the same request another way (the libcurl tier). Returns the
        host when ``exc`` says its connection was not made, for the caller to ``note_host_failure``
        if the other way fails too; None otherwise."""
        return await self._abort(exc, count=False)


@contextlib.asynccontextmanager
async def ahop_gates(request_s: Optional[float] = None, *, check_host: bool = True):
    """An ``AsyncHopGates`` for one request (``request_s``: its own timeout, for the leases), let go on
    exit; an exception leaving the block reaches the gates first, so a request that never left hands
    its reserved start back. ``check_host=False``: the host breaker is not consulted (the libcurl
    tier's one retry of a request httpx could not connect: that failure is not counted until the retry
    fails too, see ``AsyncHopGates.let_go``; if libcurl gets through, its answer closes a breaker
    earlier failures opened meanwhile)."""
    gates = AsyncHopGates(request_s, check_host)
    try:
        yield gates
    except BaseException as exc:
        await gates._abort(exc)
        raise
    await gates.close()


@contextlib.contextmanager
def egress(url: str, *, browser: bool = False, until: Optional[float] = None, wait: bool = True,
           request_s: Optional[float] = None):
    """Sync entry for a blocking call that must hold ``url``'s declared gates the whole time it runs
    (xianyu's CDP read, entered on its calling thread around the whole ``cdp_call``; notify's push
    outlet): the upstream's gate, then the host's robots.txt Crawl-delay gate, within one budget, exactly as
    ``aegress``. A thin wrapper: it makes a holder credential (``_guard.Holder``), takes
    the gates on the event loop through ``portal.call`` (``AsyncHopGates.enter``), runs the block on
    this thread with those holds registered in its context (``_guard.holding``), and lets go through
    another ``portal.call``, as the same holder. Yields the first gate's key (or None when nothing
    applies). Raises ``UpstreamBusy`` without sending when a gate cannot admit the request in time.
    ``browser=True`` (a CDP page load) also raises ``DeclaredUserAgentRefused`` for a host whose
    upstream declares a User-Agent, since the shared browser cannot send it. ``request_s``: the
    request's own timeout, for the permits' leases. ``wait=False``: one try. Never call it on the
    event loop's own thread, nor on a Playwright sync thread (a CDP callback, the pool's worker), which
    reports Playwright's loop as running: portal raises RuntimeError there."""
    if browser:
        check_browser(url)
    gates = AsyncHopGates(request_s, holder=_guard.Holder())
    portal.call(gates.enter, url, until=until, wait=wait)
    try:
        with _guard.holding(gates.handles):
            yield gates.key
    except BaseException as exc:
        portal.call(gates._abort, exc)
        raise
    portal.call(gates.close)


@contextlib.asynccontextmanager
async def aegress(url: str, *, until: Optional[float] = None, wait: bool = True,
                  request_s: Optional[float] = None):
    """Hold every declared gate of ``url`` around ONE request: the upstream's gate, then the host's
    robots.txt Crawl-delay gate, within one budget (``AsyncHopGates``). Yields the first gate's key
    (or None when nothing applies). Raises ``UpstreamBusy`` without sending when a gate cannot admit
    the request in time. ``request_s``: the request's own timeout, for the permits' leases.
    ``wait=False``: one try. A blocking caller on a thread uses ``egress``, a wrapper over this."""
    async with ahop_gates(request_s) as gates:
        await gates.enter(url, until=until, wait=wait)
        yield gates.key


@contextlib.asynccontextmanager
async def abrowser_turn(url: str, request_s: float):
    """What a CDP render enters once it HAS its turn at the browser (``acdp_call(on_turn=...)``, on the
    event loop): the host's declared gates, tried ONCE without waiting (design decision of 2026-09-29
    on a review finding: a caller holding the browser's turn must not wait in another line; no gate now means
    the turn is given up at once and the page is not loaded, ``UpstreamBusy``). ``request_s`` is the
    render's own timeout: the permits' lease, so a render stuck in its page load gives the host back
    when it runs out (pooled or not). Also refuses a host that declares a User-Agent
    (``check_browser``). Async only: the thread that has the browser's turn is a Playwright sync thread,
    where the sync ``egress`` refuses."""
    check_browser(url)
    async with aegress(url, wait=False, request_s=request_s) as key:
        yield key


# ── observation (what the upstream reports) ────────────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else None


def parse_retry_after(value: Any) -> Optional[float]:
    """Seconds a Retry-After asks for, in either RFC 9110 form (section 10.2.3): delay-seconds, or an
    HTTP-date (a date already past is 0). ``value`` is the header's value, or a response's headers
    (anything with ``.get``: httpx Headers, a plain dict naming it in either case), read for
    Retry-After. None when absent or neither form (the first number of a date used to be
    read as seconds, so "Wed, 21 Oct 2026 07:28:00 GMT" deferred 21 s). OmniSeek's ONE Retry-After
    parser: every module that reads the header calls this."""
    if value is not None and not isinstance(value, str) and hasattr(value, "get"):
        value = value.get("retry-after") or value.get("Retry-After")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        secs = float(text)
    except ValueError:
        secs = None
    if secs is not None:
        return secs if math.isfinite(secs) and secs >= 0 else None
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def retry_wait(headers: Any, *, cap: float, default: float = 0.0, reset: bool = True) -> float:
    """Seconds a module's own retry waits after a throttled / failed answer carrying ``headers``: the
    Retry-After it asks for (either RFC 9110 form, parse_retry_after), else X-RateLimit-Reset read as
    an absolute epoch minus now (never below 0), else ``default``; never more than ``cap``. The ONE
    reading every module's retry uses; how many tries, the cap and the default stay at the caller.
    ``reset=False`` skips X-RateLimit-Reset, for an upstream whose reset is not an epoch (OpenAlex
    reports seconds to its daily reset there, and its retry never read that header)."""
    h = headers if hasattr(headers, "get") else {}
    secs = parse_retry_after(h)
    if secs is None and reset:
        try:
            at = float(h.get("x-ratelimit-reset") or "")
        except (TypeError, ValueError):
            at = math.nan
        if math.isfinite(at):
            secs = max(0.0, at - time.time())
    return min(default if secs is None else secs, cap)


def observe(target: str, headers: Any, status: Optional[int] = None, *, lane: Optional[str] = None,
            defer_on_429: bool = True, host: Optional[str] = None) -> Optional[str]:
    """Record the rate-limit / quota fields ``target``'s upstream reported (``target`` is a URL, a
    host or an upstream id; ``headers`` any mapping, e.g. response headers, or a dict of body quota
    fields like Stack Exchange's quota_max). Returns the upstream id, or None if undeclared.

    Deferral is ON by default (design decision 3): a response carrying Retry-After (a 429, a 503, a
    GitHub secondary-limit 403, any status) pushes the next allowed start by that long, for EVERY
    caller, capped at _DEFER_CAP_S: the upstream's gate, and the robots.txt Crawl-delay gate of the
    host the response came from (``host``, else ``target``'s host; design decision of 2026-09-29 on
    section 16.5, item 5: a host with a Crawl-delay gate and no upstream gate used to defer nothing).
    Only a module listed in SELF_BACKOFF, which honours that upstream's Retry-After itself, passes
    ``defer_on_429=False``, and that switches off the UPSTREAM gate's deferral only: the host's
    Crawl-delay gate is deferred regardless (design decision of 2026-09-29: an exemption is no wider
    than its reason, and handling an upstream's 429 says nothing about courtesy to the host).
    Never raises."""
    try:
        _guard.mark_sent()   # a response arrived: the holds of this context sent their request
        d = _load()
        is_uid = target in d
        uid = target if is_uid else uid_for_host(target)
        h = _host(host) if host else ("" if is_uid else _host(target))
        items = headers.items() if hasattr(headers, "items") else ()
        fields = {str(k).lower(): str(v)[:120] for k, v in items if _RATE_HEADER.search(str(k))}
        if uid is not None:
            lane_header = (d[uid].get("observe") or {}).get("lane_header")
            if lane is None and lane_header:
                lane = fields.get(lane_header)
            lane = lane or "_"
            now = time.time()
            with _lock:
                rec = _readings.setdefault(uid, {"lanes": {}, "n": 0, "last_429_at": None})
                rec["n"] += 1
                if fields:
                    rec["lanes"][lane] = {"fields": fields, "status": status, "at": now}
                if status == 429:
                    rec["last_429_at"] = now
        ra = parse_retry_after(fields.get("retry-after"))
        _probe.note_response(status, where=h or uid or str(target), retry_after=ra)
        if h:
            note_host_reached(h)   # any answer: the host is reachable (host liveness)
            if isinstance(status, int):
                _last_answer[h] = (status, time.time())
        if ra and ra > 0:
            secs = min(ra, _DEFER_CAP_S)
            if status not in (429, 503):
                # 2026-10-20: zenodo was measured at about one request a minute and
                # only a Retry-After on its 200 answers could explain it, but nothing showed the
                # headers. Whether to stop deferring on a 2xx is decided once this line has been seen.
                logger.info("upstream %s answered HTTP %s with Retry-After %.0fs: deferring every caller",
                            uid or h or target, status, ra)
            if defer_on_429 and uid is not None and d[uid].get("gate"):
                guard(uid).defer(secs)
            if h and crawl_delay_for(h):      # always: SELF_BACKOFF covers the upstream gate only
                host_guard(h).defer(secs)
        return uid
    except Exception as exc:  # noqa: BLE001 (observation must never break an egress)
        logger.debug("upstreams.observe failed: %s", exc)
        return None


def _response_host(resp: Any) -> str:
    """The host a response came from (its request's URL), or "" for a stub that carries none."""
    for get in (lambda: resp.request.url, lambda: resp.url):
        try:
            return _host(str(get()))
        except Exception:  # noqa: BLE001 (httpx raises when no request is attached)
            continue
    return ""


def observe_response(target: str, resp: Any, *, lane: Optional[str] = None,
                     defer_on_429: bool = True) -> Optional[str]:
    """``observe`` for a response object: reads ``.headers`` / ``.status_code`` defensively (a test
    stub without them records nothing).

    EVERY response is recorded ONCE, on its own host, whichever client and layer made the request
    (design decision of 2026-09-29 on section 16.5, item 2): the first recording marks the response
    and a later one for the same response is a no-op, so a client's response hook and the layer that
    returns the final response can both call this. ``target`` names the host; when it is an upstream
    id, the response's own URL names the host (for that host's Crawl-delay gate). Never raises."""
    try:
        if getattr(resp, "_omniseek_observed", False):
            return None
        host = _response_host(resp) if target in _load() else None
    except Exception:  # noqa: BLE001
        host = None
    uid = observe(target, getattr(resp, "headers", None) or {}, getattr(resp, "status_code", None),
                  lane=lane, defer_on_429=defer_on_429, host=host)
    try:
        resp._omniseek_observed = True
    except Exception:  # noqa: BLE001 (a stub that refuses attributes is recorded each time)
        pass
    return uid


def readings(uid: str) -> dict:
    with _lock:
        return json.loads(json.dumps(_readings.get(uid) or {}))


# ── checks (used by the health block) ───────────────────────────────────────────────────────────
def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def schema_problems(uid: str, e: dict) -> list[str]:
    """Mechanical checks of one declaration: every term is a value or 未公布, 未公布 says where we
    looked, a source URL and a check date exist, and the gate is never looser than the terms."""
    p: list[str] = []
    if "." in uid:
        p.append(f"{uid}: an upstream id must not contain '.' (host gates are named by host)")
    t = e.get("terms")
    if not isinstance(t, dict):
        return p + [f"{uid}: no terms"]
    ua = e.get("user_agent")
    if ua is not None and not (isinstance(ua, str) and ua.strip()):
        p.append(f"{uid}: user_agent must be a non-empty string")
    for h in e.get("hosts") or ():
        if not (_HOST_RE.fullmatch(str(h)) or _SUFFIX_RE.fullmatch(str(h))):
            p.append(f"{uid}: host {h!r} must be a lower-case host name, or a domain suffix that starts "
                     "with '.' and has at least two labels ('.wikipedia.org')")
    paths = e.get("paths")
    if paths is not None:
        if not (isinstance(paths, list) and paths
                and all(isinstance(x, str) and x.startswith("/") for x in paths)):
            p.append(f"{uid}: paths must be a non-empty list of path prefixes that start with '/'")
        if not e.get("hosts") or any(str(h).startswith(".") for h in e.get("hosts") or ()):
            p.append(f"{uid}: an entry with paths must list exact hosts (no domain suffix)")
        if t.get("robots_crawl_delay_s") is not None or e.get("user_agent") is not None:
            p.append(f"{uid}: a Crawl-delay or a User-Agent holds for the whole host; declare it on the "
                     "host's entry, not on an entry with paths")
    if (e.get("gate") and t.get("max_concurrency") == 1
            and any(str(h).startswith(".") for h in e.get("hosts") or ())):
        p.append(f"{uid}: a domain suffix cannot get the one-connection transport (it is mounted per "
                 "exact host); list the hosts")
    dis = e.get("disabled")
    if dis is not None and not (
            isinstance(dis, dict) and str(dis.get("reason") or "").strip()
            and str(dis.get("source") or "").startswith("http")
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dis.get("checked") or ""))):
        p.append(f"{uid}: disabled must give a reason, its source (a URL) and checked (YYYY-MM-DD)")
    rcd = t.get("robots_crawl_delay_s")
    if rcd is not None:
        hosts = {str(h).lower() for h in e.get("hosts") or () if not str(h).startswith(".")}
        if not isinstance(rcd, dict) or not all(
                _is_num(v) and v > 0 and str(h).lower() in hosts for h, v in rcd.items()):
            p.append(f"{uid}: terms.robots_crawl_delay_s must map this entry's hosts to seconds > 0")
    for k in _TERM_NUMERIC:
        v = t.get(k)
        if not (_is_num(v) or v == UNPUBLISHED):
            p.append(f"{uid}: terms.{k} must be a number or {UNPUBLISHED!r} (got {v!r})")
    w = t.get("windows")
    if not (w == UNPUBLISHED or (isinstance(w, list) and all(
            isinstance(x, dict) and _is_num(x.get("limit")) and _is_num(x.get("seconds"))
            for x in w))):
        p.append(f"{uid}: terms.windows must be a list of {{limit, seconds}} or {UNPUBLISHED!r}")
    if not (isinstance(t.get("daily_quota"), str) and t.get("daily_quota")):
        p.append(f"{uid}: terms.daily_quota must be text (a value with its unit, or {UNPUBLISHED!r})")
    if not t.get("url"):
        p.append(f"{uid}: terms.url missing (where the terms, or the search for them, were read)")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(t.get("checked") or "")):
        p.append(f"{uid}: terms.checked must be YYYY-MM-DD")
    unpublished = any(t.get(k) == UNPUBLISHED for k in (*_TERM_NUMERIC, "windows", "daily_quota"))
    if unpublished and not t.get("where_checked"):
        p.append(f"{uid}: a term is {UNPUBLISHED} but terms.where_checked does not say where we looked")
    if not (e.get("sources") or e.get("backends") or e.get("tools")):
        p.append(f"{uid}: names no sources / backends / tools (who uses it?)")
    p.extend(gate_problems(uid, e))
    return p


def gate_problems(uid: str, e: dict) -> list[str]:
    """The gate may be stricter than the terms, never looser; a published concurrency / interval /
    window must be enforced unless the entry says why not yet (``pending``)."""
    t, g = e.get("terms") or {}, e.get("gate")
    p: list[str] = []
    published = (_is_num(t.get("max_concurrency")) or _is_num(t.get("min_interval_s"))
                 or (isinstance(t.get("windows"), list) and bool(t.get("windows"))))
    if not g:
        if published and not e.get("pending"):
            p.append(f"{uid}: publishes a concurrency / interval / window but has no gate and no "
                     "'pending' reason")
        return p
    if not (_is_num(g.get("max_inflight")) and g["max_inflight"] >= 1):
        return [f"{uid}: gate.max_inflight must be a positive number"]
    if _is_num(t.get("max_concurrency")) and g["max_inflight"] > t["max_concurrency"]:
        p.append(f"{uid}: gate.max_inflight {g['max_inflight']} > published max_concurrency "
                 f"{t['max_concurrency']}")
    gi = g.get("min_interval_s", 0.0)
    if _is_num(t.get("min_interval_s")) and gi + 1e-9 < t["min_interval_s"]:
        p.append(f"{uid}: gate.min_interval_s {gi} < published min_interval_s {t['min_interval_s']}")
    gw = [(float(n), float(s)) for n, s in (g.get("windows") or ())]
    for tw in (t.get("windows") if isinstance(t.get("windows"), list) else ()):
        tn, ts = float(tw["limit"]), float(tw["seconds"])
        # Covered if some gate window allows no more than the published count over a span at least
        # as long, or the min interval alone keeps the count in: starts spaced gi apart put at most
        # ceil(ts / gi) of them in any half-open ts-long window, which is <= tn exactly when
        # tn * gi >= ts (the guard's window log uses the same half-open convention).
        ok = any(gn <= tn and gs >= ts for gn, gs in gw) or (gi > 0 and tn * gi >= ts - 1e-9)
        if not ok and e.get("pending"):
            continue
        if not ok:
            p.append(f"{uid}: published window {int(tn)} per {ts:g}s is not enforced by the gate")
    return p


def _covered(e: dict, source: str, backend: Optional[str]) -> bool:
    return source in (e.get("sources") or ()) or (backend is not None
                                                  and backend in (e.get("backends") or ()))


def uid_for_source(source: str, backend: Optional[str] = None) -> Optional[str]:
    """The first declared upstream that lists ``source`` (or its ``backend``), or None. A source's
    default ``license`` points here (``upstreams:<uid>``): that entry carries the upstream's terms."""
    d = _load()
    for uid, e in d.items():
        if source in (e.get("sources") or ()):
            return uid
    if backend:
        for uid, e in d.items():
            if backend in (e.get("backends") or ()) or backend in (e.get("sources") or ()):
                return uid
    return None


def _site(host: str) -> str:
    h = str(host or "").lower().strip().rstrip(".").lstrip(".")
    return h[4:] if h.startswith("www.") else h


def source_on_site(source: str, target: str, backend: Optional[str] = None) -> bool:
    """Whether an entry covering ``source`` names a host on the same site as ``target``'s host: the
    same host, or one a subdomain of the other, a leading "www." and a suffix entry's "." set aside
    (arxiv.org and export.arxiv.org; alphaxiv.org and api.alphaxiv.org). An entry covers a source the
    way ``coverage`` counts it: the source in its ``sources``, or ``backend`` (the source's backend,
    fetcher.backend_of) in its ``backends`` (the OpenAlex slices). Read-only."""
    site = _site(_host(target))
    if "." not in site:   # never a bare label ("com" would sit under every .com host)
        return False
    for e in _load().values():
        if not _covered(e, source, backend):
            continue
        for h in e.get("hosts") or ():
            h = _site(h)
            if "." in h and (h == site or h.endswith("." + site) or site.endswith("." + h)):
                return True
    return False


def coverage(sources: Iterable[str], backend_of: Callable[[str], Optional[str]],
             catalog: Optional[Iterable[str]] = None) -> dict:
    """{'undeclared': [source...], 'unknown_sources': [(uid, name)...]}. ``undeclared``: the in-service
    ``sources`` no upstream covers (a source is declared when some upstream lists it in ``sources`` or its
    backend in ``backends``). ``unknown_sources``: names a declaration lists that are not in ``catalog``,
    every source the code has, in service or not (design decision of 2026-09-29: whether a source is parked
    by runtime state or an online override is a runtime fact and does not make its declaration wrong);
    without ``catalog``, ``sources`` stands in for it."""
    d = _load()
    names = list(sources)
    und = []
    for s in names:
        try:
            b = backend_of(s)
        except Exception:  # noqa: BLE001
            b = None
        if not any(_covered(e, s, b) for e in d.values()):
            und.append(s)
    known = set(names if catalog is None else catalog) | optional_sources()
    stale = sorted((uid, s) for uid, e in d.items() for s in (e.get("sources") or ()) if s not in known)
    return {"undeclared": sorted(und), "unknown_sources": stale}


def optional_sources() -> set:
    """Source names a declaration may carry although this build lacks them (the public mirror build
    removes a few adapters; see upstreams.json "optional_sources")."""
    _load()
    return set(_optional)


def _expect_problems(uid: str, e: dict, rec: dict) -> list[str]:
    """Observed header values that differ from what the declaration says the upstream allows."""
    exp = (e.get("observe") or {}).get("expect") or {}
    out = []
    for lane, obs in (rec.get("lanes") or {}).items():
        fields = obs.get("fields") or {}
        for hdr, want in exp.items():
            if hdr not in fields:
                continue
            want_l = want.get(lane) if isinstance(want, dict) else want
            if want_l is None:
                continue
            cands = want_l if isinstance(want_l, list) else [want_l]
            got = _num(fields[hdr])
            if got is None or not any(abs(got - float(c)) < 1e-9 for c in cands):
                out.append(f"{uid}: observed {hdr}={fields[hdr]} (lane {lane}) differs from declared "
                           f"{want_l}")
    return out


def _compact_terms(t: dict) -> dict:
    keep = ("max_concurrency", "min_interval_s", "windows", "daily_quota", "checked", "url")
    return {k: t.get(k) for k in keep if k in t}


def health_block(sources: Iterable[str], backend_of: Callable[[str], Optional[str]],
                 today: Optional[str] = None, catalog: Optional[Iterable[str]] = None) -> dict:
    """The omniseek_sources(check_health=True) system view: every upstream that publishes numbers, has a
    gate, reports readings, or has a problem gets a row (declared terms, the gate's live state, the
    last readings, flags). Plain 未公布 websites are only counted. Pure read, no network."""
    d = _load()
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    rows, flagged = [], 0
    for uid in sorted(d):
        e = d[uid]
        t = e.get("terms") or {}
        flags = schema_problems(uid, e)
        if e.get("pending"):
            flags.append(f"{uid}: declared limit not enforced yet: {e['pending']}")
        chk = str(t.get("checked") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", chk):
            age = (time.mktime(time.strptime(today, "%Y-%m-%d"))
                   - time.mktime(time.strptime(chk, "%Y-%m-%d"))) / 86400
            if age > RECHECK_AFTER_DAYS:
                flags.append(f"{uid}: terms checked {chk}, older than {RECHECK_AFTER_DAYS} days")
        rec = readings(uid)  # a copy taken under the lock (observe() writes from other threads)
        flags.extend(_expect_problems(uid, e, rec))
        numeric = (_is_num(t.get("max_concurrency")) or _is_num(t.get("min_interval_s"))
                   or (isinstance(t.get("windows"), list) and bool(t.get("windows"))))
        host_gates = {}
        for h in sorted({str(x).lower() for x in e.get("hosts") or ()}):
            if crawl_delay_for(h):
                hg = _host_guards.get(h)
                host_gates[h] = (hg.snapshot() if hg else
                                 {"max_inflight": 1, "min_interval_s": crawl_delay_for(h),
                                  "not_used_since_start": True})
        if not (numeric or e.get("gate") or rec or flags or host_gates or disabled_reason(uid)):
            continue
        g = _guards.get(uid)
        gate_decl = e.get("gate")
        gate_view = (g.snapshot() if g else
                     ({k: gate_decl[k] for k in ("max_inflight", "min_interval_s", "windows",
                                                 "http_gate") if k in gate_decl}
                      | {"not_used_since_start": True}) if gate_decl else None)
        row = {"id": uid, "name": e.get("name"), "terms": _compact_terms(t),
               "gate": gate_view,
               "host_gates": host_gates or None,
               "observed": {ln: {"fields": o.get("fields"), "status": o.get("status"),
                                 "age_s": round(time.time() - o.get("at", time.time()), 1)}
                            for ln, o in (rec.get("lanes") or {}).items()} or None,
               "last_429_age_s": (round(time.time() - rec["last_429_at"], 1)
                                  if rec.get("last_429_at") else None),
               "flags": flags}
        if disabled_reason(uid):
            row["disabled"] = disabled_reason(uid)
        flagged += bool(flags)
        rows.append(row)
    cov = coverage(sources, backend_of, catalog)   # in service for "undeclared", every source for "unknown"
    return {
        "declared": len(d),
        "load_error": _load_error,
        "rows": rows,
        "unpublished_only": len(d) - len(rows),
        "undeclared_sources": cov["undeclared"],
        "declared_but_unknown_sources": [f"{u}:{s}" for u, s in cov["unknown_sources"]],
        "flagged_rows": flagged,
        "host_breakers": host_breakers(),
    }
