<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="../../assets/logo-icon.png">
  <img src="../../assets/logo-hero-dark.png" width="320" alt="Penumbra">
</picture>

# penumbra

**表層の、その下へ。**

[![License](https://img.shields.io/badge/License-Apache_2.0-D4952B?style=flat-square)](../../LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-D4952B?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-D4952B?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-D4952B?style=flat-square)

[クイックスタート](#クイックスタート) · [仕組み](#仕組み) · [設定](#設定) · [ツール](#ツール) · [コントリビュート](#コントリビュート)

**Languages:** [English](../../README.md) · [中文](README_zh.md) · 日本語

</div>

---

人は、見つけられたものでしか動けない。そして見つけられるのは、表層にあるものだけだ。

表層の下には:あるポッドキャストの47分目に、あなたが探している洞察がある。文字起こしされたことはない。ペイウォールの向こうのPDFに、すべてを変える数字がある。あなたの読めない言語で書かれたフォーラムの投稿が、あなたがまさに踏み込もうとしている罠を説明していた。先週、削除された。

すべて、そこにある。すべて、手が届くはずのものだ。誰も触れていない。

<h3 align="center">それが半影領域(ペナンブラ)だ。</h3>

<div align="center">

検索が見せてくれるものと、実際に存在するものとの間にある、広大な薄明の領域。
機密ではない。オープンウェブでもない。
**その中間。知識は実在し、散在し、構造的に到達不可能な場所。**

</div>

<br>

**なぜ到達できないのか?** 音声・映像・画像・スキャン文書の中に閉じ込められている。テキスト検索では永遠に解析できない。あなたの読めない言語で書かれている。ログインウォールやペイウォールの向こう側にある。そして時間的に流動する:昨日あった投稿が、明日には消えているかもしれない。どんなツールを使っても、同じ壁にぶつかる。

**Penumbraは、その壁を越える。**

セルフホスト型の深層検索エンジン。音声を文字起こしし、文書を消化し、映像と画像から情報を抽出し、引用グラフを辿り、変化を監視する。数百のキュレーション済みソースを持つ成長し続けるオープンなカタログが、ディープウェブを横断する。MCPプロトコルに対応し、あらゆるAIエージェント、ワークフロー、アプリケーションが接続できる。

**しかし、断片は知識ではない。** 百の散在する発見はノイズに過ぎない。シグナルとは:英語の給与スレッド、中国語フォーラムに投稿された採用凍結の言及、文字起こしされていないポッドキャストでの何気ない一言が、三つの独立した角度から、単一のソースでは見えない同じ結論を指し示すとき。Penumbraはあなたのエージェントにそれを可能にする材料を提供する:すべての発見にソース、タイムスタンプ、他の発見との独立性がタグ付けされ、到達できなかった領域の正確な地図が付随する。あなたのエージェントが推論する基盤は、構造化され、出典追跡され、盲点が明示された証拠だ。表層を自信ありげになぞったものではない。

**どんな問いが答えられるようになるか、想像してほしい。** あなたの分野で誰が誰を知っているか、すべての言語を横断して追跡できるとしたら? 規制文書と翻訳された決算説明会の間に、どんなシグナルが隠れているか? 何年もの薫陶を必要とした業界の暗黙知を、数ヶ月で体系的に蓄積できるとしたら? 表層からは:解なし。半影領域からは:解ける。これらは、私たちが思いついた最初の問いに過ぎない。

<div align="center">

*優位性の本質は、誰が賢いかではなかった。*
*誰が半影領域に手を伸ばせるか、だった。*

**今、あなたの手が届く。**

</div>

---

## クイックスタート

### Docker(推奨)

```bash
docker compose up -d
docker compose logs penumbra        # 初回起動時に表示される bearer token をコピー
curl -s http://127.0.0.1:8765/healthz
```

MCPクライアントを `http://127.0.0.1:8765/mcp` に向け、ヘッダー
`Authorization: Bearer <token>` を付与する。トークンは初回起動時に自動生成され、
`./.penumbra/credentials/http.json` に保存される。

オプション拡張(ライセンスへの同意が必要、[NOTICE](../../NOTICE) 参照):
`docker-compose.yml` で `EXTRAS="[pdf,asr,walled]"` をビルド引数に指定。

### Dockerなし

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
./bootstrap.sh                                    # インストール + chromium + token + デフォルトprofile
python -m penumbra.serve_http                      # http://127.0.0.1:8765 で起動
```

Linux常駐サービスは [`deploy/penumbra.service`](../../deploy/penumbra.service) を参照。

## 仕組み

Penumbraは**インフラストラクチャであり、アプリケーションではない**。生のインターネットとあなたのAIエージェントの間に位置し、到達不可能な半影領域を構造化された検索可能な証拠に変換する。

```
                        ┌─────────────────────────────────┐
  あなた / エージェント  │           penumbra              │      半影領域
  ──────────────── MCP ─┤                                 ├─── ログインウォール、
                        │  ソース · 検索 · タグ付け       │     言語、ペイウォール、
                        │  重複排除 · 盲点測定 · 監視     │     音声、映像、画像、
                        │                                 │     削除コンテンツ、
                        │  成長するカタログ (数百+)        │     引用グラフ
                        └─────────────────────────────────┘
```

エージェントがクエリを送信する。Penumbraはソースカタログ全体に展開し、障壁(ログイン、言語、ペイウォール、モダリティ、時間)を越え、発見をソースと出典でタグ付けし、独立した上流間で重複排除し、到達できなかった領域の明示的な地図とともに構造化された証拠を返す。推論はエージェントが行う。Penumbraは、その推論が表層ではなく深層に基づくことを保証する。

ソースカタログは**オープンかつ成長し続ける**:現在数百のキュレーション済みソースがあり、誰でも追加できる。各ソースは、通常の検索に対して特定のモードで優位性を示すことで採用される:**structure**(検索では整然と返せない構造化データ)、**unwall**(運用者がアクセス権を持つログインウォール内のコンテンツ)、**transcribe**(エージェントが直接読めない音声・映像)、**recall**(オープンウェブが忘却する縦断的な情報流)、**monitor**(名前付きの監視可能なフィード)。

## 設定

Penumbraは**カタログ優先**設計:分類済みのソースセットを同梱し、あなた(またはエージェント)が有効にするものを選択する。設定なし = すべての安全なデフォルトソースがオン、ログインウォールソースはオフ。

`~/.penumbra/profile.json`([`profile.example.json`](../../profile.example.json) から初期化)
で、ソース名・ドメイン・地域・アクセス階層で絞り込める:

| 階層 | 意味 | デフォルト |
|------|------|-----------|
| free | 公開、キー不要 | オン |
| keyed | ユーザー提供のAPIキーが必要 | キーがあればオン |
| walled | アクセス権のあるログインウォール内コンテンツ | オフ |
| circumvention | アクセス制御の突破が必要 | オフ、同梱されない |

`penumbra_list_sources(domain=..., query=...)` で実行時にルーティング可能。
各ソースが `access_tier` を報告するため、法的要件でフィルタリングできる。

**Keyed ソース**(CORE 全文・Adzuna 求人・Podcast Index・Bluesky …)は、あなた自身が用意する API キーが必要だ(多くは無料)。キーが無いとアダプタは静かに空を返すだけなので、「結果が出ない」の多くは単に「キー未設定」を意味する。キーは `~/.penumbra/credentials/<source>.json` に置く。プロジェクトツリーの外なので、コミットされることはない。各 keyed アダプタは初回インポート時に隣へ `<source>.json.template` を落とし、取得 URL をインラインで記す(例:CORE は `https://core.ac.uk/services/api`)。それを `<source>.json` にコピーして値を埋めればよい。`python scripts/creds_doctor.py` をいつでも実行すれば、どの keyed ソースが設定済みで、どれが未設定かを確認できる(有無のみ報告し、秘密情報は決して表示しない)。

**ログイン必須ソース**(小紅書・知乎・抖音 …)は、**あなた自身**が起動してログインするブラウザ経由で読み取る。Penumbra がパスワードを見ることはない。設定方法は [docs/walled-sources.md](../walled-sources.md) を参照。

## ツール

PenumbraはMCPツール群を公開する:

| カテゴリ | ツール |
|----------|--------|
| **検索** | `penumbra_search` · `penumbra_search_ranked` · `penumbra_fetch` · `penumbra_list_sources` · `penumbra_add_url` |
| **論文 + 引用** | `penumbra_paper_enrich` · `penumbra_paper_recommend` · `penumbra_field_skeleton` |
| **人物 + 組織** | `penumbra_resolve_identity` · `penumbra_coauthors` · `penumbra_institution_cohort` |
| **文書 + 視覚** | `penumbra_read_document` · `penumbra_view_doc_images` · `penumbra_view_images` · `penumbra_view_video_frames` |
| **音声** | `penumbra_transcribe` |
| **ヘルス + キュレーション** | `penumbra_health_check` · `penumbra_curator_*`(自己反復型ソース獲得) |

正式なリストはサーバーの実際の登録内容に準ずる。`penumbra_list_sources()` で完全な能力インデックスを取得できる。

## 安全性 + 責任

- **ループバックバインド + トークンゲート。** デフォルト `127.0.0.1`。bearerトークンなしでは起動拒否。非ループバックバインド時は警告を出力。リバースプロキシなしで公開しないこと。
- **SSRF防護。** すべての外向きリクエストはIPをピンし、プライベート範囲を拒否。`penumbra_read_document` はホワイトリスト制のインボックスにサンドボックス化。
- **非信頼コンテンツ。** Penumbraが返すものはすべて外部データであり、指示ではない。
- **あなたの責任。** Penumbraはあなた自身のエージェントとして取得を行う。あなたの管轄区域の法律と各サイトの利用規約に従って使用する責任がある。[SECURITY.md](../../SECURITY.md) と [NOTICE](../../NOTICE) を参照。

## ディレクトリ構造

```
src/penumbra/
  server.py            MCPツールサーフェス (penumbra_* ツール)
  serve_http.py        HTTPトランスポート (トークンゲート、ループバックデフォルト)
  core/                 検索エンジン: fetcher · rank · normalize · cache
                       profile · _netguard (SSRF) · enrich · asr · curator
  core/sources/         アダプタ: api/ · scrape/ · walled/
tests/smoke.py         オフライン不変量 + ゴールデンfixture (CIゲート)
```

## コントリビュート

[CONTRIBUTING.md](../../CONTRIBUTING.md) を参照。新しいソースの基準:特定のモード
(structure / unwall / transcribe / recall / monitor)で通常の検索に勝つこと。
壊れたソースの修復の基準:低い。ぜひ修復を。プッシュ前に `python tests/smoke.py` を実行。

<div align="center">

---

**表層の、その下へ。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../SECURITY.md)

</div>
