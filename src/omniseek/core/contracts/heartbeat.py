"""Atomic producer for the versioned Eye scheduler heartbeat."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from omniseek.core.contracts.build_identity import validate_build_id


_CONTRACTS_DIR = Path(__file__).resolve().parent
_SCHEMA_PATH = _CONTRACTS_DIR / "scheduler-heartbeat-v1.json"


@dataclass(frozen=True)
class ContractArtifacts:
    schema_digest: str
    initial_delay_s: float
    tick_interval_s: float
    max_job_budget_s: float


def _number(value: object, *, field: str, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not (number > 0 if positive else number >= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be {qualifier}")
    return number


def load_contract_artifacts(*, initial_delay_s: float, tick_interval_s: float, max_job_budget_s: float,
                            contract_dir: Optional[Path] = None) -> ContractArtifacts:
    """The schema digest the heartbeat names, with the scheduler's own timing (omniseek.core.jobs holds
    those numbers; the heartbeat only reports them)."""
    base = _CONTRACTS_DIR if contract_dir is None else Path(contract_dir)
    schema_bytes = (base / _SCHEMA_PATH.name).read_bytes()
    return ContractArtifacts(
        schema_digest=hashlib.sha256(schema_bytes).hexdigest(),
        initial_delay_s=_number(initial_delay_s, field="initial_delay_s", positive=False),
        tick_interval_s=_number(tick_interval_s, field="tick_interval_s", positive=True),
        max_job_budget_s=_number(max_job_budget_s, field="max_job_budget_s", positive=True),
    )


def _canonical_json_bytes(record: dict) -> bytes:
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(_canonical_json_bytes(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class SchedulerHeartbeat:
    def __init__(
        self,
        *,
        path: Path,
        artifacts: ContractArtifacts,
        build_id: str,
        host_boot_id: str,
        omniseek_pid: int,
        generation: Optional[str] = None,
        utc_now: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.path = Path(path)
        self.artifacts = artifacts
        self.build_id = validate_build_id(build_id)
        if not isinstance(host_boot_id, str) or not host_boot_id:
            raise ValueError("host_boot_id must be non-empty")
        if isinstance(omniseek_pid, bool) or not isinstance(omniseek_pid, int) or omniseek_pid <= 0:
            raise ValueError("omniseek_pid must be a positive integer")
        selected_generation = generation or str(uuid.uuid4())
        try:
            uuid.UUID(selected_generation)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("scheduler generation must be a UUID") from exc
        self.host_boot_id = host_boot_id
        self.omniseek_pid = omniseek_pid
        self.generation = selected_generation
        self._utc_now = utc_now
        self._monotonic_ns = monotonic_ns
        self.started_at_utc = self._utc_now()
        self.started_monotonic_ns = self._monotonic_ns()
        self.tick_seq = 0
        self.last_tick_at_utc: Optional[str] = None
        self.last_tick_monotonic_ns: Optional[int] = None
        self.last_emitted_at_utc: Optional[str] = None

    def _record(self, *, phase: str) -> dict:
        return {
            "schema": "omniseek.core-scheduler-heartbeat/v1",
            "component_id": "eye-scheduler",
            "producer": {"repo": "omniseek", "build_id": self.build_id},
            "contract": {
                "schema_digest": self.artifacts.schema_digest,
            },
            "host_boot_id": self.host_boot_id,
            "omniseek_pid": self.omniseek_pid,
            "scheduler_generation": self.generation,
            "phase": phase,
            "scheduler_started_at_utc": self.started_at_utc,
            "scheduler_started_monotonic_ns": self.started_monotonic_ns,
            "tick_seq": self.tick_seq,
            "last_tick_at_utc": self.last_tick_at_utc,
            "last_tick_monotonic_ns": self.last_tick_monotonic_ns,
            "emitted_at_utc": self._utc_now(),
            "emitted_monotonic_ns": self._monotonic_ns(),
            "initial_delay_s": self.artifacts.initial_delay_s,
            "tick_interval_s": self.artifacts.tick_interval_s,
            "max_job_budget_s": self.artifacts.max_job_budget_s,
        }

    def _publish(self, *, phase: str) -> dict:
        record = self._record(phase=phase)
        _atomic_write(self.path, record)
        self.last_emitted_at_utc = record["emitted_at_utc"]
        return record

    def publish_starting(self) -> dict:
        if self.tick_seq != 0:
            raise RuntimeError("starting heartbeat cannot follow a running tick")
        return self._publish(phase="starting")

    def begin_tick(self) -> dict:
        self.tick_seq += 1
        self.last_tick_at_utc = self._utc_now()
        self.last_tick_monotonic_ns = self._monotonic_ns()
        return self._publish(phase="running")

    def publish_running(self) -> dict:
        if self.tick_seq < 1:
            raise RuntimeError("begin_tick must run before a running heartbeat")
        return self._publish(phase="running")
