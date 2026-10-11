<p align="center">
  <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/logo-lockup.png?v=2026-10-11b" width="320" alt="OmniSeek">
</p>

**ウェブ検索が届かないインターネットを、あなたの AI エージェントに。**

OmniSeek は、Claude Code、Cursor、Codex のような AI アシスタントやエージェントに追加する拡張機能です。ウェブ検索が止まるところから先を、エージェントが探せるようになります。

- 日本語で聞くだけで、Bilibili で実際に語られていること（Zhihu は名前を挙げたときに検索）を、元の投稿へのリンクつきで。
- ある研究分野の土台になった論文と、それを踏まえた最近の論文を、ひとつの答えで。
- テーマを見張らせておけば、前回から新しく出たものだけを知らせます。

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

あなた自身のコンピューターで動きます。接続には MCP を使います（MCP は Claude Code や Cursor のような AI アプリがツールを追加するための共通の仕組みです）。Cursor と Codex もそれぞれ 1 行で追加できます。[インストール（英語）](../install.md#connect-your-client)を参照してください。

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/omniseek?color=3B82F6&style=flat-square)](https://pypi.org/project/omniseek/) [![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](../../LICENSE) ![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square) ![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)
**Languages:** [English](../../README.md) · [中文](README_zh.md) · 日本語

---

## 違いを見る

2 つのエージェントに、李沐（Mu Li）が Bilibili の 87 分の Transformer 論文解説で論文の書き方をどう評したか、その原文と何分目かを尋ねました。ウェブ検索だけのエージェントはまとめの要旨と「最初の 1〜8 分ごろ」を返し、OmniSeek を使うエージェントは動画を文字に起こして、その発言を 1:21:51 に見つけました。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark) and (max-width: 480px)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-dark-narrow.gif?v=2026-10-11c">
    <source media="(max-width: 480px)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-light-narrow.gif?v=2026-10-11c">
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-dark.gif?v=2026-10-11c">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-light.gif?v=2026-10-11c" width="720" alt="実際の実行例を左右に並べたもの。左はウェブ検索だけのエージェントで、ネット上のまとめの要旨と「最初の 1〜8 分ごろ」を返す。右は OmniSeek を使うエージェントで、87 分の動画をまるごと文字に起こし、李沐（Mu Li）の発言を 1:21:51 に見つけ、その秒から開くリンクを示す。">
  </picture>
</p>

[3 つの質問の完全な比較（英語）](../compare.md)：出典、日付、短い動画つき。

---

## エージェントが届く場所

- **中国語コミュニティ**：Bilibili の動画とコメント、Zhihu の回答（名前を挙げたとき）、V2EX の最近のスレッド。どれも元の投稿へのリンクつき。詳しく：[Bilibili](https://battam1111.github.io/omniseek/bilibili-mcp.html)、[Zhihu](https://battam1111.github.io/omniseek/zhihu-mcp.html)（英語）
- **言語をまたいで**：日本語や英語で聞けば、中国語で検索して、あなたの言語で答えます。その逆も。[詳しく](https://battam1111.github.io/omniseek/cross-lingual-search-mcp.html)
- **ポッドキャストと動画**：話された言葉をあなたのコンピューター上で文字にし、何分目の発言かを添えて引用します（オプションの追加機能）。[詳しく](https://battam1111.github.io/omniseek/podcast-transcription-mcp.html)
- **研究論文**：ある分野がどの論文の上に立っていて、最近どの論文がそれを引用しているか。[詳しく](https://battam1111.github.io/omniseek/citation-graph-mcp.html)
- **前回からの新着**：検索を一度保存すれば、次からは新しいものだけを返します。[詳しく](https://battam1111.github.io/omniseek/research-monitoring-mcp.html)
- **ログインして使うサイト**：オンにすると、エージェントはあなたのブラウザーのログインを使って読みます。デフォルトはオフ。[詳しく（英語）](../walled-sources.md)

見つけたものはあなたのコンピューター上のローカルな検索インデックスに残るので、次の検索はすでに知っていることから始まります。

---

## インストール

必要なのは上の 2 つのコマンドだけです。Python 3.11 以上が必要ですが、なければ [uv](https://docs.astral.sh/uv/) が入れてくれます。OmniSeek は AI アプリが必要なときに自分で起動するので、常に動かしておく必要も、ポートを開ける必要もありません。

Cursor、Codex、pipx、Docker での導入と、オプションの追加機能（ポッドキャストの書き起こし、PDF の読み取り、ログインして使うサイト）は **[インストール（英語）](../install.md)** にあります。

---

## あなたのデータ、あなたのログイン

OmniSeek はあなたのコンピューターで動きます。OmniSeek のクラウドはなく、アカウント登録も、利用データの送信もありません。エージェントの検索は、あなたがオンにしたサイトへ直接届きます。公開サイトはデフォルトでオン。ログインが必要なサイトはオンにするまでオフで、オンにするとあなたのブラウザーにすでにあるログインを使います。OmniSeek があなたのパスワードを目にすることはありません。使ううちに積み上がるものは、すべてあなたのローカルファイルです。

OmniSeek は自前の AI モデルを持たず、私たちのサーバーに何かを送ることもありません。考えるのはあなたのエージェント、取ってくるのが OmniSeek です。設定：[Configuration（英語）](../configuration.md)。

---

## ツール

エージェントはまず `omniseek_search` を使います。論文の地図づくり、保存した検索、エージェントが残す判断の記録（`omniseek_ruling`、`omniseek_statement`）など、ほかのツールは **[tools.md（英語）](../tools.md)** にあります。

---

## もっと知る

[実例と実際の出力（英語）](../examples.md) · [ひとつの調査の全記録（英語）](../case-study.md) · [FAQ（英語）](../faq.md)、Exa や Tavily との違いも · [全ソース（英語）](../sources.md) · [ソースの稼働状況、毎週更新](https://github.com/Battam1111/omniseek/blob/health-data/README.md)

**[これらの主張をどう確かめているか（英語）](../../bench/DESIGN.md)**：テストは、通常のウェブ検索の 1 ページ目で答えが見つからないことを先に確かめたものだけを数えます（[最新結果](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md)）。Claude Code を使っているなら、[`skills/omniseek-investigate`](../../skills/omniseek-investigate/SKILL.md) がすぐ使える調査の手順です：広く探し、絞り込み、構造を足す。

---

## コントリビュート

[CONTRIBUTING.md](../../.github/CONTRIBUTING.md) を参照してください。新しいソースは、通常のウェブ検索では見つからないものを見つけられる必要があります。動かなくなったソースの修正はいつでも歓迎です。push の前に `python tests/smoke.py` を実行してください。

参加により[行動規範](../../.github/CODE_OF_CONDUCT.md)に同意したものとみなされます。

<div align="center">

---

**ウェブ検索が届かないインターネットを、あなたの AI エージェントに。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md) · [Cite](../../CITATION.cff)

</div>
