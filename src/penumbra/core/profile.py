"""Per-deployment PROFILE: the thin host-side overlay that turns the shipped engine into THIS
deployment (which sources are exposed, polite-pool contact, extra red-lines, rate knobs).

Lives at ``~/.penumbra/profile.json`` (or ``PENUMBRA_PROFILE_PATH``), OUTSIDE the source tree — so a
cold ``git clone`` carries ZERO deployer identity. ABSENT or corrupt -> the all-default profile =
exactly the pre-profile behavior (every registered source usable, today's deadlines/redlines), so
the engine always boots and an existing host is unaffected until it writes a profile. The shipped
``profile.example.json`` is the documented starting point a deployer copies (broad-default + walled
OFF). See README.md (Configure) and SECURITY.md.

RAZOR: this only STORES the deployer's declared choices and returns them; it renders no judgment.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

_PATH = Path(os.environ.get(
    "PENUMBRA_PROFILE_PATH", str(Path.home() / ".penumbra" / "profile.json")))

_cache: "Optional[dict]" = None
_lock = threading.Lock()


def _load() -> dict:
    """Read + cache the profile once. Fail-OPEN to {} (all-default) on missing/corrupt — never
    crash the engine on a bad profile."""
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                data: dict = {}
                try:
                    if _PATH.is_file():
                        d = json.loads(_PATH.read_text(encoding="utf-8"))
                        if isinstance(d, dict):
                            data = d
                except Exception:  # noqa: BLE001 — any read/parse error -> all-default
                    data = {}
                _cache = data
    return _cache


def invalidate() -> None:
    """Drop the cached profile so the next read re-loads (live edit without restart)."""
    global _cache
    with _lock:
        _cache = None


def active() -> bool:
    """True iff a non-empty profile file is present (callers keep pre-profile behavior when False)."""
    return bool(_load())


def _section(name: str) -> dict:
    v = _load().get(name)
    return v if isinstance(v, dict) else {}


def is_source_enabled(name: str, *, stability: str = "",
                      domains: "Optional[list]" = None, regions: "Optional[list]" = None,
                      kind: str = "") -> bool:
    """Whether THIS deployment EXPOSES source ``name`` (broad fan-out + penumbra_fetch). No profile ->
    True (pre-profile behavior). With a profile, most-specific-wins:
      sources.enable (explicit on)  >  sources.disable (explicit off)  >  walled tier gate
      (walled.enabled + walled.bring_your_own[name])  >  groups.disable_{stability,kinds,domains,
      regions}  >  sources.default_enabled (default True).
    Facets are passed in by the caller (fetcher derives them) so this module stays core-import-free."""
    p = _load()
    if not p:
        return True

    src = _section("sources")
    if name in set(src.get("enable", []) or []):
        return True
    if name in set(src.get("disable", []) or []):
        return False

    # Walled/login tier: OFF unless the deployer turned it on AND opted in for this source. Derived
    # from the adapter's STABILITY (not its per-adapter flag), so a new walled source can't leak in.
    if stability == "walled":
        w = _section("walled")
        if not w.get("enabled", False):
            return False
        if not (w.get("bring_your_own") or {}).get(name, False):
            return False

    groups = _section("groups")
    if stability and stability in set(groups.get("disable_stability", []) or []):
        return False
    if kind and kind in set(groups.get("disable_kinds", []) or []):
        return False
    if set(domains or []) & set(groups.get("disable_domains", []) or []):
        return False
    if set(regions or []) & set(groups.get("disable_regions", []) or []):
        return False

    return bool(src.get("default_enabled", True))


def contact_email_fallback() -> "Optional[str]":
    """Polite-pool contact email IF the profile sets one (auth.contact_email reads creds + env
    FIRST; this is only the last fallback before the RFC-2606 placeholder)."""
    v = _load().get("contact_email")
    return v if isinstance(v, str) and v.strip() else None


def extra_redlines() -> list:
    """Deployer-APPENDED red-line entries (same shape as redlines.json), ADDITIVE to the engine
    baseline (which always loads). Drops a malformed list -> []."""
    v = _section("safety").get("redline_denylist_extra")
    return v if isinstance(v, list) else []


def rate_knob(name: str, default: Any) -> Any:
    """A rate/deadline override from the profile's ``rate`` block, else ``default`` (today's
    literal). E.g. rate_knob('broad_deadline_s', 11.0)."""
    v = _section("rate").get(name)
    return v if v is not None else default
