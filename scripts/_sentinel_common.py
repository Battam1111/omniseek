"""Shared helpers for OmniSeek sentinels (content watchtower + health watchdog).

Bark push + JSON state load/save + a cooldown gate. Extracted so the two
launchd sentinels share one implementation instead of duplicating it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

BARK_CREDS = Path.home() / ".omniseek" / "credentials" / "bark.json"
WECOM_CREDS = Path.home() / ".omniseek" / "credentials" / "wecom.json"


def wecom_push(title: str, body: str, **_ignored) -> bool:
    """Push to the 企业微信 group robot (markdown). Returns True on success, False quietly.
    the operator's primary channel (desktop + phone); webhook_url lives in ~/.omniseek/credentials/wecom.json."""
    try:
        url = json.loads(WECOM_CREDS.read_text()).get("webhook_url")
    except Exception:
        print("  (no wecom creds — skipping push)")
        return False
    if not url:
        return False
    md = f"## {title}\n{body}"
    data = json.dumps({"msgtype": "markdown", "markdown": {"content": md}}).encode("utf-8")
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=8) as r:
                resp = json.loads(r.read().decode())
            if resp.get("errcode") == 0:
                return True
            last = str(resp)
        except Exception as exc:
            last = str(exc)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    print(f"  wecom push failed after 3 tries: {last}")
    return False


def push(channel: str, title: str, body: str, *, group: str = "OmniSeek", level: str = "active") -> bool:
    """Dispatch a push to a channel: 'bark' | 'wecom' | 'both' | 'wecom+bark'. True if any sent."""
    want_bark = ("bark" in channel) or (channel == "both")
    want_wecom = ("wecom" in channel) or (channel == "both")
    ok = False
    if want_bark:
        ok = bark_push(title, body, group=group, level=level) or ok
    if want_wecom:
        ok = wecom_push(title, body) or ok
    return ok


def bark_push(title: str, body: str, *, group: str = "OmniSeek", level: str = "active") -> bool:
    """Push a Bark notification. Returns True on success, False (quietly) otherwise."""
    try:
        creds = json.loads(BARK_CREDS.read_text())
    except Exception:
        print("  (no bark creds — skipping push)")
        return False
    key = creds.get("device_key")
    api = creds.get("api_base", "https://api.day.app")
    if not key:
        return False
    data = json.dumps({"title": title, "body": body, "group": group, "level": level}).encode("utf-8")
    last = ""
    for attempt in range(3):  # small retry — a transient blip shouldn't silently drop an alert
        try:
            req = urllib.request.Request(
                f"{api}/{key}", data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=8)
            return True
        except Exception as exc:
            last = str(exc)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    print(f"  bark push failed after 3 tries: {last}")
    return False


def load_state(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        # Do NOT silently return {} — for a sentinel that WIPES the seen-set and re-fires
        # every historical item as "new" (a Bark flood). Quarantine the corrupt file + warn
        # so it's visible. (The atomic save_state below makes this rare: a kill/power-loss
        # can no longer truncate the live file mid-write.)
        try:
            backup = p.with_suffix(p.suffix + ".corrupt")
            p.replace(backup)
            print(f"  WARN: state file corrupt → quarantined {backup.name}: {exc}")
        except Exception as exc2:
            print(f"  WARN: state file corrupt + quarantine failed: {exc2}")
        return {}


def save_state(path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, p)  # atomic: a kill mid-write can never truncate the real state file
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def should_alert(key: str, alerts: dict, cooldown: int) -> bool:
    """True at most once per `cooldown` seconds for `key` (mutates `alerts`)."""
    if time.time() - alerts.get(key, 0) < cooldown:
        return False
    alerts[key] = time.time()
    return True
