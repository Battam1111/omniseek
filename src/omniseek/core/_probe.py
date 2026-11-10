"""What held a health check back: the ledger a check's verdict is re-read against.

A health check answers True (verified working), False (verified broken) or None (not verified: this
check sent the upstream no request whose answer could tell good from bad; see
``fetcher.SourceAdapter.health_check``). Two things make a False wrong without the adapter knowing:

- the upstream answered HTTP 429, or an error whose body says it is a rate limit. It answered, but
  the answer says nothing about whether the path serves data: not verified;
- one of OmniSeek's own gates (a declared upstream gate, a shared permit pool) turned the request
  away before it was sent: nothing was asked, so nothing was verified.

Most adapters cannot tell either case from an outage: the shared http helpers return None on any
failure. So the egress records them here instead, at the places every request already passes:
``upstreams.observe`` sees every response's status, ``upstreams.UpstreamBusy`` and
``_guard.GateBusy`` are built only when a gate refuses, and ``http._arequest_capped`` reads the error
body. Three egress paths that never reach ``upstreams.observe`` record here too (2026-10-04): the
curl_cffi entry point (``omniseek.core.curl``), the browser helper (``_cdp.cdp_call``: the page's
main-document answers and its in-page fetches' 429s, kept only while a ledger is open, see
``active``) and the yt-dlp entry point (``omniseek.core.ytdlp``: every HTTP error answer). A ledger is
open only while a health check runs (``watching``, opened by
``fetcher._asafe_health`` and by the shared probes that cache one verdict for many sources); outside
one, recording is a no-op.

``reread`` turns a False into None when the ledger holds a 429, a rate-limit body or a gate refusal
AND no other error answer (a 4xx / 5xx that is not a rate limit): that answer is real evidence, so
the False stands. A True is never touched: an adapter that saw a served answer keeps it. Since
EYE-GATE-FIX (2026-10-20) a connection that could not be made (``note_unreachable``) is evidence too,
and a check that met an open host breaker (``note_host_down``, ``upstreams.HostDown``) reads False.

Context: the ledger rides a ContextVar, so a request made on another thread is recorded only when
that thread runs in a copy of the check's context (``contextvars.copy_context()``, as the RSS bundle
probe does); asyncio tasks inherit it on their own.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import re
import time
from typing import Awaitable, Callable, Optional

# Words an error body uses for a rate limit (GitHub's "API rate limit exceeded" / "secondary rate
# limit", Stack Exchange's "throttle_violation", the generic "Too Many Requests").
_RATE_WORDS = re.compile(r"rate[ _-]?limit|too many requests|throttl", re.I)


class Ledger:
    """One health check's record: ``held`` (what held it back), ``errors`` (other error answers),
    ``down`` (an open host breaker refused a request: the host is known unreachable) and
    ``unreachable`` (a connection that could not be made: refused, handshake timed out, EOF)."""

    __slots__ = ("held", "errors", "answers", "down", "unreachable")

    def __init__(self) -> None:
        self.held: list[tuple] = []     # ("429", where, retry_after) | ("body", where, status) | ("gate", detail)
        self.errors: list[tuple] = []   # (status, where) for every 4xx / 5xx answer that is not a rate limit
        self.answers = 0                # responses observed, any status
        self.down: list[str] = []       # HostDown messages (upstreams.HostDown)
        self.unreachable: list[tuple] = []   # (where, detail) for every connection that was not made


_current: contextvars.ContextVar[Optional[Ledger]] = contextvars.ContextVar(
    "omniseek_eye_probe_ledger", default=None)


@contextlib.contextmanager
def watching():
    """Open a ledger for one health check; what it records also reaches an enclosing ledger."""
    outer = _current.get()
    led = Ledger()
    token = _current.set(led)
    try:
        yield led
    finally:
        _current.reset(token)
        if outer is not None:
            outer.held.extend(led.held)
            outer.errors.extend(led.errors)
            outer.answers += led.answers
            outer.down.extend(led.down)
            outer.unreachable.extend(led.unreachable)


def says_rate_limited(text: Optional[str]) -> bool:
    """True when an error text names a rate limit (for an adapter's own exception messages)."""
    return bool(text) and bool(_RATE_WORDS.search(text))


def active() -> bool:
    """Whether a health check's ledger is open in this context (a recorder that has to set something
    up first, such as the browser's response listener, does so only then)."""
    return _current.get() is not None


def note_response(status: Optional[int], *, where: str = "", retry_after: Optional[float] = None) -> None:
    """Record one response (called by ``upstreams.observe`` for every response). No-op outside a check
    (the ledger) and outside a source call (``capture_answers``)."""
    _count_answer(status)
    led = _current.get()
    if led is None or not isinstance(status, int):
        return
    led.answers += 1
    if status == 429:
        led.held.append(("429", where, retry_after))
    elif status >= 400:
        led.errors.append((status, where))


def note_error_body(status: Optional[int], body: Optional[str], *, where: str = "") -> None:
    """An error answer whose body says it is a rate limit counts as one (moved from ``errors`` to
    ``held``). Called by the shared http helper, which has the body; a 429 is already held."""
    led = _current.get()
    if led is None or not isinstance(status, int) or status == 429 or not body:
        return
    if not _RATE_WORDS.search(body):
        return
    for i in range(len(led.errors) - 1, -1, -1):
        if led.errors[i][0] == status:
            del led.errors[i]
            break
    led.held.append(("body", where, status))


def note_held(detail: str) -> None:
    """A gate of OmniSeek refused a request before it was sent (``UpstreamBusy`` / ``GateBusy``)."""
    led = _current.get()
    if led is not None:
        led.held.append(("gate", str(detail)[:200]))
    _capture_held("gate", detail)


def note_host_down(detail: str) -> None:
    """An open host breaker refused a request (``upstreams.HostDown``): the host did not answer the
    last few connections, so this is evidence the path is down, not a gate holding a request back."""
    led = _current.get()
    if led is not None:
        led.down.append(str(detail)[:200])
    _capture_held("host", detail)
    _count_answer(None, failed=True)


def note_unreachable(where: str, detail: str) -> None:
    """A connection to ``where`` could not be made (refused, handshake timed out, closed before any
    answer). Real evidence about the path, like an error answer: a False backed by it stands."""
    led = _current.get()
    if led is not None:
        led.unreachable.append((where, str(detail)[:200]))
    _count_answer(None, failed=True)


# WHAT ONE SOURCE CALL HEARD FROM ITS UPSTREAM (EYE-NONE-RESET, 2026-11-07). The fetcher opens this
# around every source call (fetcher._dispatch_search) to tell a live fetch from one answered wholly
# from cache: an answer read from cache passes no egress and so reaches none of the recorders above.
# ``ok`` counts the answers with a status below 400; ``failed`` counts the error answers (400 and up,
# a rate limit included), the connections that could not be made and the open host breakers met.
# Same context rule as the held capture: a worker thread sees it when it runs in a copy of the
# caller's context. The browser helper records only while a check's ledger is open (``active``), so a
# browser source's ordinary fetch counts nothing here.
class Answers:
    __slots__ = ("ok", "failed")

    def __init__(self) -> None:
        self.ok = 0
        self.failed = 0


_answers_var: contextvars.ContextVar[Optional[Answers]] = contextvars.ContextVar(
    "omniseek_eye_answers_capture", default=None)


def _count_answer(status: Optional[int], *, failed: bool = False) -> None:
    seen = _answers_var.get()
    if seen is None:
        return
    if failed or (isinstance(status, int) and status >= 400):
        seen.failed += 1
    elif isinstance(status, int):
        seen.ok += 1


@contextlib.contextmanager
def capture_answers():
    """Count, in an ``Answers``, the upstream answers met inside this block (see the section above)."""
    seen = Answers()
    token = _answers_var.set(seen)
    try:
        yield seen
    finally:
        try:
            _answers_var.reset(token)
        except (ValueError, RuntimeError):  # reset from another context: the value dies with it
            pass


# WHAT HELD ONE SOURCE CALL BACK (EYE-GATE-FIX C/D, 2026-10-20). The search fan-out and the recall
# ingest open a capture around each source call; every gate refusal and every open host breaker met
# inside it lands there, whatever layer refused (the shared http helpers, a module's own guard, a
# browser render). The fan-out names those sources in ``_meta.held`` instead of ``_meta.empty``; the
# ingest holds its watermark. A thread the source starts sees the capture when it runs in a copy of
# the caller's context (the RSS bundle and the multi-portal fan-outs do), asyncio tasks inherit it.
_held_var: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "omniseek_eye_held_capture", default=None)

HELD_FULL = "闸满"          # no permit within the caller's budget
HELD_LATE = "时限不够"      # the next allowed start, or the budget itself, ran past the caller's time
HELD_DOWN = "主机断路"      # the host's breaker is open (see upstreams.HostDown)


def _capture_held(kind: str, detail) -> None:
    cap = _held_var.get()
    if cap is not None:
        cap.append((kind, str(detail)[:300]))


@contextlib.contextmanager
def capture_held():
    """Collect, as ``[(kind, detail)]``, every refusal met inside this block (kind "gate" or "host")."""
    cap: list = []
    token = _held_var.set(cap)
    try:
        yield cap
    finally:
        try:
            _held_var.reset(token)
        except (ValueError, RuntimeError):  # reset from another context: the value dies with it
            pass


def held_reason(kind: str, detail: str) -> str:
    """Which of the three reasons one captured refusal is (HELD_FULL / HELD_LATE / HELD_DOWN)."""
    if kind == "host":
        return HELD_DOWN
    d = str(detail)
    if "budget" in d or "next allowed start" in d or "past the caller" in d:
        return HELD_LATE
    return HELD_FULL


def summarize_held(cap: list) -> Optional[dict]:
    """``{"why", "detail", "n"}`` for a capture (the most telling reason first: a broken host, then a
    full gate, then a short budget), or None when nothing was held."""
    if not cap:
        return None
    reasons = [(held_reason(k, d), d) for k, d in cap]
    for want in (HELD_DOWN, HELD_FULL, HELD_LATE):
        hit = next((d for r, d in reasons if r == want), None)
        if hit is not None:
            return {"why": want, "detail": hit[:200], "n": len(cap)}
    return None


def rate_limited(where: str = "", retry_after: Optional[float] = None, status: int = 429) -> str:
    """The one wording for "the upstream rate-limited this check" (adapters that read the status
    themselves use it too, so every such message reads alike)."""
    at = f" from {where}" if where else ""
    ra = f", Retry-After {retry_after:.0f}s" if retry_after else ""
    return f"not verified: rate-limited (HTTP {status}{at}{ra})"


def breaker_open(what: str, seconds_left: Optional[float] = None) -> str:
    """The one wording for "a circuit breaker / back-off of OmniSeek is open, nothing was sent"."""
    left = f", reopens in {max(0.0, seconds_left):.0f}s" if seconds_left is not None else ""
    return f"not verified: circuit breaker open ({what}{left}); no request sent"


def http_429(exc: BaseException) -> Optional[str]:
    """The ``rate_limited`` message when ``exc`` carries an HTTP 429 response (an
    ``httpx.HTTPStatusError`` from ``raise_for_status``), else None. For adapters whose own client
    raises instead of returning the status."""
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", None) != 429:
        return None
    try:
        host = resp.request.url.host
    except Exception:  # noqa: BLE001
        host = ""
    from omniseek.core.upstreams import parse_retry_after  # upstreams imports this module
    return rate_limited(host, parse_retry_after(getattr(resp, "headers", None)))


def _describe(item: tuple) -> str:
    kind = item[0]
    if kind == "429":
        return rate_limited(item[1], item[2])
    if kind == "body":
        return f"{rate_limited(item[1], status=item[2])}; its body says it is a rate limit"
    return f"{GATE_HELD_PREFIX}, nothing sent ({item[1]})"


# The wording of a False re-read as None because one of OmniSeek's gates refused (``reread``). The
# watchdog reads it: the adapter itself saw a failure, so the same re-read round after round is
# reported down (infra_jobs._gate_held_rounds), unlike a check that asked nothing at all.
GATE_HELD_PREFIX = "not verified: an eye gate held the request back"


def held_by_gate(msg: object) -> bool:
    """True when a health message is a False re-read as not verified because a gate refused."""
    return str(msg or "").startswith(GATE_HELD_PREFIX)


def reread(ok: Optional[bool], msg: str, led: Optional[Ledger]) -> tuple[Optional[bool], str]:
    """``(ok, msg)`` re-read against what the check's ledger recorded (rules in the module docstring).

    Two more rules (EYE-GATE-FIX B, 2026-10-20): a check that met an open host breaker reads False
    (the host stopped answering; that is not "not verified"), unless the adapter saw a served answer;
    and a connection that could not be made is evidence like an error answer, so a False backed by
    one stands even when a gate also refused another request of the same check."""
    if led is None:
        return ok, msg
    if ok is not True and led.down:
        return False, f"{led.down[-1]}; the check said: {msg}"
    if ok is not False or not led.held or led.errors or led.unreachable:
        return ok, msg
    return None, f"{_describe(led.held[-1])}; the check said: {msg}"


async def shared_verdict(box: dict, ttl: float,
                         aprobe: Callable[[], Awaitable[tuple[Optional[bool], str]]]) -> tuple[Optional[bool], str]:
    """ONE upstream probe for every source that shares an upstream (``_github`` / ``_openalex`` / ``_s2``
    / ``_stackexchange``): the verdict in ``box`` (``{"at", "result"}``) while younger than ``ttl``
    seconds, else ``await aprobe()``, single-flighted: callers that arrive while it runs await the
    same task (``asyncio.shield``: one caller giving up never cancels the probe the others wait on), so
    N concurrent health checks cause exactly ONE upstream call. The probe runs in the first caller's
    context (its ledger and deadline), as the sync probe used to run under the first caller's lock."""
    if box.get("result") is not None and time.monotonic() - box.get("at", 0.0) < ttl:
        return box["result"]
    loop = asyncio.get_running_loop()
    task = box.get("task")
    if task is None or task.done() or task.get_loop() is not loop:
        async def _run() -> tuple[Optional[bool], str]:
            r = await aprobe()
            box["at"], box["result"] = time.monotonic(), r
            return r
        task = box["task"] = loop.create_task(_run())
    return await asyncio.shield(task)
