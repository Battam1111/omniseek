<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/hero-ja-dark.png?v=2026-10-11">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/hero-ja-light.png?v=2026-10-11" width="670" alt="OmniSeek:ウェブ検索が届かないインターネットを、あなたの AI エージェントに。">
  </picture>
</p>

OmniSeek はセルフホスト型の MCP サーバーです。一度つなぐだけで、あなたのエージェントはポッドキャストや動画の中で話された内容、Bilibili や V2EX のような中国語コミュニティ、論文の引用グラフを検索できるようになります。オンにすれば、あなた自身のログインでしか見られないサイトも読めます(デフォルトはオフ)。200 以上のソースに対応し、Claude Code、Cursor など MCP に対応したクライアントで使えます。

エージェントにこう聞いてみてください:

> speculative decoding の基盤となる論文はどれで、それを土台にした最近の論文は何か?

インストール(Python 3.11 以上。なければ [uv](https://docs.astral.sh/uv/) が取ってきます):

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

ポッドキャストの書き起こし、PDF の読み取り、ログインが必要なサイトはオプション機能です。Cursor、pipx、pip、HTTP、Docker での導入方法もあわせて[インストールの選択肢(英語)](../install.md)にあります。
ドキュメント:[ドキュメント一覧](../) | [全ソース](../sources.md) | [ツール](../tools.md)

実際の実行はこうなります(2 回の検索、先頭の結果だけ表示、それぞれ約 1 秒):

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/terminal-demo-dark.gif?v=2026-10-11">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/terminal-demo-light.gif?v=2026-10-11" width="720" alt="実際の実行例:エージェントが Zhihu と Bilibili で博士 1 年目へのアドバイスを検索し、それぞれ約 1 秒でリンク付きの投稿と動画を受け取る。">
  </picture>
</p>

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/omniseek?color=3B82F6&style=flat-square)](https://pypi.org/project/omniseek/) [![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](../../LICENSE) ![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square) ![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)

**Languages:** [English](../../README.md) · [中文](README_zh.md) · 日本語

---

## ウェブ検索では見つからないもの

答えはポッドキャストの 47 分目、コメント欄の三つ下の返信、ログインの向こう、別の言語の中に眠っています。ウェブ検索が返すのはインデックス済みのページ、単一言語のテキストだけで、そこで止まります。OmniSeek はあなたのエージェントにその先を探させます。すべてあなた自身のマシンの上で。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo-ja-dark.png?v=2026-10-11">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo-ja-light.png?v=2026-10-11" alt="実際の調査を三つの層として描いた図。第一層「書かれていて、届く」:通常の検索はルールを引用して止まる。第二層「書かれてはいるが、届かない」:ログインの内側の一次体験タイムラインと、コメント欄の奥に埋もれた回避策。第三層「そもそも書かれていない」:中国語解説動画のローカル書き起こしと、事実が画面の上にしかない動画ノート。層ごとに答えへの道が一本ずつある。">
  </picture>
</div>

各層が持ち帰ったもの、原文のまま:

- **書かれていて、届く。** ニュース見出し、公式 FAQ、人気ブログ、声はひとつ:*「2026 年より F-1 の入国は 4 年の初期滞在期間に制限。第三国での更新は引き続き可能。」* どれも同じ規則の引用で、実際にやった人はいない。
- **書かれてはいるが、ログインの向こう。** 1point3acres の一次体験タイムライン三件、あなた自身のログインで読んだもの:**バンコク**、予約からパスポートまで 25 日、面接から承認まで 30 分。**ミラノ**、枠争奪ひと月、ビザは 5 年。**東京**、*「スムーズそのもの」*。ミラノ投稿のコメント欄には投稿者が戻ってきて:*「まず遅い日程で予約し、それから領事館にメールで繰り上げを依頼。F-1 申請者一人は実際に成功した。」* 個人の経験であって、公式の案内ではない。
- **そもそも文字ではない。** bilibili の中国語解説動画をローカルで書き起こし:見出しの<em>「最長 4 年」</em>は初期滞在期間のことで、延長は審査窓口が変わっただけで消えてはいない。RED（小紅書）の動画ノートは本文がハッシュタグ四つだけ、フレームと音声をローカルで読む:第三国での 212(a)(6)(C) 拒否(虚偽陳述の認定)は F-1 への道をほぼ閉ざす。

通常の検索は規則を引用して止まりました。実際に経験した人々はタイムラインと回避策とリスクを持っていました。OmniSeek は検索しなかったソースも挙げ、それぞれを検索するための呼び出しを添えていました。

できること:中国語と英語の音声をローカルで書き起こす(クラウドを使わない)。画像と動画フレームを読む。言語をまたぐ(中国語の質問で英語の結果が見つかり、その逆も)。あなた自身のアカウントでログインが必要なサイトを読む(あなたのマシンの上で、デフォルトはオフ)。そして覚えておく(使うほど育つローカルの検索インデックスと、見つけた論文・人・ページの関係を出典つきで記録するグラフ)。

言語をまたぐ検索は、OmniSeek が使ううちに蓄積するインデックスに依拠します。つまり新規インストールは低いところから始まります。公開の検証テストはまさにその新規インストール上で実行されるため、その言語横断の数値は最も条件の悪い状態であって、通常の状態ではありません。

[ソース一覧](../sources.md)のどのソースも、五つの仕事のいずれかで通常の検索に勝って収録されています:構造化(引用グラフ、規制当局への提出書類)、ログインが必要な内容を読む、書き起こし、想起、監視。一覧は増え続けます:新しい候補はテストしてから収録し、使えなくなったソースは外します。

**[実例と実際の出力](../examples.md)** · **[一回分の調査の全記録](../case-study.md)** · **[上の主張を一つずつ確かめるテスト](../../bench/DESIGN.md)**([最新結果](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md))· **[ソースの稼働状況、毎週更新](https://github.com/Battam1111/omniseek/blob/health-data/README.md)**

---

## インストールの選択肢

冒頭の二つのコマンドが基本の道です:クライアントが stdio で `omniseek` を自分で起動するので、ポートも token も要りません。それ以外はすべて **[インストールの選択肢(英語)](../install.md)** にあります:

- Cursor の `mcp.json` の設定、`pipx` や素の `pip` で入れる方法(仮想環境に `pip` で入れたときは、クライアントがその環境を有効化しないので `omniseek` の絶対パスを登録します);
- オプション機能:`pdf`、`asr`(書き起こし。先に PyTorch が要ります。プラットフォームごとのコマンドとディスク容量も記載)、`recall`、`ocr`、`walled`(ログインが必要なサイト);
- bearer token つきの共有 HTTP サーバー、一行の `docker run`、`docker compose`。

OmniSeek の HTTP サーバーは `127.0.0.1` にだけバインドし、すべてのリクエストに token を要求します。リバースプロキシなしで外部に公開しないでください([SECURITY.md](../../.github/SECURITY.md))。

---

## ツール

MCP 接続はひとつ。中にモデルはなく、エージェントループもありません。考えるのはモデル、ループを回すのはクライアント、取ってくるのが OmniSeek。まず `omniseek_search` から。どんなソースがあるかは `omniseek_sources` で。

| ツール | 何をするか |
|--------|-----------|
| `omniseek_search` | 一覧全体をまとめて検索し、重複を除いて並べる。言語をまたげる。 |
| `omniseek_read` | 任意の URL や文書(ウェブ、PDF、arXiv)をきれいなテキストにする。 |
| `omniseek_view` | 画像、文書の図、動画フレームを見る。 |
| `omniseek_transcribe` | 音声・動画をローカルで書き起こす。中英対応、任意の時刻から開始可。 |
| `omniseek_field_skeleton` | 研究分野の引用関係を描く:基盤となる論文と最近の論文。 |
| `omniseek_resolve_identity` | 人名を各データベースの著者 ID 候補に対応づける。 |
| `omniseek_coauthors` | 共著論文数で研究者の共同研究者を挙げる。 |
| `omniseek_institution_cohort` | ある研究室で、ある分野で活発に発表している人を挙げる。 |
| `omniseek_paper_enrich` | 論文のオープンアクセス PDF、撤回状況、被引用数。 |
| `omniseek_paper_recommend` | キーワード検索では出ない似た論文(SPECTER 埋め込み)。 |
| `omniseek_graph` | OmniSeek が見つけたものからなるローカルの関係グラフを問い合わせる:find、neighborhood、between、since、similar。 |
| `omniseek_sensor` | 保存した検索を予定どおり再実行し、新しく出た結果だけを知らせる。 |
| `omniseek_ruling` | 二つの項目が同じ人や物である(またはない)と記録する。 |
| `omniseek_statement` | 二つの項目の間の向きのある関係を記録する。 |
| `omniseek_curator_act` | ソースを提案、テスト、収録、または外す。 |
| `omniseek_curator_view` | 候補ソースの待ち行列や、あるソースの報告を見る。 |
| `omniseek_gather` | 複数のツールを並列に実行し、まとめて返す。 |
| `omniseek_sources` | 分野、地域、機能、稼働状況でソースを一覧する。 |

ログインが必要なサイトに専用のツールはありません:ソースをオンにした後は、同じ `omniseek_search(..., sources=["xiaohongshu"], raw=True)` があなた自身のログイン済みブラウザを通って実行されます。詳しくは[ログインが必要なサイト(英語)](../walled-sources.md)。

完全なリファレンスは **[tools.md](../tools.md)** · **[FAQ](../faq.md)**

Claude Code をお使いなら、[`skills/omniseek-investigate`](../../skills/omniseek-investigate/SKILL.md) に調査の手順(広く探す、絞り込む、構造にまとめる)が既製の skill として入っています。

---

## 設定

何も設定しなければ、公開のソースはすべてオン、ログインが必要なサイトはすべてオフです。調整は一つのファイル `~/.omniseek/profile.json`([例](../../deploy/profile.example.json))で行います:

| ソースの種類 | デフォルト |
|----|-----------|
| 公開、キー不要 | **オン** |
| あなたが用意する API キーが必要(無料または有料) | キーを設定するとオン |
| あなたのログインが必要 | **オフ**;ブラウザは自前 |
| アクセス制御の回避が必要 | **オフ**;デフォルトにはひとつも含まれない |

詳細:**[設定(英語)](../configuration.md)** · **[ログインが必要なサイト(英語)](../walled-sources.md)** · **[法的な立場(英語)](../LEGAL-POSTURE.md)**

---

## なぜセルフホストか

OmniSeek のクラウドは存在しません。テレメトリなし、アカウントなし、中継なし。クエリがあなたのマシンを離れるのは、あなたが有効にしたソースへの直接リクエストとしてだけで、OmniSeek がその経路に第三者を加えることはありません。ログイン状態はあなた自身のブラウザにだけあり、そのサイトにだけ提示されます。OmniSeek はパスワードを保存も送信もせず、そもそも見ることがありません。使うほど育つ検索インデックスと関係グラフは、あなたのマシン上のローカルファイルです。OmniSeek をやめても、すべて手元に残ります。

---

## コントリビュート

[CONTRIBUTING.md](../../.github/CONTRIBUTING.md) を参照してください。新しいソースの基準:上の五つの仕事のいずれかで通常のウェブ検索に勝つこと。壊れたソースを直す基準:低いです、ぜひ。push する前に `python tests/smoke.py` を実行してください。

参加により[行動規範](../../.github/CODE_OF_CONDUCT.md)に同意したものとみなされます。

<div align="center">

---

**ウェブ検索が届かないインターネットを、あなたの AI エージェントに。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md) · [Cite](../../CITATION.cff)

</div>
