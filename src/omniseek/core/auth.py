"""Credentials loader for OmniSeek source adapters.

Credentials live in <credentials>/<source>.json (outside the
project directory, so they are never accidentally committed). Each
adapter that needs credentials calls load(<source>) and gets back a
dict — or None if the file doesn't exist.

This module only READS that directory; it never creates it and never writes into it.
Since the cutover (2026-10-15) the directory belongs to the credential tool. An
adapter that needs credentials declares the expected file shape with register_template()
at import: the template goes into the in-memory TEMPLATES registry, nothing touches the
disk, and the human-readable list is docs/credential-templates.md (rendered from TEMPLATES
by `scripts/creds_doctor.py --templates`).

A credentials directory this process may not read (PermissionError, or an OSError
whose errno is EACCES / EPERM: e.g. a sandbox that denies the path) reads as "no
credentials": load() returns None, is_configured() False, list_configured() [].
The reason is logged once at DEBUG; nothing is raised into the importing module.
A missing directory reads the same way, silently. Any other OSError still propagates.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional
from omniseek import where

log = logging.getLogger(__name__)

CREDS_DIR = where.credentials()

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


# source -> the credential file's expected shape. Filled by register_template() at import.
TEMPLATES: dict = {}


def _dir_usable() -> bool:
    """Is the credentials directory there to read? Missing answers False quietly; a permission-class
    failure answers False (logged once) instead of raising. Never creates the directory."""
    try:
        st = CREDS_DIR.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        if not _is_denied(exc):
            raise
        _note_denied(exc)
        return False
    return stat.S_ISDIR(st.st_mode)


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
# (<credentials>/contact.json -> {"email": "..."} or the OMNISEEK_CONTACT_EMAIL env var);
# unconfigured it degrades to an RFC-2606 reserved placeholder, so a cold checkout still forms a
# valid UA and the tree ships with no personal data.
_CONTACT_DEFAULT = "omniseek@example.com"


def contact_email() -> str:
    """The contact email OmniSeek puts in its outbound User-Agents. Host-injected, never committed."""
    creds = load("contact") or {}
    return creds.get("email") or os.environ.get("OMNISEEK_CONTACT_EMAIL") or _CONTACT_DEFAULT


def register_template(source: str, template: dict) -> None:
    """Declare the shape of <credentials>/<source>.json. In memory only: writes no file."""
    TEMPLATES[source] = template


TEMPLATE_DOC = "docs/credential-templates.md"


def template_hint(source: str) -> str:
    """Where a person finds the template of one source (for a "not configured" message)."""
    return f"template: {TEMPLATE_DOC}, section {source}"


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
