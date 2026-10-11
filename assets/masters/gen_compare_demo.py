"""Side-by-side demo: the same question to an agent with web search only (left) and an agent with
OmniSeek (right). Rendered from one recorded run to GIFs (README, site) and MP4s (X, Bilibili).

    python assets/masters/gen_compare_demo.py gif      # assets/demo/compare-{en,zh,ja}-{light,dark}{,-narrow}.gif
    python assets/masters/gen_compare_demo.py mp4      # assets/demo/compare-{en,zh}-dark.mp4 (+ posters)
    python assets/masters/gen_compare_demo.py poster   # assets/demo/compare-{en,zh}-dark-poster.png only
    python assets/masters/gen_compare_demo.py all

The -narrow GIFs stack the two agents (web search on top) for phones: the README picks them with
a (max-width: 480px) source, where the side-by-side GIF would shrink below half size. Each column
is as wide as in the side-by-side GIF, so the lines wrap the same; the columns are shorter, so the
oldest steps scroll off the top sooner.

Every GIF opens on the finished comparison (both answers in, 1:21:51 highlighted; the end card
left off) for 3 s, then replays from the question with each agent's first call already on screen
(never two empty columns) and ends on the end card. Many places show only a GIF's first frame (a
page still loading, a screenshot, a thumbnail, reduced motion), and an empty frame looks broken
there and to a reader who scrolls to the GIF late. The MP4 keeps its storyboard (it starts from the
empty stage) and uses the poster instead.

The poster is the MP4's last frame before the end card (the finished comparison), for the site's
<video poster=...>; it needs no ffmpeg.

Needs `playwright` (with its Chromium), `pillow`, and for mp4 `imageio-ffmpeg` (it ships an ffmpeg
binary). OMNISEEK_CHROMIUM may point at a Chromium executable to use instead of Playwright's own.

Where the words come from: compare-demo-run.json next to this script, built from the raw tool-call
records of the run (see its "about" field). Every Chinese line on screen is cut from that record by
`cut()`, which refuses to run if the cut is not found verbatim, so a line can be shortened (with an
ellipsis) but never reworded. The English version adds a translation under each Chinese line; the
translations are typed here (GLOSS) and set in italics as translations. The Japanese version does the
same in Japanese (GLOSS_JA, upright, in 「」), with its cards, headers and captions in Japanese; the
Chinese lines on it are marked lang="zh-CN" so they keep Chinese fonts. Tool names on screen are the
public ones from src/omniseek/server.py.

Speed: some steps are sped up. Each sped-up step carries a badge with the real time and the factor.

Colours are the brand palette in docs/BRAND.md (mixed with white or navy). Never edit the outputs by
hand; change this script or the record.
"""
import glob
import html
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = json.loads((HERE / "compare-demo-run.json").read_text(encoding="utf-8"))
OUT = HERE.parent / "demo"
# A screenshot of the video player opened at the ?t= link, shown in the MP4 only. It is a frame of
# someone else's video, so it is not kept in this repo: point OMNISEEK_PLAYER_FRAME at it.
FRAME = Path(os.environ.get("OMNISEEK_PLAYER_FRAME", "/nonexistent"))

W = 720            # CSS px; the README shows GIFs at width 720
GIF_H = 810
GIF_SCALE = 2
NARROW_W, NARROW_H = 360, 1080   # stacked layout for phones (README shows it at width 360)
NARROW_LEFT = 300                # CSS px of the web-search column (header included is extra)
MP4_W, MP4_H = 1280, 720
MP4_FPS = 30
COVER_MS = 3000  # GIF only: how long the finished comparison shows as frame 0 before the replay

R01 = RUN["R01"]
WEB = R01["web"]
READ, TRANS = R01["omniseek"]
VIDEO = R01["video"]


def cut(text: str, part: str, more: bool = True) -> str:
    """Return `part` (plus an ellipsis when the source goes on) after checking it is in `text`."""
    if part not in text:
        raise SystemExit(f"not in the record, refusing to show: {part!r}")
    return part + ("…" if more and not text.rstrip().endswith(part) else "")


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def mins(s: float) -> str:
    return f"{s / 60:.0f} min"


# Transcript lines shown on the right: every line of the whole-video transcript that contains 写作
# ("writing"), as found by searching the text. The line with the quote is shown whole; the others as
# the clause around the word. Picked from the record by rule, never typed.
HITS = R01["transcript_hits_写作"]
QUOTE = next(h for h in HITS if "非常简洁" in h["text"])
T_QUOTE = int(QUOTE["start"])
PUNCT = "，。？！；"


def clause(text: str, word: str = "写作"):
    """(part, ellipsis before, ellipsis after): the clause of `text` holding `word`."""
    i = text.index(word)
    a = max([text.rfind(c, 0, i) for c in PUNCT]) + 1
    ends = [j for j in (text.find(c, i) for c in PUNCT) if j >= 0]
    b = min(ends) + 1 if ends else len(text)
    return text[a:b], a > 0, b < len(text)


def hit_text(h) -> str:
    """The text shown for a hit; the GLOSS key is this text without ellipses."""
    if h is QUOTE:
        return h["text"]
    return clause(h["text"])[0]


def hit_show(h) -> str:
    if h is QUOTE:
        return h["text"]
    part, pre, post = clause(h["text"])
    return ("…" if pre else "") + part + ("…" if post else "")


GLOSS = {
    # left, web search answers
    "这是一篇略短的文章，但是信息量巨大。": "“A rather short paper, but packed with information.”",
    "（大意，": "(the answer's own label: “gist”)",
    "大约在开头第1~8分钟": "“around minutes 1 to 8”",
    "约在1分50秒-2分30秒之间": "“between about 1:50 and 2:30”",
    # right, page read
    "更多请见：https://github.com/mli/paper-reading": "“More at: github.com/mli/paper-reading”",
    # right, transcript hits
    "所以他整个写作的时候，": "“…so when writing it…”",
    "我们首先看一下他的写作。": "“First, let’s look at its writing.”",
    "啊，这篇文章的写作是非常简洁的，因为就是说他每一句话就基本上在讲一件事情。":
        "“The writing of this paper is very concise: basically, every sentence says one thing.”",
    "另外一个是说他没有用太多的写作技巧啊，": "“Also, it doesn’t use many writing techniques…”",
}

T = {
    "en": {
        "lang": "en",
        "ask": "Asked in Chinese (translated here)",
        "question": "In Mu Li’s Bilibili video on the Transformer paper, how does he judge the paper’s writing? "
                    "Give his exact words and the minute.",
        "left": "Agent with web search only",
        "right": "Agent with OmniSeek",
        "real": "real {r}, sped up ×{x}",
        "realtime": "real time, {r}",
        "no_chapters": "No chapter times on the page, so: transcribe the whole video",
        "no_chapters_short": "no chapter times",
        "realtime_short": "real {r}",
        "length": "length",
        "transcribing": "transcribing the whole video…",
        "transcribed": "{a} of audio, {n} timed lines",
        "search_text": "search the transcript for 写作 (“writing”)",
        "opens": "opens the video at {t}",
        "again": "Asked again",
        "same": "same line, {t} (cached, {s})",
        "check": "Search the web for his exact words",
        "extra": "Transcription is an optional extra: ",
        "cap": [
            "Same question, two agents",
            "Web search finds notes other people wrote, and paraphrases them",
            "OmniSeek reads the video page first",
            "No chapters to go on, so it transcribes the whole video locally",
            "Then it searches the text: his words, with the second they were said",
            "The link jumps straight to that second",
            "His exact words are not on the web",
            "Asked again: the web’s answer moved, the transcript did not",
        ],
        "end1": "Web search finds what others wrote about a video.",
        "end2": "With OmniSeek, the agent reads what was said in it, to the second.",
        "run": "Real runs, 2026-10-11. Sped-up steps are marked.",
    },
    "zh": {
        "lang": "zh",
        "ask": "用中文问",
        "question": R01["web"][0]["query"],
        "left": "只有网页搜索的 Agent",
        "right": "装了 OmniSeek 的 Agent",
        "real": "实际 {r}，屏上加速 ×{x}",
        "realtime": "实际速度，{r}",
        "no_chapters": "页面上没有分段时间，所以整段视频转写",
        "no_chapters_short": "页面上没有分段时间",
        "realtime_short": "实际 {r}",
        "length": "时长",
        "transcribing": "整段视频转写中…",
        "transcribed": "{a}音频，{n} 句带时间的文字",
        "search_text": "在转写文字里找「写作」",
        "opens": "打开就从 {t} 播放",
        "again": "再问一次",
        "same": "还是这句，{t}（已缓存，{s}）",
        "check": "到网上搜这句原话",
        "extra": "转写是可选功能：",
        "cap": [
            "同一个问题，两个 Agent",
            "网页搜索找到别人写的笔记，转述给你",
            "OmniSeek 先读视频页",
            "没有分段可依，就在本地把整段视频转成文字",
            "再在文字里找：原话，连同说出它的那一秒",
            "链接直接跳到那一秒",
            "这句原话在网上搜不到",
            "再问一次：网页的答案变了，转写的没变",
        ],
        "end1": "网页搜索找得到别人对视频写了什么。",
        "end2": "装了 OmniSeek，\nAgent 读得到视频里说了什么，精确到秒。",
        "run": "均为 2026-10-11 实跑；加速的步骤都已标出。",
    },
    # Japanese: the cards, headers, captions and end card in Japanese; every Chinese line from the
    # record stays as said, with a Japanese translation under it (GLOSS_JA), in the voice of
    # docs/i18n/README_ja.md.
    "ja": {
        "lang": "ja",
        "ask": "中国語で質問（ここでは日本語訳）",
        "question": "李沐（Mu Li）が Bilibili で Transformer 論文を解説した動画で、彼は論文の書き方をどう評していますか？"
                    "本人の言葉と、何分目かを教えてください。",
        "left": "ウェブ検索だけのエージェント",
        "right": "OmniSeek を使うエージェント",
        "real": "実際は {r}、画面では ×{x} に加速",
        "realtime": "実際の速度、{r}",
        "no_chapters": "ページにチャプターの時刻がないので、動画をまるごと文字起こし",
        "no_chapters_short": "チャプターの時刻なし",
        "realtime_short": "実際 {r}",
        "length": "長さ",
        "transcribing": "動画をまるごと文字起こし中…",
        "transcribed": "音声 {a}、時刻つきの {n} 行",
        "search_text": "文字起こしから「写作」（書き方）を検索",
        "opens": "{t} から再生",
        "again": "もう一度質問",
        "same": "同じ発言、{t}（キャッシュ済み、{s}）",
        "check": "本人の言葉をウェブで検索",
        "extra": "文字起こしは追加機能です：",
        "cap": [
            "同じ質問を、2 つのエージェントに",
            "ウェブ検索は、他の人が書いたメモを見つけて言い換える",
            "OmniSeek はまず動画のページを読む",
            "頼れるチャプターがないので、動画をまるごと手元で文字に起こす",
            "次に文字を検索：本人の言葉と、それを話した秒",
            "リンクはその秒へ直接飛ぶ",
            "本人の言葉はウェブでは見つからない",
            "もう一度聞くと、ウェブの答えは変わり、文字起こしは変わらない",
        ],
        "end1": "ウェブ検索が見つけるのは、動画について他の人が書いたこと。",
        "end2": "OmniSeek があれば、エージェントは\n動画で話されたことを秒単位で読めます。",
        "run": "2026-10-11 の実際の実行です。加速したステップには印をつけています。",
    },
}

GLOSS_JA = {
    "这是一篇略短的文章，但是信息量巨大。": "「やや短い論文だが、情報量はとても多い。」",
    "（大意，": "（回答自身の注記：「要旨」）",
    "大约在开头第1~8分钟": "「最初の 1〜8 分ごろ」",
    "约在1分50秒-2分30秒之间": "「およそ 1 分 50 秒から 2 分 30 秒のあいだ」",
    "更多请见：https://github.com/mli/paper-reading": "「詳しくは github.com/mli/paper-reading へ」",
    "所以他整个写作的时候，": "「…だから全体を書くときに…」",
    "我们首先看一下他的写作。": "「まず、この論文の書き方を見てみましょう。」",
    "啊，这篇文章的写作是非常简洁的，因为就是说他每一句话就基本上在讲一件事情。":
        "「この論文の書き方はとても簡潔です。基本的に、一文ごとにひとつのことを言っているからです。」",
    "另外一个是说他没有用太多的写作技巧啊，": "「もうひとつは、書き方のテクニックをあまり使っていないことで…」",
}
GLOSSES = {"en": GLOSS, "ja": GLOSS_JA}
LANGS = ("en", "zh", "ja")
MP4_LANGS = ("en", "zh")

INSTALL = 'uv tool install "omniseek[asr]" --with torch --with torchaudio'

THEMES = {
    "light": {"page": "#FFFFFF", "css": """
  --bg: color-mix(in srgb, #93C5FD 9%, #FFFFFF);
  --panel: #FFFFFF;
  --bar: color-mix(in srgb, #93C5FD 22%, #FFFFFF);
  --line: color-mix(in srgb, #3B82F6 24%, #FFFFFF);
  --fg: #0F172A;
  --muted: color-mix(in srgb, #0F172A 68%, #FFFFFF);
  --faint: color-mix(in srgb, #0F172A 55%, #FFFFFF);
  --fg-dim: color-mix(in srgb, #0F172A 72%, #FFFFFF);
  --muted-dim: color-mix(in srgb, #0F172A 52%, #FFFFFF);
  --fn-dim: color-mix(in srgb, #1D4ED8 84%, #FFFFFF);
  --fn: #1D4ED8;
  --hl: color-mix(in srgb, #F59E0B 22%, #FFFFFF);
  --amber: #B45309;
  --badge: color-mix(in srgb, #3B82F6 12%, #FFFFFF);
  --ring3: #1D4ED8;"""},
    "dark": {"page": "#0F172A", "css": """
  --bg: #0F172A;
  --panel: color-mix(in srgb, #93C5FD 5%, #0F172A);
  --bar: color-mix(in srgb, #93C5FD 10%, #0F172A);
  --line: color-mix(in srgb, #93C5FD 20%, #0F172A);
  --fg: color-mix(in srgb, #FFFFFF 94%, #0F172A);
  --muted: color-mix(in srgb, #FFFFFF 64%, #0F172A);
  --faint: color-mix(in srgb, #FFFFFF 48%, #0F172A);
  --fg-dim: color-mix(in srgb, #FFFFFF 64%, #0F172A);
  --muted-dim: color-mix(in srgb, #FFFFFF 46%, #0F172A);
  --fn-dim: color-mix(in srgb, #93C5FD 70%, #0F172A);
  --fn: #93C5FD;
  --hl: color-mix(in srgb, #F59E0B 24%, #0F172A);
  --amber: #F59E0B;
  --badge: color-mix(in srgb, #3B82F6 22%, #0F172A);
  --ring3: #2563EB;"""},
}
# --amber on light pages is the brand amber mixed toward navy for contrast on white and on the
# amber highlight (at least 4.5:1, see check_contrast.py).
THEMES["light"]["css"] = THEMES["light"]["css"].replace(
    "--amber: #B45309;", "--amber: color-mix(in srgb, #F59E0B 50%, #0F172A);")

MARK = """<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
  <circle cx="100" cy="100" r="78" fill="none" stroke="#93C5FD" stroke-width="16" stroke-linecap="round" stroke-dasharray="416 74" transform="rotate(-55 100 100)"/>
  <circle cx="100" cy="100" r="50" fill="none" stroke="#3B82F6" stroke-width="16" stroke-linecap="round" stroke-dasharray="267 47" transform="rotate(75 100 100)"/>
  <circle cx="100" cy="100" r="23" fill="none" stroke="var(--ring3)" stroke-width="15" stroke-linecap="round" stroke-dasharray="116 28" transform="rotate(195 100 100)"/>
  <circle cx="100" cy="100" r="16" fill="#F59E0B"/>
</svg>"""

GLASS = """<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg>"""

PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ background:transparent; }}
#stage {{ {css}
  position:relative; width:{w}px; height:{h}px; background:var(--bg); border:1px solid var(--line);
  border-radius:14px; overflow:hidden; display:flex; flex-direction:column; color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","Helvetica Neue",Arial,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased; }}
.mono {{ font-family:ui-monospace,"SF Mono",Menlo,Consolas,"PingFang SC","Microsoft YaHei",monospace; }}
/* Japanese page: Japanese fonts first, no italics for translations; the Chinese lines from the
   record are marked lang="zh-CN" there and keep their Chinese fonts. */
#stage:lang(ja) {{ font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","Helvetica Neue",Arial,"Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic UI","Yu Gothic",Meiryo,sans-serif; }}
#stage:lang(ja) .mono {{ font-family:ui-monospace,"SF Mono",Menlo,Consolas,"Hiragino Sans","Yu Gothic UI",Meiryo,monospace; }}
#stage:lang(ja) .gl {{ font-style:normal; }}
#stage [lang|="zh"] {{ font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","Helvetica Neue",Arial,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; }}
#stage .mono [lang|="zh"] {{ font-family:ui-monospace,"SF Mono",Menlo,Consolas,"PingFang SC","Microsoft YaHei",monospace; }}
.q {{ padding:14px 18px 12px; border-bottom:1px solid var(--line); }}
.q .ask {{ font-size:15px; color:var(--muted); font-weight:600; }}
.q .text {{ font-size:{qsize}px; font-weight:700; line-height:1.28; margin-top:3px; letter-spacing:-.005em; }}
.cols {{ flex:1 1 auto; display:flex; min-height:0; }}
.col {{ flex:1 1 0; min-width:0; display:flex; flex-direction:column; background:var(--panel); }}
.col + .col {{ border-left:1px solid var(--line); }}
.head {{ display:flex; align-items:center; gap:9px; padding:9px 14px; background:var(--bar);
  border-bottom:1px solid var(--line); font-size:19px; font-weight:750; }}
.head svg {{ width:24px; height:24px; flex:0 0 auto; color:var(--muted); }}
.feed {{ flex:1 1 auto; min-height:0; overflow:hidden; display:flex; flex-direction:column;
  justify-content:flex-end; padding:6px 14px 10px;
  -webkit-mask-image:linear-gradient(to bottom, transparent 0, #000 34px); }}
.step {{ margin-top:10px; transition:none; }}
/* A step that is no longer current steps back by colour, not opacity: its main line stays at
   4.5:1 or more and its smaller lines at 3:1 or more against the card (check_contrast.py). */
.step.dim {{ --fg:var(--fg-dim); --muted:var(--muted-dim); --fn:var(--fn-dim); color:var(--fg); }}
.call {{ font-size:15px; color:var(--muted); line-height:1.35; overflow-wrap:anywhere; }}
.call .fn {{ color:var(--fn); font-weight:650; }}
.lbl {{ font-size:16px; color:var(--muted); font-weight:600; }}
.big {{ font-size:22px; font-weight:650; line-height:1.3; margin-top:3px; }}
.mid {{ font-size:18px; line-height:1.32; margin-top:3px; }}
.gl {{ font-size:15.5px; font-style:italic; color:var(--muted); line-height:1.3; margin-top:1px; }}
.badge {{ display:inline-block; font-size:13.5px; font-weight:650; color:var(--fn); background:var(--badge);
  border-radius:6px; padding:1px 7px; margin-top:4px; }}
.line {{ display:flex; gap:9px; align-items:baseline; margin-top:4px; padding:3px 6px; border-radius:7px; }}
.line .at {{ font-size:17px; color:var(--fn); font-weight:700; flex:0 0 auto; }}
.line .tx {{ font-size:19px; line-height:1.3; }}
.line.hl {{ background:var(--hl); }}
.line.hl .at {{ color:var(--amber); }}
.line .gl {{ font-size:15px; }}
.bar2 {{ height:9px; border-radius:5px; background:var(--line); margin-top:7px; overflow:hidden; }}
.bar2 i {{ display:block; height:100%; background:#3B82F6; width:0%; }}
.link {{ font-size:16.5px; color:var(--fn); font-weight:600; }}
.no {{ font-size:20px; font-weight:700; margin-top:3px; }}
.extra {{ font-size:13px; color:var(--faint); padding:6px 14px 8px; border-top:1px solid var(--line); line-height:1.35; }}
.extra .mono {{ font-size:12px; }}
.cap {{ border-top:1px solid var(--line); padding:9px 18px 10px; font-size:{capsize}px; font-weight:650;
  text-align:center; background:var(--bar); min-height:{caph}px; display:flex; align-items:center; justify-content:center; }}
.end {{ position:absolute; inset:0; display:none; flex-direction:column; align-items:center; justify-content:center;
  text-align:center; gap:14px; padding:40px; background:var(--bg); }}
.end svg {{ width:64px; height:64px; }}
.end .e1, .end .e2 {{ text-wrap:balance; max-width:620px; white-space:pre-line; }}
.end .e1 {{ font-size:{esize}px; font-weight:600; color:var(--muted); line-height:1.3; }}
.end .e2 {{ font-size:{esize2}px; font-weight:800; line-height:1.28; }}
.end .run {{ font-size:15px; color:var(--faint); margin-top:6px; }}
.frame {{ margin-top:6px; border-radius:8px; overflow:hidden; border:1px solid var(--line); }}
.frame img {{ display:block; width:100%; }}
.h {{ display:none; }}
{narrow}</style></head><body>
<div id="stage">
  <div class="q"><div class="ask">{ask}</div><div class="text">{question}</div></div>
  <div class="cols">
    <div class="col"><div class="head">{glass}<span>{left}</span></div><div class="feed">{lsteps}</div></div>
    <div class="col"><div class="head">{mark}<span>{right}</span></div><div class="feed">{rsteps}</div>
      <div class="extra">{extra}<span class="mono">{install}</span></div></div>
  </div>
  <div class="cap" id="cap"></div>
  <div class="end" id="end">{mark}<div class="e1">{end1}</div><div class="e2">{end2}</div><div class="run">{run}</div></div>
</div>
</body></html>"""


def gl(lang: str, zh: str, cls: str = "gl") -> str:
    if lang not in GLOSSES:
        return ""
    if zh not in GLOSSES[lang]:
        raise SystemExit(f"no {lang} translation for {zh!r}; add it to GLOSS / GLOSS_JA")
    return f'<div class="{cls}">{html.escape(GLOSSES[lang][zh])}</div>'


def secs(s: float, lang: str) -> str:
    unit = {"en": ("min", "s"), "zh": ("分钟", "秒"), "ja": ("分", "秒")}[lang]
    if s >= 90:
        return f"{s / 60:.0f} {unit[0]}"
    return f"{s:.1f} {unit[1]}"


def speedup(real_s: float, mp4: bool, step: str, until: str) -> int:
    """How much faster than real time a step runs on screen: real time over the time the step is
    on screen before its next item appears (taken from the storyboard, so it follows any retiming)."""
    shown = sum(ms for st, ms in plan(mp4)
                if step in st["show"] and until not in st["show"] and not st.get("cover")) / 1000
    x = real_s / shown
    return int(round(x, -1)) if x >= 20 else max(1, round(x))


def build_steps(lang: str, mp4: bool):
    """Left and right step HTML, keyed by id."""
    t, e = T[lang], html.escape
    # On the Japanese page, a Chinese line from the record is marked as Chinese (fonts, line breaks).
    z = (lambda s: f'<span lang="zh-CN">{s}</span>') if lang == "ja" else (lambda s: s)
    w1, w2, w3 = WEB
    q1 = w1["query"]
    left = {}
    left["l1"] = (f'<div class="call mono"><span class="fn">web_search</span>("{z(e(cut(q1, q1[:22])))}")</div>'
                  f'<div class="badge" id="l1b">{e(t["real"].format(r=secs(w1["elapsed_s"], lang), x=speedup(w1["elapsed_s"], mp4, "l1", "l1r")))}</div>')
    a1 = w1["answer"]
    gist = "「" + e(cut(a1, "这是一篇略短的文章，但是信息量巨大。")) + "」" + e(cut(a1, "（大意，"))
    left["l1r"] = (f'<div class="mid">{z(gist)}</div>'
                   + gl(lang, "这是一篇略短的文章，但是信息量巨大。") + gl(lang, "（大意，")
                   + f'<div class="big">{z(e(cut(a1, "大约在开头第1~8分钟", more=False)))}</div>'
                   + gl(lang, "大约在开头第1~8分钟"))
    q2 = w2["query"]
    phrase = '"这篇文章的写作是非常简洁的"'
    left["l2"] = (f'<div class="lbl">{e(t["check"])}</div>'
                  f'<div class="call mono"><span class="fn">web_search</span>(\'…{z(e(cut(q2, phrase, more=False)))}…\')</div>'
                  f'<div class="no">{e(cut(w2["answer"], "No relevant web results found.", more=False))}</div>'
                  f'<div class="badge">{e(t["real"].format(r=secs(w2["elapsed_s"], lang), x=speedup(w2["elapsed_s"], mp4, "l2", "l3")))}</div>')
    a3 = w3["answer"]
    left["l3"] = (f'<div class="lbl">{e(t["again"])}</div>'
                  f'<div class="big">{z(e(cut(a3, "约在1分50秒-2分30秒之间", more=False)))}</div>'
                  + gl(lang, "约在1分50秒-2分30秒之间"))

    right = {}
    tgt = READ["args"]["target"].split("://", 1)[-1].replace("www.", "")
    right["r1"] = (f'<div class="call mono"><span class="fn">omniseek_read</span>("{e(tgt)}")</div>'
                   f'<div class="badge">{e(t["realtime"].format(r=secs(READ["elapsed_s"], lang)))}</div>')
    desc = READ["result"]["content"]
    right["r1r"] = (f'<div class="mid">{z(e(READ["result"]["title"]))} · {e(t["length"])} {hms(READ["result"]["duration_seconds"])}</div>'
                    f'<div class="mid">{z(e(cut(desc, "更多请见：https://github.com/mli/paper-reading", more=False)))}</div>'
                    + gl(lang, "更多请见：https://github.com/mli/paper-reading")
                    + f'<div class="lbl" style="margin-top:4px">→ {e(t["no_chapters"])}</div>')
    a = TRANS["args"]
    wall = TRANS["wall_seconds"]
    right["r2"] = (f'<div class="call mono"><span class="fn">omniseek_transcribe</span>(url="…{e(a["url"].rsplit("/", 1)[-1])}", '
                   f'language="{a["language"]}", segments=true)</div>'
                   f'<div class="lbl" id="r2s">{e(t["transcribing"])}</div><div class="bar2"><i id="r2p"></i></div>'
                   f'<div class="badge">{e(t["real"].format(r=secs(wall, lang), x=speedup(wall, mp4, "r2", "r2r")))}</div>')
    n_lines = format(TRANS["segments_count"], ",")
    right["r2r"] = f'<div class="mid">{e(t["transcribed"].format(a=secs(TRANS["audio_seconds"], lang), n=n_lines))}</div>'
    right["r1c"] = (f'<div class="call mono"><span class="fn">omniseek_read</span> → '
                    f'<span style="font-family:inherit">{e(t["no_chapters_short"])}</span></div>')
    right["r2c"] = (f'<div class="call mono"><span class="fn">omniseek_transcribe</span> → '
                    f'{e(t["transcribed"].format(a=secs(TRANS["audio_seconds"], lang), n=n_lines))} '
                    f'({e(t["realtime_short"].format(r=secs(wall, lang)))})</div>')
    lines = []
    for h in HITS:
        hl = " hl" if h is QUOTE else ""
        lines.append(f'<div class="line{hl}"><span class="at mono">{hms(h["start"])}</span>'
                     f'<div><div class="tx">{z(e(hit_show(h)))}</div>{gl(lang, hit_text(h))}</div></div>')
    right["r3"] = f'<div class="lbl">{e(t["search_text"])}</div>' + "".join(lines)
    link = f'bilibili.com/video/BV1pu411o7BE<wbr><span style="white-space:nowrap">?t={T_QUOTE}</span>'
    frame = ""
    if mp4 and FRAME.exists():
        frame = f'<div class="frame"><img src="{FRAME.as_uri()}"></div>'
    right["r4"] = f'<div class="link mono">{link}</div>'
    right["r4o"] = (f'<div class="lbl">→ {e(t["opens"].format(t=hms(T_QUOTE)))}</div>'
                    f'<div id="r4f">{frame}</div>')
    again = R01.get("omniseek_again")
    if again:
        right["r5"] = (f'<div class="lbl">{e(t["again"])}</div>'
                       f'<div class="mid">{e(t["same"].format(t=hms(T_QUOTE), s=secs(again["wall_seconds"], lang)))}</div>')
    return left, right




NARROW_CSS = """.cols {{ flex-direction:column; }}
.col {{ min-height:0; }}
.col:first-child {{ flex:0 0 {left}px; }}
.col:last-child {{ flex:1 1 0; }}
.col + .col {{ border-left:none; border-top:1px solid var(--line); }}
.cap {{ min-height:{caph}px; }}
"""


def page_html(lang: str, theme: str, w: int, h: int, mp4: bool, narrow: bool = False) -> str:
    t = T[lang]
    extra_css = NARROW_CSS.format(left=NARROW_LEFT + 43, caph=72) if narrow else ""
    left, right = build_steps(lang, mp4)
    st = lambda d: "".join(f'<div class="step h" id="{k}">{v}</div>' for k, v in d.items())
    return PAGE.format(css=THEMES[theme]["css"], w=w, h=h, lang=lang, ask=html.escape(t["ask"]),
                       question=html.escape(t["question"]), left=html.escape(t["left"]),
                       right=html.escape(t["right"]), glass=GLASS, mark=MARK, lsteps=st(left), rsteps=st(right),
                       extra=html.escape(t["extra"]), install=html.escape(INSTALL),
                       end1=html.escape(t["end1"]), end2=html.escape(t["end2"]), run=html.escape(t["run"]),
                       qsize=19 if lang == "en" else 20, capsize=19, caph=30, esize=24, esize2=29,
                       narrow=extra_css)


def plan(mp4: bool):
    """(state, ms). state = dict(show=[ids], dim=[ids], p=progress percent, cap=caption index, end=bool)."""
    k = 1.25 if mp4 else 1.0
    S = []

    def add(ms, **st):
        S.append((st, int(ms * k)))

    if mp4:
        # The video starts from the empty stage (it has a poster for its still). A GIF starts its
        # replay on the question with each agent's first call already in: a reader who scrolls to a
        # GIF late never lands on two empty columns.
        add(3000, show=[], cap=0)
    add(1500, show=["l1", "r1"], cap=0)
    add(2200, show=["l1", "r1", "r1r"], cap=2)
    for i, p in enumerate((0, 20, 40, 60, 80)):
        sh = ["l1", "r1", "r1r", "r2"] + (["l1r"] if i >= 2 else [])
        add(700 if i else 1400, show=sh, dim=["r1", "r1r"], p=p, cap=3 if i < 2 else 1)
    add(2200, show=["l1", "l1r", "r1", "r1r", "r2", "r2r"], dim=["r1", "r1r"], p=100, cap=3)
    add(3200, show=["l1", "l1r", "r1", "r1r", "r2", "r2r", "r3"], dim=["r1", "r1r", "r2", "r2r"], p=100, cap=4)
    base = ["l1", "l1r", "r1", "r1r", "r2", "r2r", "r3", "r4"]
    if mp4 and FRAME.exists():
        add(1600, show=base + ["r4o"], dim=["r1", "r1r", "r2", "r2r"], p=100, cap=5, frame=False)
        add(3200, show=base + ["r4o"], dim=["r1", "r1r", "r2", "r2r"], p=100, cap=5, frame=True)
    else:
        add(2200, show=base + ["r4o"], dim=["r1", "r1r", "r2", "r2r"], p=100, cap=5)
    add(3000, show=base + ["l2"], dim=["l1", "l1r", "r1", "r1r", "r2", "r2r"], p=100, cap=6)
    fin = base + ["l2", "l3"] + (["r5"] if R01.get("omniseek_again") else [])
    add(3000, show=fin, dim=["l1", "l1r", "l2", "r1", "r1r", "r2", "r2r", "r4"], p=100, cap=7)
    add(3000, show=fin, dim=["l1", "l1r", "l2", "r1", "r1r", "r2", "r2r", "r4"], p=100, cap=7, end=True)
    # Once the transcript is in, the page read and the transcription shrink to one line each.
    full, short = ["r1", "r1r", "r2", "r2r"], ["r1c", "r2c"]
    for st, _ in S:
        if "r3" in st["show"]:
            st["show"] = [i for i in st["show"] if i not in full] + short
            st["dim"] = [i for i in st.get("dim", []) if i not in full] + short
    if not mp4:
        # GIF frame 0 is the finished comparison (the last frame before the end card): GitHub, PyPI,
        # thumbnails and reduced-motion readers often show only the first frame. Then it replays
        # from the first calls. The MP4 has a poster for that, so its storyboard is unchanged.
        last = next(st for st, _ in reversed(S) if not st.get("end"))
        S.insert(0, (dict(last, cover=True), COVER_MS))
    return S


APPLY = """([st, caps]) => {
  document.querySelectorAll('.step').forEach(el => {
    el.classList.toggle('h', !st.show.includes(el.id));
    el.classList.toggle('dim', (st.dim || []).includes(el.id));
  });
  const p = document.getElementById('r2p'); if (p) p.style.width = (st.p || 0) + '%';
  const s = document.getElementById('r2s'); if (s) s.style.visibility = (st.p || 0) >= 100 ? 'hidden' : 'visible';
  const f = document.getElementById('r4f'); if (f) f.style.display = st.frame ? 'block' : 'none';
  document.getElementById('cap').textContent = caps[st.cap];
  document.getElementById('end').style.display = st.end ? 'flex' : 'none';
}"""


def browser(p):
    exe = os.environ.get("OMNISEEK_CHROMIUM")
    if not exe:
        found = sorted(glob.glob(os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
        exe = found[-1] if found else None
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


def shoot(lang, theme, w, h, scale, mp4, narrow=False):
    from playwright.sync_api import sync_playwright
    shots = []
    with tempfile.TemporaryDirectory() as td, sync_playwright() as p:
        b = browser(p)
        ctx = b.new_context(viewport={"width": w + 20, "height": h + 20}, device_scale_factor=scale)
        page = ctx.new_page()
        f = Path(td) / "page.html"
        f.write_text(page_html(lang, theme, w, h, mp4, narrow), encoding="utf-8")
        page.goto(f.as_uri())
        page.wait_for_timeout(400)
        for st, ms in plan(mp4):
            page.evaluate(APPLY, [st, T[lang]["cap"]])
            shots.append((page.locator("#stage").screenshot(omit_background=not mp4), ms))
        ctx.close()
        b.close()
    return shots


def to_gif(shots, page_hex: str, path: Path) -> None:
    from PIL import Image
    page = tuple(int(page_hex[i:i + 2], 16) for i in (1, 3, 5))
    rgba = [Image.open(io.BytesIO(png)).convert("RGBA") for png, _ in shots]

    def flat(im):
        bg = Image.new("RGBA", im.size, page + (255,))
        return Image.alpha_composite(bg, im).convert("RGB")

    # One palette for all frames, from a strip of every frame (each step adds colours), with the last
    # index kept free for the transparent corners outside the rounded stage. A frame that repeats
    # later (the cover) is left out: it adds no colours, and the replay keeps its exact palette.
    pngs = [png for png, _ in shots]
    src = [im for i, im in enumerate(rgba) if pngs[i] not in pngs[i + 1:]]
    strip = Image.new("RGB", (rgba[0].width, rgba[0].height * len(src)))
    for i, im in enumerate(src):
        strip.paste(flat(im), (0, i * im.height))
    pal = strip.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    entries = pal.getpalette()[: 255 * 3]
    entries += [0, 0, 0] * (256 - len(entries) // 3)
    pal.putpalette(entries)
    frames = []
    for im in rgba:
        q = flat(im).quantize(palette=pal, dither=Image.Dither.NONE)
        q.paste(255, mask=im.getchannel("A").point(lambda a: 255 if a < 128 else 0))
        frames.append(q)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[ms for _, ms in shots],
                   loop=0, transparency=255, disposal=1, optimize=False)


def to_mp4(shots, path: Path) -> None:
    import imageio_ffmpeg
    from PIL import Image
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{MP4_W}x{MP4_H}",
           "-r", str(MP4_FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for png, ms in shots:
        im = Image.open(io.BytesIO(png)).convert("RGB").resize((MP4_W, MP4_H), Image.LANCZOS)
        raw = im.tobytes()
        for _ in range(max(1, round(ms * MP4_FPS / 1000))):
            proc.stdin.write(raw)
    proc.stdin.close()
    if proc.wait():
        raise SystemExit("ffmpeg failed")


def to_poster(shots, path: Path) -> None:
    from PIL import Image
    last = max(i for i, (st, _) in enumerate(plan(True)) if not st.get("end"))
    im = Image.open(io.BytesIO(shots[last][0])).convert("RGB").resize((MP4_W, MP4_H), Image.LANCZOS)
    im.save(path, optimize=True)
    print(f"wrote {path.name}: frame {last + 1} of {len(shots)}, {path.stat().st_size / 1e6:.2f} MB")


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what not in ("gif", "mp4", "poster", "all"):
        sys.exit(__doc__)
    OUT.mkdir(parents=True, exist_ok=True)
    for lang in LANGS:
        if what in ("gif", "all"):
            for theme in ("light", "dark"):
                for narrow, (w, h), tail in ((False, (W, GIF_H), ""), (True, (NARROW_W, NARROW_H), "-narrow")):
                    shots = shoot(lang, theme, w, h, GIF_SCALE, False, narrow)
                    out = OUT / f"compare-{lang}-{theme}{tail}.gif"
                    to_gif(shots, THEMES[theme]["page"], out)
                    print(f"wrote {out.name}: {len(shots)} frames, {sum(m for _, m in shots) / 1000:.1f}s, "
                          f"{out.stat().st_size / 1e6:.2f} MB")
        if what in ("mp4", "poster", "all") and lang in MP4_LANGS:
            # 1024 x 576 CSS rendered at 1.25 = 1280 x 720 px.
            shots = shoot(lang, "dark", 1024, 576, MP4_W / 1024, True)
            to_poster(shots, OUT / f"compare-{lang}-dark-poster.png")
            if what != "poster":
                out = OUT / f"compare-{lang}-dark.mp4"
                to_mp4(shots, out)
                print(f"wrote {out.name}: {sum(m for _, m in shots) / 1000:.1f}s, {out.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
