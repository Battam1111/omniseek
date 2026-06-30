<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="../../assets/logo-icon.png">
  <img src="../../assets/logo-hero-dark.png" width="320" alt="Penumbra:日食のマーク、紺色の球体と琥珀色に照らされた縁">
</picture>

# penumbra

**表層の、その下へ。**

AIエージェントのための、セルフホスト型ディープ検索MCPサーバー。

[![CI](https://github.com/Battam1111/penumbra/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/penumbra/actions/workflows/ci.yml)
&nbsp;[![License](https://img.shields.io/badge/License-Apache_2.0-D4952B?style=flat-square)](../../LICENSE)
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

**なぜ到達できないのか?** 音声・映像・画像の中に閉じ込められている。テキスト検索では永遠に解析できない。あなたの読めない言語で書かれている。ログインウォールやペイウォールの向こう側にある。そして時間的に流動する:昨日あった投稿が、明日には消えているかもしれない。どんなツールを使っても、同じ壁にぶつかる。

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
git clone https://github.com/Battam1111/penumbra.git && cd penumbra
docker compose up -d
docker compose logs penumbra        # 初回起動時に表示される bearer token をコピー
curl -s http://127.0.0.1:8765/healthz
```

MCPクライアントを `http://127.0.0.1:8765/mcp` に向け、ヘッダー
`Authorization: Bearer <token>` を付与する。トークンは初回起動時に自動生成され、
`~/.penumbra/credentials/http.json` に保存される(Docker では `./.penumbra/credentials/http.json` にマウントされる)。

オプション拡張(ライセンスへの同意が必要、[NOTICE](../../NOTICE) 参照):
`docker-compose.yml` で `EXTRAS="[pdf,asr,walled]"` をビルド引数に指定。

### Dockerなし

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
./bootstrap.sh                                    # インストール + chromium + token + デフォルトprofile
python -m penumbra.serve_http                      # http://127.0.0.1:8765 で起動
```

Windows では `bootstrap.sh` を Git Bash または WSL で実行してください(POSIX シェルスクリプトです)。Docker が最も簡単な経路です。

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

*エージェントは MCP で Penumbra と対話する。Penumbra は半影領域(ログイン、言語、ペイウォール、音声、映像、画像、削除コンテンツ、引用グラフ)に手を伸ばし、タグ付け・重複排除・盲点を地図化した証拠を返す。*

エージェントがクエリを送信する。Penumbraはソースカタログ全体に展開し、障壁(ログイン、言語、ペイウォール、モダリティ、時間)を越え、発見をソースと出典でタグ付けし、独立した上流間で重複排除し、到達できなかった領域の明示的な地図とともに構造化された証拠を返す。推論はエージェントが行う。Penumbraは、その推論が表層ではなく深層に基づくことを保証する。

ソースカタログは**オープンかつ成長し続ける**:現在数百のキュレーション済みソースがあり、誰でも追加できる。各ソースは、特定のモード(structure、unwall、transcribe、recall、monitor)で通常の検索を上回ることで採用される。検索が既に返すものを焼き直すだけでは採られない。

## 得られるもの

ひとつのブロード呼び出しで、カタログ全体を横断して重複排除 + ランク付けし、「どこに到達できなかったか」の台帳まで添える。`penumbra_search_ranked("retrieval augmented generation survey")` の実際の応答(抜粋):

```jsonc
{
  "query": "retrieval augmented generation survey",
  "count": 12,
  "documents": [
    {
      "source": "openreview",
      "title": "Graph Retrieval-Augmented Generation: A Survey",
      "url": "https://openreview.net/forum?id=9ldXNHQFMl",
      "date": "2024-01-01T00:00:00Z",
      "metadata": {
        "corroboration": 5,                                 // 同じ作品が5つの独立した上流から現れた
        "also_in": ["dblp", "hackernews", "openalex", "youtube"],
        "merge_basis": "title",
        "_rank": 0.68
      }
    },
    {
      "source": "github_trending",
      "title": "taichengguo/LLM_MultiAgents_Survey_Papers",
      "url": "https://github.com/taichengguo/LLM_MultiAgents_Survey_Papers",
      "signals": { "stars": { "value": 1282, "kind": "engagement", "computed_by": "source:github_trending/stars" } },
      "metadata": { "live_sources": ["github_trending"], "_rank": 0.76 }
    }
    // ... 他に10件
  ],
  "_meta": {
    "searched": 91,                          // この呼び出しで検索したソース数
    "elapsed_s": 26.1,
    "deduped": { "in": 402, "out": 12 },     // 402件の生ヒットを上流の同一性で12件に統合
    "empty": ["core", "bluesky", "acl_anthology", "..."],  // 空で返った(キー未設定、または一致なし)
    "excluded_relevant": []                  // この問い合わせに合致する walled / 低速ソース。各々 sources=[...] の再実行ヒント付き
  }
}
```

独立性は標語ではなく具体的だ:同じ作品が複数の上流から現れると一つのエントリにまとまり、`corroboration`(いくつの異なるソースか)と `also_in`(どれか)を帯びる。だからエージェントは「5ソースに裏付けられたサーベイ」を単独ヒットより重く扱える。そして `_meta` は盲点の台帳だ:何を検索し、何が空で返り、何が除外されたか。何も黙って捨てない。デフォルトのブロード呼び出しは free ソースのみを使う。keyed とログイン必須ソースは、キーを追加するかログインするまで静かなままだ(「設定」を参照)。

## 設定

Penumbraは**カタログ優先**設計:設定なしでも、すべての安全なソースはオン、ログインウォールソースはオフ。調整はすべて一つのファイル `~/.penumbra/profile.json`([`profile.example.json`](../../profile.example.json) から初期化)で、ソース名・ドメイン・地域・アクセス階層ごとに行う:

| 階層 | デフォルト |
|------|-----------|
| **free**(公開、キー不要) | **オン** |
| **keyed**(あなたが用意する無料/有料の API キー) | キー設定でオン |
| **walled**(あなたが権利を持つログイン) | **オフ**;ブラウザは自前 |
| **circumvention** | **オフ、同梱されない** |

keyed ソースの設定、polite-pool 連絡先、すべての環境変数は **[configuration](../configuration.md)** に、ログイン必須ソースへのログインは **[walled sources](../walled-sources.md)** にある。

## ツール

Penumbra は MCP ツール群を公開する:検索とルーティング、論文と引用、人物と組織、文書と視覚、音声、ヘルス、そして自己反復型のキュレーター。まず **`penumbra_search_ranked`**(カタログ全体を重複排除 + ランク付けした一覧。「Xの最良・最新」のデフォルト)を使う。`penumbra_list_sources()` が実行時の能力インデックスを返す。完全なグループ別一覧は **[tools](../tools.md)** にある。

## 安全性と責任

- **デフォルトでループバック、トークンゲート。** `127.0.0.1` にバインドし、bearer トークンなしでは起動を拒否し、非ループバックバインドには警告を出す。リバースプロキシなしで公開しないこと。
- **設計上、非信頼。** 外向きリクエストは SSRF 防護下にある。Penumbra が返すものはすべて外部データであり、指示ではない。`penumbra_read_document` はホワイトリスト制のインボックスにサンドボックス化される。
- **あなたの責任。** Penumbra はあなた自身のエージェントとして、管轄区域の法律と各サイトの規約の範囲で取得する。全体の姿勢は [SECURITY.md](../../.github/SECURITY.md) と [NOTICE](../../NOTICE) を参照。

## コントリビュート

[CONTRIBUTING.md](../../.github/CONTRIBUTING.md) を参照。新しいソースの基準:特定のモード
(structure / unwall / transcribe / recall / monitor)で通常の検索に勝つこと。
壊れたソースの修復の基準:低い。ぜひ修復を。プッシュ前に `python tests/smoke.py` を実行。

参加することで[行動規範](../../.github/CODE_OF_CONDUCT.md)に同意したものとみなされます。

<div align="center">

---

**表層の、その下へ。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md)

</div>
