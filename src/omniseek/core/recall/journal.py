"""Durable append-only observation journal for OmniSeek recall layer.

The journal is the durable source of observations. SQLite is a materialized view and may be
replayed from these events. This module deliberately contains no retrieval or ranking policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import sys
import threading
import time
import uuid
from array import array
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from omniseek import where

logger = logging.getLogger(__name__)

# How many events may be written after the last checkpoint before the journal writes the next one
# (2026-10-24). A boot reads the checkpoint and replays only the events after it,
# so this number bounds what a boot costs, however long the history gets. Derivation: the journal's
# boot budget is 0.5 s (its share of the 3 s line the release check sets for an eye search); one
# replayed event (JSON parse, canonical re-hash of the chain link, two index keys) costs up to 94 us
# (full builds of a copy of the live journal, 282,319 events, ran 47 to 94 us an event on the host machine at
# load average 22 to 28 on 2026-10-24; OmniSeek restarts with a release, right after the merge gate's
# full test run, so the busy reading is the one that counts); so a tail of about 5,300 events fits the
# budget, and half of that, for margin, is 2500. Re-derive it when the per-event cost or the budget
# changes. Reading on 2026-10-24 (same copy, load 24 to 28, nice 10): a boot with a 5000-event tail
# took 0.17 to 0.21 s, and 0.43 to 0.62 s while a full test suite shared the cores, over the budget;
# boot_info["seconds"] (journal_boot_seconds in the health reading) shows it on every boot.
CKPT_EVERY = 2500
CHECKPOINT_NAME = "events.ckpt"

# Checkpoint file layout, little-endian, fixed width:
#   header  magic b"OBSJCKPT", u32 version, u32 reserved (0), i64 head_seq, i64 end (byte length of
#           the events file through event head_seq), i64 n_obs, i64 n_ident, 32 bytes head event_hash
#           (raw sha256; zeros when head_seq is 0)
#   body    i64 offsets[head_seq]; u64 observation keys[n_obs], i64 seqs[n_obs];
#           u64 identity keys[n_ident], i64 seqs[n_ident]
#   footer  sha256 of header + body (32 bytes)
# A reader refuses the file when the magic or version is unknown, when its length is not exactly
# what the header's counts say (a truncated or padded file), or when the footer digest does not match
# (an altered byte). It then checks the file against the events file: see _restore_checkpoint.
_CKPT_MAGIC = b"OBSJCKPT"
_CKPT_VERSION = 1
_CKPT_HEADER = struct.Struct("<8sIIqqqq32s")
_CKPT_DIGEST = 32


class CheckpointInvalid(ValueError):
    """A checkpoint file that cannot be used; the boot falls back to a full build."""


class _SortedIndex(Mapping):
    """An immutable key -> seq map kept as two aligned arrays sorted by key, read by binary search.

    This is how a checkpoint's index comes back into memory: two array copies, not one Python
    object per entry. Rebuilding a dict of the live journal's 182,609 + 182,609 entries took 0.23 to
    0.46 s on the host machine (load 22 to 27, 2026-10-24) and grows with the history; the copies take about
    a millisecond. A lookup is about 18 comparisons. The arrays are never changed after construction."""

    __slots__ = ("_keys", "_seqs")

    def __init__(self, keys: array | None = None, seqs: array | None = None):
        self._keys = keys if keys is not None else array("Q")
        self._seqs = seqs if seqs is not None else array("q")

    def get(self, key, default=None):
        keys = self._keys
        i = bisect_left(keys, key)
        if i < len(keys) and keys[i] == key:
            return self._seqs[i]
        return default

    def __getitem__(self, key):
        seq = self.get(key)
        if seq is None:
            raise KeyError(key)
        return seq

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def _merge_sorted(base: _SortedIndex, overlay: dict[int, int]) -> _SortedIndex:
    """``base`` with every entry of ``overlay`` set (a new index; ``base`` is not changed)."""
    if not overlay:
        return base
    if not base:
        keys = sorted(overlay)
        return _SortedIndex(array("Q", keys), array("q", (overlay[key] for key in keys)))
    base_keys, seqs = base._keys, base._seqs[:]
    n = len(base_keys)
    new = []
    for key, seq in overlay.items():
        i = bisect_left(base_keys, key)
        if i < n and base_keys[i] == key:
            seqs[i] = seq
        else:
            new.append(key)
    if not new:
        return _SortedIndex(base_keys, seqs)
    new.sort()
    keys_out, seqs_out = array("Q"), array("q")
    previous = 0
    for key in new:
        i = bisect_left(base_keys, key, previous)
        keys_out += base_keys[previous:i]
        seqs_out += seqs[previous:i]
        keys_out.append(key)
        seqs_out.append(overlay[key])
        previous = i
    keys_out += base_keys[previous:]
    seqs_out += seqs[previous:]
    return _SortedIndex(keys_out, seqs_out)


class _LatestIndex(Mapping):
    """key -> latest seq: a _SortedIndex base (what the last checkpoint holds) plus a dict of the
    entries set since. ``_index`` sets into the dict; after a checkpoint is written, ``rebase`` swaps
    in the merged base and keeps in the dict only what was set after the checkpoint's copy, so the
    dict holds at most about ``CKPT_EVERY`` entries plus what arrives while a checkpoint is written."""

    __slots__ = ("base", "overlay")

    def __init__(self, base: _SortedIndex | None = None, overlay: dict[int, int] | None = None):
        self.base = base if base is not None else _SortedIndex()
        self.overlay = overlay if overlay is not None else {}

    def get(self, key, default=None):
        seq = self.overlay.get(key)
        if seq is None:
            seq = self.base.get(key)
        return default if seq is None else seq

    def __getitem__(self, key):
        seq = self.get(key)
        if seq is None:
            raise KeyError(key)
        return seq

    def __setitem__(self, key, seq: int) -> None:
        self.overlay[key] = seq

    def __iter__(self):
        overlay = self.overlay
        yield from overlay
        for key in self.base:
            if key not in overlay:
                yield key

    def __len__(self) -> int:
        return len(self.base) + sum(1 for key in self.overlay if self.base.get(key) is None)

    def frozen_copy(self) -> "_LatestIndex":
        """A copy sharing the (immutable) base and copying the dict: what the lock holder copies."""
        return _LatestIndex(self.base, self.overlay.copy())

    def rebase(self, merged: _SortedIndex, written: dict[int, int]) -> None:
        self.base = merged
        self.overlay = {key: seq for key, seq in self.overlay.items() if written.get(key) != seq}


def _as_sorted(index: Mapping) -> _SortedIndex:
    if isinstance(index, _SortedIndex):
        return index
    if isinstance(index, _LatestIndex):
        return _merge_sorted(index.base, index.overlay)
    return _merge_sorted(_SortedIndex(), dict(index))


class _Snapshot(NamedTuple):
    """The four things the journal keeps in memory, as one consistent copy."""

    end: int
    head_hash: str | None
    offsets: array
    by_observation: Mapping
    by_identity: Mapping

    @property
    def head_seq(self) -> int:
        return len(self.offsets)


def _le_bytes(values: array) -> bytes:
    if sys.byteorder != "little":
        values = array(values.typecode, values)
        values.byteswap()
    return values.tobytes()


def _encode_checkpoint(snap: _Snapshot) -> bytes:
    by_observation, by_identity = _as_sorted(snap.by_observation), _as_sorted(snap.by_identity)
    obs_keys, obs_seqs = by_observation._keys, by_observation._seqs
    ident_keys, ident_seqs = by_identity._keys, by_identity._seqs
    head = bytes.fromhex(snap.head_hash) if snap.head_seq else bytes(32)
    if len(head) != 32:
        raise ValueError("head event_hash is not a sha256 hex digest")
    body = b"".join((
        _CKPT_HEADER.pack(_CKPT_MAGIC, _CKPT_VERSION, 0, snap.head_seq, snap.end,
                          len(obs_keys), len(ident_keys), head),
        _le_bytes(snap.offsets), _le_bytes(obs_keys), _le_bytes(obs_seqs),
        _le_bytes(ident_keys), _le_bytes(ident_seqs),
    ))
    return body + hashlib.sha256(body).digest()


def _decode_checkpoint(data: bytes) -> _Snapshot:
    """Parse and self-check a checkpoint file; raise CheckpointInvalid saying why it is unusable."""
    minimum = _CKPT_HEADER.size + _CKPT_DIGEST
    if len(data) < minimum:
        raise CheckpointInvalid(f"checkpoint length {len(data)} is shorter than its header ({minimum})")
    magic, version, _reserved, head_seq, end, n_obs, n_ident, head = _CKPT_HEADER.unpack_from(data)
    if magic != _CKPT_MAGIC:
        raise CheckpointInvalid("checkpoint magic is not OBSJCKPT")
    if version != _CKPT_VERSION:
        raise CheckpointInvalid(f"checkpoint version {version} is not {_CKPT_VERSION}")
    if min(head_seq, end, n_obs, n_ident) < 0:
        raise CheckpointInvalid("checkpoint header has a negative count")
    expected = minimum + 8 * (head_seq + 2 * n_obs + 2 * n_ident)
    if len(data) != expected:
        raise CheckpointInvalid(f"checkpoint length {len(data)} does not match its header ({expected})")
    if hashlib.sha256(memoryview(data)[:-_CKPT_DIGEST]).digest() != data[-_CKPT_DIGEST:]:
        raise CheckpointInvalid("checkpoint digest mismatch")
    view = memoryview(data)
    pos = _CKPT_HEADER.size

    def take(typecode: str, count: int) -> array:
        nonlocal pos
        values = array(typecode)
        values.frombytes(view[pos:pos + 8 * count])
        if sys.byteorder != "little":
            values.byteswap()
        pos += 8 * count
        return values

    offsets = take("q", head_seq)
    obs_keys, obs_seqs = take("Q", n_obs), take("q", n_obs)
    ident_keys, ident_seqs = take("Q", n_ident), take("q", n_ident)
    if head_seq:
        if offsets[0] != 0 or offsets[-1] >= end:
            raise CheckpointInvalid("checkpoint offsets do not start at 0 and end before its end")
    elif end != 0 or head != bytes(32) or n_obs or n_ident:
        raise CheckpointInvalid("empty checkpoint carries an end, a head hash or index entries")
    # The index entries are not scanned here (min/max over the arrays cost 45 ms on the live journal
    # and grow with it): the digest says they are the bytes the encoder wrote from a validated
    # journal, and verify_chain, run after every checkpoint boot, compares every entry by lookup.
    return _Snapshot(
        end=end,
        head_hash=head.hex() if head_seq else None,
        offsets=offsets,
        by_observation=_SortedIndex(obs_keys, obs_seqs),
        by_identity=_SortedIndex(ident_keys, ident_seqs),
    )


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
    journal lock.

    BOOT CONTRACT (2026-10-24): the boot load reads the events only. It validates
    the hash chain line by line and builds the index, and reads no payload blob. A blob's existence
    and content hash are checked in two places instead: every read (``_read_blob``, which fails closed
    with JournalCorrupt) and ``scrub_blobs``, a pass over every referenced blob that the service runs
    on a background thread after boot, off the journal lock, reporting bad blobs without stopping
    anything. Reading every blob at boot cost 41.4 s on the live journal of 2026-10-24 (253345 blobs,
    574 MB), all of it inside the first maybe_ingest of every source after a restart.

    CHECKPOINT (2026-10-24): the index itself still cost one JSON parse and one
    canonical re-hash per event (282,319 events, 13.3 s on the live copy, about 47 us an event and
    growing with the history), under the lock the first searches wait on. So the four things the
    journal keeps in memory (the offsets, the two latest-seq indexes, the chain head with its end
    offset) are also kept in a checkpoint file (``events.ckpt`` beside the events, fixed-width binary
    with its own sha256, replaced atomically; layout above ``_encode_checkpoint``). A boot reads the
    checkpoint, checks it against the events file (the file is at least as long as the checkpoint's
    end, and the bytes of its last event parse to its head seq and head event_hash), and then reads
    and validates only the events after it, line by line, chained onto its head. The two indexes come
    back as sorted arrays read by binary search (``_SortedIndex``), with a dict on top for the entries
    set since (``_LatestIndex``), so reading the checkpoint is array copies plus one sha256, never
    one Python object per entry (live 2026-10-24: 7 to 15 ms; rebuilding dicts took 0.23 to 0.46 s and
    grew with the history). No checkpoint, an unusable one, or a tail that fails to validate: a full
    build as before, with the reason logged in one line and kept in ``boot_info``. The events file
    stays the only source of truth; the checkpoint is derived from it and rebuilt whenever it does not
    match. ``maybe_checkpoint`` writes the next one once ``CKPT_EVERY`` events have been written after
    the last (and once after every boot). Under the lock it only copies the offsets array and each
    index's small dict, and afterwards swaps in the merged sorted arrays; merging, encoding and the
    durable write run outside it. The service calls it on the recall-journal-boot and recall-writer
    threads, never on the foreground append path. A boot
    therefore replays at most ``CKPT_EVERY`` events plus what one writer cycle adds, however long the
    history. The part of the chain before the checkpoint is no longer validated at boot: the service
    runs ``verify_chain`` on its boot thread after the build, which re-validates the whole chain off
    the lock and compares the four things it derives with the ones in memory, reporting a break or a
    mismatch with its first bad seq without stopping anything. Every read still checks the seq of the
    line it reads (``_read_line``).
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else where.state() / "observation-journal"
        self.events_path = self.root / "events.ndjson"
        self.blobs_path = self.root / "blobs"
        self._lock = threading.RLock()
        self._offsets = array("q")
        self._end = 0
        self._head_hash: str | None = None
        self._latest_by_observation = _LatestIndex()
        self._latest_by_identity = _LatestIndex()
        self.checkpoint_path = self.root / CHECKPOINT_NAME
        self._ckpt_lock = threading.Lock()
        self._ckpt_seq: int | None = None
        self._boot_ckpt_pending = True
        self.boot_info: dict[str, Any] = {}
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
        started = time.monotonic()
        if not self.events_path.exists():
            self.boot_info = {"mode": "empty", "reason": None, "checkpoint_seq": None,
                              "replayed": 0, "head_seq": 0, "seconds": 0.0}
            return

        reason = self._restore_checkpoint()
        checkpoint_seq = None
        replayed = 0
        if reason is None:
            checkpoint_seq = self.head_seq
            try:
                replayed = self._replay()
            except JournalCorrupt as exc:
                reason = f"tail replay after checkpoint seq {checkpoint_seq} failed: {exc}"
                checkpoint_seq = None
                self._reset_index()
        if reason is not None:
            (logger.info if reason == "no checkpoint" else logger.warning)(
                "observation journal checkpoint not used, full build: %s", reason)
            replayed = self._replay()
        self._ckpt_seq = checkpoint_seq
        self._boot_ckpt_pending = checkpoint_seq is None or replayed > 0
        self.boot_info = {
            "mode": "checkpoint" if reason is None else "full",
            "reason": reason,
            "checkpoint_seq": checkpoint_seq,
            "replayed": replayed,
            "head_seq": self.head_seq,
            "seconds": round(time.monotonic() - started, 3),
        }

    def _reset_index(self) -> None:
        self._offsets = array("q")
        self._end = 0
        self._head_hash = None
        self._latest_by_observation = _LatestIndex()
        self._latest_by_identity = _LatestIndex()

    def _restore_checkpoint(self) -> str | None:
        """Install the checkpoint's four things if it is usable; else return why it is not.

        Usable means: the file reads and passes its own checks (_decode_checkpoint), the events file
        is at least as long as the checkpoint's end, and the bytes of the checkpoint's last event
        (its last offset up to its end) are exactly one event line whose seq is the checkpoint's head
        seq and whose event_hash is the checkpoint's head hash (and is that event's real hash)."""
        try:
            data = self.checkpoint_path.read_bytes()
        except FileNotFoundError:
            return "no checkpoint"
        except OSError as exc:
            return f"checkpoint unreadable: {type(exc).__name__}: {exc}"
        try:
            snap = _decode_checkpoint(data)
        except CheckpointInvalid as exc:
            return str(exc)
        try:
            size = self.events_path.stat().st_size
        except OSError as exc:
            return f"events file unreadable: {type(exc).__name__}: {exc}"
        if snap.end > size:
            return f"checkpoint end {snap.end} is past the end of the events file ({size} bytes)"
        if snap.head_seq:
            start = snap.offsets[-1]
            with open(self.events_path, "rb") as fh:
                fh.seek(start)
                raw = fh.read(snap.end - start)
            try:
                if not raw.endswith(b"\n"):
                    raise ValueError("no line end")
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return (f"checkpoint seq {snap.head_seq}: bytes {start}..{snap.end} of the events file "
                        "are not one event line")
            if not isinstance(event, dict) or event.get("seq") != snap.head_seq:
                got = event.get("seq") if isinstance(event, dict) else None
                return f"checkpoint head seq {snap.head_seq} does not match the event at its last offset (seq {got!r})"
            if event.get("event_hash") != snap.head_hash or self._event_hash(event) != snap.head_hash:
                return f"checkpoint event_hash at seq {snap.head_seq} does not match the events file"
        self._offsets = snap.offsets
        self._end = snap.end
        self._head_hash = snap.head_hash
        self._latest_by_observation = _LatestIndex(_as_sorted(snap.by_observation))
        self._latest_by_identity = _LatestIndex(_as_sorted(snap.by_identity))
        return None

    @staticmethod
    def _parse_event_line(line: bytes, expected_seq: int) -> Any:
        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalCorrupt(f"invalid journal line {expected_seq}") from exc

    def _replay(self) -> int:
        """Read, validate and index every event after the current head (all of them when the index
        is empty), chained onto the current head; return how many. A torn last line is repaired as
        before: completed if it parses, truncated away if it does not."""
        previous_hash = self._head_hash or ""
        expected_seq = self.head_seq + 1
        offset = self._end
        replayed = 0
        with open(self.events_path, "rb") as fh:
            fh.seek(offset)
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
                event = self._parse_event_line(line, expected_seq)
                self._validate_event(event, expected_seq, previous_hash)
                self._index(event, offset, len(line))
                offset += len(line)
                expected_seq += 1
                previous_hash = event["event_hash"]
                replayed += 1
        return replayed

    def _snapshot(self) -> _Snapshot:
        """Copy the four things under the lock: the offsets array, and of each index its small dict of
        entries since the last checkpoint (its sorted base is immutable and shared). No encoding."""
        with self._lock:
            return _Snapshot(
                end=self._end,
                head_hash=self._head_hash,
                offsets=self._offsets[:],
                by_observation=self._latest_by_observation.frozen_copy(),
                by_identity=self._latest_by_identity.frozen_copy(),
            )

    @property
    def checkpoint_seq(self) -> int | None:
        """Head seq of the checkpoint on disk known to match this journal (None: none yet)."""
        return self._ckpt_seq

    def checkpoint_due(self) -> bool:
        """True after a boot that has not written its checkpoint yet, or once ``CKPT_EVERY`` events
        were written after the last checkpoint."""
        with self._lock:
            head = self.head_seq
        written = self._ckpt_seq
        return written is None or self._boot_ckpt_pending or head - written >= CKPT_EVERY

    def write_checkpoint(self) -> dict[str, Any] | None:
        """Write the checkpoint for the current head; return {seq, bytes, seconds}, or None when a
        newer one was written meanwhile. Only the copy of the four things holds the journal lock;
        encoding and the durable write (``_durable_replace``) run outside it, so appends go on.
        ``_ckpt_lock`` keeps two writers in order (a later head is never replaced by an earlier)."""
        with self._ckpt_lock:
            started = time.monotonic()
            snap = self._snapshot()
            if self._ckpt_seq is not None and snap.head_seq < self._ckpt_seq:
                return None
            by_observation, by_identity = _as_sorted(snap.by_observation), _as_sorted(snap.by_identity)
            data = _encode_checkpoint(snap._replace(by_observation=by_observation, by_identity=by_identity))
            _durable_replace(self.checkpoint_path, data)
            with self._lock:
                # the merged bases now hold everything the copies held; the dicts keep only newer sets
                self._latest_by_observation.rebase(by_observation, snap.by_observation.overlay)
                self._latest_by_identity.rebase(by_identity, snap.by_identity.overlay)
            self._ckpt_seq = snap.head_seq
            self._boot_ckpt_pending = False
            return {"seq": snap.head_seq, "bytes": len(data),
                    "seconds": round(time.monotonic() - started, 3)}

    def maybe_checkpoint(self) -> dict[str, Any] | None:
        """``write_checkpoint`` when ``checkpoint_due``; else None."""
        if not self.checkpoint_due():
            return None
        return self.write_checkpoint()

    def verify_chain(self) -> dict[str, Any]:
        """Validate the whole chain from the first event and compare what it derives with memory.

        Takes a snapshot of the four things under the lock, then reads the events file up to the
        snapshot's head without the lock (the file is append-only), validating every event exactly as
        a full build does and deriving the offsets, the two indexes and the head. Never raises for a
        bad journal: returns {"ok", "checked" (events validated), "head_seq", "first_bad_seq",
        "error", "seconds"}, ``first_bad_seq`` being the first event that fails to validate or whose
        offset or index entry differs from memory."""
        started = time.monotonic()
        snap = self._snapshot()
        head = snap.head_seq
        offsets = array("q")
        by_observation: dict[int, int] = {}
        by_identity: dict[int, int] = {}
        previous_hash = ""
        checked = 0
        offset = 0
        first_bad: int | None = None
        error: str | None = None
        if head:
            with open(self.events_path, "rb") as fh:
                while checked < head:
                    expected = checked + 1
                    line = fh.readline()
                    if not line.endswith(b"\n"):
                        first_bad, error = expected, f"events file ends before seq {expected}"
                        break
                    try:
                        event = self._parse_event_line(line, expected)
                        self._validate_event(event, expected, previous_hash)
                    except JournalCorrupt as exc:
                        first_bad, error = expected, str(exc)
                        break
                    offsets.append(offset)
                    by_observation[_identity_key(event["observation_id"])] = expected
                    by_identity[_identity_key(event["source"], event["source_id"])] = expected
                    offset += len(line)
                    previous_hash = event["event_hash"]
                    checked = expected
        if first_bad is None:
            first_bad, error = self._first_mismatch(
                snap, offsets, offset, previous_hash or None, by_observation, by_identity)
        return {"ok": first_bad is None, "checked": checked, "head_seq": head,
                "first_bad_seq": first_bad, "error": error,
                "seconds": round(time.monotonic() - started, 3)}

    @staticmethod
    def _first_mismatch(snap: _Snapshot, offsets: array, end: int, head_hash: str | None,
                        by_observation: dict[int, int], by_identity: dict[int, int]):
        """The first seq at which what the events derive differs from the memory snapshot, and why.
        The indexes are compared by lookup (what ``_latest`` does), and by size for extra keys."""
        found: list[tuple[int, str]] = []
        if offsets != snap.offsets:
            seq = next((i + 1 for i, (a, b) in enumerate(zip(offsets, snap.offsets)) if a != b),
                       min(len(offsets), len(snap.offsets)) + 1)
            found.append((seq, f"offset index differs from the events file at seq {seq}"))
        if end != snap.end or head_hash != snap.head_hash:
            found.append((snap.head_seq, f"chain head differs from the events file at seq {snap.head_seq}"))
        for name, derived, memory in (("observation", by_observation, snap.by_observation),
                                      ("source identity", by_identity, snap.by_identity)):
            seqs = []
            for key, seq in derived.items():
                held = memory.get(key)
                if held != seq:
                    seqs.append(seq if held is None else min(seq, held))
            if len(memory) != len(derived):
                seqs.extend(memory.get(key) for key in memory if key not in derived)
            if not seqs:
                continue
            seq = min(seqs)
            found.append((seq, f"latest-by-{name} index differs from the events file at seq {seq}"))
        if not found:
            return None, None
        return min(found)

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

    def scrub_blobs(self) -> dict[str, Any]:
        """Check every blob the journal references, once each, with the same check a read uses.

        Runs without the journal lock: the events file is append-only, so the bytes up to the end
        seen at the start never change, and ``_read_blob`` touches no shared state. One blob at a
        time. A missing or altered blob is reported, not raised (a read of it still raises).
        Returns {"checked": unique blobs checked, "bad": [{"payload_hash", "seq", "error"}] with
        ``seq`` the first event naming it, "seconds"}."""
        started = time.monotonic()
        with self._lock:
            end = self._end
        seen: set[bytes | str] = set()
        bad: list[dict[str, Any]] = []
        checked = 0
        seq = 0
        if end:
            with open(self.events_path, "rb") as fh:
                while fh.tell() < end:
                    line = fh.readline()
                    if not line:
                        break
                    seq += 1
                    try:
                        payload_hash = json.loads(line.decode("utf-8"))["payload_hash"]
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                        raise JournalCorrupt(f"journal line {seq} changed on disk") from exc
                    if payload_hash is None:
                        continue
                    try:
                        key: bytes | str = bytes.fromhex(payload_hash)
                    except (TypeError, ValueError):
                        key = str(payload_hash)
                    if key in seen:
                        continue
                    seen.add(key)
                    checked += 1
                    try:
                        self._read_blob(payload_hash)
                    except JournalCorrupt as exc:
                        bad.append({"payload_hash": payload_hash, "seq": seq, "error": str(exc)})
        return {"checked": checked, "bad": bad, "seconds": round(time.monotonic() - started, 3)}

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
