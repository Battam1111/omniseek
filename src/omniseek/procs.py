"""Where OmniSeek reads process facts: omo_proc, the ONE place that asks the kernel about processes.

It is loaded from omo_proc.py next to this file. OMNISEEK_OMO_PROC, when set, is the only path tried.

Fail-open: lib() returns None when no copy loads and why() says which paths were tried; callers treat
that as "cannot read processes" (memguard disables itself, the clone sweep deletes nothing).
"""

from __future__ import annotations

import importlib.util
import os
from types import ModuleType
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE: dict = {}


def paths() -> List[str]:
    env = os.environ.get("OMNISEEK_OMO_PROC")
    if env:
        return [env]
    return [os.path.join(_HERE, "omo_proc.py")]


def lib() -> Optional[ModuleType]:
    """The loaded omo_proc module (once per process), or None when no copy loads."""
    if "mod" in _STATE:
        return _STATE["mod"]
    tried = []
    for p in paths():
        if not os.path.isfile(p):
            tried.append("%s (missing)" % p)
            continue
        try:
            spec = importlib.util.spec_from_file_location("omo_proc", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 -- a broken copy means "cannot read", never a crash
            tried.append("%s (%r)" % (p, exc))
            continue
        _STATE["mod"] = mod
        return mod
    _STATE["why"] = "omo_proc not loadable: " + "; ".join(tried)
    return None


def why() -> str:
    return _STATE.get("why", "")


def reset() -> None:
    """Forget the loaded module (tests)."""
    _STATE.clear()
