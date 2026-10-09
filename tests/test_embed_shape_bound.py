"""The embedder pads every forward to a fixed token-length ladder, so the MPS graph cache (one
compiled graph per input shape, never evicted) has a bounded key set. No torch, no weights: the
model is a fake that records what encode() was asked to do."""

import unittest
from unittest.mock import patch

import numpy as np


class _FakeTokenizer:
    def __call__(self, texts, add_special_tokens=True):
        return {"input_ids": [[0] * (len(t.split()) + 1) for t in texts]}


class _FakeModel:
    max_seq_length = 32768

    def __init__(self, accept_processing_kwargs=True):
        self.tokenizer = _FakeTokenizer()
        self.accept = accept_processing_kwargs
        self.calls = []

    def encode(self, texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False, **kw):
        if kw and not self.accept:
            raise TypeError("encode() got an unexpected keyword argument 'processing_kwargs'")
        self.calls.append((len(texts), kw))
        return np.ones((len(texts), 4), dtype=np.float32)


class EmbedShapeBoundTests(unittest.TestCase):
    def setUp(self):
        from omniseek.core.recall import embed
        self.embed = embed
        self._saved = embed._PAD_SUPPORTED
        embed._PAD_SUPPORTED = None

    def tearDown(self):
        self.embed._PAD_SUPPORTED = self._saved

    def test_bucket_len_ladder_and_past_it(self):
        b = self.embed._bucket_len
        self.assertEqual(b(1), 16)
        self.assertEqual(b(16), 16)
        self.assertEqual(b(17), 32)
        self.assertEqual(b(257), 384)
        self.assertEqual(b(1389), 1536)
        self.assertEqual(b(2048), 2048)
        self.assertEqual(b(2049), 3072)
        self.assertEqual(b(5000), 5120)
        self.assertEqual(b(5000, cap=4096), 4096)
        self.assertEqual(b(0), 16)
        for n in range(1, 2049):
            self.assertGreaterEqual(b(n), n)

    def test_every_length_maps_into_a_small_shape_set(self):
        rungs = {self.embed._bucket_len(n) for n in range(1, 2049)}
        self.assertEqual(rungs, set(self.embed._LEN_LADDER))
        # batch sizes 1.._PASSAGE_LOCK_CHUNK times the ladder: the whole graph-cache key set for
        # inputs within the 2000-char cap of _embed_text.
        self.assertLessEqual(len(rungs) * self.embed._PASSAGE_LOCK_CHUNK, 44)

    def test_encode_pads_to_rung_of_longest_text(self):
        fake = _FakeModel()
        with patch.object(self.embed, "_get_model", return_value=fake):
            out = self.embed._encode(["a b c", "a " * 40], prefix="")
        self.assertEqual(out.shape, (2, 4))
        (_n, kw), = fake.calls
        self.assertEqual(kw["processing_kwargs"]["text"], {"padding": "max_length", "max_length": 64})
        self.assertTrue(self.embed._PAD_SUPPORTED)

    def test_passage_chunks_are_each_padded(self):
        fake = _FakeModel()
        with patch.object(self.embed, "_get_model", return_value=fake):
            out = self.embed.embed_passage(["w " * 10] * 4 + ["w " * 300] * 2)
        self.assertEqual(out.shape, (6, 4))
        self.assertEqual([(n, kw["processing_kwargs"]["text"]["max_length"]) for n, kw in fake.calls],
                         [(4, 16), (2, 384)])

    def test_old_sentence_transformers_falls_back_unpadded_once(self):
        fake = _FakeModel(accept_processing_kwargs=False)
        with patch.object(self.embed, "_get_model", return_value=fake):
            first = self.embed.embed_query("hello world")
            second = self.embed.embed_query("hello again")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIs(self.embed._PAD_SUPPORTED, False)
        self.assertEqual([kw for _n, kw in fake.calls], [{}, {}])

    def test_tokenizer_failure_runs_unpadded_not_failed(self):
        fake = _FakeModel()
        fake.tokenizer = None
        with patch.object(self.embed, "_get_model", return_value=fake):
            out = self.embed.embed_query("x")
        self.assertEqual(out.shape, (4,))
        self.assertEqual(fake.calls, [(1, {})])

    def test_mps_cache_released_after_every_forward_cpu_untouched(self):
        import sys
        import types
        from unittest.mock import MagicMock
        fake_torch = types.SimpleNamespace(mps=types.SimpleNamespace(empty_cache=MagicMock()))
        fake = _FakeModel()
        fake.device = types.SimpleNamespace(type="mps")
        with patch.dict(sys.modules, {"torch": fake_torch}), \
                patch.object(self.embed, "_get_model", return_value=fake):
            self.embed.embed_passage(["w"] * 6)
            self.assertEqual(fake_torch.mps.empty_cache.call_count, 2)   # one per locked chunk
            fake.device = types.SimpleNamespace(type="cpu")
            self.embed.embed_query("x")
            self.assertEqual(fake_torch.mps.empty_cache.call_count, 2)
            fake.device = types.SimpleNamespace(type="mps")
            fake_torch.mps.empty_cache.side_effect = RuntimeError("no mps")
            self.assertIsNotNone(self.embed.embed_query("x"))   # a failed release never fails the embed


if __name__ == "__main__":
    unittest.main()
