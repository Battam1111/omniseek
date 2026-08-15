#!/usr/bin/env python3
"""Standalone cache pre-warmer — a thin CLI wrapper over omniseek.core.prewarm.warm_sources().

The PRIMARY warmer now runs INSIDE OmniSeek-http service as a daemon thread (see
omniseek.core.prewarm + omniseek.serve_http): warming is tied to the always-on service so it fires
reliably. The standalone launchd cron that used to drive THIS script was unreliable (it fired
once at load and never on its 30-min interval), which silently let Lever B's caches expire.

This script is kept for a MANUAL one-shot warm (e.g. right after a deploy, before the service's
first cycle completes). It is no longer the production mechanism.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "omniseek-mcp"
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] cache prewarm (manual) starting")
    try:
        from omniseek.server import load_sources
        load_sources()  # adapters self-register on import (same path the service uses)
        from omniseek.core import prewarm
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL import: {exc}")
        return 2
    ok, total = prewarm.warm_sources()
    print(f"  done. {ok}/{total} warmed")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
