"""The embedder keeps every buffer of one MPS forward under _MPS_BUF_CAP (a request of 10 MiB or
more opens a fresh 1 GiB heap). These tests check that each piece of that bound changes no result:
the token groups, the row steps, the blockwise attention, and the whole bound on a tiny Qwen3."""

import types
import unittest
from unittest.mock import patch

import numpy as np

try:
    import torch
    from transformers.integrations.sdpa_attention import sdpa_attention_forward
    HAVE_TORCH = True
except Exception:  # noqa: BLE001
    HAVE_TORCH = False


class _Tok:
    def __call__(self, texts, add_special_tokens=True):
        return {"input_ids": [[0] * int(t.split(":")[1]) for t in texts]}


class _Model:
    """encode() returns, per text, a vector holding the text's id, so order is checkable."""
    max_seq_length = 2048

    def __init__(self):
        self.tokenizer = _Tok()
        self.calls = []

    def encode(self, texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False, **kw):
        self.calls.append((len(texts), kw["processing_kwargs"]["text"]["max_length"]))
        return np.array([[float(t.split(":")[0]), 0.0] for t in texts], dtype=np.float32)


class TokenGroupTests(unittest.TestCase):
    def setUp(self):
        from omniseek.core.recall import embed
        self.e = embed

    def test_no_budget_is_one_group(self):
        self.assertEqual(self.e._token_groups([5, 1, 3], None), [[1, 2, 0]])
        self.assertEqual(self.e._token_groups([], None), [])

    def test_padded_size_within_budget_and_every_index_once(self):
        rng = np.random.default_rng(7)
        for _ in range(200):
            lens = [int(x) for x in rng.integers(1, 2049, size=int(rng.integers(1, 9)))]
            groups = self.e._token_groups(lens, 2048, 2048)
            self.assertEqual(sorted(i for g in groups for i in g), list(range(len(lens))))
            for g in groups:
                rung = self.e._bucket_len(max(lens[i] for i in g), 2048)
                self.assertTrue(len(g) * rung <= 2048 or len(g) == 1, (lens, groups))

    def test_packing_at_the_service_budget(self):
        g = self.e._token_groups
        self.assertEqual(g([500, 500, 500, 500], 2048, 2048), [[0, 1, 2, 3]])        # 4 x 512
        self.assertEqual(g([900, 900, 900, 900], 2048, 2048), [[0, 1], [2, 3]])      # 2 x 1024
        self.assertEqual(g([2000, 10, 2000], 2048, 2048), [[1], [0], [2]])
        self.assertEqual(g([5000], 2048, 2048), [[0]])                                # alone past it


class ForwardSplitTests(unittest.TestCase):
    def setUp(self):
        from omniseek.core.recall import embed
        self.e = embed
        self._saved = (embed._FWD_TOKENS, embed._PAD_SUPPORTED)

    def tearDown(self):
        self.e._FWD_TOKENS, self.e._PAD_SUPPORTED = self._saved

    def test_groups_restore_the_callers_order(self):
        self.e._FWD_TOKENS = 2048
        m = _Model()
        texts = ["0:1800", "1:20", "2:900", "3:30", "4:700"]
        out = self.e._forward(m, texts)
        self.assertEqual(out[:, 0].tolist(), [0.0, 1.0, 2.0, 3.0, 4.0])
        for n, length in m.calls:
            self.assertLessEqual(n * length, 2048)

    def test_without_budget_one_forward_as_before(self):
        self.e._FWD_TOKENS = None
        m = _Model()
        out = self.e._forward(m, ["0:1800", "1:20", "2:900"])
        self.assertEqual(out[:, 0].tolist(), [0.0, 1.0, 2.0])
        self.assertEqual(m.calls, [(3, 2048)])

    def test_bound_is_skipped_off_mps(self):
        self.e._FWD_TOKENS = None
        m = _Model()
        m.device = types.SimpleNamespace(type="cpu")
        self.e._bound_forward_buffers(m)
        self.assertIsNone(self.e._FWD_TOKENS)
        self.assertEqual(m.max_seq_length, 2048)


@unittest.skipUnless(HAVE_TORCH, "torch/transformers not installed")
class BlockwiseEquivalenceTests(unittest.TestCase):
    def setUp(self):
        from omniseek.core.recall import embed
        self.e = embed
        torch.manual_seed(0)

    def test_row_steps_same_result(self):
        lin = torch.nn.Linear(8, 5)
        x = torch.randn(3, 7, 8)
        want = lin(x)
        self.e._row_steps(lin, 4)
        self.assertTrue(torch.allclose(lin(x), want, atol=1e-6))
        self.assertTrue(torch.allclose(lin(x[0, 0]), want[0, 0], atol=1e-6))   # 1-D input untouched

    def _attn(self, mask, is_causal, groups=2):
        mod = types.SimpleNamespace(num_key_value_groups=groups, is_causal=True)
        q = torch.randn(2, 4, 12, 8)
        k = torch.randn(2, 4 // groups, 12, 8)
        v = torch.randn(2, 4 // groups, 12, 8)
        want, _ = sdpa_attention_forward(mod, q, k, v, mask, scaling=0.3, is_causal=is_causal)
        with patch.object(self.e, "_MPS_BUF_CAP", 2 * 4 * 12 * 4 * 3):   # 3 query rows a block
            got, _ = self.e._bounded_attention(mod, q, k, v, mask, scaling=0.3, is_causal=is_causal)
        self.assertEqual(got.shape, want.shape)
        self.assertTrue(torch.allclose(got, want, atol=1e-5), (got - want).abs().max())

    def test_attention_with_padding_mask(self):
        causal = torch.ones(12, 12, dtype=torch.bool).tril()
        pad = torch.ones(2, 12, dtype=torch.bool)
        pad[1, 9:] = False
        mask = causal[None, None] & pad[:, None, None, :]
        self._attn(mask, None)

    def test_attention_causal_without_mask(self):
        self._attn(None, True)
        self._attn(None, None)

    def test_attention_full_without_mask(self):
        self._attn(None, False, groups=1)

    def test_whole_bound_on_a_tiny_qwen3(self):
        try:
            from transformers import Qwen3Config, Qwen3Model
        except Exception:  # noqa: BLE001
            self.skipTest("no Qwen3 in this transformers")
        cfg = Qwen3Config(vocab_size=50, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                          num_attention_heads=4, num_key_value_heads=2, head_dim=8,
                          max_position_embeddings=64, attn_implementation="sdpa")
        net = Qwen3Model(cfg).eval()
        ids = torch.randint(0, 50, (2, 12))
        am = torch.ones(2, 12, dtype=torch.long)
        am[1, 9:] = 0
        with torch.no_grad():
            want = net(input_ids=ids, attention_mask=am).last_hidden_state
        st = types.SimpleNamespace(device=types.SimpleNamespace(type="mps"), max_seq_length=None)
        wrap = type("W", (), {"__getitem__": lambda s, i: types.SimpleNamespace(auto_model=net)})()
        wrap.device, wrap.max_seq_length = st.device, None
        saved = self.e._FWD_TOKENS
        try:
            with patch.object(self.e, "_MPS_BUF_CAP", 1024):
                self.e._bound_forward_buffers(wrap)
                self.assertEqual(self.e._FWD_TOKENS, 8)          # 1024 / (32 x 4 bytes)
                self.assertEqual(wrap.max_seq_length, 8)
                self.assertEqual(net.config._attn_implementation, self.e._ATTN_IMPL)
                with torch.no_grad():
                    got = net(input_ids=ids, attention_mask=am).last_hidden_state
        finally:
            self.e._FWD_TOKENS = saved
        self.assertTrue(torch.allclose(got[0], want[0], atol=1e-5), (got - want).abs().max())
        self.assertTrue(torch.allclose(got[1, :9], want[1, :9], atol=1e-5))


if __name__ == "__main__":
    unittest.main()
