"""Opt-in failure-evidence trace: the eye's "what went wrong" capture for a SINGLE source.

When eye_fetch(source, query) comes back empty or errored, the agent that has to FIX
the source (the /eye-fix skill) needs evidence: which egress helper failed, the HTTP
status + a body snippet, the exception type. This module is that capture, and nothing
more. The razor: it stores; it never judges (the fixing agent judges the root cause).

Design (deliberately small):
  - A contextvar holds either None (the default: capture OFF) or a list (capture ON).
  - ``enable()`` turns capture on FOR THE CURRENT THREAD/CONTEXT by setting the var to a
    fresh list; ``drain()`` returns that list and resets to None.
  - The shared egress helpers (http / _openalex / _github / _s2 / _stackexchange / _cdp)
    call ``note(...)`` ONLY on their failure branches. When capture is OFF (the var is
    None) ``note`` is a cheap no-op, so the broad search_many fan-out (which never calls
    ``enable()``) pays ZERO cost and never cross-contaminates one source's trace with
    another's. Capture is armed ONLY around a single fetch_one_with_diag run.

fail-open is absolute: every function here swallows its own errors. A bug in the
diagnostic path must NEVER turn a working retrieval into a broken one. note() that raises
internally is caught; a body that will not stringify is dropped; a URL that will not parse
is passed through best-effort. The capture is a luxury; the retrieval is the product.
"""

from __future__ import annotations

import contextvars
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# None = capture OFF (the default, and the broad-search state). A list = capture ON; note()
# appends to it. The default is shared, but enable() always sets a FRESH list, so two
# concurrent armed contexts never share a list (contextvars are per-logical-context).
_trace_var: contextvars.ContextVar = contextvars.ContextVar("polaris_eye_diag", default=None)

_MAX_BODY = 500       # a body snippet beyond this is truncated (a marker is appended)
_MAX_CAPTURES = 50    # an upper bound on captures per run, so a retry storm cannot grow unbounded

# Query-string keys whose VALUE is a credential and must be stripped before a capture is shown
# to the fixing agent (the trace is read by an agent + may be logged). Case-insensitive match.
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "auth", "authorization",
    "password", "passwd", "secret", "client_secret", "session", "sig", "signature",
})


def enable() -> None:
    """Arm capture for the current context: set the var to a FRESH list. Idempotent-ish (a
    second call just starts a new list, dropping any not-yet-drained captures, which is the
    intended reset). Called by fetch_one_with_diag right before it runs the adapter."""
    try:
        _trace_var.set([])
    except Exception:  # noqa: BLE001 (arming must never raise into the caller)
        pass


def active() -> bool:
    """True iff capture is currently armed (the var holds a list, not None)."""
    try:
        return _trace_var.get() is not None
    except Exception:  # noqa: BLE001
        return False


def _strip_secrets(url: Optional[str]) -> Optional[str]:
    """Return ``url`` with any credential-bearing query-string values replaced by ``<redacted>``.
    Best-effort: a URL that will not parse is returned unchanged (better a raw URL in the trace
    than a dropped capture). Never raises."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        cleaned = [(k, "<redacted>" if k.lower() in _SECRET_KEYS else v) for k, v in pairs]
        return urlunsplit(parts._replace(query=urlencode(cleaned)))
    except Exception:  # noqa: BLE001
        return url


def note(helper: str, *, url: Optional[str] = None, status: Optional[int] = None,
         body: Optional[object] = None, exc: Optional[BaseException] = None) -> None:
    """Append ONE failure record to the active trace (a no-op when capture is OFF).

    Called ONLY from the failure branches of the shared egress helpers (never the success
    path). ``helper`` names the egress (e.g. "http.get", "openalex.get_json", "cdp_call").
    ``url`` has its credential query values stripped; ``body`` is stringified + truncated to
    ``_MAX_BODY``; ``exc`` is reduced to ``type: message``. Fail-open: any internal error is
    swallowed so a diagnostic bug can never break the retrieval it is observing."""
    try:
        trace = _trace_var.get()
        if trace is None or len(trace) >= _MAX_CAPTURES:
            return  # capture OFF (the broad-search path), or this run already hit the cap
        rec: dict = {"helper": helper}
        if url is not None:
            rec["url"] = _strip_secrets(str(url))
        if status is not None:
            rec["status"] = status
        if body is not None:
            try:
                text = body if isinstance(body, str) else str(body)
            except Exception:  # noqa: BLE001 (an object whose __str__ raises is just dropped)
                text = None
            if text:
                rec["body"] = text[:_MAX_BODY] + ("…(truncated)" if len(text) > _MAX_BODY else "")
        if exc is not None:
            rec["exc"] = f"{type(exc).__name__}: {exc}"[:_MAX_BODY]
        trace.append(rec)
    except Exception:  # noqa: BLE001 (capture must never raise into a live retrieval)
        pass


def drain() -> list:
    """Return the captures collected since ``enable()`` and reset capture to OFF.

    Always returns a list (``[]`` when nothing was captured or capture was never armed), and
    leaves the var at None so a reused pool thread starts clean. Never raises."""
    try:
        trace = _trace_var.get()
        _trace_var.set(None)
        return list(trace) if trace else []
    except Exception:  # noqa: BLE001
        return []
