"""Optional-dependency import helper: turn a missing EXTRA into a clear, actionable message
instead of a raw ImportError / ModuleNotFoundError.

The lean core install (`pip install -e .`) is Apache-clean and omits the heavy / AGPL / walled
deps (see pyproject [project.optional-dependencies]). A feature whose extra is absent should fail
OPEN with "install omniseek[<extra>]" the FIRST TIME a deployer actually calls it — never on
server import (so the engine always boots). Call this at the lazy import site inside the feature."""
from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import shutil
from types import ModuleType
from typing import Optional

# One import per extra that says the extra is installed, in pyproject order (the boot line's
# "installed / not installed" list reads the same table).
EXTRA_MODULES = {
    "pdf": "fitz",
    "asr": "funasr",
    "recall": "sentence_transformers",
    "ocr": "rapidocr_onnxruntime",
    "walled": "patchright.sync_api",
}


def extra_installed(extra: str, probe=importlib.util.find_spec) -> bool:
    try:
        return probe(EXTRA_MODULES[extra]) is not None
    except Exception:  # noqa: BLE001: a missing parent package raises instead of returning None
        return False


def has_cuda_gpu() -> bool:
    """Cheap, side-effect-free NVIDIA presence check (no torch needed: it may be what is missing)."""
    return bool(shutil.which("nvidia-smi")) or os.path.exists("/proc/driver/nvidia/version")


def uv_install(extra: str, system: Optional[str] = None, cuda: Optional[bool] = None,
               probe=importlib.util.find_spec) -> str:
    """The `uv tool install` command that adds ``extra`` and keeps what is already there. A second
    `uv tool install` re-creates the tool's environment from that command alone (uv docs, Tools,
    "Upgrading tools": "re-created entirely via subsequent `uv tool install` operations"), so the
    command names every extra already installed plus the new one, and for asr torch again (the
    extra leaves torch out, see asr.py). Linux picks the torch build with uv's --torch-backend (cpu
    without an NVIDIA GPU, auto with one); macOS and Windows need no flag."""
    extras = [e for e in EXTRA_MODULES if e == extra or extra_installed(e, probe)]
    cmd = f'uv tool install "omniseek[{",".join(extras)}]"'
    if "asr" in extras:
        cmd += " --with torch --with torchaudio"
        if (system or platform.system()).lower() == "linux":
            cuda = has_cuda_gpu() if cuda is None else cuda
            cmd += " --torch-backend auto" if cuda else " --torch-backend cpu"
    return cmd


def install_hint(extra: str) -> str:
    """How to add an extra, in the words the install guide uses. The uv command comes first: a tool
    installed with `uv tool install` has no pip of its own on PATH, so a pip line cannot reach it.
    The pip line stays as the second way, for an install made with pip (pip adds to what is there)."""
    return f"{uv_install(extra)}, or with pip: pip install 'omniseek[{extra}]'"


def require(module_name: str, extra: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"this feature needs the optional '{extra}' dependencies (missing "
            f"'{module_name}'): {install_hint(extra)}") from exc
