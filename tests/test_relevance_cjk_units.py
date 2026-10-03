"""Chinese queries must not admit docs on a lone 2-char fragment (work order E, 2026-10-03).

The lexical filter used to admit any doc holding ONE query token. A Chinese run is sliced into
overlapping bigrams, so any fragment of it (the generic "模型" of "奖励模型", the cross-word "化学"
of "强化学习") admitted unrelated docs, and a zero-hit query returned noise instead of an honest
empty. On an 18-query / 60-doc labelled corpus the old rule admitted 80 irrelevant docs (10 of them
for zero-hit queries); the run-as-one-unit rule admits 29 (1) and blocks no relevant doc."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import relevance as R  # noqa: E402


def _doc(title, content=""):
    return SimpleNamespace(title=title, content=content)


def _old_any_term_scores(items, query):
    """The pre-2026-10-03 scorer verbatim (no unit gate): the reference ASCII queries must equal."""
    terms = R.query_terms(query)
    n = len(items)
    if not terms or n == 0:
        return [0.0] * n
    tfs, lens = [], []
    for fields in items:
        tf, dl = {}, 0.0
        for text, w in fields:
            toks = R.tokenize(text)
            dl += w * len(toks)
            for t in toks:
                tf[t] = tf.get(t, 0.0) + w
        tfs.append(tf)
        lens.append(dl)
    avgdl = (sum(lens) / n) or 1.0
    idf = {t: math.log(1.0 + (n - sum(1 for tf in tfs if t in tf) + 0.5)
                       / (sum(1 for tf in tfs if t in tf) + 0.5)) for t in terms}
    out = []
    for tf, dl in zip(tfs, lens):
        norm = 1.0 - R._B + R._B * (dl / avgdl)
        s = 0.0
        for t in terms:
            f = tf.get(t, 0.0)
            if f > 0.0:
                s += idf[t] * (f * (R._K1 + 1.0)) / (f + R._K1 * norm)
        out.append(s)
    return out


POOL = {
    "rlhf": _doc("RLHF 中奖励模型的训练技巧", "讨论偏好数据收集、奖励模型过拟合与奖励黑客问题。"),
    "compress": _doc("模型压缩综述：剪枝、蒸馏与低秩分解", "回顾深度神经网络模型压缩的主要方法及其在移动端的应用。"),
    "rl_robot": _doc("强化学习在机器人操作中的最新进展", "综述基于强化学习的机械臂抓取与灵巧手操作方法。"),
    "rl_grasp": _doc("基于强化学习的机器人抓取", "提出一种用于机械臂抓取的强化学习算法。"),
    "chem": _doc("化学实验室安全管理规范", "规定危险化学品的储存、使用与废弃处理流程。"),
    "med": _doc("大模型在医疗诊断中的应用前景", "分析大语言模型辅助影像与病历分析的机会与风险。"),
    "chain": _doc("区块链技术在供应链金融中的应用", "探讨区块链如何提升中小企业融资透明度。"),
    "infer": _doc("LLM 推理优化实战：量化、KV Cache 与投机解码", "总结大模型部署时常用的推理优化手段。"),
    "jax": _doc("PyTorch和JAX该选哪个", "从易用性、性能与生态三方面比较两个框架。"),
    "gpt": _doc("GPT-4o 与 Claude 3.5 Sonnet 编程能力对比评测", "对比两款模型的代码生成表现。"),
    "tesla": _doc("特斯拉 Model Y 降价", "特斯拉中国宣布 Model Y 全系降价。"),
    "car": _doc("二手 车 交易新规", "取消限迁政策。"),
    "dl": _doc("深度学习入门：从感知机到卷积网络", "面向初学者的深度学习基础教程。"),
    "safety": _doc("网络安全等级保护 2.0 解读", "介绍等保 2.0 的定级、测评与整改要求。"),
    "canada": _doc("加拿大快速通道 EE 最新一轮抽签分数公布", "IRCC 本轮邀请 3000 人。"),
    "us_imm": _doc("美国 H-1B 移民政策收紧", "新规提高 H-1B 申请费用。"),
}
NAMES = list(POOL)
DOCS = [POOL[k] for k in NAMES]


def admitted(query):
    return {k for k, s in zip(NAMES, R.doc_scores(DOCS, query)) if s > 0.0}


class QueryUnits(unittest.TestCase):
    def test_run_is_one_unit_of_distinct_bigrams(self):
        self.assertEqual(R.query_units("奖励模型"), [("奖励", "励模", "模型")])
        self.assertEqual(R.query_units("RLHF奖励模型"), [("rlhf",), ("奖励", "励模", "模型")])

    def test_lone_char_is_a_unit_only_when_nothing_else_is(self):
        self.assertEqual(R.query_units("GPT和Claude"), [("gpt",), ("claude",)])
        self.assertEqual(R.query_units("车"), [("车",)])
        self.assertIn("和", R.query_terms("GPT和Claude"))  # still scored, just never admits alone

    def test_word_tokens_are_their_own_units(self):
        self.assertEqual(R.query_units("chain of thought"), [("chain",), ("of",), ("thought",)])
        self.assertEqual(R.query_units("a"), [])


class ChineseFragmentsNoLongerAdmit(unittest.TestCase):
    def test_generic_fragment_of_a_two_word_run(self):
        got = admitted("RLHF奖励模型")
        self.assertIn("rlhf", got)
        self.assertNotIn("compress", got)  # only 模型: 1 of 3 bigrams
        self.assertNotIn("med", got)

    def test_cross_word_junction_and_particle_tail(self):
        got = admitted("强化学习在机器人中的应用")
        self.assertTrue({"rl_robot", "rl_grasp"} <= got)  # rl_grasp: 5 of 11 bigrams (> 1/3)
        self.assertNotIn("chem", got)   # only the junction 化学
        self.assertNotIn("med", got)    # only 中的 / 的应 / 应用
        self.assertNotIn("chain", got)  # only 中的 / 的应 / 应用
        self.assertNotIn("dl", got)     # only 学习

    def test_long_run_needs_more_than_a_third(self):
        got = admitted("大模型推理优化")
        self.assertIn("infer", got)
        self.assertNotIn("compress", got)
        self.assertNotIn("med", got)  # 大模 + 模型 = 2 of 6

    def test_lone_char_between_ascii_words_does_not_admit(self):
        got = admitted("GPT和Claude对比")
        self.assertIn("gpt", got)
        self.assertNotIn("jax", got)  # shared only the isolated 和

    def test_lone_char_query_still_matches(self):
        self.assertEqual(admitted("车"), {"car"})
        self.assertEqual(admitted("特斯拉 车"), {"tesla"})

    def test_zero_hit_queries_are_honestly_empty(self):
        self.assertEqual(R.filter_rank(DOCS, "联邦学习 隐私保护"), [])
        self.assertEqual(R.filter_rank(DOCS, "Mamba和RWKV架构"), [])

    def test_space_separated_words_keep_or_semantics(self):
        got = admitted("加拿大 移民 政策")
        self.assertEqual(got, {"canada", "us_imm", "car"})  # each spaced word is its own unit, like English

    def test_particle_is_bridged(self):
        a, b = R.doc_scores([_doc("大模型的推理能力评测研究"), _doc("化学反应动力学研究")], "大模型推理")
        self.assertGreater(a, 0.0)
        self.assertEqual(b, 0.0)


class UnchangedWhereNotAffected(unittest.TestCase):
    def test_ascii_queries_score_exactly_as_before(self):
        docs = [_doc("Chain of thought prompting", "We study reasoning in large models."),
                _doc("Reinforcement learning for robotics", "Policy gradients on arms."),
                _doc("Of mice and men", "a novel"),
                _doc("AI safety overview", "alignment and interpretability")]
        items = [[(d.title, 3.0), (d.content, 1.0)] for d in docs]
        for q in ("chain of thought", "reinforcement learning robotics", "AI", "of", "python"):
            self.assertEqual(R.field_scores(items, q), _old_any_term_scores(items, q), q)

    def test_admitted_docs_keep_their_bm25_score(self):
        items = [[(d.title, 3.0), (d.content, 1.0)] for d in DOCS]
        for q in ("强化学习在机器人中的应用", "RLHF奖励模型", "GPT和Claude对比"):
            new, old = R.field_scores(items, q), _old_any_term_scores(items, q)
            for n_, o_ in zip(new, old):
                self.assertTrue(n_ == 0.0 or n_ == o_, q)

    def test_termless_query_keeps_caller_order(self):
        self.assertEqual(R.filter_rank(DOCS, ""), DOCS)

    def test_matches_agrees_with_field_scores(self):
        for q in ("强化学习在机器人中的应用", "RLHF奖励模型", "GPT和Claude对比", "车", "加拿大 移民 政策"):
            toks = [set(R.tokenize(d.title)) | set(R.tokenize(d.content)) for d in DOCS]
            want = {k for k, t in zip(NAMES, toks) if R.matches(t, q)}
            self.assertEqual(want, admitted(q), q)


if __name__ == "__main__":
    unittest.main()
