"""Optional-dependency import helper: turn a missing EXTRA into a clear, actionable message
instead of a raw ImportError / ModuleNotFoundError.

The lean core install (`pip install -e .`) is Apache-clean and omits the heavy / AGPL / walled
deps (see pyproject [project.optional-dependencies]). A feature whose extra is absent should fail
OPEN with "install omniseek-mcp[<extra>]" the FIRST TIME a deployer actually calls it — never on
server import (so the engine always boots). Call this at the lazy import site inside the feature."""
from __future__ import annotations

import importlib
from types import ModuleType


def require(module_name: str, extra: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"this feature needs the optional '{extra}' dependencies (missing "
            f"'{module_name}'): pip install 'omniseek-mcp[{extra}]'") from exc
