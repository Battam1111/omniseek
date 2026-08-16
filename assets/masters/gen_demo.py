# -*- coding: utf-8 -*-
"""Generate the 6 demo-figure HTML files (en/zh/ja x light/dark) from one template."""
import pathlib

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:@@PAGEBG@@; }
  body { font-family:-apple-system,"SF Pro Text","Segoe UI","PingFang SC","Hiragino Sans","Hiragino Sans GB",sans-serif; padding:20px; }
  #shot { width:940px; background:@@BG@@; border-radius:14px; padding:34px 40px 26px; }
  .ask { text-align:center; font-size:13.5px; color:@@MUTED@@; letter-spacing:.02em; }
  h1 { text-align:center; font-size:27px; font-weight:700; color:@@FG@@; margin-top:8px; letter-spacing:-.01em; }
  .sub { text-align:center; font-size:12.5px; color:@@MUTED@@; margin-top:8px; }
  .surflabel { font-size:12.5px; color:@@MUTED@@; margin-top:26px; }
  .ghosts { display:flex; gap:12px; margin-top:9px; }
  .ghost { flex:1; border:1.5px dashed @@GHOSTBORDER@@; border-radius:10px; padding:13px 10px; text-align:center; color:@@GHOSTFG@@; font-size:14px; }
  .allsay { font-style:italic; color:@@MUTED2@@; font-size:13.5px; margin-top:9px; }
  .divider { display:flex; align-items:center; gap:14px; margin:24px 0 4px; }
  .divider .line { flex:1; height:1px; background:@@LINE@@; }
  .divider .t { font-size:13px; color:@@ACCENT@@; font-weight:600; letter-spacing:.02em; }
  .card { display:flex; gap:13px; margin-top:13px; background:@@CARDBG@@; border:1px solid @@CARDBORDER@@; border-left:3.5px solid @@ACCENT@@; border-radius:11px; padding:13px 17px; }
  .card .ico { font-size:19px; line-height:1.2; flex:0 0 auto; padding-top:2px; }
  .card .meta { font-size:12.5px; color:@@MUTED@@; }
  .card .meta b { color:@@ACCENT@@; font-weight:650; }
  .card .txt { font-size:15px; color:@@FG@@; line-height:1.5; margin-top:3px; }
  .card .txt b { font-weight:650; }
  .conv { text-align:center; font-size:13.5px; color:@@MUTED2@@; margin-top:20px; }
  .verdict { margin-top:12px; border:1.5px solid #F59E0B; border-radius:12px; padding:15px 22px; text-align:center; font-size:16px; color:@@FG@@; line-height:1.55; }
  .verdict b { color:@@AMBERTEXT@@; font-weight:650; }
  .held { font-size:12.5px; color:@@MUTED@@; margin-top:14px; }
</style></head><body>
<div id="shot">
  <div class="ask">@@ASK@@</div>
  <h1>@@Q@@</h1>
  <div class="sub">@@SUB@@</div>

  <div class="surflabel">@@SURFLABEL@@</div>
  <div class="ghosts">
    <div class="ghost">@@G1@@</div><div class="ghost">@@G2@@</div><div class="ghost">@@G3@@</div>
  </div>
  <div class="allsay">@@ALLSAY@@</div>

  <div class="divider"><div class="line"></div><div class="t">@@DIVIDER@@</div><div class="line"></div></div>

  <div class="card"><div class="ico">&#127908;</div><div><div class="meta">@@C1META@@</div><div class="txt">@@C1@@</div></div></div>
  <div class="card"><div class="ico">&#128275;</div><div><div class="meta">@@C2META@@</div><div class="txt">@@C2@@</div></div></div>
  <div class="card"><div class="ico">&#128172;</div><div><div class="meta">@@C3META@@</div><div class="txt">@@C3@@</div></div></div>
  <div class="card"><div class="ico">&#128065;&#65039;</div><div><div class="meta">@@C4META@@</div><div class="txt">@@C4@@</div></div></div>

  <div class="conv">@@CONV@@</div>
  <div class="verdict">@@VERDICT@@</div>
  <div class="held">@@HELD@@</div>
</div>
</body></html>
"""

MODES = {
    "light": {
        "@@PAGEBG@@": "#f1f5f9", "@@BG@@": "#ffffff", "@@FG@@": "#0F172A",
        "@@MUTED@@": "#64748B", "@@MUTED2@@": "#475569", "@@LINE@@": "#E2E8F0",
        "@@GHOSTBORDER@@": "#CBD5E1", "@@GHOSTFG@@": "#64748B",
        "@@CARDBG@@": "#F8FAFC", "@@CARDBORDER@@": "#E2E8F0",
        "@@ACCENT@@": "#3B82F6", "@@AMBERTEXT@@": "#B45309",
    },
    "dark": {
        "@@PAGEBG@@": "#0d1117", "@@BG@@": "#0F172A", "@@FG@@": "#E2E8F0",
        "@@MUTED@@": "#94A3B8", "@@MUTED2@@": "#A8B4C4", "@@LINE@@": "#1E293B",
        "@@GHOSTBORDER@@": "#334155", "@@GHOSTFG@@": "#94A3B8",
        "@@CARDBG@@": "#16233F", "@@CARDBORDER@@": "#1E3A8A",
        "@@ACCENT@@": "#60A5FA", "@@AMBERTEXT@@": "#FBBF24",
    },
}

LANGS = {
    "en": {
        "@@ASK@@": "your agent asks",
        "@@Q@@": "&ldquo;Can I renew my F-1 in a third country? What changed in 2026?&rdquo;",
        "@@SUB@@": "one live session &middot; outputs quoted as returned",
        "@@SURFLABEL@@": "plain search &middot; the surface",
        "@@G1@@": "the headlines", "@@G2@@": "the official FAQ", "@@G3@@": "the top blog posts",
        "@@ALLSAY@@": "All quote the same rule. None of them have done it.",
        "@@DIVIDER@@": "OmniSeek &middot; beneath the surface",
        "@@C1META@@": "explainer video &middot; Chinese &middot; <b>transcribed locally</b>",
        "@@C1@@": "The &ldquo;4-year cap&rdquo; in the headlines is the <b>initial period</b>. Extensions moved desks; they didn&rsquo;t vanish.",
        "@@C2META@@": "login-walled posts &middot; <b>your own logged-in account</b>",
        "@@C2@@": "Three first-person timelines: <b>Bangkok</b>, booked to passport in 25 days (interview to approval: 30 minutes) &middot; <b>Milan</b>, a month-long slot war, visa good for 5 years &middot; <b>Tokyo</b>, &ldquo;silky-smooth&rdquo;.",
        "@@C3META@@": "the comments under the Milan post &middot; <b>the author returns</b>",
        "@@C3@@": "&ldquo;Book any late slot first, <b>then email the consulate to expedite</b>. For one F-1 applicant it worked.&rdquo;",
        "@@C4META@@": "a dissenting video note &middot; <b>body is four hashtags</b>",
        "@@C4@@": "The facts are on screen, not in the text: a 212(a)(6)(C) refusal abroad can nearly close the F-1 road. <b>Vision reads the frames; local ASR takes the speech.</b>",
        "@@CONV@@": "Five walls crossed: language &middot; login &middot; comments &middot; audio &middot; pixels &darr;",
        "@@VERDICT@@": "The surface quoted the rule. The people who already lived it had <b>the timelines, the workaround, and the risk</b>. OmniSeek brought back all of it.",
        "@@HELD@@": "&#9702; The very first response also named the sources it held back (login-walled, quota-priced), each with the exact call to drill it.",
    },
    "zh": {
        "@@ASK@@": "你的 agent 被问到",
        "@@Q@@": "「F-1 过期了,能去第三国续签吗?2026 新规改了什么?」",
        "@@SUB@@": "一次真实会话 &middot; 输出按原样引用",
        "@@SURFLABEL@@": "普通搜索 &middot; 表面",
        "@@G1@@": "新闻头条", "@@G2@@": "官方 FAQ", "@@G3@@": "热门博客",
        "@@ALLSAY@@": "引的都是同一条规则。谁都没真的办过。",
        "@@DIVIDER@@": "OmniSeek &middot; 表面之下",
        "@@C1META@@": "解读视频 &middot; 中文 &middot; <b>本地转写</b>",
        "@@C1@@": "头条说的「最多待 4 年」,其实是<b>初始停留期</b>。延期只是换了审批部门,并没有消失。",
        "@@C2META@@": "登录墙内的帖子 &middot; <b>你自己的登录账号</b>",
        "@@C2@@": "三份第一手时间线:<b>曼谷</b>,预约到拿护照 25 天(面签到通过 30 分钟)&middot; <b>米兰</b>,抢号一个月,签出 5 年 &middot; <b>东京</b>,「丝滑」。",
        "@@C3META@@": "米兰帖的评论区 &middot; <b>楼主回来补充</b>",
        "@@C3@@": "「先约一个靠后的日期,<b>再发邮件给大使馆申请加急</b>。有一位 F-1 申请人真的成功了。」",
        "@@C4META@@": "持反方的视频笔记 &middot; <b>正文只有四个话题标签</b>",
        "@@C4@@": "干货在画面里,不在文字里:第三国遇上 212(a)(6)(C) 拒签,F-1 之路几乎断送。<b>视觉读帧,本地 ASR 取语音。</b>",
        "@@CONV@@": "穿过五堵墙:语言 &middot; 登录 &middot; 评论区 &middot; 音频 &middot; 像素 &darr;",
        "@@VERDICT@@": "表面复述规则。真正办过的人手里有<b>时间线、解法和风险</b>。OmniSeek 把它们全部带回。",
        "@@HELD@@": "&#9702; 第一次响应就点名了被扣下的源(登录墙、按量计费),并附上钻取每一个的确切调用。",
    },
    "ja": {
        "@@ASK@@": "エージェントへの質問",
        "@@Q@@": "「F-1 が切れた。第三国で更新できる?2026 年に何が変わった?」",
        "@@SUB@@": "実セッションひとつ &middot; 出力は返ってきたまま引用",
        "@@SURFLABEL@@": "通常の検索 &middot; 表面",
        "@@G1@@": "ニュース見出し", "@@G2@@": "公式 FAQ", "@@G3@@": "人気ブログ",
        "@@ALLSAY@@": "どれも同じ規則を引用するだけ。実際にやった人はいない。",
        "@@DIVIDER@@": "OmniSeek &middot; 表面の下",
        "@@C1META@@": "解説動画 &middot; 中国語 &middot; <b>ローカルで書き起こし</b>",
        "@@C1@@": "見出しの「最長 4 年」は<b>初期滞在期間</b>のこと。延長は審査窓口が変わっただけで、消えてはいない。",
        "@@C2META@@": "ログインの内側の投稿 &middot; <b>あなた自身のログイン</b>",
        "@@C2@@": "一次体験のタイムライン三件:<b>バンコク</b>、予約からパスポートまで 25 日(面接から承認まで 30 分)&middot; <b>ミラノ</b>、枠争奪ひと月、ビザは 5 年 &middot; <b>東京</b>、「スムーズそのもの」。",
        "@@C3META@@": "ミラノ投稿のコメント欄 &middot; <b>投稿者が戻ってきて</b>",
        "@@C3@@": "「まず遅い日程で予約し、<b>それから領事館にメールで繰り上げを依頼</b>。F-1 申請者一人は実際に成功した。」",
        "@@C4META@@": "反対意見の動画ノート &middot; <b>本文はハッシュタグ四つ</b>",
        "@@C4@@": "事実は文字ではなく画面の上に:第三国での 212(a)(6)(C) 拒否は F-1 への道をほぼ閉ざす。<b>視覚がフレームを読み、ローカル ASR が音声を取る。</b>",
        "@@CONV@@": "五つの壁を越えて:言語 &middot; ログイン &middot; コメント &middot; 音声 &middot; ピクセル &darr;",
        "@@VERDICT@@": "表面は規則を引用するだけ。実際に経験した人々は<b>タイムラインと回避策とリスク</b>を知っていた。OmniSeek はそのすべてを持ち帰る。",
        "@@HELD@@": "&#9702; 最初の応答の時点で、保留にしたソース(ログイン制・従量課金)を名指しし、それぞれを掘る正確な呼び出しを添えていた。",
    },
}

out = pathlib.Path(__file__).parent
for lang, strings in LANGS.items():
    for mode, colors in MODES.items():
        html = TEMPLATE
        for k, v in {**colors, **strings}.items():
            html = html.replace(k, v)
        assert "@@" not in html, f"unreplaced token in {lang}-{mode}"
        (out / f"demo-{lang}-{mode}.html").write_text(html, encoding="utf-8")
        print(f"wrote demo-{lang}-{mode}.html")
