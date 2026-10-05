"""Run one scheduler job in a disposable child process."""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m omniseek.core.job_runner MODULE CALLABLE", file=sys.stderr)
        return 2
    module_name, callable_name = args
    # The child's stderr is OmniSeek-http .err log, but nothing set up logging in this process, so the
    # root logger stayed at WARNING and every INFO line a job wrote was dropped (found 2026-10-05: the
    # cdp-reaper had run 598 times with none of its own lines in the log). Same set-up as
    # serve_http.main, done before the job's module is imported; masking is already on (omniseek
    # installs it on import).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from omniseek.core import _lograte
    _lograte.install_on_root()
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
        # The child's stderr is OmniSeek-http .err log; an HTTPStatusError's text carries the full
        # request address, so the traceback is masked like every log record (omniseek.redact).
        from omniseek import redact as _redact
        sys.stderr.write(_redact.format_exception(type(exc), exc, exc.__traceback__))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
