"""Credentials loader for OmniSeek source adapters.

Credentials live in ~/.omniseek/credentials/<source>.json (outside the
project directory, so they are never accidentally committed). Each
adapter that needs credentials calls load(<source>) and gets back a
dict — or None if the file doesn't exist.

To set up credentials, adapters call write_template() once on first
import to drop a .template file the user can copy and fill in.

A credentials directory this process may not read (PermissionError, or an OSError
whose errno is EACCES / EPERM: e.g. a sandbox that denies the path) reads as "no
credentials": load() returns None, is_configured() False, list_configured() [], and
write_template() writes nothing. The reason is logged once at DEBUG; nothing is raised
into the importing module. Any other OSError still propagates, as before.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CREDS_DIR = Path.home() / ".omniseek" / "credentials"

_DENIED_ERRNOS = (errno.EACCES, errno.EPERM)
_denied_logged: set[str] = set()


def _is_denied(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and getattr(exc, "errno", None) in _DENIED_ERRNOS)


def _note_denied(exc: OSError) -> None:
    key = str(CREDS_DIR)
    if key in _denied_logged:
        return
    _denied_logged.add(key)
    log.debug("credentials dir %s not readable (%s: %s); treating every source as unconfigured",
              CREDS_DIR, type(exc).__name__, exc)


def ensure_dir() -> Path:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    return CREDS_DIR


def _dir_usable() -> bool:
    """ensure_dir(), but a permission-class failure answers False (logged once) instead of raising."""
    try:
        ensure_dir()
    except OSError as exc:
        if not _is_denied(exc):
            raise
        _note_denied(exc)
        return False
    return True


def load(source: str) -> Optional[dict]:
    """Load credentials for the given source. Returns None if not configured."""
    if not _dir_usable():
        return None
    path = CREDS_DIR / f"{source}.json"
    try:
        if not path.exists():
            return None
    except OSError as exc:
        if not _is_denied(exc):
            raise
        _note_denied(exc)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# A contact email for polite-pool / fair-access User-Agents (OpenAlex, SEC, Unpaywall, Crossref).
# This is PII and must never be hardcoded in the tree. The real address lives only on the host
# (~/.omniseek/credentials/contact.json -> {"email": "..."} or the OMNISEEK_CONTACT_EMAIL env var);
# unconfigured it degrades to an RFC-2606 reserved placeholder, so a cold checkout still forms a
# valid UA and the tree ships with no personal data.
_CONTACT_DEFAULT = "omniseek@example.com"


def contact_email() -> str:
    """The contact email OmniSeek puts in its outbound User-Agents. Host-injected, never committed."""
    creds = load("contact") or {}
    return creds.get("email") or os.environ.get("OMNISEEK_CONTACT_EMAIL") or _CONTACT_DEFAULT


def write_template(source: str, template: dict, force: bool = False) -> Path:
    """Drop a credential template at ~/.omniseek/credentials/<source>.json.template

    Templates are NEVER overwritten if they already exist (unless force=True).
    Real credentials at <source>.json are never touched. An unreadable credentials
    directory writes nothing (the would-be path is still returned).
    """
    path = CREDS_DIR / f"{source}.json.template"
    if not _dir_usable():
        return path
    try:
        if path.exists() and not force:
            return path
        path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        if not _is_denied(exc):
            raise
        _note_denied(exc)
    return path


def is_configured(source: str) -> bool:
    """Cheap check: is <source>.json present?"""
    try:
        return (CREDS_DIR / f"{source}.json").exists()
    except OSError as exc:
        if not _is_denied(exc):
            raise
        _note_denied(exc)
        return False


def list_configured() -> list[str]:
    """List sources that have credential files."""
    if not _dir_usable():
        return []
    try:
        return [p.stem for p in CREDS_DIR.glob("*.json")]
    except OSError as exc:
        if not _is_denied(exc):
            raise
        _note_denied(exc)
        return []
