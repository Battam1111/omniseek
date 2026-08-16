"""Validation and canonicalization for published benchmark tasks."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping


class TaskValidationError(ValueError):
    """Raised when a task does not satisfy the published task contract."""


CANONICAL_TOOL_PREFIX = "omniseek_"
LEGACY_TOOL_PREFIX = "eye_"


def canonicalize_tool_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise TaskValidationError("input.tool must be a non-empty string")
    value = name.strip()
    if value.startswith(LEGACY_TOOL_PREFIX):
        value = CANONICAL_TOOL_PREFIX + value[len(LEGACY_TOOL_PREFIX):]
    if not value.startswith(CANONICAL_TOOL_PREFIX):
        raise TaskValidationError(
            f"input.tool must start with {LEGACY_TOOL_PREFIX!r} or {CANONICAL_TOOL_PREFIX!r}"
        )
    return value


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskValidationError(f"{path} must be an object")
    return value


def canonicalize_task(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep-copied, validated task with canonical tool naming."""
    if not isinstance(raw, Mapping):
        raise TaskValidationError("task must be an object")
    task = copy.deepcopy(dict(raw))

    for field in ("id", "suite", "claim"):
        if not isinstance(task.get(field), str) or not task[field].strip():
            raise TaskValidationError(f"{field} must be a non-empty string")

    input_spec = dict(_require_mapping(task.get("input"), "input"))
    if "tool" not in input_spec and "eye_tool" in input_spec:
        input_spec["tool"] = input_spec.pop("eye_tool")
    input_spec["tool"] = canonicalize_tool_name(input_spec.get("tool"))
    args = input_spec.get("args")
    if not isinstance(args, Mapping):
        raise TaskValidationError("input.args must be an object")
    input_spec["args"] = dict(args)
    task["input"] = input_spec

    truth = dict(_require_mapping(task.get("ground_truth"), "ground_truth"))
    if not isinstance(truth.get("type"), str) or not truth["type"].strip():
        raise TaskValidationError("ground_truth.type must be a non-empty string")
    task["ground_truth"] = truth

    # Probe-less tasks assert the server's own contract and are always live.
    if "liveness_probe" in task and task["liveness_probe"] is not None:
        probe = _require_mapping(task["liveness_probe"], "liveness_probe")
        if not isinstance(probe.get("url"), str) or not probe["url"].strip():
            raise TaskValidationError("liveness_probe.url must be a non-empty string")
        task["liveness_probe"] = dict(probe)

    if "fallback_similarity" in task:
        value = task["fallback_similarity"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise TaskValidationError("fallback_similarity must be a number between 0 and 1")
        task["fallback_similarity"] = float(value)

    if "timeout_s" in task:
        value = task["timeout_s"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise TaskValidationError("timeout_s must be a positive number")
        task["timeout_s"] = float(value)

    if "added_in" in task and not isinstance(task["added_in"], str):
        raise TaskValidationError("added_in must be a string when present")
    if "lang" in task and not isinstance(task["lang"], str):
        raise TaskValidationError("lang must be a string when present")
    if "notes" in task and not isinstance(task["notes"], str):
        raise TaskValidationError("notes must be a string when present")
    return task


def load_task_file(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskValidationError(f"cannot read {path}: {exc}") from exc
    rows = payload if isinstance(payload, list) else [payload]
    return [canonicalize_task(row) for row in rows]


def load_tasks(tasks_dir: str | Path, suites: set[str] | None = None) -> list[dict[str, Any]]:
    root = Path(tasks_dir)
    if not root.exists():
        raise TaskValidationError(f"tasks directory does not exist: {root}")
    files = sorted(root.rglob("*.json"))
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        for task in load_task_file(path):
            if suites and task["suite"] not in suites:
                continue
            if task["id"] in seen:
                raise TaskValidationError(f"duplicate task id: {task['id']}")
            seen.add(task["id"])
            tasks.append(task)
    return tasks
