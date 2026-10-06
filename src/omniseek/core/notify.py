"""The ONE fail-open notification primitive OmniSeek's own daemons share.

Extracted from sensor.py's push helper (P6) so the sensor tap and the generalized job registry push
through ONE implementation instead of duplicating the credential read. The external sentinel keeps
its OWN copy in scripts/_sentinel_common (it must work when the organ's code is broken, so it may
never import omniseek.*); this module is the IN-PROCESS half, used only by code already running
inside the writer process.

2026-08-12: Bark is RETIRED and deleted from the fleet. It had been unreachable from the mini
(three probes, the connection never establishing, 20s timeouts) while every infra alarm pushed to
it and to nothing else, so alarms were written, counted, logged as pushed, and delivered nowhere.
WeCom answers in 0.06s and is the operator's actual channel (desktop + phone). One channel, and it works.

FAIL-OPEN contract: an absent credentials file, a missing webhook, or any HTTP/parse failure is a
log line and a return, NEVER an exception. A push failure must never break the run that emitted it.
But it is reported rather than swallowed: alert() returns whether the alarm landed and records a
durable marker, because a siren nobody hears is worse than no siren, the quiet reading as calm.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

_WECOM_CREDS_PATH = Path.home() / ".omniseek" / "credentials" / "wecom.json"
_ALERT_DELIVERY_PATH = Path.home() / ".omniseek" / "state" / "alert-delivery.json"
# The system push outlet (one ledger for every push to the operator); OMNISEEK_NOTIFY_OUTLET overrides.
# A build with no outlet sets this to None: with the variable unset too, pushes go out directly.
_OUTLET_DEFAULT = None  # the public build ships no default outlet
_OUTLET_CACHE: dict = {}


def _outlet_path() -> Path | None:
    """The configured outlet file, or None when no outlet is configured (a normal state)."""
    path = os.environ.get("OMNISEEK_NOTIFY_OUTLET") or _OUTLET_DEFAULT
    return Path(path).expanduser() if path else None


def _load_outlet():
    """Load the system push outlet (an external push outlet) BY FILE PATH, never via sys.path: the outlet
    belongs to neither OmniSeek nor the sentinel. Returns None when no outlet is configured (nothing
    is loaded). A successful load is cached per path; a failed one raises, so the caller falls back
    to the direct send and the next push tries again."""
    configured = _outlet_path()
    if configured is None:
        return None
    path = str(configured)
    mod = _OUTLET_CACHE.get(path)
    if mod is not None:
        return mod
    if not os.path.isfile(path):
        raise FileNotFoundError(f"outlet not found: {path}")
    spec = importlib.util.spec_from_file_location("push_outlet", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"outlet not loadable: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "send", None)):
        raise ImportError(f"outlet has no send(): {path}")
    _OUTLET_CACHE[path] = mod
    return mod


def _caller_source() -> str:
    """``eye.<short module name>`` of the first frame outside this module (alert() and wecom_push
    both live here, so an alert() from sensor.py reads ``eye.sensor``)."""
    try:
        f = sys._getframe(1)
        while f is not None and f.f_globals.get("__name__") == __name__:
            f = f.f_back
        name = (f.f_globals.get("__name__") if f is not None else "") or ""
        return "eye." + (name.rsplit(".", 1)[-1] or "unknown")
    except Exception:  # noqa: BLE001 -- a label must never break a push
        return "eye.unknown"


def _direct_push(url: str, content: str) -> bool:
    """The pre-outlet send, kept as the fallback for when the outlet cannot be reached."""
    try:
        from omniseek.core import http, upstreams
        # The declared WeCom robot gate: at most 20 messages a minute per robot (qyapi.weixin.qq.com,
        # developer doc 91770). A burst of alerts past that queues up to the gate's wait, then is
        # dropped here (returns False) instead of being rejected by WeCom.
        with upstreams.egress(url, request_s=5.0):
            r = http.direct("POST", url, json={"msgtype": "markdown", "markdown": {"content": content}},
                            timeout=5.0)
        upstreams.observe_response(url, r)
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort; a push failure never breaks the run
        log.debug("wecom push failed (%s)", exc)
        return False


def wecom_push(title: str, body: str) -> bool:
    """POST one 企业微信 (WeCom) group-robot MARKDOWN message via the webhook in
    ~/.omniseek/credentials/wecom.json ({webhook_url}). the operator's channel (desktop + phone).
    Returns True only if the message was actually sent. WeCom markdown content hard-caps ~4096
    BYTES, so the content is truncated byte-safely (CJK is 3 bytes/char).

    2026-10-06: the send goes through the system push outlet (an external push outlet; path overridable
    by OMNISEEK_NOTIFY_OUTLET) so every push to the operator lands in its one ledger (sent.jsonl), tagged
    ``eye.<caller module>``. With no outlet configured (no OMNISEEK_NOTIFY_OUTLET and no default) the
    message goes out directly, quietly: that is a normal state, not a failure. OmniSeek's own masking and its declared WeCom gate (20 msg/min) still run
    first, and the masked title and body are what the outlet gets. If the outlet cannot be reached
    (file missing, load failure, send() raising) the message goes out the old direct way with a
    warning: an alarm is never dropped for the outlet's sake. If the outlet answers False (its own
    rate limit or a failed send) nothing else is sent, so its limit is not bypassed and nothing is
    sent twice."""
    try:
        if not _WECOM_CREDS_PATH.exists():
            log.debug("wecom push skipped: no credentials at %s", _WECOM_CREDS_PATH)
            return False
        url = (json.loads(_WECOM_CREDS_PATH.read_text(encoding="utf-8")) or {}).get("webhook_url")
    except Exception as exc:  # noqa: BLE001 -- unreadable creds -> no-op, never raise
        log.debug("wecom push skipped: credentials unreadable (%s)", exc)
        return False
    if not url:
        log.debug("wecom push skipped: no webhook_url in credentials")
        return False
    source = _caller_source()
    from omniseek import redact as _redact
    title, body = _redact.redact(title), _redact.redact(body)  # every push leaves the machine here
    content = (f"**{title}**\n\n{body}" if title else body)
    content = _redact.redact(content)  # no key rides along
    enc = content.encode("utf-8")
    if len(enc) > 4000:  # stay safely under WeCom's ~4096-byte markdown cap (byte-safe, not char-safe)
        content = enc[:4000].decode("utf-8", errors="ignore")
    try:
        outlet = _load_outlet()
    except Exception as exc:  # noqa: BLE001 -- outlet unreachable: send directly, never drop the alarm
        log.warning("push outlet unreachable, sent directly: %s",
                    _redact.redact(f"{type(exc).__name__}: {exc}"))
        return _direct_push(url, content)
    if outlet is None:
        log.debug("no push outlet configured; sending directly")
        return _direct_push(url, content)
    failure = None
    try:
        from omniseek.core import upstreams
        with upstreams.egress(url, request_s=5.0):  # OmniSeek's declared WeCom gate, as before
            try:
                ok = outlet.send(source, title, body)
            except Exception as exc:  # noqa: BLE001 -- outlet broke mid-send: fall back below
                failure = exc
    except Exception as exc:  # noqa: BLE001 -- the gate refused the push: dropped, as before
        log.debug("wecom push failed (%s)", exc)
        return False
    if failure is not None:
        log.warning("push outlet unreachable, sent directly: %s",
                    _redact.redact(f"{type(failure).__name__}: {failure}"))
        return _direct_push(url, content)
    if not ok:
        log.warning("push outlet declined the push (returned False); not resent")
        return False
    return True


def alert(title: str, body: str, **_ignored) -> list:
    """Deliver ONE alarm. Returns the channels that took it (empty means nobody heard it).

    ``_ignored`` absorbs the retired Bark hints (group / level) so a caller that still passes them
    keeps working while the fleet converges; they mean nothing to WeCom.

    A lane that delivered NOTHING leaves a WARNING plus a durable marker with a running streak, so
    the daily off-machine audit can surface a disconnected siren. That marker is the whole reason
    this wrapper exists rather than callers pushing directly: an alarm channel is itself a guard,
    and an unwatched guard is the failure this codebase spent 2026-08-11 learning about."""
    from omniseek import redact as _redact
    title, body = _redact.redact(title), _redact.redact(body)
    delivered = ["wecom"] if wecom_push(title, body) else []
    try:
        prev = {}
        if _ALERT_DELIVERY_PATH.exists():
            prev = json.loads(_ALERT_DELIVERY_PATH.read_text(encoding="utf-8")) or {}
        _ALERT_DELIVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ALERT_DELIVERY_PATH.write_text(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "title": title[:80],
            "delivered": delivered,
            "undelivered_streak": 0 if delivered else int(prev.get("undelivered_streak", 0)) + 1,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must not break the alarm
        log.debug("alert delivery bookkeeping failed (%s)", exc)
    if not delivered:
        log.warning("ALERT NOT DELIVERED on any channel: %s | %s", title, body[:120])
    return delivered
