import json
import os
import queue
import sqlite3
import threading
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from omniseek.core.recall import journal as journal_module
from omniseek.core.recall.journal import JournalCorrupt, ObservationJournal
from omniseek.core.normalize import Document
from omniseek.core import fetcher, profile
from omniseek.core import recall as recall_package
from omniseek.core.recall import writer
from omniseek.core.recall import store
from omniseek.core.recall import embed


@contextmanager
def _temporary_store(root: Path):
    original_db_path = store.DB_PATH
    original_disabled = store._disabled
    original_local = store._local
    store.DB_PATH = root / "index.db"
    store._disabled = False
    store._local = threading.local()
    try:
        if not store.init():
            raise AssertionError("temporary recall store failed to initialize")
        con = store.connect()
        try:
            yield con
        finally:
            con.close()
    finally:
        store.DB_PATH = original_db_path
        store._disabled = original_disabled
        store._local = original_local


def _full_doc(source_id: str = "item-1") -> Document:
    return Document(
        source="public-feed",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        title=f"Full {source_id}",
        content=f"Durable content for {source_id}",
    )


def _seed_full(con, doc: Document) -> None:
    with patch.object(embed, "available", return_value=False):
        writer._apply(con, [[doc]])
    con.execute(
        "UPDATE docs SET last_seen = 0 WHERE source = ? AND source_id = ?",
        (doc.source, doc.source_id),
    )
    con.commit()


def _seed_thin(con, doc: Document) -> None:
    with patch.object(embed, "available", return_value=False):
        writer._apply(con, [("__thin__", doc)])
    from omniseek.core.recall.graph import doc_node_id
    con.execute("UPDATE graph_nodes SET last_seen = 0 WHERE id = ?", (
        doc_node_id(doc.source, doc.source_id),
    ))
    con.commit()


class ObservationJournalTests(unittest.TestCase):
    def test_append_is_durable_content_addressed_and_privacy_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            journal = ObservationJournal(Path(td))
            receipt = journal.append_payload(
                {
                    "source": "public-feed",
                    "source_id": "item-1",
                    "title": "A finding",
                    "metadata": {
                        "doi": "10.1234/example",
                        "access_token": "must-not-persist",
                        "raw": {"cookie": "must-not-persist"},
                    },
                },
                source="public-feed",
                source_id="item-1",
                observed_at=100.0,
                provenance="retrieved",
                privacy_namespace="public",
                lane="full",
            )
            self.assertEqual(receipt.journal_status, "local-durable")
            self.assertEqual(receipt.materialization_status, "pending")
            self.assertIsNotNone(receipt.payload_hash)
            events = journal.events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["seq"], 1)
            self.assertEqual(events[0]["payload_hash"], receipt.payload_hash)
            blobs = list((Path(td) / "blobs").iterdir())
            self.assertEqual([p.name for p in blobs], [receipt.payload_hash])
            payload = json.loads(blobs[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["metadata"]["doi"], "10.1234/example")
            self.assertNotIn("access_token", payload["metadata"])
            self.assertNotIn("raw", payload["metadata"])
            pending = journal.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].observation_id, receipt.observation_id)
            self.assertEqual(pending[0].payload["title"], "A finding")

    def test_append_survives_a_platform_without_directory_fsync(self):
        """Windows exposes no directory fsync: os.open(<dir>) raises PermissionError there. That
        used to take the whole append down (the file was written AND fsynced, then the
        directory-entry commit blew up), so every observation was lost while the failure surfaced
        only as a WARNING line. Simulate that platform exactly and require the append to land."""
        real_open = os.open

        def _posixless_open(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            journal = ObservationJournal(Path(td))
            with patch.object(journal_module, "_DIR_FSYNC_SUPPORTED", False), \
                 patch.object(journal_module.os, "open", _posixless_open):
                receipt = journal.append_payload(
                    {"source": "public-feed", "source_id": "win-1", "title": "No dir fsync here"},
                    source="public-feed",
                    source_id="win-1",
                    observed_at=100.0,
                    provenance="retrieved",
                    privacy_namespace="public",
                    lane="full",
                )
            self.assertEqual(receipt.journal_status, "local-durable")
            self.assertEqual(len(journal.events()), 1)
            self.assertEqual(
                [p.name for p in (Path(td) / "blobs").iterdir()], [receipt.payload_hash]
            )

    def test_directory_fsync_failure_is_not_swallowed_where_supported(self):
        """The fix is a PLATFORM gate, not a try/except. Where directory fsync IS available a
        genuine failure must still propagate: a journal that swallowed it would be reporting a
        durability it does not have, which is the one lie this component cannot afford."""
        if os.name != "posix":
            self.skipTest("directory fsync is POSIX-only")

        def _boom(fd):
            raise OSError(5, "Input/output error")

        with tempfile.TemporaryDirectory() as td:
            with patch.object(journal_module, "_DIR_FSYNC_SUPPORTED", True), \
                 patch.object(journal_module.os, "fsync", _boom):
                with self.assertRaises(OSError):
                    journal_module._fsync_directory(Path(td))

    def test_same_observation_and_payload_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            journal = ObservationJournal(Path(td))
            kwargs = dict(
                source="public-feed",
                source_id="item-1",
                observed_at=100.0,
                provenance="retrieved",
                privacy_namespace="public",
                lane="full",
            )
            first = journal.append_payload({"title": "same"}, **kwargs)
            second = journal.append_payload({"title": "same"}, **kwargs)
            self.assertEqual(first.observation_id, second.observation_id)
            self.assertEqual(first.journal_seq, second.journal_seq)
            self.assertEqual(len(journal.events()), 1)

    def test_torn_final_line_is_repaired_but_interior_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = ObservationJournal(root)
            journal.append_payload(
                {"title": "one"}, source="s", source_id="1", observed_at=1.0,
                provenance="retrieved", privacy_namespace="public", lane="full",
            )
            events_path = root / "events.ndjson"
            clean = events_path.read_text(encoding="utf-8")
            events_path.write_text(clean + '{"seq":', encoding="utf-8")
            repaired = ObservationJournal(root)
            self.assertEqual(len(repaired.events()), 1)
            self.assertEqual(events_path.read_text(encoding="utf-8"), clean)

            events_path.write_text(clean + "not-json\n" + clean, encoding="utf-8")
            with self.assertRaises(JournalCorrupt):
                ObservationJournal(root)

    def test_tombstone_is_replayed_as_a_first_class_event(self):
        with tempfile.TemporaryDirectory() as td:
            journal = ObservationJournal(Path(td))
            receipt = journal.append_tombstone(
                source="public-feed", source_id="item-1", observed_at=2.0,
                provenance="sweep", privacy_namespace="public", reason="expired",
            )
            self.assertEqual(receipt.journal_status, "local-durable")
            event = journal.events()[0]
            self.assertEqual(event["kind"], "tombstone")
            self.assertEqual(journal.pending()[0].payload, None)

    def test_guarded_tombstone_reuses_latest_privacy_and_skips_pending_identity(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            journal = ObservationJournal(Path(td))
            journal.append_payload(
                {"title": "private observation"},
                source="walled-feed",
                source_id="item-1",
                observed_at=1.0,
                provenance="retrieved",
                privacy_namespace="walled",
                lane="thin",
            )

            skipped = journal.append_tombstone(
                source="walled-feed",
                source_id="item-1",
                observed_at=2.0,
                provenance="sweep",
                privacy_namespace="public",
                reason="expired",
                materialized_through=0,
            )
            self.assertIsNone(skipped)
            self.assertEqual(len(journal.events()), 1)

            receipt = journal.append_tombstone(
                source="walled-feed",
                source_id="item-1",
                observed_at=3.0,
                provenance="sweep",
                privacy_namespace="public",
                reason="expired",
                materialized_through=1,
            )
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt.journal_seq, 2)
            self.assertEqual(journal.events()[-1]["privacy_namespace"], "walled")

    def test_writer_journals_before_queue_overflow(self):
        with tempfile.TemporaryDirectory() as td:
            journal = ObservationJournal(Path(td) / "journal")
            original_queue = writer._queue
            original_enabled = writer.WRITES_ENABLED
            original_journal = getattr(writer, "_observation_journal", None)
            writer._queue = queue.Queue(maxsize=1)
            writer._observation_journal = journal
            writer.WRITES_ENABLED = True
            try:
                writer._enqueue("already-queued")
                doc = Document(
                    source="public-feed",
                    source_id="item-1",
                    url="https://example.test/item-1",
                    title="A finding",
                    content="Useful content",
                )
                with patch.object(recall_package, "indexable", return_value=False), \
                     patch.object(fetcher, "is_walled_source", return_value=False), \
                     patch.object(profile, "remember_walled_retrievals", return_value=False):
                    self.assertIsNone(writer.maybe_ingest([doc]))
                self.assertEqual(writer._queue.qsize(), 1)
                pending = journal.pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].source_id, "item-1")
                self.assertEqual(pending[0].payload["title"], "A finding")
            finally:
                writer._queue = original_queue
                writer.WRITES_ENABLED = original_enabled
                writer._observation_journal = original_journal

    def test_walled_privacy_denial_prevents_journal_event(self):
        with tempfile.TemporaryDirectory() as td:
            journal = ObservationJournal(Path(td) / "journal")
            original_queue = writer._queue
            original_enabled = writer.WRITES_ENABLED
            original_journal = getattr(writer, "_observation_journal", None)
            writer._queue = queue.Queue(maxsize=1)
            writer._observation_journal = journal
            writer.WRITES_ENABLED = True
            try:
                doc = Document(
                    source="private-forum",
                    source_id="private-1",
                    url="https://private.example/item-1",
                    title="Private finding",
                    content="Must not be remembered",
                )
                with patch.object(recall_package, "indexable", return_value=False), \
                     patch.object(fetcher, "is_walled_source", return_value=True), \
                     patch.object(profile, "remember_walled_retrievals", return_value=False):
                    self.assertIsNone(writer.maybe_ingest([doc]))
                self.assertEqual(journal.events(), [])
                self.assertEqual(writer._queue.qsize(), 0)
            finally:
                writer._queue = original_queue
                writer.WRITES_ENABLED = original_enabled
                writer._observation_journal = original_journal

    def test_sqlite_cursor_and_empty_database_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            full_doc = Document(
                source="public-feed",
                source_id="full-1",
                url="https://example.test/full-1",
                title="Full observation",
                content="Durable full content",
            )
            thin_doc = Document(
                source="query-source",
                source_id="thin-1",
                url="https://example.test/thin-1",
                title="Thin observation",
                content="This content must not enter the thin materialization",
                metadata={"doi": "10.1234/thin", "raw": {"cookie": "no"}},
            )
            journal.append_payload(
                full_doc.model_dump(mode="json"),
                source=full_doc.source,
                source_id=full_doc.source_id,
                observed_at=1.0,
                provenance="retrieved",
                privacy_namespace="public",
                lane="full",
            )
            journal.append_payload(
                writer._journal_payload(thin_doc, "thin"),
                source=thin_doc.source,
                source_id=thin_doc.source_id,
                observed_at=2.0,
                provenance="retrieved",
                privacy_namespace="public",
                lane="thin",
            )

            original_db_path = store.DB_PATH
            original_disabled = store._disabled
            original_local = store._local
            try:
                with patch.object(embed, "available", return_value=False):
                    for db_name in ("first.db", "rebuilt.db"):
                        store.DB_PATH = root / db_name
                        store._disabled = False
                        store._local = threading.local()
                        self.assertTrue(store.init())
                        con = store.connect()
                        try:
                            self.assertEqual(writer._materialize_pending(con, journal), 2)
                            self.assertEqual(
                                con.execute(
                                    "SELECT count(*) FROM docs WHERE source='public-feed' AND source_id='full-1'"
                                ).fetchone()[0],
                                1,
                            )
                            thin_row = con.execute(
                                "SELECT label, attrs_json FROM graph_nodes WHERE id='doc:query-source:thin-1'"
                            ).fetchone()
                            self.assertIsNotNone(thin_row)
                            self.assertEqual(thin_row[0], "Thin observation")
                            self.assertNotIn("This content must not enter", thin_row[1])
                            cursor = dict(
                                con.execute(
                                    "SELECT k, v FROM meta WHERE k IN "
                                    "('journal_materialized_seq', 'journal_materialized_hash')"
                                ).fetchall()
                            )
                            self.assertEqual(cursor["journal_materialized_seq"], "2")
                            self.assertEqual(cursor["journal_materialized_hash"], journal.events()[1]["event_hash"])
                            self.assertEqual(writer._materialize_pending(con, journal), 0)
                        finally:
                            con.close()
            finally:
                store.DB_PATH = original_db_path
                store._disabled = original_disabled
                store._local = original_local

    def test_queue_overflow_replays_every_pending_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            original_queue = writer._queue
            original_enabled = writer.WRITES_ENABLED
            original_journal = getattr(writer, "_observation_journal", None)
            original_db_path = store.DB_PATH
            original_disabled = store._disabled
            original_local = store._local
            writer._queue = queue.Queue(maxsize=1)
            writer._observation_journal = journal
            writer.WRITES_ENABLED = True
            store.DB_PATH = root / "index.db"
            store._disabled = False
            store._local = threading.local()
            try:
                self.assertTrue(store.init())
                docs = [
                    Document(
                        source="public-feed",
                        source_id=f"item-{index}",
                        url=f"https://example.test/item-{index}",
                        title=f"Finding {index}",
                        content=f"Content {index}",
                    )
                    for index in (1, 2)
                ]
                with patch.object(recall_package, "indexable", return_value=True), \
                     patch.object(fetcher, "is_walled_source", return_value=False), \
                     patch.object(profile, "remember_walled_retrievals", return_value=False):
                    writer.maybe_ingest([docs[0]])
                    writer.maybe_ingest([docs[1]])
                self.assertEqual(writer._queue.qsize(), 1)
                items = [writer._queue.get_nowait()]
                con = store.connect()
                try:
                    with patch.object(embed, "available", return_value=False):
                        writer._process_writer_items(con, items, journal=journal)
                    self.assertEqual(
                        con.execute("SELECT count(*) FROM docs WHERE source='public-feed'").fetchone()[0],
                        2,
                    )
                    cursor = dict(
                        con.execute(
                            "SELECT k, v FROM meta WHERE k IN "
                            "('journal_materialized_seq', 'journal_materialized_hash')"
                        ).fetchall()
                    )
                    self.assertEqual(cursor["journal_materialized_seq"], "2")
                    self.assertEqual(cursor["journal_materialized_hash"], journal.events()[1]["event_hash"])
                finally:
                    con.close()
            finally:
                writer._queue = original_queue
                writer.WRITES_ENABLED = original_enabled
                writer._observation_journal = original_journal
                store.DB_PATH = original_db_path
                store._disabled = original_disabled
                store._local = original_local

    def test_tombstone_then_same_payload_creates_a_new_observation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            kwargs = dict(
                source="public-feed",
                source_id="item-1",
                provenance="retrieved",
                privacy_namespace="public",
                lane="full",
            )
            first = journal.append_payload({"title": "same"}, observed_at=1.0, **kwargs)
            deleted = journal.append_tombstone(
                source="public-feed",
                source_id="item-1",
                observed_at=2.0,
                provenance="sweep",
                privacy_namespace="public",
                reason="expired",
            )
            restored = journal.append_payload({"title": "same"}, observed_at=3.0, **kwargs)
            self.assertEqual((first.journal_seq, deleted.journal_seq, restored.journal_seq), (1, 2, 3))
            self.assertEqual([event["kind"] for event in journal.events()], [
                "observation", "tombstone", "observation",
            ])

    def test_tombstone_materialization_and_reobservation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = Document(
                source="public-feed",
                source_id="item-1",
                url="https://example.test/item-1",
                title="Restorable",
                content="Same content",
            )
            append_kwargs = dict(
                source=doc.source,
                source_id=doc.source_id,
                provenance="retrieved",
                privacy_namespace="public",
                lane="full",
            )
            journal.append_payload(doc.model_dump(mode="json"), observed_at=1.0, **append_kwargs)
            journal.append_tombstone(
                source=doc.source,
                source_id=doc.source_id,
                observed_at=2.0,
                provenance="sweep",
                privacy_namespace="public",
                reason="expired",
            )

            original_db_path = store.DB_PATH
            original_disabled = store._disabled
            original_local = store._local
            store.DB_PATH = root / "index.db"
            store._disabled = False
            store._local = threading.local()
            try:
                self.assertTrue(store.init())
                con = store.connect()
                try:
                    with patch.object(embed, "available", return_value=False):
                        self.assertEqual(writer._materialize_pending(con, journal), 2)
                    self.assertEqual(
                        con.execute("SELECT count(*) FROM docs WHERE source_id='item-1'").fetchone()[0],
                        0,
                    )
                    restored = journal.append_payload(
                        doc.model_dump(mode="json"), observed_at=3.0, **append_kwargs
                    )
                    self.assertEqual(restored.journal_seq, 3)
                    with patch.object(embed, "available", return_value=False):
                        self.assertEqual(writer._materialize_pending(con, journal), 1)
                    self.assertEqual(
                        con.execute("SELECT count(*) FROM docs WHERE source_id='item-1'").fetchone()[0],
                        1,
                    )
                finally:
                    con.close()
            finally:
                store.DB_PATH = original_db_path
                store._disabled = original_disabled
                store._local = original_local

    def test_sweep_full_document_is_journaled_then_materialized(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = _full_doc()
            original_journal = writer._observation_journal
            writer._observation_journal = journal
            try:
                with _temporary_store(root) as con:
                    _seed_full(con, doc)
                    writer._sweep(con)

                    events = journal.events()
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["kind"], "tombstone")
                    self.assertEqual((events[0]["source"], events[0]["source_id"]),
                                     (doc.source, doc.source_id))
                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM docs WHERE source = ? AND source_id = ?",
                            (doc.source, doc.source_id),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(writer._materialization_cursor(con, journal), 1)
            finally:
                writer._observation_journal = original_journal

    def test_sweep_thin_document_is_journaled_then_materialized(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = _full_doc("thin-1")
            original_journal = writer._observation_journal
            writer._observation_journal = journal
            try:
                with _temporary_store(root) as con:
                    _seed_thin(con, doc)
                    writer._sweep(con)

                    self.assertEqual([event["kind"] for event in journal.events()], ["tombstone"])
                    from omniseek.core.recall.graph import doc_node_id
                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM graph_nodes WHERE id = ?",
                            (doc_node_id(doc.source, doc.source_id),),
                        ).fetchone()[0],
                        0,
                    )
            finally:
                writer._observation_journal = original_journal

    def test_sweep_deduplicates_full_and_thin_identity(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = _full_doc("both-1")
            original_journal = writer._observation_journal
            writer._observation_journal = journal
            try:
                with _temporary_store(root) as con:
                    _seed_full(con, doc)
                    _seed_thin(con, doc)
                    writer._sweep(con)

                    self.assertEqual(len(journal.events()), 1)
                    self.assertEqual(journal.events()[0]["kind"], "tombstone")
                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM docs WHERE source = ? AND source_id = ?",
                            (doc.source, doc.source_id),
                        ).fetchone()[0],
                        0,
                    )
                    from omniseek.core.recall.graph import doc_node_id
                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM graph_nodes WHERE id = ?",
                            (doc_node_id(doc.source, doc.source_id),),
                        ).fetchone()[0],
                        0,
                    )
            finally:
                writer._observation_journal = original_journal

    def test_snapshot_plus_sweep_journal_replay_does_not_resurrect(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = _full_doc("snapshot-1")
            snapshot_path = root / "snapshot.db"
            original_journal = writer._observation_journal
            writer._observation_journal = journal
            try:
                with _temporary_store(root) as con:
                    _seed_full(con, doc)
                    snapshot = sqlite3.connect(snapshot_path)
                    try:
                        con.backup(snapshot)
                    finally:
                        snapshot.close()

                    writer._sweep(con)

                restored = sqlite3.connect(snapshot_path)
                try:
                    with patch.object(embed, "available", return_value=False):
                        self.assertEqual(writer._materialize_pending(restored, journal), 1)
                    self.assertEqual(
                        restored.execute(
                            "SELECT count(*) FROM docs WHERE source = ? AND source_id = ?",
                            (doc.source, doc.source_id),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    restored.close()
            finally:
                writer._observation_journal = original_journal

    def test_sweep_keeps_rows_when_tombstone_append_fails(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = _full_doc("disk-full")
            original_journal = writer._observation_journal
            original_failures = writer._journal_failures
            original_last_failure = writer._last_journal_failure
            writer._observation_journal = journal
            writer._journal_failures = 0
            writer._last_journal_failure = None
            try:
                with _temporary_store(root) as con:
                    _seed_full(con, doc)
                    with patch.object(
                        journal,
                        "append_tombstone",
                        side_effect=OSError("journal disk full"),
                    ):
                        writer._sweep(con)

                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM docs WHERE source = ? AND source_id = ?",
                            (doc.source, doc.source_id),
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(journal.events(), [])
                    self.assertEqual(writer._journal_failures, 1)
                    self.assertIn("journal disk full", writer._last_journal_failure)
            finally:
                writer._observation_journal = original_journal
                writer._journal_failures = original_failures
                writer._last_journal_failure = original_last_failure

    def test_sweep_materializes_durable_prefix_before_a_later_append_failure(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            first = _full_doc("a-first")
            second = _full_doc("b-second")
            original_journal = writer._observation_journal
            original_append = journal.append_tombstone
            original_failures = writer._journal_failures
            original_last_failure = writer._last_journal_failure
            writer._observation_journal = journal
            writer._journal_failures = 0
            writer._last_journal_failure = None

            def append_or_fail(**kwargs):
                if kwargs["source_id"] == second.source_id:
                    raise OSError("later journal failure")
                return original_append(**kwargs)

            try:
                with _temporary_store(root) as con:
                    _seed_full(con, first)
                    _seed_full(con, second)
                    with patch.object(journal, "append_tombstone", side_effect=append_or_fail):
                        writer._sweep(con)

                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM docs WHERE source_id = ?",
                            (first.source_id,),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM docs WHERE source_id = ?",
                            (second.source_id,),
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual([event["source_id"] for event in journal.events()], ["a-first"])
                    self.assertEqual(writer._materialization_cursor(con, journal), 1)
            finally:
                writer._observation_journal = original_journal
                writer._journal_failures = original_failures
                writer._last_journal_failure = original_last_failure

    def test_sweep_does_not_tombstone_a_pending_reobservation(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("omniseek.core.recall.journal._fsync_directory"):
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = _full_doc("pending-1")
            original_journal = writer._observation_journal
            writer._observation_journal = journal
            try:
                with _temporary_store(root) as con:
                    _seed_full(con, doc)
                    journal.append_payload(
                        doc.model_dump(mode="json"),
                        source=doc.source,
                        source_id=doc.source_id,
                        observed_at=1.0,
                        provenance="retrieved",
                        privacy_namespace="public",
                        lane="full",
                    )

                    writer._sweep(con)

                    self.assertEqual([event["kind"] for event in journal.events()], ["observation"])
                    self.assertEqual(
                        con.execute(
                            "SELECT count(*) FROM docs WHERE source = ? AND source_id = ?",
                            (doc.source, doc.source_id),
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(writer._materialization_cursor(con, journal), 1)
            finally:
                writer._observation_journal = original_journal

    def test_materialization_failure_keeps_cursor_and_is_visible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = ObservationJournal(root / "journal")
            doc = Document(
                source="public-feed",
                source_id="bad-lane",
                url="https://example.test/bad-lane",
                title="Bad lane",
                content="Valid payload, invalid materialization lane",
            )
            journal.append_payload(
                doc.model_dump(mode="json"),
                source=doc.source,
                source_id=doc.source_id,
                observed_at=1.0,
                provenance="retrieved",
                privacy_namespace="public",
                lane="unknown-lane",
            )

            original_db_path = store.DB_PATH
            original_disabled = store._disabled
            original_local = store._local
            original_failures = writer._materialization_failures
            original_last_failure = writer._last_materialization_failure
            store.DB_PATH = root / "index.db"
            store._disabled = False
            store._local = threading.local()
            writer._materialization_failures = 0
            writer._last_materialization_failure = None
            try:
                self.assertTrue(store.init())
                con = store.connect()
                try:
                    with patch.object(embed, "available", return_value=False):
                        self.assertEqual(writer._materialize_pending(con, journal), 0)
                    cursor = dict(
                        con.execute(
                            "SELECT k, v FROM meta WHERE k IN "
                            "('journal_materialized_seq', 'journal_materialized_hash')"
                        ).fetchall()
                    )
                    self.assertNotIn("journal_materialized_seq", cursor)
                    health = writer.journal_health(journal=journal, con=con)
                    self.assertEqual(health["journal_head_seq"], 1)
                    self.assertEqual(health["last_materialized_seq"], 0)
                    self.assertEqual(health["pending_materializations"], 1)
                    self.assertEqual(health["failed_receipts"], 1)
                    self.assertIn("seq=1", health["last_failure"])
                finally:
                    con.close()
            finally:
                store.DB_PATH = original_db_path
                store._disabled = original_disabled
                store._local = original_local
                writer._materialization_failures = original_failures
                writer._last_materialization_failure = original_last_failure


if __name__ == "__main__":
    unittest.main(verbosity=2)
