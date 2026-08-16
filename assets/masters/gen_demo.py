# -*- coding: utf-8 -*-
"""Generate the 6 demo-figure HTML files (en/zh/ja x light/dark) from one template."""
import pathlib
import re

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:@@PAGEBG@@; }
  body { font-family:-apple-system,"SF Pro Text","Segoe UI","PingFang SC","Hiragino Sans","Hiragino Sans GB",sans-serif; padding:20px; }
  #shot { width:940px; background:@@BG@@; border-radius:14px; padding:26px 40px 16px; }
  .ask { text-align:center; font-size:13.5px; color:@@MUTED@@; letter-spacing:.02em; }
  h1 { text-align:center; font-size:27px; font-weight:700; color:@@FG@@; margin-top:8px; letter-spacing:-.01em; }
  .whatis { text-align:center; font-size:14px; color:@@MUTED2@@; margin-top:9px; }
  .divider { display:flex; align-items:center; gap:14px; margin:20px 0 2px; }
  .divider .line { flex:1; height:1px; background:@@LINE@@; }
  .divider .t { font-size:13px; color:@@ACCENT@@; font-weight:600; letter-spacing:.02em; }
  .divider.gray .t { color:@@MUTED@@; }
  .card { display:flex; gap:13px; margin-top:12px; background:@@CARDBG@@; border:1px solid @@CARDBORDER@@; border-left:3.5px solid @@ACCENT@@; border-radius:11px; padding:9px 17px; }
  .card.surface { border-left-color:@@SURFRAIL@@; }
  .card .ico { font-size:19px; line-height:1.2; flex:0 0 auto; padding-top:2px; }
  .card .meta { font-size:12.5px; color:@@MUTED@@; }
  .card .meta b { color:@@ACCENT@@; font-weight:650; }
  .wall { display:inline-block; font-size:10.5px; font-weight:650; letter-spacing:.06em; color:@@ACCENT@@; background:@@CHIPBG@@; border-radius:5px; padding:2px 7px; margin-right:8px; vertical-align:1px; }
  .card .txt { font-size:14px; color:@@FG@@; line-height:1.4; margin-top:3px; }
  .card .txt b { font-weight:650; }
  .verdict { margin-top:14px; border:1.5px solid #F59E0B; border-radius:12px; padding:10px 22px; text-align:center; font-size:16px; color:@@FG@@; line-height:1.55; }
  .verdict b { color:@@AMBERTEXT@@; font-weight:650; }
  .verdict .v2 { font-size:12.5px; color:@@MUTED@@; margin-top:7px; font-weight:400; }
</style></head><body>
<div id="shot">
  <div class="ask">@@ASK@@</div>
  <h1>@@Q@@</h1>
  <div class="whatis">@@WHATIS@@</div>

  <div class="divider gray"><div class="line"></div><div class="t">@@SURFDIV@@</div><div class="line"></div></div>
  <div class="card surface"><div class="ico">&#128269;</div><div><div class="meta">@@SMETA@@</div><div class="txt">@@S1@@</div></div></div>

  <div class="divider"><div class="line"></div><div class="t">@@DIVIDER@@</div><div class="line"></div></div>

  <div class="card"><div class="ico">&#127908;</div><div><div class="meta"><span class="wall">@@W1@@</span>@@C1META@@</div><div class="txt">@@C1@@</div></div></div>
  <div class="card"><div class="ico">&#128275;</div><div><div class="meta"><span class="wall">@@W2@@</span>@@C2META@@</div><div class="txt">@@C2@@</div></div></div>
  <div class="card"><div class="ico">&#128172;</div><div><div class="meta"><span class="wall">@@W3@@</span>@@C3META@@</div><div class="txt">@@C3@@</div></div></div>
  <div class="card"><div class="ico">&#128065;&#65039;</div><div><div class="meta"><span class="wall">@@W4@@</span>@@C4META@@</div><div class="txt">@@C4@@</div></div></div>

  <div class="verdict">@@VERDICT@@<div class="v2">@@VERDICT2@@</div></div>
</div>
</body></html>
"""

MODES = {
    "light": {
        "@@PAGEBG@@": "#f1f5f9", "@@BG@@": "#ffffff", "@@FG@@": "#0F172A",
        "@@MUTED@@": "#64748B", "@@MUTED2@@": "#475569", "@@LINE@@": "#E2E8F0",
        "@@CARDBG@@": "#F8FAFC", "@@CARDBORDER@@": "#E2E8F0",
        "@@ACCENT@@": "#3B82F6", "@@AMBERTEXT@@": "#B45309",
        "@@SURFRAIL@@": "#94A3B8", "@@CHIPBG@@": "#EFF6FF",
    },
    "dark": {
        "@@PAGEBG@@": "#0d1117", "@@BG@@": "#0F172A", "@@FG@@": "#E2E8F0",
        "@@MUTED@@": "#94A3B8", "@@MUTED2@@": "#A8B4C4", "@@LINE@@": "#1E293B",
        "@@CARDBG@@": "#16233F", "@@CARDBORDER@@": "#1E3A8A",
        "@@ACCENT@@": "#60A5FA", "@@AMBERTEXT@@": "#FBBF24",
        "@@SURFRAIL@@": "#475569", "@@CHIPBG@@": "#1E3A5F",
    },
}

LANGS = {
    "en": {
        "@@ASK@@": "your agent is asked",
        "@@Q@@": "&ldquo;Can I renew my F-1 visa in a third country? What changed in 2026?&rdquo;",
        "@@WHATIS@@": "OmniSeek is a self-hosted perception server your agent calls. One live session below &middot; outputs quoted as returned.",
        "@@SURFDIV@@": "plain search &middot; the surface",
        "@@SMETA@@": "headlines, official FAQ, top blogs &middot; <b>one voice</b>",
        "@@S1@@": "&ldquo;From 2026, F-1 admission is limited to a 4-year initial period; renewal in a third country remains possible.&rdquo; <b>All quote the same rule. None of them have done it.</b>",
        "@@DIVIDER@@": "OmniSeek &middot; beneath the surface",
        "@@W1@@": "TRANSCRIBED FROM AUDIO", "@@W2@@": "BEHIND LOGIN", "@@W3@@": "DEEP IN THE COMMENTS", "@@W4@@": "READ FROM PIXELS",
        "@@C1META@@": "explainer video &middot; bilibili, Chinese &middot; <b>transcribed locally</b>",
        "@@C1@@": "The &ldquo;4-year cap&rdquo; in the headlines is the <b>initial period</b>. Extensions moved desks; they didn&rsquo;t vanish.",
        "@@C2META@@": "three first-person threads &middot; 1point3acres, logged in &middot; <b>your own account</b>",
        "@@C2@@": "<b>Bangkok</b>: booked to passport in 25 days; interview to approval, 30 minutes.<br><b>Milan</b>: a month-long fight for a slot; visa issued for 5 years.<br><b>Tokyo</b>: &ldquo;silky-smooth&rdquo;.",
        "@@C3META@@": "the comments under the Milan post &middot; <b>the author returns</b>",
        "@@C3@@": "&ldquo;Book any late slot first, <b>then email the consulate to expedite</b>. For one F-1 applicant it worked.&rdquo; One person&rsquo;s experience, not official guidance.",
        "@@C4META@@": "video note &middot; rednote &middot; caption is four hashtags &middot; <b>frames and speech read locally</b>",
        "@@C4@@": "The facts are on screen, not in the caption: a 212(a)(6)(C) refusal abroad (a misrepresentation finding) can nearly close the F-1 road.",
        "@@VERDICT@@": "The surface quoted the rule. The people who had lived it held <b>the timelines, the workaround, and the risk</b>. OmniSeek brought back all of it, each line attributed.",
        "@@VERDICT2@@": "It also named the sources it held back, each with the exact call to drill it.",
    },
    "zh": {
        "@@ASK@@": "你的 agent 被问到",
        "@@Q@@": "「F-1 过期了，能去第三国续签吗？2026 新规改了什么？」",
        "@@WHATIS@@": "OmniSeek：自托管的感知服务器，你的 agent 直接调用。下方为一次真实会话，输出按原样引用。",
        "@@SURFDIV@@": "普通搜索 &middot; 表面",
        "@@SMETA@@": "新闻头条、官方 FAQ、热门博客 &middot; <b>同一个声音</b>",
        "@@S1@@": "「2026 年起，F-1 入境限四年初始期；第三国续签仍被允许。」<b>引的都是同一条规则，谁都没真的办过。</b>",
        "@@DIVIDER@@": "OmniSeek &middot; 表面之下",
        "@@W1@@": "音频转写", "@@W2@@": "登录墙内", "@@W3@@": "评论区深处", "@@W4@@": "像素读出",
        "@@C1META@@": "解读视频 &middot; bilibili &middot; 中文 &middot; <b>本地转写</b>",
        "@@C1@@": "头条说的「最多待 4 年」，其实是<b>初始停留期</b>。延期只是换了审批部门，并没有消失。",
        "@@C2META@@": "三份第一手时间线 &middot; 一亩三分地，已登录 &middot; <b>你自己的账号</b>",
        "@@C2@@": "<b>曼谷</b>：预约到拿护照 25 天；面签到通过 30 分钟。<br><b>米兰</b>：抢号一个月；签出 5 年。<br><b>东京</b>：「丝滑」。",
        "@@C3META@@": "米兰帖的评论区 &middot; <b>楼主回来补充</b>",
        "@@C3@@": "「先约一个靠后的日期，<b>再发邮件给领事馆申请加急</b>。有一位 F-1 申请人真的成功了。」个人经验，非官方指引。",
        "@@C4META@@": "视频笔记 &middot; 小红书 &middot; 正文只有四个话题标签 &middot; <b>帧与语音本地读取</b>",
        "@@C4@@": "干货在画面里，不在文字里：第三国遇上 212(a)(6)(C) 拒签（虚假陈述认定），F-1 之路几乎断送。",
        "@@VERDICT@@": "表面复述规则。真正办过的人手里有<b>时间线、解法和风险</b>。OmniSeek 把它们全部带回，每一行都有出处。",
        "@@VERDICT2@@": "它还点名了被扣下的源，并附上钻取每一个的确切调用。",
    },
    "ja": {
        "@@ASK@@": "エージェントへの質問",
        "@@Q@@": "「F-1 が切れた。第三国で更新できる？2026 年に何が変わった？」",
        "@@WHATIS@@": "OmniSeek はエージェントが呼び出すセルフホスト型の知覚サーバー。以下はひとつの実セッション、出力は返ってきたまま引用。",
        "@@SURFDIV@@": "通常の検索 &middot; 表面",
        "@@SMETA@@": "ニュース見出し、公式 FAQ、人気ブログ &middot; <b>声はひとつ</b>",
        "@@S1@@": "「2026 年より F-1 の入国は 4 年の初期滞在期間に制限。第三国での更新は引き続き可能。」<b>どれも同じ規則の引用。実際にやった人はいない。</b>",
        "@@DIVIDER@@": "OmniSeek &middot; 表面の下",
        "@@W1@@": "音声から書き起こし", "@@W2@@": "ログインの内側", "@@W3@@": "コメント欄の奥", "@@W4@@": "ピクセルから読解",
        "@@C1META@@": "解説動画 &middot; bilibili &middot; 中国語 &middot; <b>ローカルで書き起こし</b>",
        "@@C1@@": "見出しの「最長 4 年」は<b>初期滞在期間</b>のこと。延長は審査窓口が変わっただけで、消えてはいない。",
        "@@C2META@@": "一次体験のタイムライン三件 &middot; 1point3acres、ログイン済み &middot; <b>あなた自身のアカウント</b>",
        "@@C2@@": "<b>バンコク</b>：予約からパスポートまで 25 日；面接から承認まで 30 分。<br><b>ミラノ</b>：枠争奪ひと月；ビザは 5 年。<br><b>東京</b>：「スムーズそのもの」。",
        "@@C3META@@": "ミラノ投稿のコメント欄 &middot; <b>投稿者が戻ってきて</b>",
        "@@C3@@": "「まず遅い日程で予約し、<b>それから領事館にメールで繰り上げを依頼</b>。F-1 申請者一人は実際に成功した。」",
        "@@C4META@@": "動画ノート &middot; RED（小紅書）&middot; 本文はハッシュタグ四つ &middot; <b>フレームと音声をローカルで読む</b>",
        "@@C4@@": "事実は文字ではなく画面の上に：第三国での 212(a)(6)(C) 拒否（虚偽陳述の認定）は F-1 への道をほぼ閉ざす。",
        "@@VERDICT@@": "表面は規則を引用するだけ。実際に経験した人々は<b>タイムラインと回避策とリスク</b>を持っていた。OmniSeek はそのすべてを、出典つきで持ち帰る。",
        "@@VERDICT2@@": "保留にしたソースも名指しし、それぞれを掘る正確な呼び出しを添えていた。",
    },
}

def _lint_cjk_punctuation():
    allowed = ("F-1", "212(a)(6)(C)", "FAQ", "OmniSeek", "bilibili", "agent", "ASR")
    forbidden = ",?:;()"
    findings = []
    for lang in ("zh", "ja"):
        for token, value in LANGS[lang].items():
            remaining = value
            for exception in allowed:
                remaining = remaining.replace(exception, "")
            remaining = re.sub(r"<[^>]+>|&(?:[A-Za-z][A-Za-z0-9]+|#\d+);", "", remaining)
            for char in forbidden:
                if char in remaining:
                    findings.append(f"{lang} {token} contains ASCII {char!r}: {value}")
    assert not findings, "CJK punctuation lint failed:\n" + "\n".join(findings)
    return findings


_lint_cjk_punctuation()

out = pathlib.Path(__file__).parent
for lang, strings in LANGS.items():
    for mode, colors in MODES.items():
        html = TEMPLATE
        for k, v in {**colors, **strings}.items():
            html = html.replace(k, v)
        assert "@@" not in html, f"unreplaced token in {lang}-{mode}"
        (out / f"demo-{lang}-{mode}.html").write_text(html, encoding="utf-8")
        print(f"wrote demo-{lang}-{mode}.html")
