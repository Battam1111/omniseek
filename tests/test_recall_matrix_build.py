"""eye-mem-2: the recall matrices are built by streaming the cursor into one preallocated array.

The result must equal the old one-shot build (fetchall + join + normalize + astype) row for row,
keep ids / srcs aligned, absorb a row count that moved between the count and the fetch, and stay
fail-open (a malformed row -> no matrix, the next call retries)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from omniseek.core.recall import store

MV = "test-mv/d8"
DIM = 8


def _old_build(rows):
    M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    nrm = np.linalg.norm(M, axis=1, keepdims=True)
    return (M / np.where(nrm > 0, nrm, 1.0)).astype(np.float32)


def _db(n_vec=0, n_thin=0, n_chunk=0, seed=0):
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("CREATE TABLE docs (rowid INTEGER PRIMARY KEY, source TEXT)")
    con.execute("CREATE TABLE vec (rowid INTEGER PRIMARY KEY, v BLOB, model_version TEXT)")
    con.execute("CREATE TABLE vec_thin (node_id TEXT PRIMARY KEY, v BLOB, model_version TEXT)")
    con.execute("CREATE TABLE vec_chunk (rowid INTEGER, v BLOB, model_version TEXT)")
    for i in range(1, max(n_vec, n_chunk) + 1):
        con.execute("INSERT INTO docs VALUES (?, ?)", (i * 3, f"src{i % 5}"))
    for i in range(1, n_vec + 1):
        v = rng.normal(size=DIM).astype(np.float32)
        if i == 2:
            v[:] = 0.0  # zero-norm row stays zero, as before
        con.execute("INSERT INTO vec VALUES (?, ?, ?)", (i * 3, v.tobytes(), MV))
    con.execute("INSERT INTO vec VALUES (?, ?, ?)", (999999, np.ones(DIM, np.float32).tobytes(), "other"))
    for i in range(n_thin):
        con.execute("INSERT INTO vec_thin VALUES (?, ?, ?)",
                    (f"doc:x:{i}", rng.normal(size=DIM).astype(np.float32).tobytes(), MV))
    for i in range(n_chunk):
        con.execute("INSERT INTO vec_chunk VALUES (?, ?, ?)",
                    (((i % max(n_vec, 1)) + 1) * 3, rng.normal(size=DIM).astype(np.float32).tobytes(), MV))
    con.commit()
    return con


class _StoreState(unittest.TestCase):
    def setUp(self):
        names = [n for n in vars(store) if n.startswith(("_vec_", "_thin_", "_chunk_"))]
        self._saved = {n: getattr(store, n) for n in names}
        self._saved["_BUILD_BATCH"] = store._BUILD_BATCH
        self._saved["_mstate"] = store._mstate
        self._saved["DB_PATH"] = store.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        store.DB_PATH = Path(self._tmp.name) / "index.db"  # file-backed matrix rows land here
        store._mstate = {k: store._MatrixState() for k in ("vec", "thin", "chunk")}
        store.discard_staged()
        for p in ("vec", "thin", "chunk"):
            setattr(store, f"_{p}_M", None)
            setattr(store, f"_{p}_ids", None)
            setattr(store, f"_{p}_built_gen", -1)
            setattr(store, f"_{p}_built_mv", "")
        store._vec_srcs = store._chunk_srcs = None
        self._mv = mock.patch.object(store, "_model_version", return_value=MV)
        self._mv.start()

    def tearDown(self):
        self._mv.stop()
        for n, v in self._saved.items():
            setattr(store, n, v)
        self._tmp.cleanup()


class MatrixBuildTest(_StoreState):
    def test_vec_equals_old_build_across_batches(self):
        store._BUILD_BATCH = 7  # 50 rows -> 8 batches, last one partial
        con = _db(n_vec=50)
        rows = con.execute("SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
                           "WHERE v.model_version = ?", (MV,)).fetchall()
        M, ids = store._ensure_matrix(con)
        self.assertEqual(M.dtype, np.float32)
        self.assertEqual(M.shape, (50, DIM))
        np.testing.assert_array_equal(M, _old_build(rows))
        np.testing.assert_array_equal(ids, np.array([r[0] for r in rows], dtype=np.int64))
        self.assertEqual(list(store._vec_srcs), [r[2] for r in rows])
        self.assertTrue(np.all(M[1] == 0.0))

    def test_thin_and_chunk_equal_old_build(self):
        store._BUILD_BATCH = 4
        con = _db(n_vec=5, n_thin=13, n_chunk=11)
        trows = con.execute("SELECT node_id, v FROM vec_thin WHERE model_version = ?", (MV,)).fetchall()
        M, ids = store._ensure_thin_matrix(con)
        np.testing.assert_array_equal(M, _old_build(trows))
        self.assertEqual(list(ids), [r[0] for r in trows])
        self.assertEqual(ids.dtype, object)
        crows = con.execute("SELECT c.rowid, c.v, d.source FROM vec_chunk c JOIN docs d ON c.rowid = d.rowid "
                            "WHERE c.model_version = ?", (MV,)).fetchall()
        M, ids = store._ensure_chunk_matrix(con)
        np.testing.assert_array_equal(M, _old_build(crows))
        np.testing.assert_array_equal(ids, [r[0] for r in crows])
        self.assertEqual(list(store._chunk_srcs), [r[2] for r in crows])

    def test_vector_search_unchanged(self):
        store._BUILD_BATCH = 3
        con = _db(n_vec=20, n_chunk=6)
        q = np.random.default_rng(5).normal(size=DIM).astype(np.float32)
        with mock.patch.object(store, "_read_con", return_value=con), \
                mock.patch.object(store, "_hydrate_rowids", side_effect=lambda c, r, recall_via: r), \
                mock.patch.object(store, "_disabled", False):
            got = store.vector_search(q, k=5)
            scoped = store.vector_search(q, k=5, sources=frozenset({"src1"}))
        rows = con.execute("SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
                           "WHERE v.model_version = ?", (MV,)).fetchall()
        crows = con.execute("SELECT c.rowid, c.v, d.source FROM vec_chunk c JOIN docs d ON c.rowid = d.rowid "
                            "WHERE c.model_version = ?", (MV,)).fetchall()
        qn = q / np.linalg.norm(q)
        best: dict = {}
        for r, s in zip(rows + crows, np.concatenate([_old_build(rows) @ qn, _old_build(crows) @ qn])):
            best[r[0]] = max(best.get(r[0], -np.inf), s)
        self.assertEqual(got, sorted(best, key=lambda k: -best[k])[:5])
        src = {r[0]: r[2] for r in rows + crows}
        self.assertTrue(scoped and all(src[r] == "src1" for r in scoped))

    def test_count_lower_than_rows_grows(self):
        store._BUILD_BATCH = 4
        con = _db(n_vec=10)
        sql = ("SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
               "WHERE v.model_version = ?")
        M, ids, srcs = store._build_rows(con, "SELECT 3 WHERE ? IS NOT NULL", sql, (MV,), True, True)
        np.testing.assert_array_equal(M, _old_build(con.execute(sql, (MV,)).fetchall()))
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(srcs), 10)

    def test_count_higher_than_rows_trims(self):
        con = _db(n_thin=6)
        M, ids, srcs = store._build_rows(con, "SELECT 100 WHERE ? IS NOT NULL",
                                         "SELECT node_id, v FROM vec_thin WHERE model_version = ?",
                                         (MV,), False, False)
        self.assertEqual(M.shape, (6, DIM))
        self.assertEqual(len(ids), 6)
        self.assertIsNone(srcs)

    def test_empty_table_records_generation(self):
        con = _db()
        self.assertEqual(store._ensure_thin_matrix(con), (None, None))
        self.assertEqual(store._thin_built_mv, MV)
        self.assertEqual(store._thin_built_gen, store._thin_write_gen)

    def test_malformed_row_fails_open_then_retries(self):
        con = _db(n_vec=4)
        M0, _ = store._ensure_matrix(con)
        self.assertIsNotNone(M0)
        con.execute("INSERT INTO docs VALUES (?, ?)", (777, "srcX"))
        con.execute("INSERT INTO vec VALUES (?, ?, ?)", (777, b"\x00" * 5, MV))  # not a float32 vector
        con.commit()
        store._vec_built_gen = -1
        store._vec_built_ts = 0.0
        self.assertEqual(store._ensure_matrix(con), (None, None))
        self.assertIsNone(store._vec_M)
        con.execute("DELETE FROM vec WHERE rowid = 777")
        con.commit()
        M, ids = store._ensure_matrix(con)
        self.assertEqual(M.shape, (4, DIM))

    def test_rebuild_does_not_reuse_old_array(self):
        con = _db(n_vec=4)
        M0, _ = store._ensure_matrix(con)
        held = M0.copy()
        store._vec_built_gen = -1
        store._vec_built_ts = 0.0
        M1, _ = store._ensure_matrix(con)
        self.assertIsNot(M0, M1)
        np.testing.assert_array_equal(M0, held)  # a reader still holding M0 sees it intact


def _live(M, ids, srcs=None):
    """{id: (row, src)} over the rows an incremental update has not removed."""
    out = {}
    for i, k in enumerate(ids):
        if k is None or (not isinstance(k, str) and int(k) < 0):
            continue
        out.setdefault(k if isinstance(k, str) else int(k), []).append(
            (M[i].copy(), None if srcs is None else srcs[i]))
    return out


def _vec(seed):
    return np.random.default_rng(seed).normal(size=DIM).astype(np.float32)


class MatrixDeltaTest(_StoreState):
    """eye-mem-3: committed vector changes are applied to the cached matrices in place; the
    result must equal a full rebuild from SQLite, without a rebuild."""

    def _no_rebuild(self):
        return mock.patch.object(store, "_build_rows", side_effect=AssertionError("full rebuild"))

    def _fresh(self, con, ensure):
        st = store._mstate
        store._mstate = {k: store._MatrixState() for k in ("vec", "thin", "chunk")}
        for p in ("vec", "thin", "chunk"):
            setattr(store, f"_{p}_M", None)
        try:
            M, ids = ensure(con)
            srcs = {"_ensure_matrix": "_vec_srcs", "_ensure_chunk_matrix": "_chunk_srcs"}.get(ensure.__name__)
            return _live(M, ids, getattr(store, srcs) if srcs else None)
        finally:
            store._mstate = st

    def _assert_same(self, a, b):
        self.assertEqual(sorted(a), sorted(b))
        for k in a:
            self.assertEqual(len(a[k]), len(b[k]))
            for (ra, sa), (rb, sb) in zip(a[k], b[k]):
                np.testing.assert_allclose(ra, rb, rtol=1e-6, atol=1e-7)
                self.assertEqual(sa, sb)

    def test_vec_changes_apply_in_place_and_equal_a_rebuild(self):
        con = _db(n_vec=10)
        store._ensure_matrix(con)
        self.assertGreater(store._mstate["vec"].bufM.shape[0], 10)  # spare rows past the count
        con.execute("INSERT INTO docs VALUES (?, ?)", (500, "srcNew"))
        con.execute("INSERT INTO vec VALUES (?, ?, ?)", (500, _vec(1).tobytes(), MV))
        con.execute("UPDATE vec SET v = ? WHERE rowid = 6", (_vec(2).tobytes(),))
        con.execute("DELETE FROM vec WHERE rowid = 9")
        con.execute("UPDATE vec SET v = ?, model_version = 'other-model' WHERE rowid = 12", (_vec(3).tobytes(),))
        con.commit()
        store.stage_vec(500, _vec(1), "srcNew", MV)
        store.stage_vec(6, None)              # _upsert drops the stale vector first ...
        store.stage_vec(6, _vec(2), "src2", MV)   # ... then the re-embed writes the new one
        store.stage_vec(9)
        store.stage_vec(12, _vec(3), "src4", "other-model")  # another space: must not enter
        gen0 = store._vec_write_gen
        store.publish_staged()
        self.assertEqual(store._vec_write_gen, gen0 + 1)
        with self._no_rebuild():
            M, ids = store._ensure_matrix(con)
        got = _live(M, ids, store._vec_srcs)
        self.assertNotIn(9, got)
        self.assertNotIn(12, got)
        self._assert_same(got, self._fresh(con, store._ensure_matrix))

    def test_search_skips_removed_rows(self):
        con = _db(n_vec=6)
        store._ensure_matrix(con)
        q = np.ones(DIM, np.float32)
        top_before = None
        with mock.patch.object(store, "_read_con", return_value=con), \
                mock.patch.object(store, "_hydrate_rowids", side_effect=lambda c, r, recall_via: r), \
                mock.patch.object(store, "_disabled", False):
            top_before = store.vector_search(q, k=1)[0]
            con.execute("DELETE FROM vec WHERE rowid = ?", (top_before,))
            con.commit()
            store.stage_vec(top_before)
            store.publish_staged()
            with self._no_rebuild():
                got = store.vector_search(q, k=10)
        self.assertNotIn(top_before, got)
        self.assertEqual(len(got), 5)

    def test_reader_holding_previous_arrays_sees_them_unchanged(self):
        con = _db(n_vec=8)
        M0, ids0 = store._ensure_matrix(con)
        M0c, ids0c = M0.copy(), ids0.copy()
        store.stage_vec(int(ids0[0]))
        store.stage_vec(700, _vec(7), "src7", MV)
        store.publish_staged()
        with self._no_rebuild():
            M1, ids1 = store._ensure_matrix(con)
        np.testing.assert_array_equal(M0, M0c)
        np.testing.assert_array_equal(ids0, ids0c)
        self.assertEqual(int(ids1[0]), -1)
        self.assertEqual(len(ids1), len(ids0) + 1)

    def test_rollback_discards_staged_changes(self):
        from omniseek.core.recall import writer
        con = _db(n_vec=4)
        M0, _ = store._ensure_matrix(con)
        gen0 = store._vec_write_gen
        con.execute("BEGIN")
        store.stage_vec(3)
        writer._rollback(con)
        con.execute("BEGIN")
        writer._commit(con)  # a later commit must not publish what the rollback dropped
        self.assertEqual(store._vec_write_gen, gen0)
        self.assertIs(store._ensure_matrix(con)[0], M0)

    def test_commit_publishes_staged_changes(self):
        from omniseek.core.recall import writer
        con = _db(n_vec=4)
        store._ensure_matrix(con)
        con.execute("BEGIN")
        con.execute("DELETE FROM vec WHERE rowid = 3")
        store.stage_vec(3)
        writer._commit(con)
        with self._no_rebuild():
            M, ids = store._ensure_matrix(con)
        self.assertNotIn(3, _live(M, ids))

    def test_chunk_rows_of_a_doc_are_replaced_as_a_set(self):
        con = _db(n_vec=4, n_chunk=8)  # rowids 3,6,9,12 with two chunk rows each
        store._ensure_chunk_matrix(con)
        con.execute("DELETE FROM vec_chunk WHERE rowid = 6")
        new = [_vec(20), _vec(21), _vec(22)]
        for v in new:
            con.execute("INSERT INTO vec_chunk VALUES (?, ?, ?)", (6, v.tobytes(), MV))
        con.execute("DELETE FROM vec_chunk WHERE rowid = 9")
        con.commit()
        store.stage_chunks(6, new, "src2", MV)
        store.stage_chunks(9)
        store.publish_staged()
        with self._no_rebuild():
            M, ids = store._ensure_chunk_matrix(con)
        got = _live(M, ids, store._chunk_srcs)
        self.assertEqual(len(got[6]), 3)
        self.assertNotIn(9, got)
        self._assert_same(got, self._fresh(con, store._ensure_chunk_matrix))

    def test_thin_changes_apply_in_place(self):
        con = _db(n_thin=5)
        store._ensure_thin_matrix(con)
        con.execute("DELETE FROM vec_thin WHERE node_id = 'doc:x:1'")
        con.execute("INSERT INTO vec_thin VALUES (?, ?, ?)", ("doc:y:9", _vec(30).tobytes(), MV))
        con.commit()
        store.stage_thin("doc:x:1")
        store.stage_thin("doc:y:9", _vec(30), MV)
        store.publish_staged()
        with self._no_rebuild():
            M, ids = store._ensure_thin_matrix(con)
        self.assertIsNone(ids[1])
        self._assert_same(_live(M, ids), self._fresh(con, store._ensure_thin_matrix))

    def test_undescribed_change_forces_a_rebuild(self):
        con = _db(n_vec=4)
        M0, _ = store._ensure_matrix(con)
        store.stage_vec(3)
        store.publish_staged()
        store.note_vec_write()  # a change nobody described: only SQLite knows the truth
        store._vec_built_ts = 0.0
        M1, ids1 = store._ensure_matrix(con)
        self.assertIsNot(M1, M0)
        self.assertIn(3, _live(M1, ids1))  # the row is still in SQLite, so the rebuild keeps it
        self.assertEqual(store._mstate["vec"].pending, [])

    def test_model_change_forces_a_rebuild(self):
        con = _db(n_vec=4)
        con.execute("INSERT INTO docs VALUES (?, ?)", (999999, "srcO"))
        con.commit()
        store._ensure_matrix(con)
        store.stage_vec(3)
        store.publish_staged()
        with mock.patch.object(store, "_model_version", return_value="other"):
            M, ids = store._ensure_matrix(con)
        self.assertEqual(list(ids), [999999])

    def test_dead_rows_past_their_bound_force_a_compacting_rebuild(self):
        store._BUILD_BATCH = 2
        con = _db(n_vec=10)
        store._ensure_matrix(con)
        for rid in (3, 6, 9):
            con.execute("DELETE FROM vec WHERE rowid = ?", (rid,))
            store.stage_vec(rid)
        con.commit()
        store.publish_staged()
        store._vec_built_ts = 0.0
        M, ids = store._ensure_matrix(con)  # 3 dead > max(2, 10 // 8): rebuild, no dead rows left
        self.assertEqual(len(ids), 7)
        self.assertTrue(np.all(ids > 0))

    def test_spare_rows_running_out_forces_a_rebuild(self):
        store._BUILD_BATCH = 2
        con = _db(n_vec=8)  # spare = max(2, 8 // 4) = 2 rows
        store._ensure_matrix(con)
        for i in range(3):
            con.execute("INSERT INTO docs VALUES (?, ?)", (900 + i, "s"))
            con.execute("INSERT INTO vec VALUES (?, ?, ?)", (900 + i, _vec(40 + i).tobytes(), MV))
            store.stage_vec(900 + i, _vec(40 + i), "s", MV)
        con.commit()
        store.publish_staged()
        store._vec_built_ts = 0.0
        M, ids = store._ensure_matrix(con)
        self.assertEqual(len(ids), 11)
        self._assert_same(_live(M, ids, store._vec_srcs), self._fresh(con, store._ensure_matrix))


if __name__ == "__main__":
    unittest.main()


def _mapping(a):
    """The np.memmap behind ``a`` (following view bases), or None when ``a`` lives in process memory."""
    while a is not None:
        if isinstance(a, np.memmap):
            return a
        a = a.base if isinstance(a.base, np.ndarray) else None
    return None


class FileBackedRowsTest(_StoreState):
    """eye-mem-3: matrix rows live in a deleted temporary file next to the index (outside the
    process footprint); a file that cannot be made falls back to process memory."""

    def test_build_is_file_backed_and_leaves_no_file(self):
        store._BUILD_BATCH = 4
        con = _db(n_vec=10)
        M, ids = store._ensure_matrix(con)
        self.assertIsNotNone(_mapping(M))
        self.assertIs(_mapping(store._mstate["vec"].bufM), _mapping(M))
        self.assertEqual(os.listdir(self._tmp.name), [])  # unnamed: nothing to leave behind
        sql = ("SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
               "WHERE v.model_version = ?")
        np.testing.assert_array_equal(M, _old_build(con.execute(sql, (MV,)).fetchall()))
        self.assertEqual(type(M @ np.ones(DIM, np.float32)), np.ndarray)

    def test_growth_past_the_count_stays_file_backed(self):
        store._BUILD_BATCH = 4
        con = _db(n_vec=10)
        sql = ("SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
               "WHERE v.model_version = ?")
        M, ids, _ = store._build_rows(con, "SELECT 3 WHERE ? IS NOT NULL", sql, (MV,), True, True)
        self.assertIsNotNone(_mapping(M))
        np.testing.assert_array_equal(M, _old_build(con.execute(sql, (MV,)).fetchall()))
        self.assertEqual(len(ids), 10)

    def test_full_buffer_is_still_a_view(self):
        con = _db(n_thin=6)
        M, ids, _ = store._build_rows(con, "SELECT 6 WHERE ? IS NOT NULL",
                                      "SELECT node_id, v FROM vec_thin WHERE model_version = ?",
                                      (MV,), False, False)
        self.assertEqual(M.base.shape, (6, DIM))  # adopt() takes .base as the buffer
        self.assertEqual(ids.base.shape[0], 6)

    def test_appended_rows_go_into_the_same_file(self):
        con = _db(n_vec=10)
        M0, _ = store._ensure_matrix(con)
        mm = _mapping(M0)
        con.execute("INSERT INTO docs VALUES (?, ?)", (500, "srcNew"))
        con.execute("INSERT INTO vec VALUES (?, ?, ?)", (500, _vec(1).tobytes(), MV))
        con.commit()
        store.stage_vec(500, _vec(1), "srcNew", MV)
        store.publish_staged()
        with mock.patch.object(store, "_build_rows", side_effect=AssertionError("full rebuild")):
            M1, ids1 = store._ensure_matrix(con)
        self.assertIs(_mapping(M1), mm)
        v = _vec(1)
        np.testing.assert_allclose(M1[list(ids1).index(500)], v / np.linalg.norm(v), rtol=1e-6)

    def test_no_file_falls_back_to_memory(self):
        con = _db(n_vec=10)
        with mock.patch.object(store.tempfile, "TemporaryFile", side_effect=OSError(28, "No space left")), \
                self.assertLogs(store.logger, "WARNING"):
            M, ids = store._ensure_matrix(con)
        self.assertIsNone(_mapping(M))
        sql = ("SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
               "WHERE v.model_version = ?")
        np.testing.assert_array_equal(M, _old_build(con.execute(sql, (MV,)).fetchall()))

    def test_disk_full_while_filling_falls_back_before_mapping(self):
        real = tempfile.TemporaryFile

        class _Full:
            def __init__(self, f):
                self.f = f
            def write(self, b):
                raise OSError(28, "No space left on device")
            def __getattr__(self, k):
                return getattr(self.f, k)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.f.close()

        with mock.patch.object(store.tempfile, "TemporaryFile", lambda **kw: _Full(real(**kw))), \
                mock.patch.object(store._np, "memmap", side_effect=AssertionError("mapped")), \
                self.assertLogs(store.logger, "WARNING"):
            a = store._alloc_rows(5, DIM)
        self.assertIsNone(_mapping(a))
        self.assertEqual(a.shape, (5, DIM))

    def test_allocated_file_is_written_out_in_full(self):
        written = []
        real = tempfile.TemporaryFile

        class _Count:
            def __init__(self, f):
                self.f = f
            def write(self, b):
                written.append(len(b))
                return self.f.write(b)
            def __getattr__(self, k):
                return getattr(self.f, k)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.f.close()

        with mock.patch.object(store.tempfile, "TemporaryFile", lambda **kw: _Count(real(**kw))), \
                mock.patch.object(store, "_FILL_CHUNK", 64):
            a = store._alloc_rows(7, DIM)
        self.assertIsNotNone(_mapping(a))
        self.assertEqual(sum(written), 7 * DIM * 4)  # every block allocated before the mapping
        self.assertGreater(len(written), 1)
        a[:] = 1.0
        self.assertEqual(float(a.sum()), 7 * DIM)
