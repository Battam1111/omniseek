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
                      entry wins, then an exact entry, then the longest suffix (driver decision,
                      2026-09-29: the whole Wikimedia family is one entry).
  guard(uid)          THE shared _guard.BackendGuard for that upstream, built from its gate. Every
                      egress path to the upstream (the owning module, the shared http client's host
                      gate, a health probe) takes the same object, so the limit holds across sources,
                      tools, threads and event loops.
  egress(url)         the host gate for any direct egress: a no-op for an ungated host or when the
                      current holder already holds that upstream (the owning module's own hold).
  budget              a request's permit wait and start wait, over every gate it passes, stay
                      within the declared max_wait_s AND the caller's own deadline (driver ruling 2,
                      2026-09-29); past it the request is not sent. See _guard.wait_until.
  redirects           HopGates holds the gates of the host a request is on: a redirect to the same
                      host continues the same visit; a redirect to another host lets go of the first
                      host's gates, then takes the new host's within the time left; never two hosts'
                      gates at once (driver ruling 1). Every redirect-following path uses it.
  host gates          a host whose robots.txt sets a Crawl-delay (terms.robots_crawl_delay_s) gets
                      its own gate, one request at a time and starts at least that delay apart.
                      egress(url) takes it after the upstream's gate, so the stricter of the two
                      always holds (driver decision, 2026-09-29).
  single connection   a gated upstream whose terms publish max_concurrency 1 (arXiv: "a single
                      connection at a time") is listed by single_connection_hosts(); the shared http
                      clients give those hosts a no-keep-alive HTTP/1.1 transport and send
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
                      the calling module is in SELF_BACKOFF (driver ruling 3).
  health_block(...)   declared vs gate vs observed, mismatches flagged, undeclared sources listed.

Judgment-free plumbing: it stores the declaration, enforces the numbers it is given and reports
differences. Deciding what an upstream's terms ARE is the job of whoever edits upstreams.json.
"""

from __future__ import annotations

import contextlib
import email.utils
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

from omniseek.core import _guard, _probe
from omniseek.core._guard import BackendGuard, deadline_after, deadline_until, wait_until  # noqa: F401

logger = logging.getLogger(__name__)

DECL_PATH = Path(__file__).with_name("upstreams.json")
UNPUBLISHED = "未公布"
# A declaration older than this is flagged for re-checking (the terms of several upstreams changed
# within months in 2025-2026: OpenAlex, Crossref, Wikimedia, Brave, Context7).
RECHECK_AFTER_DAYS = 90
_DEFAULT_MAX_WAIT_S = 15.0
# A Retry-After longer than this still sheds callers (they fail fast), it just is not waited out.
_DEFER_CAP_S = 600.0

_TERM_NUMERIC = ("max_concurrency", "min_interval_s")
_RATE_HEADER = re.compile(r"^(x-)?(rate-?limit|ratelimit)|^x-concurrency-limit$|^x-api-pool$"
                          r"|^retry-after$|^quota_(max|remaining)$", re.I)


class UpstreamBusy(RuntimeError):
    """A declared upstream's gate had no permit / no start slot within the caller's budget: the
    request was NOT sent. Callers map it to their failure contract (None / [] / degrade)."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        _probe.note_held(str(self))   # a running health check records that nothing was sent


class DeclaredUserAgentRefused(RuntimeError):
    """The host's upstream declares a User-Agent and the caller is the shared browser, which sends its
    own: the page was NOT rendered (sending a browser's User-Agent there would break the upstream's
    User-Agent policy). Callers map it to their failure contract."""


class DeclarationUnreadable(RuntimeError):
    """upstreams.json is missing or unreadable. OmniSeek does not run without it (driver ruling 4,
    2026-09-29): no built-in defaults, the server refuses to start and names the file and the reason."""


# Both mean the request inside a hold was never sent, so its reserved start is handed back.
_guard.register_unsent(UpstreamBusy, DeclaredUserAgentRefused)

# Modules that honour an upstream's Retry-After themselves, and therefore record responses with
# defer_on_429=False (driver ruling 3, 2026-09-29). Everywhere else the default holds: a response that
# carries Retry-After defers the whole upstream for every caller. The exemption covers the upstream
# gate only; the host's robots.txt Crawl-delay gate is deferred regardless (see observe).
# tests/test_upstream_limits.py fails on any call that switches the deferral off from a module not
# listed here.
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
_HOST_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_SUFFIX_RE = re.compile(r"\.[a-z0-9-]+(\.[a-z0-9-]+)+")   # at least two labels: never a bare ".org"
_optional: set = set()
_guards: dict[str, BackendGuard] = {}
_host_guards: dict[str, BackendGuard] = {}
_readings: dict[str, dict] = {}


# ── loading ────────────────────────────────────────────────────────────────────────────────────
def _load() -> dict:
    global _decl, _load_error, _host_index, _suffix_index, _host_delay, _host_ua, _suffix_ua, _optional
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
            # No built-in defaults (driver ruling 4): a silently empty declaration dropped every
            # source that builds its gate at import and left the shared client ungated (review F10).
            _load_error = f"{DECL_PATH}: {type(exc).__name__}: {exc}"
            logger.error("upstreams.json unreadable: %s", _load_error)
            raise DeclarationUnreadable(
                f"upstream declarations unreadable, OmniSeek will not run without them: {_load_error}"
            ) from exc
        index: dict[str, str] = {}
        sfx_index: dict[str, str] = {}
        # Gated entries win a shared host (the gate is what must see the request); otherwise the
        # first declaration of a host is its owner for header readings. A host entry starting with
        # "." is a domain suffix (see uid_for_host for how the two kinds meet).
        for uid, e in sorted(ups.items(), key=lambda kv: (not kv[1].get("gate"), kv[0])):
            for h in e.get("hosts") or ():
                h = str(h).lower()
                (sfx_index if h.startswith(".") else index).setdefault(h, uid)
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


def uid_for_host(target: str) -> Optional[str]:
    """The upstream id owning ``target`` (a URL or a bare host), or None if undeclared. Candidates are
    the entry listing the exact host and every entry whose domain suffix matches it; a gated one wins
    (the gate must see the request), then the exact one, then the longest suffix."""
    d = _load()
    h = _host(target)
    if not h:
        return None
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


def max_wait(uid: str) -> float:
    """The declared ``gate.max_wait_s`` of upstream ``uid`` (default 15 s): the most one request may
    wait on that upstream's gates in total, before the caller's own deadline cuts it shorter."""
    return float(gate_value(uid, "max_wait_s", _DEFAULT_MAX_WAIT_S))


_max_wait = max_wait


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
    host's Crawl-delay gate): [(key, guard, max_wait)]. Taking both is the stricter of the two."""
    out = []
    uid = gated_uid_for_url(url)
    if uid is not None:
        out.append((uid, guard(uid), _max_wait(uid)))
    host = _host(url)
    if crawl_delay_for(host):
        out.append((host, host_guard(host), _max_wait(uid) if uid else _DEFAULT_MAX_WAIT_S))
    return out


def _busy_factories(key: str) -> tuple[Callable, Callable]:
    """The two ways a gate turns a request away, both raised BEFORE anything is sent."""
    def on_busy(waited: float) -> UpstreamBusy:
        return UpstreamBusy(f"{key}: no permit within {waited:.1f}s (gate saturated); not sent")

    def on_late(wait: float) -> UpstreamBusy:
        if wait <= 0:   # nothing left of the budget (driver ruling of 2026-09-29: not "saturated")
            return UpstreamBusy(f"{key}: past this call's budget (declared max_wait_s or the caller's "
                                "deadline); not sent")
        return UpstreamBusy(f"{key}: next allowed start {wait:.1f}s away, past this caller's budget "
                            "(declared max_wait_s or the caller's deadline); not sent")
    return on_busy, on_late


@contextlib.contextmanager
def hold(uid: Optional[str], *, request_s: Optional[float] = None):
    """Hold upstream ``uid``'s declared gate around ONE request (sync): a permit, then the start
    slot, both within ONE budget (the declared ``max_wait_s``, cut to the caller's deadline). For
    egress that is not addressed by URL (a client library such as atproto). Raises ``UpstreamBusy``
    without sending when the gate cannot admit the request in time; ``uid=None`` is a no-op.
    ``request_s``: the request's own timeout, for the permit's lease (see ``BackendGuard.hold``)."""
    if uid is None:
        yield None
        return
    on_busy, on_late = _busy_factories(uid)
    with guard(uid).hold(max_wait(uid), on_busy, on_late, request_s=request_s):
        yield uid


@contextlib.asynccontextmanager
async def ahold(uid: Optional[str], *, request_s: Optional[float] = None):
    """Async twin of ``hold``."""
    if uid is None:
        yield None
        return
    on_busy, on_late = _busy_factories(uid)
    async with guard(uid).ahold(max_wait(uid), on_busy, on_late, request_s=request_s):
        yield uid


def budget_until(url: str, default: Optional[float] = None) -> Optional[float]:
    """The monotonic moment one request to ``url`` must stop waiting by: the smallest declared
    max_wait_s of its gates (else ``default``, else no bound of its own), cut to the caller's
    deadline. For a path that waits somewhere else first (a render's Chrome turn), so that wait and
    the gate wait share one budget."""
    gates = _gates_for_url(url)
    if gates:
        return wait_until(min(mw for _, _, mw in gates))
    if default is not None:
        return wait_until(default)
    return _guard.current_deadline()


def check_browser(url: str) -> None:
    """Raise ``DeclaredUserAgentRefused`` when ``url``'s host declares a User-Agent: the shared browser
    sends its own, so it must not load that host (checked before queueing for the browser)."""
    if user_agent_for(url):
        raise DeclaredUserAgentRefused(
            f"{_host(url)}: its upstream declares a User-Agent the shared browser cannot send; "
            "not rendered")


class HopGates:
    """The declared gates of the host one request is on right now, carried across its redirects: the
    ONE place the redirect rule lives (driver ruling 1, 2026-09-29).

    ``enter(url)`` before every hop. A hop to the SAME host continues the same visit: no second
    permit, no second start slot (a same-host 301 used to wait a whole Crawl-delay, or be refused
    outright past 15 s, review F3). A hop to ANOTHER host first lets go of the current host's gates,
    then takes the new host's gates within the time left (review F5: a redirect used to land on a
    gated host without its gate). Two hosts' gates are never held at once, so two redirect chains can
    never wait on each other.

    ONE budget for the whole chain (a chain is one read; driver rulings of 2026-09-29 on section 16.5,
    item 1, and on the objection in 16.8). With a caller's deadline, the deadline is the chain's
    absolute end. Without one, the budget is the first gated hop's smallest declared ``max_wait_s``,
    and only the time actually spent waiting at gates uses it up: a slow response or building a
    client does not (a chain across N gated hosts used to be able to wait N times ``max_wait_s``).
    Either way the gates of one hop (the upstream's, then the host's Crawl-delay gate, in that fixed
    order) wait no longer than their own smallest declared ``max_wait_s`` (ruling 2), so a hop waits
    at most the smaller of that and what is left of the chain's budget. One HopGates per request.
    ``request_s`` is the request's own timeout: each permit's lease ends that long after its hop's
    waiting budget (review P1; None: the gate's declared ``max_wait_s``). ``enter(url, wait=False)``
    tries each gate once without waiting (review P6). ``close()`` lets go; ``close(unsent=True)``
    also hands back the reserved start, for a request that was never sent (review F8), unless a
    response already arrived for it (review N2, see ``_guard.mark_sent``)."""

    def __init__(self, request_s: Optional[float] = None) -> None:
        self._host: Optional[str] = None
        self._stack: Optional[contextlib.ExitStack] = None
        self.key: Optional[str] = None
        self._request_s = request_s
        self._deadline: Optional[float] = _guard.current_deadline()   # the chain's absolute end
        self._budget: Optional[float] = None   # without a deadline: set by the first gated hop
        self._waited = 0.0                     # seconds spent waiting at gates so far

    def _hop_end(self, gates: list, until: Optional[float]) -> float:
        own = min(mw for _, _, mw in gates)               # this hop's own declared wait (ruling 2)
        if self._deadline is None:
            if self._budget is None:
                self._budget = own                        # the first gated hop fixes the budget
            own = min(own, max(0.0, self._budget - self._waited))
        end = wait_until(own)                             # never past the caller's deadline
        return end if until is None else min(end, until)

    def enter(self, url: str, *, until: Optional[float] = None, wait: bool = True) -> None:
        host = _host(url)
        if host == self._host:
            return
        self.close()
        gates = _gates_for_url(url)
        if gates:
            end = self._hop_end(gates, until)
            stack = contextlib.ExitStack()
            t0 = time.monotonic()
            try:
                for key, g, mw in gates:
                    on_busy, on_late = _busy_factories(key)
                    stack.enter_context(g.hold(mw, on_busy, on_late, until=end,
                                               request_s=self._request_s, wait=wait))
            except BaseException as exc:
                stack.__exit__(type(exc), exc, exc.__traceback__)  # the gates taken see why (refund)
                raise
            finally:
                self._waited += time.monotonic() - t0   # only the time spent at the gates counts
            self._stack, self.key = stack, gates[0][0]
        self._host = host

    def close(self, unsent: bool = False) -> None:
        stack, self._stack, self._host, self.key = self._stack, None, None, None
        if stack is None:
            return
        if unsent:
            exc = _guard.RequestNotSent()
            stack.__exit__(type(exc), exc, None)
        else:
            stack.__exit__(None, None, None)

    def _abort(self, exc: BaseException) -> None:
        stack, self._stack, self._host, self.key = self._stack, None, None, None
        if stack is not None:
            stack.__exit__(type(exc), exc, exc.__traceback__)


class AsyncHopGates:
    """Async twin of ``HopGates`` (same rule, same order, same one budget per chain)."""

    def __init__(self, request_s: Optional[float] = None) -> None:
        self._host: Optional[str] = None
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self.key: Optional[str] = None
        self._request_s = request_s
        self._deadline: Optional[float] = _guard.current_deadline()   # the chain's absolute end
        self._budget: Optional[float] = None
        self._waited = 0.0

    _hop_end = HopGates._hop_end

    async def enter(self, url: str, *, until: Optional[float] = None, wait: bool = True) -> None:
        host = _host(url)
        if host == self._host:
            return
        await self.close()
        gates = _gates_for_url(url)
        if gates:
            end = self._hop_end(gates, until)
            stack = contextlib.AsyncExitStack()
            t0 = time.monotonic()
            try:
                for key, g, mw in gates:
                    on_busy, on_late = _busy_factories(key)
                    await stack.enter_async_context(g.ahold(mw, on_busy, on_late, until=end,
                                                            request_s=self._request_s, wait=wait))
            except BaseException as exc:
                await stack.__aexit__(type(exc), exc, exc.__traceback__)
                raise
            finally:
                self._waited += time.monotonic() - t0
            self._stack, self.key = stack, gates[0][0]
        self._host = host

    async def close(self, unsent: bool = False) -> None:
        stack, self._stack, self._host, self.key = self._stack, None, None, None
        if stack is None:
            return
        if unsent:
            exc = _guard.RequestNotSent()
            await stack.__aexit__(type(exc), exc, None)
        else:
            await stack.__aexit__(None, None, None)

    async def _abort(self, exc: BaseException) -> None:
        stack, self._stack, self._host, self.key = self._stack, None, None, None
        if stack is not None:
            await stack.__aexit__(type(exc), exc, exc.__traceback__)


@contextlib.contextmanager
def hop_gates(request_s: Optional[float] = None):
    """A ``HopGates`` for one request (``request_s``: its own timeout, for the leases), let go on
    exit; an exception leaving the block reaches the gates first, so a request that never left hands
    its reserved start back."""
    gates = HopGates(request_s)
    try:
        yield gates
    except BaseException as exc:
        gates._abort(exc)
        raise
    gates.close()


@contextlib.asynccontextmanager
async def ahop_gates(request_s: Optional[float] = None):
    """Async twin of ``hop_gates``."""
    gates = AsyncHopGates(request_s)
    try:
        yield gates
    except BaseException as exc:
        await gates._abort(exc)
        raise
    await gates.close()


@contextlib.contextmanager
def egress(url: str, *, browser: bool = False, until: Optional[float] = None, wait: bool = True,
           request_s: Optional[float] = None):
    """Hold every declared gate of ``url`` around ONE request (sync): the upstream's gate, then the
    host's robots.txt Crawl-delay gate, within one budget (``HopGates``). Yields the first gate's key
    (or None when nothing applies). Raises ``UpstreamBusy`` without sending when a gate cannot admit
    the request in time. ``browser=True`` (a CDP page load) also raises ``DeclaredUserAgentRefused``
    for a host whose upstream declares a User-Agent, since the shared browser cannot send it.
    ``request_s``: the request's own timeout, for the permits' leases. ``wait=False``: one try."""
    if browser:
        check_browser(url)
    with hop_gates(request_s) as gates:
        gates.enter(url, until=until, wait=wait)
        yield gates.key


@contextlib.asynccontextmanager
async def aegress(url: str, *, until: Optional[float] = None, wait: bool = True,
                  request_s: Optional[float] = None):
    """Async twin of ``egress`` (same gates, same order, same budget)."""
    async with ahop_gates(request_s) as gates:
        await gates.enter(url, until=until, wait=wait)
        yield gates.key


def browser_turn(url: str, request_s: float):
    """What a CDP render enters once it HAS its turn at the browser (``cdp_call(on_turn=...)``): the
    host's declared gates, tried ONCE without waiting (driver ruling of 2026-09-29 on review P6: a
    caller holding the browser's turn must not wait in another line; no gate now means the turn is
    given up at once and the page is not loaded, ``UpstreamBusy``). ``request_s`` is the render's
    own timeout: the permits' lease, so a render stuck in its page load gives the host back when it
    runs out (review P1, pooled or not)."""
    return egress(url, browser=True, wait=False, request_s=request_s)


# ── observation (what the upstream reports) ────────────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else None


def parse_retry_after(value: Any) -> Optional[float]:
    """Seconds a Retry-After value asks for, in either RFC 9110 form: delay-seconds, or an HTTP-date
    (a date already past is 0). None when it is neither (review F7: the first number of a date used to
    be read as seconds, so "Wed, 21 Oct 2026 07:28:00 GMT" deferred 21 s)."""
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


def observe(target: str, headers: Any, status: Optional[int] = None, *, lane: Optional[str] = None,
            defer_on_429: bool = True, host: Optional[str] = None) -> Optional[str]:
    """Record the rate-limit / quota fields ``target``'s upstream reported (``target`` is a URL, a
    host or an upstream id; ``headers`` any mapping, e.g. response headers, or a dict of body quota
    fields like Stack Exchange's quota_max). Returns the upstream id, or None if undeclared.

    Deferral is ON by default (driver ruling 3): a response carrying Retry-After (a 429, a 503, a
    GitHub secondary-limit 403, any status) pushes the next allowed start by that long, for EVERY
    caller, capped at _DEFER_CAP_S: the upstream's gate, and the robots.txt Crawl-delay gate of the
    host the response came from (``host``, else ``target``'s host; driver ruling of 2026-09-29 on
    section 16.5, item 5: a host with a Crawl-delay gate and no upstream gate used to defer nothing).
    Only a module listed in SELF_BACKOFF, which honours that upstream's Retry-After itself, passes
    ``defer_on_429=False``, and that switches off the UPSTREAM gate's deferral only: the host's
    Crawl-delay gate is deferred regardless (driver ruling of 2026-09-29: an exemption is no wider
    than its reason, and handling an upstream's 429 says nothing about courtesy to the host).
    Never raises."""
    try:
        _guard.mark_sent()   # a response arrived: the holds of this context sent their request (N2)
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
        if ra and ra > 0:
            secs = min(ra, _DEFER_CAP_S)
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
    (driver ruling of 2026-09-29 on section 16.5, item 2): the first recording marks the response
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


# ── checks (shared by the health block and the smoke tests) ─────────────────────────────────────
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


def coverage(sources: Iterable[str], backend_of: Callable[[str], Optional[str]],
             catalog: Optional[Iterable[str]] = None) -> dict:
    """{'undeclared': [source...], 'unknown_sources': [(uid, name)...]}. ``undeclared``: the in-service
    ``sources`` no upstream covers (a source is declared when some upstream lists it in ``sources`` or its
    backend in ``backends``). ``unknown_sources``: names a declaration lists that are not in ``catalog``,
    every source the code has, in service or not (driver ruling of 2026-09-29: whether a source is parked
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
    }
