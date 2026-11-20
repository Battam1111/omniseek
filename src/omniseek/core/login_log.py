"""Login verdicts on disk, and one push per logged-out account.

Why. On 11-18 four login sources were found logged out and nothing on disk said what the sites had
answered: the watchdog and the warmer keep only their last result, the adapters only a diagnostic in
memory. And one account's logout was pushed four times in a day (一亩三分地: the adapter's own cooldown
lives in process memory, so each process and each restart pushed again; the warmer has a cooldown of
its own).

What it keeps, both in <state>:
  * ``login-verdicts.jsonl``: one line each time the watchdog, the warmer or a login source's adapter
    decides whether an account is logged in: time, source, account, who decided (``by``: watchdog,
    warmer, adapter), the verdict (``in`` / ``out`` / ``unknown``) and the parts of the site's answer
    the verdict rests on (an HTTP status, an API's error code and message, a page's login-wall marks).
    Never the value of a cookie, token or request header: only whether one is there (``web_session:
    true``). The file is rotated and expired with OmniSeek's logs (infra_jobs.rotate_logs).
  * ``login-down.json``: which account is logged out since when, and whether that was pushed. An
    ``out`` verdict opens an entry, an ``in`` verdict closes it (no push on recovery); claim_push()
    says yes once per entry. The warmer and the adapters both push only through claim_push(), so one
    logout is pushed once, until the account is back in. A file lock keeps it right across OmniSeek's
    processes (the service, the isolated jobs).

Accounts: one per site and browser profile (ACCOUNTS); zhihu and zhihu_users share the 知乎 login.
Nothing here raises: a failed write is logged at debug and the caller goes on.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from omniseek import where

log = logging.getLogger(__name__)

VERDICTS_FILE = "login-verdicts.jsonl"
DOWN_FILE = "login-down.json"
_LOCK_FILE = "login-down.lock"

# source name -> account (site @ browser profile). The warmer's 小号-rednote warms the xiaohongshu account.
ACCOUNTS = {
    "zhihu": "知乎@9222",
    "zhihu_users": "知乎@9222",
    "yipinsanfendi": "一亩三分地@9222",
    "douban_groups": "豆瓣@9222",
    "xianyu": "闲鱼小号@9222",
    "xiaohongshu": "小红书小号@9223",
}

_MAX_STR = 300   # a site's message is kept up to this many characters


def account_of(source: str) -> str:
    return ACCOUNTS.get(source, "")


def verdicts_path():
    return where.state() / VERDICTS_FILE


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in list(v)[:20]]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in list(v.items())[:20]}
    s = str(v)
    return s if len(s) <= _MAX_STR else s[:_MAX_STR] + "…"


def record(source: str, verdict: str, *, by: str, **seen: Any) -> None:
    """Append one verdict line; ``verdict`` is "in", "out" or "unknown", ``seen`` the fields of the
    site's answer it rests on (scalars, short; never a cookie, token or header value). An "out" or "in"
    also opens or closes the account's entry in login-down.json."""
    account = account_of(source)
    try:
        from omniseek import redact
        line = {"at": _now(), "source": source, "account": account, "by": by, "verdict": verdict,
                "seen": redact.redact_obj(_clean(seen))}
        p = verdicts_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 -- the record is best-effort, never breaks a check
        log.debug("login verdict not written (%s): %r", source, exc)
    if account and verdict in ("in", "out"):
        _mark(account, verdict, source)


@contextmanager
def _locked():
    d = where.state()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / _LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_down() -> dict:
    try:
        data = json.loads((where.state() / DOWN_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_down(data: dict) -> None:
    p = where.state() / DOWN_FILE
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _mark(account: str, verdict: str, source: str) -> None:
    try:
        with _locked():
            down = _read_down()
            if verdict == "in":
                if account not in down:
                    return
                down.pop(account)
            else:
                if account in down:
                    return
                down[account] = {"since": _now(), "first_seen_by": source, "pushed_at": None}
            _write_down(down)
    except Exception as exc:  # noqa: BLE001
        log.debug("login-down not updated (%s): %r", account, exc)


def claim_push(source: str) -> bool:
    """True exactly once per logout of ``source``'s account: the caller then pushes. Opens the entry
    when no "out" verdict did yet. False for a source without an account (the caller keeps its own
    rule), or when the state cannot be read or written (a push missed is better than one per call)."""
    account = account_of(source)
    if not account:
        return False
    try:
        with _locked():
            down = _read_down()
            entry = down.setdefault(account, {"since": _now(), "first_seen_by": source, "pushed_at": None})
            if entry.get("pushed_at"):
                return False
            entry["pushed_at"] = _now()
            _write_down(down)
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("login-down unreadable, push for %s withheld: %r", account, exc)
        return False
