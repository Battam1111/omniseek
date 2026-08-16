"""Durable append-only observation journal for OmniSeek recall layer.

The journal is the durable source of observations. SQLite is a materialized view and may be
replayed from these events. This module deliberately contains no retrieval or ranking policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class JournalCorrupt(RuntimeError):
    """The journal contains a non-recoverable corruption or an invalid hash chain."""


@dataclass(frozen=True)
class ObservationReceipt:
    observation_id: str
    journal_seq: int
    event_hash: str
    payload_hash: str | None
    journal_status: str
    materialization_status: str
    fsynced: bool


@dataclass(frozen=True)
class JournaledObservation:
    observation_id: str
    journal_seq: int
    event_hash: str
    payload_hash: str | None
    source: str
    source_id: str
    payload: Any | None
    kind: str
    provenance: str
    privacy_namespace: str
    lane: str


_SENSITIVE_EXACT = frozenset(
    {
        "raw",
        "cookie",
        "cookies",
        "token",
        "password",
        "secret",
        "authorization",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "set_cookie",
    }
)
_SENSITIVE_SUFFIXES = ("_token", "_password", "_secret", "_authorization", "_credential")
_KEY_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _KEY_NORMALIZE.sub("_", key.lower()).strip("_")
    return normalized in _SENSITIVE_EXACT or normalized.endswith(_SENSITIVE_SUFFIXES)


def _filter_private(value: Any) -> Any:
    """Remove sensitive metadata keys recursively without changing safe payload values."""
    if isinstance(value, dict):
        return {
            key: _filter_private(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_filter_private(item) for item in value]
    if isinstance(value, tuple):
        return [_filter_private(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("short write while appending observation journal")
        offset += written


# Directory fsync is the POSIX primitive that durably commits a directory ENTRY (a create or a
# rename); the file's own contents are already committed by the fsync on its fd. Windows exposes
# no equivalent through os.open: opening a directory there raises PermissionError, which used to
# fail every append in this journal (the file was written and fsynced, then the directory-entry
# commit blew up and took the whole append down with it, silently, at WARNING level).
#
# So SKIP the step where the platform cannot do it, and only there. Not a try/except: swallowing
# OSError everywhere would let a genuine fsync failure on POSIX pass for durability, in the one
# component whose entire job is durability. On Windows the guarantee is honestly weaker (file
# contents durable, directory entry left to the filesystem), which is what NTFS gives us.
_DIR_FSYNC_SUPPORTED = os.name == "posix"


def _fsync_directory(path: Path) -> None:
    if not _DIR_FSYNC_SUPPORTED:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace one file with fsync on both file and parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_directory(path.parent)


class ObservationJournal:
    """A single-process append-only journal with content-addressed JSON blobs."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else Path.home() / ".omniseek" / "state" / "observation-journal"
        self.events_path = self.root / "events.ndjson"
        self.blobs_path = self.root / "blobs"
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._pending: dict[int, JournaledObservation] = {}
        self._load()

    @property
    def head_seq(self) -> int:
        return self._events[-1]["seq"] if self._events else 0

    @property
    def head_hash(self) -> str | None:
        return self._events[-1]["event_hash"] if self._events else None

    def _load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs_path.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            return

        raw = self.events_path.read_bytes()
        if not raw:
            return
        lines = raw.splitlines(keepends=True)
        if raw and not raw.endswith(b"\n"):
            tail = lines[-1]
            try:
                json.loads(tail.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                prefix = b"".join(lines[:-1])
                self._truncate_events(prefix)
                lines = prefix.splitlines(keepends=True)
            else:
                self._durable_append(b"\n")
                lines[-1] = tail + b"\n"

        previous_hash = ""
        expected_seq = 1
        for index, line in enumerate(lines):
            if not line.endswith(b"\n"):
                raise JournalCorrupt(f"journal line {index + 1} is not newline terminated")
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JournalCorrupt(f"invalid journal line {index + 1}") from exc
            self._validate_event(event, expected_seq, previous_hash)
            self._events.append(event)
            observation = self._observation_from_event(event)
            self._pending[event["seq"]] = observation
            expected_seq += 1
            previous_hash = event["event_hash"]

    def _truncate_events(self, prefix: bytes) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.events_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.ftruncate(fd, len(prefix))
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.events_path.parent)

    def _durable_append(self, data: bytes) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.events_path.exists()
        fd = os.open(self.events_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            _fsync_directory(self.events_path.parent)

    @staticmethod
    def _event_hash(event: dict[str, Any]) -> str:
        unsigned = {key: value for key, value in event.items() if key != "event_hash"}
        return _sha256(_canonical_json(unsigned))

    @classmethod
    def _validate_event(cls, event: Any, expected_seq: int, previous_hash: str) -> None:
        if not isinstance(event, dict):
            raise JournalCorrupt(f"event {expected_seq} is not an object")
        required = {
            "kind",
            "seq",
            "prev_hash",
            "event_hash",
            "observation_id",
            "source",
            "source_id",
            "observed_at",
            "provenance",
            "privacy_namespace",
            "lane",
            "payload_hash",
        }
        if not required.issubset(event):
            missing = sorted(required.difference(event))
            raise JournalCorrupt(f"event {expected_seq} is missing fields: {missing}")
        if event["seq"] != expected_seq:
            raise JournalCorrupt(f"expected seq {expected_seq}, got {event['seq']!r}")
        if event["prev_hash"] != previous_hash:
            raise JournalCorrupt(f"prev_hash mismatch at seq {expected_seq}")
        if event["event_hash"] != cls._event_hash(event):
            raise JournalCorrupt(f"event hash mismatch at seq {expected_seq}")
        if event["kind"] not in {"observation", "tombstone"}:
            raise JournalCorrupt(f"unknown event kind at seq {expected_seq}")
        if event["kind"] == "observation" and not event["payload_hash"]:
            raise JournalCorrupt(f"observation {expected_seq} has no payload hash")
        if event["kind"] == "tombstone" and event["payload_hash"] is not None:
            raise JournalCorrupt(f"tombstone {expected_seq} carries a payload hash")

    @staticmethod
    def _observation_id(source: str, source_id: str, privacy_namespace: str) -> str:
        identity = _canonical_json(
            {
                "source": source,
                "source_id": source_id,
                "privacy_namespace": privacy_namespace,
            }
        )
        return _sha256(identity)

    def _observation_from_event(self, event: dict[str, Any]) -> JournaledObservation:
        payload = None
        payload_hash = event["payload_hash"]
        if payload_hash is not None:
            blob = self.blobs_path / payload_hash
            try:
                blob_bytes = blob.read_bytes()
            except OSError as exc:
                raise JournalCorrupt(f"missing payload blob {payload_hash}") from exc
            if _sha256(blob_bytes) != payload_hash:
                raise JournalCorrupt(f"payload hash mismatch for {payload_hash}")
            try:
                payload = json.loads(blob_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JournalCorrupt(f"invalid payload blob {payload_hash}") from exc
        return JournaledObservation(
            observation_id=event["observation_id"],
            journal_seq=event["seq"],
            event_hash=event["event_hash"],
            payload_hash=payload_hash,
            source=event["source"],
            source_id=event["source_id"],
            payload=payload,
            kind=event["kind"],
            provenance=event["provenance"],
            privacy_namespace=event["privacy_namespace"],
            lane=event["lane"],
        )

    def _append_event(self, event: dict[str, Any]) -> ObservationReceipt:
        event["event_hash"] = self._event_hash(event)
        self._durable_append(_canonical_json(event) + b"\n")
        self._events.append(event)
        observation = self._observation_from_event(event)
        self._pending[event["seq"]] = observation
        return ObservationReceipt(
            observation_id=event["observation_id"],
            journal_seq=event["seq"],
            event_hash=event["event_hash"],
            payload_hash=event["payload_hash"],
            journal_status="local-durable",
            materialization_status="pending",
            fsynced=True,
        )

    def append_payload(
        self,
        payload: Any,
        *,
        source: str,
        source_id: str,
        observed_at: float,
        provenance: str,
        privacy_namespace: str,
        lane: str,
    ) -> ObservationReceipt:
        safe_payload = _filter_private(payload)
        blob_bytes = _canonical_json(safe_payload)
        payload_hash = _sha256(blob_bytes)
        observation_id = self._observation_id(source, source_id, privacy_namespace)
        with self._lock:
            latest = next(
                (event for event in reversed(self._events)
                 if event["observation_id"] == observation_id),
                None,
            )
            if latest is not None and latest["kind"] == "observation" \
                    and latest["payload_hash"] == payload_hash:
                return ObservationReceipt(
                    observation_id=observation_id,
                    journal_seq=latest["seq"],
                    event_hash=latest["event_hash"],
                    payload_hash=payload_hash,
                    journal_status="local-durable",
                    materialization_status="pending",
                    fsynced=True,
                )
            blob = self.blobs_path / payload_hash
            if blob.exists():
                if _sha256(blob.read_bytes()) != payload_hash:
                    raise JournalCorrupt(f"content-addressed blob mismatch for {payload_hash}")
            else:
                _durable_replace(blob, blob_bytes)
            event = {
                "kind": "observation",
                "seq": self.head_seq + 1,
                "prev_hash": self.head_hash or "",
                "event_hash": "",
                "observation_id": observation_id,
                "source": source,
                "source_id": source_id,
                "observed_at": observed_at,
                "provenance": provenance,
                "privacy_namespace": privacy_namespace,
                "lane": lane,
                "payload_hash": payload_hash,
            }
            return self._append_event(event)

    def append_tombstone(
        self,
        *,
        source: str,
        source_id: str,
        observed_at: float,
        provenance: str,
        privacy_namespace: str,
        reason: str,
        lane: str = "full",
        materialized_through: int | None = None,
    ) -> ObservationReceipt | None:
        with self._lock:
            if materialized_through is not None:
                latest_identity = next(
                    (
                        event
                        for event in reversed(self._events)
                        if event["source"] == source and event["source_id"] == source_id
                    ),
                    None,
                )
                if latest_identity is not None and latest_identity["seq"] > materialized_through:
                    return None
                if latest_identity is not None:
                    privacy_namespace = latest_identity["privacy_namespace"]
            observation_id = self._observation_id(source, source_id, privacy_namespace)
            latest = next(
                (event for event in reversed(self._events)
                 if event["observation_id"] == observation_id),
                None,
            )
            if materialized_through is None and latest is not None \
                    and latest["kind"] == "tombstone" \
                    and latest.get("reason") == reason:
                return ObservationReceipt(
                    observation_id=observation_id,
                    journal_seq=latest["seq"],
                    event_hash=latest["event_hash"],
                    payload_hash=None,
                    journal_status="local-durable",
                    materialization_status="pending",
                    fsynced=True,
                )
            event = {
                "kind": "tombstone",
                "seq": self.head_seq + 1,
                "prev_hash": self.head_hash or "",
                "event_hash": "",
                "observation_id": observation_id,
                "source": source,
                "source_id": source_id,
                "observed_at": observed_at,
                "provenance": provenance,
                "privacy_namespace": privacy_namespace,
                "lane": lane,
                "payload_hash": None,
                "reason": reason,
            }
            return self._append_event(event)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]

    def event(self, seq: int) -> dict[str, Any] | None:
        with self._lock:
            if seq < 1 or seq > len(self._events):
                return None
            return dict(self._events[seq - 1])

    def pending(self, *, after_seq: int = 0, limit: int | None = None) -> list[JournaledObservation]:
        with self._lock:
            observations = [item for seq, item in self._pending.items() if seq > after_seq]
            if limit is not None:
                observations = observations[: max(0, limit)]
            return observations
