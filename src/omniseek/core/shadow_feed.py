"""Shadow-run output: the new items each sensor run found, one JSONL file per source.

An external digest runs its own pullers for Cubox and GitHub stars. Before those pullers retire, the
eye's ``cubox`` and ``github_starred`` sources run beside them in SHADOW, and an external comparison
job reads what OmniSeek saw. This module is the one place that writes it.

A source opts in with the class attribute ``shadow_feed = True``; every other source is untouched
(the sensor calls :func:`record_new` after every run, and it filters to opted-in sources).

FILE: ``<dir>/<source>.jsonl`` where ``<dir>`` is ``$EYE_SHADOW_FEED_DIR`` or
``~/.omniseek/state/shadow_feed``. One JSON object per line, one line per NEW item per sensor run,
lines only ever appended. The stable line format (``schema`` 1) is::

    {"schema": 1, "source": "cubox", "source_id": "7247...", "url": "https://...",
     "first_seen_at": "2026-10-11T04:00:01.123456+00:00", "item_time": "2026-10-11T09:12:00+08:00",
     "title": "...", "sensor_id": "a1b2c3d4", "first_run": false}

- ``source_id``: Cubox card id / GitHub repo full name (the comparison's primary key).
- ``url``: the item url as the upstream gave it (the comparison normalizes it itself).
- ``first_seen_at``: UTC ISO time of the sensor run that first saw the item (the run time, the same
  value as the sensor's ``last_run_at``).
- ``item_time``: the item's own time (Cubox ``create_time`` / GitHub ``starred_at``), ISO, or null.
- ``first_run``: true on the run that SEEDS the sensor's baseline (its first run that saw any item;
  earlier empty runs, e.g. before the Cubox credential exists, do not count): those lines are the
  backlog in the lookback window, not new arrivals, and the comparison should skip them.

New fields may be added later; existing fields keep their name and meaning (a meaning change bumps
``schema``). Appends are atomic: the file is rewritten to a temp file in the same directory and
``os.replace``-d over the old one under a process lock plus an ``flock`` on ``<file>.lock``, so a
reader never sees a half line. FAIL-OPEN: a write failure is logged and swallowed, never breaking the
sensor run that called it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover (non-POSIX)
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SCHEMA = 1
ENV_DIR = "EYE_SHADOW_FEED_DIR"
_lock = threading.Lock()


def shadow_dir() -> Path:
    env = os.environ.get(ENV_DIR, "").strip()
    return Path(env).expanduser() if env else Path.home() / ".omniseek" / "state" / "shadow_feed"


def shadow_path(source: str) -> Path:
    return shadow_dir() / f"{source}.jsonl"


def _opted_in(source: str) -> bool:
    try:
        from omniseek.core import fetcher
        return bool(getattr(fetcher.get_adapter(source), "shadow_feed", False))
    except Exception:  # noqa: BLE001
        return False


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def line_for(doc, *, run_at: str, sensor_id: str, first_run: bool) -> dict:
    return {
        "schema": SCHEMA,
        "source": doc.source,
        "source_id": str(doc.source_id),
        "url": doc.url,
        "first_seen_at": run_at,
        "item_time": _iso(getattr(doc, "date", None)),
        "title": doc.title,
        "sensor_id": sensor_id,
        "first_run": bool(first_run),
    }


def append_lines(path: Path, lines: list[dict]) -> None:
    """Atomically append ``lines`` (JSON objects) to ``path``: copy + new lines -> temp -> replace."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in lines)
    with _lock:
        lock_fh = open(str(path) + ".lock", "a+")
        try:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            old = path.read_bytes() if path.exists() else b""
            if old and not old.endswith(b"\n"):
                old += b"\n"
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(old)
                    fh.write(payload.encode("utf-8"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_fh.close()


def record_new(sensor, new_docs: Iterable, run_at: str, first_run: bool) -> dict:
    """Write this run's new docs of opted-in sources to their shadow files. Returns
    ``{source: lines_written}``; never raises."""
    written: dict = {}
    try:
        by_source: dict[str, list[dict]] = {}
        opted: dict[str, bool] = {}
        for doc in new_docs:
            src = getattr(doc, "source", None)
            if not src:
                continue
            if src not in opted:
                opted[src] = _opted_in(src)
            if not opted[src]:
                continue
            by_source.setdefault(src, []).append(
                line_for(doc, run_at=run_at, sensor_id=sensor.id, first_run=first_run))
        for src, lines in by_source.items():
            try:
                append_lines(shadow_path(src), lines)
                written[src] = len(lines)
            except Exception as exc:  # noqa: BLE001, a shadow write must never break the sensor run
                log.warning("shadow feed write failed for %s: %s", src, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("shadow feed record swallowed: %s", exc)
    return written
