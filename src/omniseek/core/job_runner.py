"""Run one scheduler job in a disposable child process."""

from __future__ import annotations

import asyncio
import importlib
import sys
import traceback


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m omniseek.core.job_runner MODULE CALLABLE", file=sys.stderr)
        return 2
    module_name, callable_name = args
    try:
        target = getattr(importlib.import_module(module_name), callable_name)
        if not callable(target):
            raise TypeError(f"{module_name}.{callable_name} is not callable")
        target()
    except BaseException as exc:
        # A cancellation must propagate rather than become a plain non-zero exit: the parent kills
        # this process group on budget overrun, and swallowing the cancellation would report that
        # kill as an ordinary job failure. Required by the S0.2 AST tripwire, which this file
        # violated from the day it shipped (2026-08-25) because the branch it came on was deployed
        # without ever running the full smoke against the main line.
        if isinstance(exc, asyncio.CancelledError):
            raise
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
