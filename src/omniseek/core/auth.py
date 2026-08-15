"""Credentials loader for OmniSeek eye source adapters.

Credentials live in ~/.omniseek/credentials/<source>.json (outside the
project directory, so they are never accidentally committed). Each
adapter that needs credentials calls load(<source>) and gets back a
dict — or None if the file doesn't exist.

To set up credentials, adapters call write_template() once on first
import to drop a .template file the user can copy and fill in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

CREDS_DIR = Path.home() / ".omniseek" / "credentials"


def ensure_dir() -> Path:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    return CREDS_DIR


def load(source: str) -> Optional[dict]:
    """Load credentials for the given source. Returns None if not configured."""
    ensure_dir()
    path = CREDS_DIR / f"{source}.json"
    if not path.exists():
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
    """The contact email the eye puts in its outbound User-Agents. Host-injected, never committed."""
    creds = load("contact") or {}
    return creds.get("email") or os.environ.get("OMNISEEK_CONTACT_EMAIL") or _CONTACT_DEFAULT


def write_template(source: str, template: dict, force: bool = False) -> Path:
    """Drop a credential template at ~/.omniseek/credentials/<source>.json.template

    Templates are NEVER overwritten if they already exist (unless force=True).
    Real credentials at <source>.json are never touched.
    """
    ensure_dir()
    path = CREDS_DIR / f"{source}.json.template"
    if path.exists() and not force:
        return path
    path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def is_configured(source: str) -> bool:
    """Cheap check: is <source>.json present?"""
    return (CREDS_DIR / f"{source}.json").exists()


def list_configured() -> list[str]:
    """List sources that have credential files."""
    ensure_dir()
    return [p.stem for p in CREDS_DIR.glob("*.json")]
