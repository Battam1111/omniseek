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
from array import array
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


# Every file this journal writes is opened through os.open, and on Windows os.open opens in TEXT
# mode unless O_BINARY is passed: os.write then turns each b"\n" into b"\r\n" on disk. The event
# index records len(line) as written, so every line on disk was one byte longer than its index
# entry and the offset read of line 3 onwards landed mid-line ("journal line 3 changed on disk").
# O_BINARY exists only on Windows; elsewhere this is 0 and the flags are unchanged.
_O_BINARY = getattr(os, "O_BINARY", 0)


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
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_directory(path.parent)


def _identity_key(*parts: str) -> int:
    """64-bit index key for an identity. A key is only a HINT: every lookup re-reads the event and
    compares the real fields, falling back to a full scan on a (vanishingly rare) collision."""
    digest = hashlib.blake2b("\x00".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class ObservationJournal:
    """A single-process append-only journal with content-addressed JSON blobs.

    MEMORY CONTRACT (2026-10-08): the journal holds NO event dicts and NO payloads in
    memory. It keeps one byte offset per event (an int64 array) plus two latest-sequence indexes
    (observation id, and source + source id), and reads events and payload blobs back from disk on
    demand. The earlier design kept every event dict and every parsed payload resident forever
    (``_pending`` was never pruned): on the live journal (212232 events, 1.1 GB of blobs) that was
    1951 MB of phys_footprint and a 142 s load during which every maybe_ingest caller waited on the
    journal lock. Integrity is unchanged at boot: the hash chain is validated line by line and every
    referenced blob must exist and match its content hash; a blob's JSON is parsed when it is read.
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else Path.home() / ".omniseek" / "state" / "observation-journal"
        self.events_path = self.root / "events.ndjson"
        self.blobs_path = self.root / "blobs"
        self._lock = threading.RLock()
        self._offsets = array("q")
        self._end = 0
        self._head_hash: str | None = None
        self._latest_by_observation: dict[int, int] = {}
        self._latest_by_identity: dict[int, int] = {}
        self._load()

    @property
    def head_seq(self) -> int:
        return len(self._offsets)

    @property
    def head_hash(self) -> str | None:
        return self._head_hash

    def _index(self, event: dict[str, Any], offset: int, length: int) -> None:
        self._offsets.append(offset)
        self._end = offset + length
        self._head_hash = event["event_hash"]
        seq = event["seq"]
        self._latest_by_observation[_identity_key(event["observation_id"])] = seq
        self._latest_by_identity[_identity_key(event["source"], event["source_id"])] = seq

    def _load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs_path.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            return

        previous_hash = ""
        expected_seq = 1
        offset = 0
        verified_blobs: set[str] = set()
        with open(self.events_path, "rb") as fh:
            while True:
                line = fh.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    try:
                        json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._truncate_events(offset)
                        break
                    self._durable_append(b"\n")
                    line += b"\n"
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JournalCorrupt(f"invalid journal line {expected_seq}") from exc
                self._validate_event(event, expected_seq, previous_hash)
                payload_hash = event["payload_hash"]
                if payload_hash is not None and payload_hash not in verified_blobs:
                    self._read_blob(payload_hash)
                    verified_blobs.add(payload_hash)
                self._index(event, offset, len(line))
                offset += len(line)
                expected_seq += 1
                previous_hash = event["event_hash"]

    def _truncate_events(self, length: int) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.events_path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
        try:
            os.ftruncate(fd, length)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.events_path.parent)

    def _durable_append(self, data: bytes) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.events_path.exists()
        fd = os.open(self.events_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | _O_BINARY, 0o600)
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

    def _read_blob(self, payload_hash: str) -> bytes:
        blob = self.blobs_path / payload_hash
        try:
            blob_bytes = blob.read_bytes()
        except OSError as exc:
            raise JournalCorrupt(f"missing payload blob {payload_hash}") from exc
        if _sha256(blob_bytes) != payload_hash:
            raise JournalCorrupt(f"payload hash mismatch for {payload_hash}")
        return blob_bytes

    def _observation_from_event(self, event: dict[str, Any]) -> JournaledObservation:
        payload = None
        payload_hash = event["payload_hash"]
        if payload_hash is not None:
            blob_bytes = self._read_blob(payload_hash)
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
        line = _canonical_json(event) + b"\n"
        self._durable_append(line)
        self._index(event, self._end, len(line))
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
            latest = self._latest_for_observation(observation_id)
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
                latest_identity = self._latest_for_identity(source, source_id)
                if latest_identity is not None and latest_identity["seq"] > materialized_through:
                    return None
                if latest_identity is not None:
                    privacy_namespace = latest_identity["privacy_namespace"]
            observation_id = self._observation_id(source, source_id, privacy_namespace)
            latest = self._latest_for_observation(observation_id)
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

    def _read_line(self, seq: int) -> dict[str, Any]:
        start = self._offsets[seq - 1]
        stop = self._offsets[seq] if seq < len(self._offsets) else self._end
        with open(self.events_path, "rb") as fh:
            fh.seek(start)
            raw = fh.read(stop - start)
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalCorrupt(f"journal line {seq} changed on disk") from exc
        if not isinstance(event, dict) or event.get("seq") != seq:
            raise JournalCorrupt(f"journal line {seq} changed on disk")
        return event

    def _latest(self, index: dict[int, int], key: int, match) -> dict[str, Any] | None:
        seq = index.get(key)
        if seq is None:
            return None
        event = self._read_line(seq)
        if match(event):
            return event
        for candidate in range(self.head_seq, 0, -1):  # 64-bit key collision: exact scan
            event = self._read_line(candidate)
            if match(event):
                return event
        return None

    def _latest_for_observation(self, observation_id: str) -> dict[str, Any] | None:
        return self._latest(
            self._latest_by_observation,
            _identity_key(observation_id),
            lambda event: event["observation_id"] == observation_id,
        )

    def _latest_for_identity(self, source: str, source_id: str) -> dict[str, Any] | None:
        return self._latest(
            self._latest_by_identity,
            _identity_key(source, source_id),
            lambda event: event["source"] == source and event["source_id"] == source_id,
        )

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._read_line(seq) for seq in range(1, self.head_seq + 1)]

    def event(self, seq: int) -> dict[str, Any] | None:
        with self._lock:
            if seq < 1 or seq > self.head_seq:
                return None
            return self._read_line(seq)

    def iter_pending(self, *, after_seq: int = 0, limit: int | None = None):
        """Yield observations after ``after_seq`` in order, reading each event and payload from
        disk as it is reached: a replay holds one payload at a time, never the whole tail."""
        with self._lock:
            head = self.head_seq
        stop = head if limit is None else min(head, max(0, after_seq) + max(0, limit))
        for seq in range(max(0, after_seq) + 1, stop + 1):
            with self._lock:
                event = self._read_line(seq)
            yield self._observation_from_event(event)

    def pending(self, *, after_seq: int = 0, limit: int | None = None) -> list[JournaledObservation]:
        return list(self.iter_pending(after_seq=after_seq, limit=limit))
