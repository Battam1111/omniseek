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

[クイックスタート](#クイックスタート) · [仕組み](#仕組み) · [得られるもの](#得られるもの) · [設定](#設定) · [コントリビュート](#コントリビュート)

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

Penumbra は `127.0.0.1` にバインドし、すべてのリクエストに bearer token を要求する。
リバースプロキシなしで公開しないこと([SECURITY.md](../../.github/SECURITY.md))。

## 仕組み

Penumbra は**インフラであり、アプリケーションではない**:生のインターネットとAIエージェントの間にある検索レイヤーだ。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/architecture_dark.svg">
    <img src="../../assets/architecture_light.svg" alt="エージェントは MCP で Penumbra に問い合わせ、Penumbra は半影領域(ログイン・言語・ペイウォール・音声・映像・画像・削除コンテンツ・引用グラフ)に手を伸ばし、タグ付け・重複排除・盲点を地図化した証拠を返す。" width="700">
  </picture>
</div>

Penumbra はカタログ全体に展開し、障壁を越え、独立した上流間で重複排除し、到達できなかった領域の地図とともに構造化された証拠を返す。推論はエージェントが行う。Penumbra はそれが表層ではなく深層に基づくことを保証する。

ソースカタログは**オープンかつ成長し続ける**:現在数百のキュレーション済みソースがあり、誰でも追加できる。各ソースは、特定のモード(structure、unwall、transcribe、recall、monitor)で通常の検索を上回ることで採用される。検索が既に返すものを焼き直すだけでは採られない。

エントリポイントは **`penumbra_search_ranked`**;`penumbra_list_sources()` が実行時の能力
インデックスを返す。[完全なツール一覧](../tools.md)は検索、論文、引用、人物、文書、
音声、監視をカバーする。

## 得られるもの

`penumbra_search_ranked("retrieval augmented generation survey")` は91のソースに展開し、
402件の生ヒットを上流の同一性で12件にまとめ、26秒で返す。最上位の結果は5つの独立した
上流(OpenReview、DBLP、HackerNews、OpenAlex、YouTube)からそれぞれ見つかった。

すべての結果に `corroboration`(いくつの独立したソースが見つけたか)と `also_in`
(どれか)が付く。`_meta` は盲点の台帳:何を検索し、何が空で返り、何が除外されたか。
5ソースの合意は単独ヒットに勝る。到達*できなかった*ところを知ることは、
到達できたところを知ることと同じくらい重要だ。

<details>
<summary>レスポンス構造(実データ、トリム済み)</summary>

```jsonc
{
  "query": "retrieval augmented generation survey",
  "count": 12,
  "documents": [
    {
      "source": "openreview",
      "title": "Graph Retrieval-Augmented Generation: A Survey",
      "metadata": {
        "corroboration": 5,                    // 同じ作品、5つの独立した上流
        "also_in": ["dblp", "hackernews", "openalex", "youtube"],
        "_rank": 0.68
      }
    }
    // ... 他に11件
  ],
  "_meta": {
    "searched": 91,                            // この呼び出しで検索したソース数
    "deduped": { "in": 402, "out": 12 },       // 402件の生ヒット -> 12件(上流の同一性で)
    "empty": ["core", "bluesky", "..."]        // キー未設定、または一致なし
  }
}
```

</details>

## 設定

Penumbraは**カタログ優先**設計:設定なしでも、すべての安全なソースはオン、ログインウォールソースはオフ。調整はすべて一つのファイル `~/.penumbra/profile.json`([`profile.example.json`](../../profile.example.json) から初期化)で、ソース名・ドメイン・地域・アクセス階層ごとに行う:

| 階層 | デフォルト |
|------|-----------|
| **free**(公開、キー不要) | **オン** |
| **keyed**(あなたが用意する無料/有料の API キー) | キー設定でオン |
| **walled**(あなたが権利を持つログイン) | **オフ**;ブラウザは自前 |
| **circumvention** | **オフ、同梱されない** |

詳細は **[configuration](../configuration.md)** に、ログイン必須ソースへのブラウザログインは **[walled sources](../walled-sources.md)** にある。

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
