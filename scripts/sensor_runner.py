#!/usr/bin/env python3
"""Cron entry point: run all due sensors. Called by the launchd job (initially disabled).

Usage:  .venv/bin/python scripts/sensor_runner.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "penumbra-mcp" / "scripts"))  # sibling _sentinel_common

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    stream=sys.stderr)
log = logging.getLogger("sensor_runner")


def main() -> None:
    from penumbra.server import load_sources
    load_sources()

    from penumbra.core.sensor import SensorStore, run_sensor

    store = SensorStore()
    sensors = store.list_all()
    if not sensors:
        log.info("no sensors registered, exiting")
        return

    log.info("running %d sensor(s)", len(sensors))
    for raw in sensors:
        s = store.get(raw["id"])
        if s is None:
            continue
        try:
            summary = run_sensor(s, store)
            if summary["new_count"] > 0:
                log.info("sensor %s (%s): %d NEW results", s.id, s.query, summary["new_count"])
                if s.notify:
                    _notify(s, summary)
            else:
                log.info("sensor %s (%s): no new results", s.id, s.query)
        except Exception:
            log.exception("sensor %s (%s) failed", s.id, s.query)

    log.info("sensor run complete")


def _notify(sensor, summary) -> None:
    """Push ONE Bark for a notify=True sensor that turned up new results (title = the query,
    body = the new count + the first new titles). Bark failure never breaks the run."""
    from _sentinel_common import bark_push
    titles = summary.get("new_titles") or []
    body = f"{summary['new_count']} 条新结果" + ("\n" + "\n".join(titles) if titles else "")
    try:
        bark_push(sensor.query, body, group="Penumbra")
    except Exception:
        log.exception("sensor %s (%s) bark failed", sensor.id, sensor.query)


if __name__ == "__main__":
    main()
