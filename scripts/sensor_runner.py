#!/usr/bin/env python3
"""Cron entry point: run all due sensors. Called by the launchd job (initially disabled).

Usage:  .venv/bin/python scripts/sensor_runner.py
"""

import logging
import sys

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
            else:
                log.info("sensor %s (%s): no new results", s.id, s.query)
        except Exception:
            log.exception("sensor %s (%s) failed", s.id, s.query)

    log.info("sensor run complete")


if __name__ == "__main__":
    main()
