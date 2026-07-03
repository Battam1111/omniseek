"""Bark push for the in-process scheduler (P9): the ONE fail-open notification primitive the
eye's own daemons share.

Extracted from sensor.py's _bark_push (P6) so both the sensor tap (sensor.py) and the generalized
job registry (jobs.py) push through ONE implementation instead of duplicating the credential read.
The external sentinel keeps its OWN copy in scripts/_sentinel_common (it must work when the organ's
code is broken, so it may never import penumbra.*); this module is the IN-PROCESS half, used only by
code already running inside the writer process.

FAIL-OPEN contract (unchanged from P6): an absent credentials file, a missing device_key, or any
HTTP/parse failure is a debug log and a silent return, NEVER an exception. A push failure must never
break the run that emitted it. Reads ~/.penumbra/credentials/bark.json ({device_key, api_base}) the
way auth.load() reads a source credential file. The GROUP string is spelled "Penumbra" so the
penumbra sync's Penumbra->Penumbra rename lands correctly on both sides.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_BARK_CREDS_PATH = Path.home() / ".penumbra" / "credentials" / "bark.json"


def bark_push(title: str, body: str, *, group: str = "Penumbra", level: str = "active") -> None:
    """POST one Bark notification (~3s timeout). FAIL-OPEN: an absent credentials file, a missing
    device_key, or any HTTP/parse failure is a debug log and a silent return, NEVER an exception.
    Uses the eye's EXISTING httpx dependency (in-process, so httpx is always importable here).
    ``level``: Bark's urgency hint, a transplant of the old _sentinel_common callers' distinction;
    "active" (default) behaves like an alarm, "passive" is the quiet no-screen-wake lane the
    periodic report pushes (digest / source-audit / curator) deliberately use."""
    try:
        if not _BARK_CREDS_PATH.exists():
            log.debug("bark push skipped: no credentials at %s", _BARK_CREDS_PATH)
            return
        creds = json.loads(_BARK_CREDS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- unreadable creds -> no-op, never raise
        log.debug("bark push skipped: credentials unreadable (%s)", exc)
        return
    key = (creds or {}).get("device_key")
    api = (creds or {}).get("api_base") or "https://api.day.app"
    if not key:
        log.debug("bark push skipped: no device_key in credentials")
        return
    try:
        import httpx
        httpx.post(f"{api.rstrip('/')}/{key}",
                   json={"title": title, "body": body, "group": group, "level": level},
                   timeout=3.0)
    except Exception as exc:  # noqa: BLE001 -- the push is best-effort; a failure never breaks a run
        log.debug("bark push failed (%s)", exc)
