"""Validation for the immutable build identity embedded by git archive.

A RELEASED tree (git archive | tar, the C* deploy path) carries the exact source commit,
substituted into build_id.py by export-subst. A CHECKOUT or a docker build context carries the
raw placeholder instead, and that tree still needs a truthful identity (the public mirror's
`docker compose up` runs exactly this shape), so ``resolve_build_id`` falls back to a
deterministic fingerprint of the package's own source bytes: 40 hex chars, stable for identical
trees, different for different code. The strict validator stays for the release path; nothing
ever invents a git hash.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from omniseek.core.contracts.build_id import EYE_BUILD_ID

_BUILD_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PLACEHOLDER = "$Format:%H$"
_cached_fingerprint: Optional[str] = None


def validate_build_id(value: str) -> str:
    if value == _PLACEHOLDER:
        raise ValueError("build identity must be archive-substituted")
    if not isinstance(value, str) or _BUILD_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("build identity must be 40 lowercase hexadecimal characters")
    return value


def _source_fingerprint() -> str:
    """Deterministic 40-hex identity for an UNRELEASED tree: sha1 over every .py and .json file
    of the installed package (sorted relative path + content). It identifies the code actually
    running; pasting it into git finds nothing, and that is honest: this tree is not a release."""
    global _cached_fingerprint
    if _cached_fingerprint is None:
        root = Path(__file__).resolve().parents[1]
        digest = hashlib.sha1()
        files = [p for pat in ("*.py", "*.json") for p in root.rglob(pat)
                 if "__pycache__" not in p.as_posix()]
        for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        _cached_fingerprint = digest.hexdigest()
    return _cached_fingerprint


def current_build_id() -> str:
    return validate_build_id(EYE_BUILD_ID)


def resolve_build_id() -> str:
    """The archived commit when this tree is a release; otherwise the source fingerprint."""
    if EYE_BUILD_ID != _PLACEHOLDER:
        return validate_build_id(EYE_BUILD_ID)
    return _source_fingerprint()
