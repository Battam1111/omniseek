# -*- coding: utf-8 -*-
"""Generate the 6 demo-figure HTML files (en/zh/ja x light/dark) from one template.

The figure is a DESCENT, not a list. The logo says the thesis already: three rings, each with
one opening, the openings rotated layer by layer, so the way in exists but is not a straight
line, and the amber point at the centre is the thing found at depth. This figure is that
cylinder cut open and laid flat, with one real session filling it in.

Which layer a piece of evidence sits in is a CLAIM, not decoration, so the boundary is defined
by the reason plain search misses it:

  layer 1  indexed and written down            -> search reaches it, and stops there
  layer 2  written down, but out of reach      -> behind a login, or buried in a reply thread
  layer 3  never written down at all           -> spoken, or on screen and not in the caption

Crossing languages is NOT a layer. It runs through every layer, so it rides on the card as a
detail rather than becoming a floor of its own.
"""
import pathlib
import re

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:@@PAGEBG@@; }
  body { font-family:-apple-system,"SF Pro Text","Segoe UI","PingFang SC","Hiragino Sans","Hiragino Sans GB",sans-serif; padding:20px; }
  #shot { width:940px; background:@@BG@@; border-radius:14px; padding:26px 40px 20px; }
  .ask { text-align:center; font-size:13.5px; color:@@MUTED@@; letter-spacing:.02em; }
  h1 { text-align:center; font-size:27px; font-weight:700; color:@@FG@@; margin-top:8px; letter-spacing:-.01em; }
  h1 + .layer { margin-top:22px; }

  /* The rail wanders sideways from layer to layer, so the way down is never a straight drop.
     The seam SVGs below carry the same x values, in the same 860-wide coordinate space. */
  .layer { display:flex; gap:14px; align-items:stretch; }
  .layer .rail { flex:0 0 4px; border-radius:2px; }
  .layer .body { flex:1; min-width:0; }
  .l1 { margin-left:40px; }
  .l2 { margin-left:76px; }
  .l3 { margin-left:56px; }
  .l1 .rail { background:@@RING1@@; }
  .l2 .rail { background:@@RING2@@; }
  .l3 .rail { background:@@RING3@@; }
  .lname { font-size:12px; letter-spacing:.11em; margin-bottom:9px; font-weight:650; }
  .l1 .lname { color:@@RING1TEXT@@; }
  .l2 .lname { color:@@RING2@@; }
  .l3 .lname { color:@@RING3TEXT@@; }

  .card { display:flex; gap:13px; background:@@CARDBG@@; border:1px solid @@CARDBORDER@@; border-left:3.5px solid @@ACCENT@@; border-radius:11px; padding:9px 17px; }
  .card + .card { margin-top:9px; }
  .card.surface { border-left-color:@@SURFRAIL@@; }
  .l3 .card { border-left-color:@@RING3@@; }
  .card .ico { font-size:19px; line-height:1.2; flex:0 0 auto; padding-top:2px; }
  .card .meta { font-size:12.5px; color:@@MUTED@@; }
  .card .meta b { color:@@ACCENT@@; font-weight:650; }
  .wall { display:inline-block; font-size:11.5px; font-weight:650; letter-spacing:.01em; color:@@ACCENT@@; background:@@CHIPBG@@; border-radius:5px; padding:2px 7px; margin-right:8px; vertical-align:1px; }
  .card .txt { font-size:14px; color:@@FG@@; line-height:1.4; margin-top:3px; }
  .card .txt b { font-weight:650; }

  .stopnote { margin-top:8px; font-size:12.5px; font-weight:600; color:@@STOPINK@@; letter-spacing:.01em; }
  .seam { display:block; width:860px; height:60px; }

  .arrive { margin-left:74px; margin-top:2px; }
  .aname { font-size:12px; letter-spacing:.11em; margin-bottom:7px; font-weight:650; color:@@AMBERTEXT@@; }
  .arrive .say { font-size:16.5px; color:@@FG@@; line-height:1.5; }
  .arrive b { color:@@AMBERTEXT@@; font-weight:650; }
  .arrive .v2 { font-size:12.5px; color:@@MUTED@@; margin-top:6px; }
</style></head><body>
<div id="shot">
  <div class="ask">@@ASK@@</div>
  <h1>@@Q@@</h1>

  <div class="layer l1">
    <div class="rail"></div>
    <div class="body">
      <div class="lname">@@L1NAME@@</div>
      <div class="card surface"><div class="ico">&#128269;</div><div><div class="meta">@@SMETA@@</div><div class="txt">@@SF@@</div></div></div>
      <div class="stopnote">@@SURFDIV@@</div>
    </div>
  </div>

  <!-- Seam 1. The rail arrives at x=24 and the floor of layer 1 is closed under it: plain search
       ends against the buffer stop. The seek finds the opening at x=54 and drops through. -->
  <svg class="seam" style="height:52px" viewBox="0 0 860 52" xmlns="http://www.w3.org/2000/svg">
    <path d="M 0 28 L 64 28" stroke="@@RING1@@" stroke-width="3" stroke-linecap="round" fill="none"/>
    <path d="M 92 28 L 860 28" stroke="@@RING1@@" stroke-width="3" stroke-linecap="round" fill="none"/>
    <path d="M 42 0 L 42 13" stroke="@@STOPINK@@" stroke-width="3.6" fill="none" stroke-linecap="round"/>
    <path d="M 30 16 L 54 16" stroke="@@STOPINK@@" stroke-width="4.4" stroke-linecap="round"/>
    <path d="M 34 22 L 50 22" stroke="@@STOPINK@@" stroke-width="3" stroke-opacity=".7" stroke-linecap="round"/>
    <path d="M 42 0 C 42 10 78 12 78 28 C 78 38 78 44 78 52" stroke="@@RING2@@" stroke-width="3.4" fill="none" stroke-linecap="round"/>
    <circle cx="78" cy="28" r="5" fill="@@BG@@" stroke="@@RING2@@" stroke-width="2.6"/>
  </svg>

  <div class="layer l2">
    <div class="rail"></div>
    <div class="body">
      <div class="lname">@@L2NAME@@</div>
      <div class="card"><div class="ico">&#128275;</div><div><div class="meta"><span class="wall">@@W2@@</span>@@C2META@@</div><div class="txt">@@F2@@</div></div></div>
      <div class="card"><div class="ico">&#128172;</div><div><div class="meta"><span class="wall">@@W3@@</span>@@C3META@@</div><div class="txt">@@F3@@</div></div></div>
    </div>
  </div>

  <!-- Seam 2. The opening moved back the other way, to x=36: the drift is not a drift, it wanders. -->
  <svg class="seam" viewBox="0 0 860 60" xmlns="http://www.w3.org/2000/svg">
    <path d="M 0 32 L 44 32" stroke="@@RING2@@" stroke-width="3" stroke-linecap="round" fill="none"/>
    <path d="M 72 32 L 860 32" stroke="@@RING2@@" stroke-width="3" stroke-linecap="round" fill="none"/>
    <path d="M 78 0 C 78 12 58 14 58 32 C 58 44 58 50 58 60" stroke="@@RING3@@" stroke-width="3.4" fill="none" stroke-linecap="round"/>
    <circle cx="58" cy="32" r="5" fill="@@BG@@" stroke="@@RING3@@" stroke-width="2.6"/>
  </svg>

  <div class="layer l3">
    <div class="rail"></div>
    <div class="body">
      <div class="lname">@@L3NAME@@</div>
      <div class="card"><div class="ico">&#127908;</div><div><div class="meta"><span class="wall">@@W1@@</span>@@C1META@@</div><div class="txt">@@F1@@</div></div></div>
      <div class="card"><div class="ico">&#128065;&#65039;</div><div><div class="meta"><span class="wall">@@W4@@</span>@@C4META@@</div><div class="txt">@@F4@@</div></div></div>
    </div>
  </div>

  <!-- Arrival. The rail leans in to the amber point instead of stopping short of it. -->
  <!-- The descent resolves into the mark: the three rails ARE its three rings, and the rail
       enters through the outer opening, which the -55 degree rotation already points upward.
       The amber centre is the find, so the figure ends inside the thing it is named for. -->
  <svg class="seam" style="height:74px" viewBox="0 0 860 74" xmlns="http://www.w3.org/2000/svg">
    <path d="M 58 0 C 58 8 58 12 58 16" stroke="@@RING3@@" stroke-width="3.4" fill="none" stroke-linecap="round"/>
    <g transform="translate(29 16) scale(0.29)">
      <circle cx="100" cy="100" r="78" fill="none" stroke="@@RING1@@" stroke-width="16" stroke-linecap="round" stroke-dasharray="416 74" transform="rotate(-55 100 100)"/>
      <circle cx="100" cy="100" r="50" fill="none" stroke="@@RING2@@" stroke-width="16" stroke-linecap="round" stroke-dasharray="267 47" transform="rotate(75 100 100)"/>
      <circle cx="100" cy="100" r="23" fill="none" stroke="@@RING3@@" stroke-width="15" stroke-linecap="round" stroke-dasharray="116 28" transform="rotate(195 100 100)"/>
      <circle cx="100" cy="100" r="16" fill="#F59E0B"/>
    </g>
  </svg>

  <div class="arrive">
    <div class="aname">@@ANAME@@</div>
    <div class="say">@@ARRIVE@@</div>
  </div>
</div>
</body></html>
"""

MODES = {
    "light": {
        "@@PAGEBG@@": "#f1f5f9", "@@BG@@": "#ffffff", "@@FG@@": "#0F172A",
        "@@MUTED@@": "#64748B", "@@MUTED2@@": "#475569",
        "@@CARDBG@@": "#F8FAFC", "@@CARDBORDER@@": "#E2E8F0",
        "@@ACCENT@@": "#3B82F6", "@@AMBERTEXT@@": "#B45309",
        "@@SURFRAIL@@": "#94A3B8", "@@STOPINK@@": "#94A3B8", "@@CHIPBG@@": "#EFF6FF",
        # the three rings of the mark, in order of depth
        "@@RING1@@": "#93C5FD", "@@RING2@@": "#3B82F6", "@@RING3@@": "#1D4ED8",
        # ring 1 is too pale to set type in on white, so the layer name gets a readable sibling
        "@@RING1TEXT@@": "#5B8FD9", "@@RING3TEXT@@": "#1D4ED8",
    },
    "dark": {
        "@@PAGEBG@@": "#0d1117", "@@BG@@": "#0F172A", "@@FG@@": "#E2E8F0",
        "@@MUTED@@": "#94A3B8", "@@MUTED2@@": "#A8B4C4",
        "@@CARDBG@@": "#16233F", "@@CARDBORDER@@": "#1E3A8A",
        "@@ACCENT@@": "#60A5FA", "@@AMBERTEXT@@": "#FBBF24",
        "@@SURFRAIL@@": "#475569", "@@STOPINK@@": "#94A3B8", "@@CHIPBG@@": "#1E3A5F",
        # BRAND.md: the innermost ring lifts to #2563EB on navy, where #1D4ED8 loses contrast
        "@@RING1@@": "#93C5FD", "@@RING2@@": "#3B82F6", "@@RING3@@": "#2563EB",
        "@@RING1TEXT@@": "#93C5FD", "@@RING3TEXT@@": "#60A5FA",
    },
}

# @@S1@@ and @@C1@@..@@C4@@ no longer render: the figure carries the sharpest fragment and the
# README carries the full quote as real text, because evidence has to be selectable,
# translatable and readable by a screen reader. They stay here as that transcript's source.
LANGS = {
    "en": {
        "@@ASK@@": "your agent is asked",
        "@@Q@@": "&ldquo;Can I renew my F-1 visa in a third country? What changed in 2026?&rdquo;",
        "@@WHATIS@@": "OmniSeek is a self-hosted perception server your agent calls. One live session below &middot; outputs quoted as returned.",
        "@@L1NAME@@": "WRITTEN DOWN, AND IN REACH",
        "@@L2NAME@@": "WRITTEN DOWN, BUT OUT OF REACH",
        "@@L3NAME@@": "NEVER WRITTEN DOWN",
        "@@SURFDIV@@": "plain search stops here",
        "@@SF@@": "All quote the same rule. <b>None of them have done it.</b>",
        "@@F1@@": "The &ldquo;4-year cap&rdquo; is the <b>initial period</b>, not the whole stay.",
        "@@F2@@": "<b>Bangkok</b> 25 days. <b>Milan</b> a month. <b>Tokyo</b> silky-smooth.",
        "@@F3@@": "Book a late slot first, <b>then email the consulate to expedite</b>.",
        "@@F4@@": "The facts are <b>on screen, not in the caption</b>.",
        "@@ANAME@@": "WHAT CAME BACK",
        "@@ARRIVE@@": "With OmniSeek, your agent came back with <b>how long it actually took people, how to get seen sooner, and what can shut the road down</b>. Every line has its source.",
        "@@SMETA@@": "headlines, official FAQ, top blogs &middot; <b>one voice</b>",
        "@@S1@@": "&ldquo;From 2026, F-1 admission is limited to a 4-year initial period; renewal in a third country remains possible.&rdquo; <b>All quote the same rule. None of them have done it.</b>",
        "@@W1@@": "Transcribed from audio", "@@W2@@": "Behind login", "@@W3@@": "Deep in the comments", "@@W4@@": "Read from pixels",
        "@@C1META@@": "bilibili, Chinese &middot; <b>transcribed locally</b>",
        "@@C1@@": "The &ldquo;4-year cap&rdquo; in the headlines is the <b>initial period</b>. Extensions moved desks; they didn&rsquo;t vanish.",
        "@@C2META@@": "1point3acres, logged in &middot; <b>your own account</b>",
        "@@C2@@": "<b>Bangkok</b>: booked to passport in 25 days; interview to approval, 30 minutes.<br><b>Milan</b>: a month-long fight for a slot; visa issued for 5 years.<br><b>Tokyo</b>: &ldquo;silky-smooth&rdquo;.",
        "@@C3META@@": "under the Milan post &middot; <b>the author returns</b>",
        "@@C3@@": "&ldquo;Book any late slot first, <b>then email the consulate to expedite</b>. For one F-1 applicant it worked.&rdquo; One person&rsquo;s experience, not official guidance.",
        "@@C4META@@": "rednote video note &middot; <b>frames and speech read locally</b>",
        "@@C4@@": "The facts are on screen, not in the caption: a 212(a)(6)(C) refusal abroad (a misrepresentation finding) can nearly close the F-1 road.",
        "@@VERDICT@@": "Plain search quoted the rule and stopped. The people who had lived it held <b>the timelines, the workaround, and the risk</b>. Your agent went and got all of it, each line attributed.",
        "@@VERDICT2@@": "It also named the sources it held back, each with the exact call to drill it.",
    },
    "zh": {
        "@@ASK@@": "你的 agent 被问到",
        "@@Q@@": "「F-1 过期了，能去第三国续签吗？2026 新规改了什么？」",
        "@@WHATIS@@": "OmniSeek：自托管的感知服务器，你的 agent 直接调用。下方为一次真实会话，输出按原样引用。",
        "@@L1NAME@@": "搜得到",
        "@@L2NAME@@": "搜不到，因为要登录、埋得太深",
        "@@L3NAME@@": "搜不到，因为压根就不是文字",
        "@@SURFDIV@@": "普通搜索到此为止",
        "@@SF@@": "引的都是同一条规则，<b>谁都没真的办过</b>。",
        "@@F1@@": "「最多待 4 年」其实是<b>初始停留期</b>，不是全程。",
        "@@F2@@": "<b>曼谷</b> 25 天。<b>米兰</b> 一个月。<b>东京</b> 丝滑。",
        "@@F3@@": "先约靠后的日期，<b>再发邮件给领事馆申请加急</b>。",
        "@@F4@@": "干货<b>在画面里，不在文字里</b>。",
        "@@ANAME@@": "带回来的",
        "@@ARRIVE@@": "有了 OmniSeek，你的 agent 带回了<b>别人实际办下来要多久、怎么把时间抢回来、什么情况会把这条路堵死</b>。每一行都有出处。",
        "@@SMETA@@": "新闻头条、官方 FAQ、热门博客 &middot; <b>同一个声音</b>",
        "@@S1@@": "「2026 年起，F-1 入境限四年初始期；第三国续签仍被允许。」<b>引的都是同一条规则，谁都没真的办过。</b>",
        "@@W1@@": "音频转写", "@@W2@@": "登录墙内", "@@W3@@": "评论区深处", "@@W4@@": "像素读出",
        "@@C1META@@": "bilibili &middot; 中文 &middot; <b>本地转写</b>",
        "@@C1@@": "头条说的「最多待 4 年」，其实是<b>初始停留期</b>。延期只是换了审批部门，并没有消失。",
        "@@C2META@@": "一亩三分地，已登录 &middot; <b>你自己的账号</b>",
        "@@C2@@": "<b>曼谷</b>：预约到拿护照 25 天；面签到通过 30 分钟。<br><b>米兰</b>：抢号一个月；签出 5 年。<br><b>东京</b>：「丝滑」。",
        "@@C3META@@": "米兰帖下 &middot; <b>楼主回来补充</b>",
        "@@C3@@": "「先约一个靠后的日期，<b>再发邮件给领事馆申请加急</b>。有一位 F-1 申请人真的成功了。」个人经验，非官方指引。",
        "@@C4META@@": "小红书视频笔记 &middot; <b>帧与语音本地读取</b>",
        "@@C4@@": "干货在画面里，不在文字里：第三国遇上 212(a)(6)(C) 拒签（虚假陈述认定），F-1 之路几乎断送。",
        "@@VERDICT@@": "普通搜索引到规则就停了。真正办过的人手里有<b>时间线、解法和风险</b>。你的 agent 一路下潜，把它们全部带回，每一行都有出处。",
        "@@VERDICT2@@": "它还点名了被扣下的源，并附上钻取每一个的确切调用。",
    },
    "ja": {
        "@@ASK@@": "エージェントへの質問",
        "@@Q@@": "「F-1 が切れた。第三国で更新できる？2026 年に何が変わった？」",
        "@@WHATIS@@": "OmniSeek はエージェントが呼び出すセルフホスト型の知覚サーバー。以下はひとつの実セッション、出力は返ってきたまま引用。",
        "@@L1NAME@@": "書かれていて、届く",
        "@@L2NAME@@": "書かれてはいるが、届かない",
        "@@L3NAME@@": "そもそも書かれていない",
        "@@SURFDIV@@": "通常の検索はここで止まる",
        "@@SF@@": "どれも同じ規則の引用。<b>実際にやった人はいない</b>。",
        "@@F1@@": "「最長 4 年」は<b>初期滞在期間</b>のことで、全期間ではない。",
        "@@F2@@": "<b>バンコク</b> 25 日。<b>ミラノ</b> ひと月。<b>東京</b> スムーズ。",
        "@@F3@@": "まず遅い日程で予約し、<b>領事館にメールで繰り上げを依頼</b>。",
        "@@F4@@": "事実は<b>画面の上にあって、文字にはない</b>。",
        "@@ANAME@@": "持ち帰ったもの",
        "@@ARRIVE@@": "OmniSeek があれば、あなたのエージェントは<b>実際にどれくらいかかったのか、どうすれば早く見てもらえるのか、何がこの道を塞ぐのか</b>を持ち帰る。一行ごとに出典がある。",
        "@@SMETA@@": "ニュース見出し、公式 FAQ、人気ブログ &middot; <b>声はひとつ</b>",
        "@@S1@@": "「2026 年より F-1 の入国は 4 年の初期滞在期間に制限。第三国での更新は引き続き可能。」<b>どれも同じ規則の引用。実際にやった人はいない。</b>",
        "@@W1@@": "音声から書き起こし", "@@W2@@": "ログインの内側", "@@W3@@": "コメント欄の奥", "@@W4@@": "ピクセルから読解",
        "@@C1META@@": "bilibili &middot; 中国語 &middot; <b>ローカルで書き起こし</b>",
        "@@C1@@": "見出しの「最長 4 年」は<b>初期滞在期間</b>のこと。延長は審査窓口が変わっただけで、消えてはいない。",
        "@@C2META@@": "1point3acres、ログイン済み &middot; <b>あなた自身のアカウント</b>",
        "@@C2@@": "<b>バンコク</b>：予約からパスポートまで 25 日；面接から承認まで 30 分。<br><b>ミラノ</b>：枠争奪ひと月；ビザは 5 年。<br><b>東京</b>：「スムーズそのもの」。",
        "@@C3META@@": "ミラノ投稿の下 &middot; <b>投稿者が戻ってきて</b>",
        "@@C3@@": "「まず遅い日程で予約し、<b>それから領事館にメールで繰り上げを依頼</b>。F-1 申請者一人は実際に成功した。」",
        "@@C4META@@": "RED（小紅書）の動画ノート &middot; <b>フレームと音声をローカルで読む</b>",
        "@@C4@@": "事実は文字ではなく画面の上に：第三国での 212(a)(6)(C) 拒否（虚偽陳述の認定）は F-1 への道をほぼ閉ざす。",
        "@@VERDICT@@": "通常の検索は規則を引用して止まった。実際に経験した人々は<b>タイムラインと回避策とリスク</b>を持っていた。あなたのエージェントは降り続け、そのすべてを出典つきで持ち帰った。",
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


def _lint_layer_assignment():
    """The layer a card sits in is a claim, so assert the template puts each card where the
    docstring says it belongs. Cards move when someone edits the template; this catches it."""
    l2 = TEMPLATE.split('class="layer l2"')[1].split('class="layer l3"')[0]
    l3 = TEMPLATE.split('class="layer l3"')[1]
    problems = []
    for token, layer, body in (("@@F2@@", "l2", l2), ("@@F3@@", "l2", l2),
                               ("@@F1@@", "l3", l3), ("@@F4@@", "l3", l3)):
        if token not in body:
            problems.append(f"{token} is not in layer {layer}")
    assert not problems, "layer assignment drifted:\n" + "\n".join(problems)


_lint_cjk_punctuation()
_lint_layer_assignment()

out = pathlib.Path(__file__).parent
for lang, strings in LANGS.items():
    for mode, colors in MODES.items():
        html = TEMPLATE
        for k, v in {**colors, **strings}.items():
            html = html.replace(k, v)
        assert "@@" not in html, f"unreplaced token in {lang}-{mode}"
        (out / f"demo-{lang}-{mode}.html").write_text(html, encoding="utf-8")
        print(f"wrote demo-{lang}-{mode}.html")
