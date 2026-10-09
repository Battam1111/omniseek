"""Mask credentials in text OmniSeek lets out of its process: logs, tool results, state files, alerts.

The leak this closes (found 2026-10-04 in the live logs): httpx logs every request at INFO with its FULL
address, so ``HTTP Request: GET https://api.openalex.org/works?...&api_key=<the real key>`` sat in
organ.eye-http.err and its rotation. The same address rides inside an httpx HTTPStatusError's text,
and that text flows on into health messages, ``_meta.diagnostic`` and the watchdog state file.

ONE rule, applied at the few places every such string must pass on its way out:
  - a query parameter whose NAME (case-insensitive, percent-decoded) is in ``SECRET_PARAMS`` keeps
    its name and loses its value: ``api_key=XYZ`` becomes ``api_key=***``. The parameter must follow
    ``?``, ``&`` or ``;``, so prose such as "cache key=..." is left alone. A percent-encoded nested
    address (``%3Fapi_key%3DXYZ``) is handled the same way.
  - ``Bearer <token>`` keeps the word and loses the token; ``Authorization: <scheme> <value>`` keeps
    the header name and scheme and loses the value.
Everything else is kept byte for byte (no decode / re-encode), so a masked address still shows which
host, path and non-secret parameters were involved. A value that is already masked, empty, or a
printf placeholder (``%s``, used by a log call's format string) is left as it is.

Fail-open: every function here returns its input unchanged on an internal error. A masking bug must
never break logging, a tool call or a state write.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import traceback
from collections.abc import Mapping
from typing import Any, Callable, Optional
from urllib.parse import unquote

MASK = "***"

# Query-parameter names whose VALUE is a credential (or, for mailto, the operator's address).
SECRET_PARAMS = frozenset({
    "api_key", "apikey", "api-key", "x_api_key", "x-api-key", "key",
    "token", "access_token", "refresh_token", "private_token", "auth", "authorization",
    "client_secret", "secret", "secret_key", "access_key", "password", "passwd", "mailto",
    "session", "sig", "signature",
    # credential params that ride in the URL query on real adapters (adzuna app_key / app_id, etc.)
    "app_key", "app_id", "appkey", "appid", "client_id", "subscription_key",
})

# Values that are already masked (this module's MASK, diag's historical marker) stay as they are.
_ALREADY_MASKED = frozenset({MASK, "<redacted>"})
_PLACEHOLDER = re.compile(r"%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]\Z")

# A value ends at the next separator, quote, bracket or whitespace; credentials never contain those.
_VALUE = r"[^&#\s'\"<>(){}\[\],;|\\]*"
_QUERY_PARAM = re.compile(r"(?<=[?&;])([A-Za-z0-9_.%\-]+)=(" + _VALUE + ")")
_ENCODED_PARAM = re.compile(r"(?<=%3[Ff]|%26)([A-Za-z0-9_.\-]+)%3[Dd]((?:(?!%26)[^&#\s'\"<>(){}\[\],;|\\])*)")
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9\-._~+/]{8,}=*)")
_AUTH_HEADER = re.compile(
    r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?)(?!bearer\b)((?:basic|token|digest|bot)\s+)?([^\s\"',;}]+)")
_TRIGGER = re.compile(r"=|%3[Dd]|(?i:bearer|authorization)")


def _keep(value: str, mask: str) -> bool:
    return (not value or value == mask or value in _ALREADY_MASKED
            or _PLACEHOLDER.match(value) is not None)


def _is_secret_name(name: str) -> bool:
    try:
        name = unquote(name)
    except Exception:  # noqa: BLE001
        pass
    return name.lower() in SECRET_PARAMS


def redact(text: Any, mask: str = MASK) -> Any:
    """Return ``text`` with every credential value masked (see the module docstring for the rule).
    A non-str input is returned unchanged. Never raises."""
    if not isinstance(text, str) or not text or _TRIGGER.search(text) is None:
        return text
    try:
        def _param(m: "re.Match") -> str:
            name, value = m.group(1), m.group(2)
            if _keep(value, mask) or not _is_secret_name(name):
                return m.group(0)
            return f"{name}={mask}"

        def _encoded(m: "re.Match") -> str:
            name, value = m.group(1), m.group(2)
            if _keep(value, mask) or not _is_secret_name(name):
                return m.group(0)
            return m.group(0)[: len(m.group(0)) - len(value)] + mask

        def _bearer(m: "re.Match") -> str:
            return m.group(1) + m.group(2) + mask

        def _auth(m: "re.Match") -> str:
            if _keep(m.group(3), mask):
                return m.group(0)
            return m.group(1) + (m.group(2) or "") + mask

        out = _QUERY_PARAM.sub(_param, text)
        out = _ENCODED_PARAM.sub(_encoded, out)
        out = _BEARER.sub(_bearer, out)
        out = _AUTH_HEADER.sub(_auth, out)
        return text if out == text else out
    except Exception:  # noqa: BLE001
        return text


def redact_obj(obj: Any, mask: str = MASK, skip: Optional[Callable[[Any], bool]] = None,
               _depth: int = 0) -> Any:
    """``redact`` every str inside dicts / lists / tuples (dict KEYS are kept). Returns the SAME
    object when nothing changed, a shallow rebuild otherwise. ``skip(node)`` True passes a subtree
    through untouched. Never raises."""
    try:
        if _depth > 64:
            return obj
        if skip is not None and skip(obj):
            return obj
        if isinstance(obj, str):
            return redact(obj, mask)
        if isinstance(obj, dict):
            changed = False
            out = {}
            for k, v in obj.items():
                nv = redact_obj(v, mask, skip, _depth + 1)
                changed = changed or nv is not v
                out[k] = nv
            return out if changed else obj
        if isinstance(obj, (list, tuple)):
            items = [redact_obj(v, mask, skip, _depth + 1) for v in obj]
            if all(a is b for a, b in zip(items, obj)):
                return obj
            return items if isinstance(obj, list) else tuple(items)
        return obj
    except Exception:  # noqa: BLE001
        return obj


# ── logging: one record factory, so every logger (ours and httpx / httpcore / urllib3 / uvicorn)
# and every handler is covered, whatever an entry point's own logging configuration is ──────────
_FACTORY_MARK = "_omniseek_redacting_factory"
_install_lock = threading.Lock()
_exc_formatter = logging.Formatter()


def _redact_arg(arg: Any) -> Any:
    if arg is None or isinstance(arg, (bool, int, float)):
        return arg
    if isinstance(arg, str):
        return redact(arg)
    try:
        text = str(arg)  # e.g. an httpx.URL, or an exception whose message carries the address
    except Exception:  # noqa: BLE001
        return arg
    red = redact(text)
    return arg if red == text else red


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Mask a LogRecord in place: msg, every arg (httpx passes the address as a %s arg), the
    formatted traceback and the stack text. Arg types and tuple shape are kept when nothing is
    masked, so a formatter that reads ``record.args`` (uvicorn's access formatter does) still works."""
    try:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        elif record.msg is not None:
            text = str(record.msg)
            red = redact(text)
            if red != text:
                record.msg = red
        args = record.args
        if isinstance(args, tuple) and args:
            new = tuple(_redact_arg(a) for a in args)
            if any(a is not b for a, b in zip(new, args)):
                record.args = new
        elif isinstance(args, Mapping) and args:
            new_map = {k: _redact_arg(v) for k, v in args.items()}
            if any(new_map[k] is not v for k, v in args.items()):
                record.args = new_map
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            text = _exc_formatter.formatException(record.exc_info)
            red = redact(text)
            if red != text:
                # Formatter.format appends exc_text as is. exc_info is dropped because some handlers
                # render the live exception themselves (the SDK installs rich's RichHandler, which
                # rebuilds the traceback from exc_info and would print the unmasked message).
                record.exc_text = red
                record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
    except Exception:  # noqa: BLE001
        pass
    return record


def install_log_redaction() -> None:
    """Wrap the process-wide LogRecord factory once (idempotent). Records are masked as they are
    CREATED, before any filter or handler sees them, so no entry point can forget to attach it."""
    with _install_lock:
        current = logging.getLogRecordFactory()
        if getattr(current, _FACTORY_MARK, False):
            return

        def _factory(*args, **kwargs):
            return redact_record(current(*args, **kwargs))

        setattr(_factory, _FACTORY_MARK, True)
        logging.setLogRecordFactory(_factory)


# ── uncaught exceptions: the interpreter prints their traceback straight to stderr (the .err log)
def format_exception(exc_type, exc_value, exc_tb) -> str:
    """``traceback.format_exception`` joined and masked."""
    return redact("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))


def _sys_excepthook(exc_type, exc_value, exc_tb) -> None:
    try:
        if sys.stderr is not None:
            sys.stderr.write(format_exception(exc_type, exc_value, exc_tb))
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        sys.__excepthook__(exc_type, exc_value, exc_tb)


def _threading_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    try:
        if sys.stderr is not None:
            name = args.thread.name if args.thread is not None else threading.get_ident()
            sys.stderr.write(f"Exception in thread {name}:\n"
                             + format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        threading.__excepthook__(args)


def install_excepthooks() -> None:
    """Mask uncaught-exception tracebacks. Only replaces the interpreter DEFAULT hooks, so a hook
    someone else installed (a test runner, a debugger) is left in place."""
    if sys.excepthook is sys.__excepthook__:
        sys.excepthook = _sys_excepthook
    if threading.excepthook is threading.__excepthook__:
        threading.excepthook = _threading_excepthook


def install() -> None:
    """Everything above, for a process. Called on ``import omniseek`` so every entry point is covered."""
    install_log_redaction()
    install_excepthooks()
