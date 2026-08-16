"""Convert an authoring-pipeline candidate array into task files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from schema import TaskValidationError, canonicalize_task


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(value: str) -> str:
    name = _SAFE_NAME.sub("_", value).strip("._")
    if not name:
        raise TaskValidationError("task id cannot produce an empty filename")
    return name


def convert_candidates(
    candidate_file: str | Path,
    tasks_dir: str | Path = Path("bench") / "tasks",
) -> list[Path]:
    """Validate candidates and write one canonical JSON file per task."""
    candidate_file = Path(candidate_file)
    try:
        payload: Any = json.loads(candidate_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskValidationError(f"cannot read candidates: {exc}") from exc
    if not isinstance(payload, list):
        raise TaskValidationError("candidate file must contain an array")

    output_root = Path(tasks_dir)
    written: list[Path] = []
    ids: set[str] = set()
    for raw in payload:
        task = canonicalize_task(raw)
        task_id = task["id"]
        if task_id in ids:
            raise TaskValidationError(f"duplicate candidate id: {task_id}")
        ids.add(task_id)
        target_dir = output_root / _safe_filename(task["suite"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{_safe_filename(task_id)}.json"
        target.write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(target)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_file", type=Path)
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=Path("bench") / "tasks",
    )
    args = parser.parse_args()
    convert_candidates(args.candidate_file, args.tasks_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
