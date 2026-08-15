"""The ONE fail-open notification primitive the eye's own daemons share.

Extracted from sensor.py's push helper (P6) so the sensor tap and the generalized job registry push
through ONE implementation instead of duplicating the credential read. The external sentinel keeps
its OWN copy in scripts/_sentinel_common (it must work when the organ's code is broken, so it may
never import omniseek.*); this module is the IN-PROCESS half, used only by code already running
inside the writer process.

2026-08-12: Bark is RETIRED and deleted from the fleet. It had been unreachable from the mini
(three probes, the connection never establishing, 20s timeouts) while every infra alarm pushed to
it and to nothing else, so alarms were written, counted, logged as pushed, and delivered nowhere.
WeCom answers in 0.06s and is Captain's actual channel (desktop + phone). One channel, and it works.

FAIL-OPEN contract: an absent credentials file, a missing webhook, or any HTTP/parse failure is a
log line and a return, NEVER an exception. A push failure must never break the run that emitted it.
But it is reported rather than swallowed: alert() returns whether the alarm landed and records a
durable marker, because a siren nobody hears is worse than no siren, the quiet reading as calm.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

_WECOM_CREDS_PATH = Path.home() / ".omniseek" / "credentials" / "wecom.json"
_ALERT_DELIVERY_PATH = Path.home() / ".omniseek" / "state" / "alert-delivery.json"


def wecom_push(title: str, body: str) -> bool:
    """POST one 企业微信 (WeCom) group-robot MARKDOWN message via the webhook in
    ~/.omniseek/credentials/wecom.json ({webhook_url}). Captain's channel (desktop + phone).
    Returns True only if the POST was actually made. qyapi.weixin.qq.com routes DIRECT (mainland)
    through the mini's Clash; the group-robot API rate-limits ~20 msg/min, so a caller pushing more
    than a couple messages a minute must pace itself (a weekly digest is one message, so no pacing
    here). WeCom markdown content hard-caps ~4096 BYTES, so the body is truncated byte-safely
    (CJK is 3 bytes/char)."""
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
    content = (f"**{title}**\n\n{body}" if title else body)
    enc = content.encode("utf-8")
    if len(enc) > 4000:  # stay safely under WeCom's ~4096-byte markdown cap (byte-safe, not char-safe)
        content = enc[:4000].decode("utf-8", errors="ignore")
    try:
        import httpx
        httpx.post(url, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=5.0)
        return True
    except Exception as exc:  # noqa: BLE001 -- best-effort; a push failure never breaks the run
        log.debug("wecom push failed (%s)", exc)
        return False


def alert(title: str, body: str, **_ignored) -> list:
    """Deliver ONE alarm. Returns the channels that took it (empty means nobody heard it).

    ``_ignored`` absorbs the retired Bark hints (group / level) so a caller that still passes them
    keeps working while the fleet converges; they mean nothing to WeCom.

    A lane that delivered NOTHING leaves a WARNING plus a durable marker with a running streak, so
    the daily off-machine audit can surface a disconnected siren. That marker is the whole reason
    this wrapper exists rather than callers pushing directly: an alarm channel is itself a guard,
    and an unwatched guard is the failure this codebase spent 2026-08-11 learning about."""
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
