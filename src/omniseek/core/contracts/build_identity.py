"""Validation for the immutable Eye build identity embedded by git archive."""

from __future__ import annotations

import re

from omniseek.core.contracts.build_id import EYE_BUILD_ID


_BUILD_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def validate_build_id(value: str) -> str:
    if value == "$Format:%H$":
        raise ValueError("Eye build identity must be archive-substituted")
    if not isinstance(value, str) or _BUILD_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Eye build identity must be 40 lowercase hexadecimal characters")
    return value


def current_build_id() -> str:
    return validate_build_id(EYE_BUILD_ID)
