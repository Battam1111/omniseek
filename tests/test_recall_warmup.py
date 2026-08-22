import unittest
from unittest.mock import patch


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class RecallWarmupTests(unittest.TestCase):
    def test_warm_pretouches_model_and_vector_matrices(self):
        from omniseek.core import recall

        calls = []
        connection = _FakeConnection()

        with patch.object(recall.embed, "warm", side_effect=lambda: calls.append("embed")), \
                patch.object(recall.store, "connect", return_value=connection), \
                patch.object(recall.store, "_ensure_matrix",
                             side_effect=lambda con: calls.append(("matrix", con))), \
                patch.object(recall.store, "_ensure_chunk_matrix",
                             side_effect=lambda con: calls.append(("chunk", con))):
            recall.warm()

        self.assertEqual(calls, ["embed", ("matrix", connection), ("chunk", connection)])
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
