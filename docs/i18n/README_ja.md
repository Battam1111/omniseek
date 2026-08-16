<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="../../assets/logo-icon.png">
  <img src="../../assets/logo-hero-dark.png" width="320" alt="OmniSeek">
</picture>

# OmniSeek

**検索では届かないものを、あなたのエージェントが探し当てる。**

答えはポッドキャストの 47 分目、コメント欄の三つ下の返信、ログインの向こう、別の言語の中に眠っている。あなたのエージェントは、それでも取ってくる。

<sub>セルフホスト型の知覚 MCP サーバー · 接続はひとつ</sub>

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml)
&nbsp;[![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](../../LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-3B82F6?style=flat-square)

[クイックスタート](#クイックスタート) · [ツール](#ツール) · [設定](#設定) · [コントリビュート](#コントリビュート)

**Languages:** [English](../../README.md) · [中文](README_zh.md) · 日本語

</div>

---

検索がエージェントに与えるのは、インデックス済みのページ。単一言語のテキストだけで、そこで止まります。

OmniSeek は探索を続けます:言語、ログイン、コメント欄、音声、ピクセルを抜けて。すべてあなた自身のマシンの上で。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/demo-ja-dark.png">
    <img src="../../assets/demo-ja-light.png" alt="実際の調査:ルール変更について英語で質問。検索はルールを引用して止まる。OmniSeek は探し続け、中国語解説動画の書き起こし、ログインの内側の一次体験タイムライン、コメント欄に埋もれた回避策、反対意見の動画ノートまで、出典つきで持ち帰る。">
  </picture>
</div>

聴く(ローカル二言語 ASR、クラウド不使用)、視る(画像と動画フレームを直接読む)、言語をまたぐ(中国語クエリが英語の結果を返し、その逆も)、ログインの壁の内側に届く(あなたの認証情報、あなたのマシン、デフォルトはオフ)、そして記憶する(ローカルに蓄積される永続的な検索メモリと、型付きで出典を辿れるエビデンスグラフ)。

[カタログ](../sources.md)は OmniSeek が届く先すべての厳選名簿です。どのソースも五つの仕事のいずれか(構造化・壁越え・書き起こし・想起・監視)で通常の検索に勝って席を得ています:引用グラフ、規制当局への提出書類、ログイン制フォーラム、中国語動画。そしてカタログは育つ設計です:内蔵のキュレーター・パイプラインが新ソースを検証・審査・承認し、朽ちたソースを引退させます。

**[実例と実出力](../examples.md)** · **[フル・ケーススタディ](../case-study.md)** · **[ベンチマーク](../../bench/DESIGN.md)**([最新結果](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md))· **[ソースヘルス、毎週更新](https://github.com/Battam1111/omniseek/blob/health-data/README.md)**

---

## クイックスタート

### Docker(推奨)

```bash
git clone https://github.com/Battam1111/omniseek.git && cd omniseek
docker compose up -d
docker compose logs omniseek        # 初回起動時に bearer token を表示
curl -s http://127.0.0.1:8765/healthz
```

MCP クライアントを `http://127.0.0.1:8765/mcp` に向け、`Authorization: Bearer <token>` を付けてください。token は初回起動時に生成され、`~/.omniseek/credentials/omniseek_http.json` に保存されます(compose で動かす場合、ホスト側では `./.omniseek/credentials/omniseek_http.json`)。

ここから道は二つ。ビルド済みコアイメージ(`docker pull ghcr.io/battam1111/omniseek`、amd64 + arm64)はビルド不要で、コアの感覚をすべて備えます。Apache クリーン構成のため、PDF 読取・聴覚(ASR + 動画フレーム)・ログイン制ソースは含みません。これらの extras が欲しいときだけローカルビルドになります:`EXTRAS="[pdf,asr,walled]"` を設定して `docker compose build`、その後 `up -d`(初回ビルドはヘッドレス Chromium も取得し、以後の起動は即時)。`OMNISEEK_CONTACT_EMAIL` の設定も推奨します(Crossref、SEC、Unpaywall が優先レーンをくれます)。

### Docker なし

```bash
python -m venv .venv && . .venv/bin/activate
scripts/bootstrap.sh
python -m omniseek.serve_http
```

素のインストールは **Core** 層です:キー不要の API と静的ソースすべて、PDF を除く文書読取、字句ベースの記憶インデックス。`pip install "omniseek[pdf,asr,recall,ocr]"` で **Research** 層(PDF・聴覚・多言語ベクトル・OCR)が目覚め、`omniseek[walled]` はログインの壁の層を加えます(自分のアカウントを持ち込むまではオフのまま)。`omniseek[all]` で全部です。サーバーは起動のたびに、どの感覚がオンラインでどれが休眠中かを表示します。

Windows では `bootstrap.sh` を Git Bash か WSL で実行してください。Docker が最も簡単です。常駐 Linux サービスは [`deploy/omniseek.service`](../../deploy/omniseek.service) を参照。

stdio 派には:インストールと同時に入る `omniseek` コマンドが MCP over stdio を話します(クライアントがサーバーを自ら起動する構成向け)。[`Dockerfile.stdio`](../../Dockerfile.stdio) はそのコンテナ版です。

OmniSeek は `127.0.0.1` にバインドし、全リクエストに bearer token を要求します。リバースプロキシなしで公開しないでください([SECURITY.md](../../.github/SECURITY.md))。

---

## ツール

MCP 接続はひとつ。中にモデルはなく、エージェントループもありません。考えるのはモデル、ループを回すのはハーネス、届くのが OmniSeek。まず `omniseek_search` から。何が使えるかは `omniseek_sources` で。

| ツール | 何をするか |
|--------|-----------|
| `omniseek_search` | カタログ全体へファンアウト、重複排除、ランキング。クロスリンガル(意味 + 字句)。 |
| `omniseek_read` | 任意の URL や文書(ウェブ、PDF、arXiv)をクリーンなテキストに正規化。 |
| `omniseek_view` | 画像・図版・動画フレームを視覚で読む。 |
| `omniseek_transcribe` | 音声・動画をローカルで書き起こし。二言語 ASR、タイムスタンプ指定可。 |
| `omniseek_field_skeleton` | 研究分野の引用近傍を地図化:基盤とフロンティア。 |
| `omniseek_resolve_identity` | 人名を複数データベースの著者 ID 候補に解決。 |
| `omniseek_coauthors` | 共著論文数で研究者の共同研究ネットワークを描く。 |
| `omniseek_institution_cohort` | ある研究室で活発に発表している人を分野を絞って列挙。 |
| `omniseek_paper_enrich` | 論文の OA PDF、撤回状況、被引用数。 |
| `omniseek_paper_recommend` | 意味的に近い論文(SPECTER 埋め込み)。キーワード検索では出ないもの。 |
| `omniseek_graph` | 蓄積されたエビデンスグラフへの問い合わせ:find、neighborhood、between、since、similar。 |
| `omniseek_sensor` | 常設クエリ + 新規性検出。新しいものがあるときだけ通知。 |
| `omniseek_ruling` | 同一性の裁定(same / not-same)を記録。読み取り時に適用。 |
| `omniseek_statement` | 有向リレーションを記録。グラフが引き継ぐ。 |
| `omniseek_curator_act` | ソースのライフサイクル:提出、探査、裁定、承認、引退。 |
| `omniseek_curator_view` | ソース承認キューや監査資料の閲覧。 |
| `omniseek_gather` | 複数ツールを並列実行、応答はひとつ。 |
| `omniseek_sources` | 一覧とルーティング:分野、地域、能力、ヘルス。 |

ログイン制の層に専用ツールはありません:ソースごとにオプトインした後は、同じ `omniseek_search(..., sources=["xiaohongshu"], raw=True)` があなた自身のログイン済みブラウザを通って実行されます。詳細は [walled sources](../walled-sources.md)。

完全なリファレンスは **[tools.md](../tools.md)** · **[FAQ](../faq.md)**

Claude Code をお使いなら、[`skills/omniseek-investigate`](../../skills/omniseek-investigate/SKILL.md) に調査メソッド(スイープ、ズーム、構造化)が既製スキルとして同梱されています。

---

## 設定

OmniSeek は**カタログ・ファースト**:設定ゼロで無害なソースはすべてオン、ログインが必要なソースはすべてオフ。調整は一つのファイル `~/.omniseek/profile.json`([例](../../deploy/profile.example.json))で:

| 層 | デフォルト |
|----|-----------|
| **free**(公開、キー不要) | **オン** |
| **keyed**(あなたが用意する API キー) | キー設定後オン |
| **walled**(あなたが持つログイン) | **オフ**;ブラウザは自前 |
| **circumvention**(アクセス制御の回避) | **オフ**;デフォルトパックにはひとつも含まれない |

詳細:**[configuration](../configuration.md)** · **[walled sources](../walled-sources.md)** · **[legal posture](../LEGAL-POSTURE.md)**

---

## なぜセルフホストか

OmniSeek のクラウドは存在しません。テレメトリなし、アカウントなし、中継なし。クエリがあなたのマシンを離れるのは、あなたが有効化したソースへの直接リクエストとしてだけであり、OmniSeek がその経路に第三者を加えることはありません。壁の内側のソースの認証情報はあなた自身のブラウザにだけ存在し、本来のサイトにのみ提示されます。OmniSeek はパスワードを保存も送信もせず、そもそも見ることがありません。数ヶ月かけて蓄積された検索メモリとエビデンスグラフは、あなたのマシン上のローカルファイルです。OmniSeek を止めても、すべて手元に残ります。機能のスイッチではなく、アーキテクチャそのものです。

---

## コントリビュート

[CONTRIBUTING.md](../../.github/CONTRIBUTING.md) を参照。新ソースの基準:いずれかのモード(structure / unwall / transcribe / recall / monitor)で通常のウェブ検索に勝つこと。壊れたソースの修理の基準:低いです、ぜひ。push 前に `python tests/smoke.py` を。

参加により[行動規範](../../.github/CODE_OF_CONDUCT.md)に同意したものとみなされます。

<div align="center">

---

**検索では届かないものを、あなたのエージェントが探し当てる。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md) · [Cite](../../CITATION.cff)

</div>
