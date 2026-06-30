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

[仕組み](#仕組み) · [クイックスタート](#クイックスタート) · [設定](#設定) · [コントリビュート](#コントリビュート)

**Languages:** [English](../../README.md) · [中文](README_zh.md) · 日本語

</div>

---

人は、見つけられたものでしか動けない。そして見つけられるのは、表層にあるものだけだ。

表層の下には:あるポッドキャストの47分目に、あなたが探している洞察がある。文字起こしされたことはない。決め手となる数字は、スライドに3秒だけ映った。示されたが、語られはしなかった。あなたが探し続けてきた答えは、何年も前に書かれていた。あなたの読めない言語で。

すべて、そこにある。すべて、手が届くはずのものだ。誰も触れていない。

<h3 align="center">それが半影領域(ペナンブラ)だ。</h3>

<div align="center">

検索が見せてくれるものと、実際に存在するものとの間にある、広大な薄明の領域。
機密ではない。オープンウェブでもない。
**その中間。知識は実在し、散在し、構造的に到達不可能な場所。**

</div>

<br>

**なぜ到達できないのか?** 音声・映像・画像の中に閉じ込められている。テキスト検索では永遠に解析できない。あなたの読めない言語で書かれている。ログインウォールの向こう側にある。そして時間的に流動する:昨日あった投稿が、明日には消えているかもしれない。どんなツールを使っても、同じ壁にぶつかる。

**Penumbraは、その壁を越える。**

セルフホスト型の深層検索エンジン。音声を文字起こしし、文書を消化し、映像と画像から情報を抽出し、言語を越えて読み、引用グラフを辿り、変化を監視する。数百のキュレーション済みソースを持つ成長し続けるオープンなカタログが、ディープウェブを横断する。MCPプロトコルに対応し、あらゆるAIエージェント、ワークフロー、アプリケーションが接続できる。

**しかし、断片は知識ではない。** 百の散在する発見はノイズに過ぎない。シグナルとは:英語の給与スレッド、中国語フォーラムに投稿された採用凍結の言及、文字起こしされていないポッドキャストでの何気ない一言が、三つの独立した角度から、単一のソースでは見えない同じ結論を指し示すとき。Penumbraはあなたのエージェントにそれを可能にする材料を提供する:すべての発見にソース、タイムスタンプ、他の発見との独立性がタグ付けされ、到達できなかった領域の正確な地図が付随する。あなたのエージェントが推論する基盤は、構造化され、出典追跡され、盲点が明示された証拠だ。表層を自信ありげになぞったものではない。

**どんな問いが答えられるようになるか、想像してほしい。** あなたの分野で誰が誰を知っているか、すべての言語を横断して追跡できるとしたら? 規制文書と翻訳された決算説明会の間に、どんなシグナルが隠れているか? 何年もの薫陶を必要とした業界の暗黙知を、数ヶ月で体系的に蓄積できるとしたら? 表層からは:解なし。半影領域からは:解ける。これらは、私たちが思いついた最初の問いに過ぎない。

<div align="center">

*優位性の本質は、誰が賢いかではなかった。*
*誰が半影領域に手を伸ばせるか、だった。*

**今、あなたの手が届く。**

</div>

---

## 仕組み

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/demo-ja-dark.png">
    <img src="../../assets/demo-ja-light.png" alt="実例:エージェントが、あるものが本当に言われているほど良いのかを尋ねる。通常の検索(公式サイト・レビュー・ドキュメント)はどれも「良い」と言う。Penumbraは表層の下に手を伸ばし、表層が取りこぼした3つの断片を返す:実際に運用したエンジニアがポッドキャストでふと漏らした「規模を上げると崩れる」、デモ動画に一瞬映って口にされなかったエラー、同じ壁にぶつかった人々が別の言語で書いた回避策。語られなかった部分は、語られ、映され、別の言語で書かれていた。Penumbraはその3つを持ち帰った。" width="780">
  </picture>
</div>

エージェントはMCPで接続する。エントリポイントは **`penumbra_search_ranked`**;`penumbra_list_sources()` が利用可能な能力を表示する。カタログはオープンかつ成長し続ける:各ソースは通常の検索に勝つことで採用される。完全なツール一覧は **[tools](../tools.md)** にある。

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
