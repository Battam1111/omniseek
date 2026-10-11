"""Comparison infographic: three questions, what web search gave, what the agent with OmniSeek gave.

    python assets/masters/gen_compare_infographic.py
    -> assets/demo/compare-table-{en,zh}-{light,dark}.png        (wide, 1200 CSS px at 2x)
       assets/demo/compare-table-{en,zh}-{light,dark}-narrow.png (stacked, 400 CSS px at 2x)

Every quoted line is cut from compare-demo-run.json (the recorded runs) by `cut()`, which stops if the
cut is not in the record verbatim: lines are shortened with an ellipsis, never reworded. English
translations of Chinese lines are typed in GLOSS and set in italics. Comment authors are never shown
(the record does not carry them). Same palette, logo and checks as gen_compare_demo.py.
"""
import html
import tempfile
from pathlib import Path

import gen_compare_demo as D

e = html.escape
RUN, cut, hms = D.RUN, D.cut, D.hms
R01, R04, R08 = RUN["R01"], RUN["R04"], RUN["R08"]
QUOTE, T_QUOTE = D.QUOTE, D.T_QUOTE
WIDE, NARROW, SCALE = 1200, 400, 2

GLOSS = dict(D.GLOSS)
GLOSS.update({
    "顶级评论和弹幕主要分为以下几个类型": "“The top comments and bullet comments fall into these types”",
    "对内容的褒奖和认可": "“praise and recognition of the content”",
    "学习资料分享与交流": "“sharing and swapping study materials”",
    "沐神一己之力拉高国内研究生教育水平[doge]": "“Mu Li single-handedly raised the level of graduate education in China [doge]”",
    "Mu Li Is All You Need[doge]": None,
})

T = {
    "en": {
        "lang": "en", "title": "Same question, two agents",
        "q": "Question", "web": "Agent with web search only", "omni": "Agent with OmniSeek",
        "ask_zh": "Asked in Chinese (translated here)", "ask_en": "Asked in English",
        "q1": D.T["en"]["question"],
        "q2": R04["question_en"],
        "q3": R08["web"][0]["query"],
        "again": "asked again:", "exact": "searching his exact words:",
        "nolikes": "No comment text with like counts in the answer.",
        "likes": "{n:,} likes",
        "shown": "{k} of the {n} comments returned, with their like counts; authors left out here",
        "both": "papers in the returned citation graph cite both",
        "eg": "for example",
        "day": "Run {d}. Citing papers change every day.",
        "foot": "Real runs, 2026-10-11. Quotes are cut from the recorded answers, not reworded; "
                "translations in italics.",
        "opens": "opens at {t}",
    },
    "zh": {
        "lang": "zh", "title": "同一个问题，两个 Agent",
        "q": "问题", "web": "只有网页搜索的 Agent", "omni": "装了 OmniSeek 的 Agent",
        "ask_zh": "用中文问", "ask_en": "用英文问",
        "q1": R01["web"][0]["query"],
        "q2": R04["question_en"],
        "q3": R08["web"][0]["query"],
        "again": "再问一次：", "exact": "搜原话：",
        "nolikes": "回答里没有带点赞数的评论原文。",
        "likes": "{n:,} 赞",
        "shown": "返回的 {n} 条评论里的 {k} 条，带点赞数；这里不列作者",
        "both": "篇同时引用了两篇（在返回的引用关系里）",
        "eg": "例如",
        "day": "{d} 实跑。引用它们的论文每天都在增加。",
        "foot": "均为 2026-10-11 实跑。引文从记录里原样截取，没有改字。",
        "opens": "从 {t} 播放",
    },
}

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html, body { background:transparent; }
#stage { %(theme)s
  width:%(w)spx; background:var(--bg); border:1px solid var(--line); border-radius:14px; overflow:hidden;
  color:var(--fg); -webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","Helvetica Neue",Arial,"PingFang SC","Hiragino Sans GB",sans-serif; }
.mono { font-family:ui-monospace,"SF Mono",Menlo,"PingFang SC",monospace; }
.top { display:flex; align-items:center; gap:12px; padding:16px 22px; border-bottom:1px solid var(--line); background:var(--bar); }
.top svg { width:34px; height:34px; }
.top h1 { font-size:24px; font-weight:800; letter-spacing:-.01em; }
.hdr, .row { display:grid; grid-template-columns: 300px 1fr 1.2fr; }
.hdr > div { padding:10px 18px; font-size:16px; font-weight:750; color:var(--muted); border-bottom:1px solid var(--line); }
.hdr .o { color:var(--fn); display:flex; align-items:center; gap:7px; }
.hdr svg { width:20px; height:20px; }
.row + .row { border-top:1px solid var(--line); }
.cell { padding:14px 18px 16px; min-width:0; }
.cell + .cell { border-left:1px solid var(--line); }
.cq { background:var(--bar); }
.cw { background:var(--panel); }
.co { background:var(--panel); }
.ask { font-size:13.5px; font-weight:700; color:var(--fn); text-transform:none; }
.qt { font-size:17px; font-weight:650; line-height:1.32; margin-top:4px; }
.qr { font-size:16px; line-height:1.35; }
.qr + .qr, .lab + .qr { margin-top:6px; }
.big { font-size:21px; font-weight:700; line-height:1.3; }
.lab { font-size:13.5px; color:var(--muted); font-weight:600; margin-top:9px; }
.gl { font-size:14px; font-style:italic; color:var(--muted); line-height:1.32; margin-top:1px; }
.note { font-size:13.5px; color:var(--muted); line-height:1.35; margin-top:8px; }
.hit { background:var(--hl); border-radius:8px; padding:7px 10px; }
.hit .at { color:var(--amber); font-weight:750; font-size:19px; }
.cm { display:flex; gap:10px; align-items:baseline; margin-top:7px; }
.cm .n { flex:0 0 auto; font-size:15px; font-weight:750; color:var(--amber); min-width:76px; }
.cm .t { font-size:17px; line-height:1.3; }
.num { font-size:30px; font-weight:800; color:var(--amber); line-height:1.1; }
.ids { font-size:15.5px; color:var(--fn); margin-top:4px; line-height:1.45; }
.foot { padding:10px 22px 12px; font-size:13px; color:var(--faint); border-top:1px solid var(--line); background:var(--bar); }
.mini { display:none; font-size:12.5px; font-weight:750; color:var(--muted); margin-bottom:4px; }
.mini.o { color:var(--fn); }
/* narrow: stack the three cells of each row */
.narrow .hdr { display:none; }
.narrow .row { display:block; }
.narrow .cell + .cell { border-left:0; border-top:1px dashed var(--line); }
.narrow .mini { display:block; }
.narrow .top h1 { font-size:21px; }
.narrow .row + .row { border-top:6px solid var(--line); }
"""


def gl(lang, zh):
    if lang != "en":
        return ""
    if zh not in GLOSS:
        raise SystemExit(f"no English translation for {zh!r}")
    return "" if GLOSS[zh] is None else f'<div class="gl">{e(GLOSS[zh])}</div>'


def q(zh_text, part, more=True):
    return e(cut(zh_text, part, more))


def rows(lang):
    t = T[lang]
    w1, w2, w3 = R01["web"]
    r1 = (
        f'<div class="ask">{e(t["ask_zh"])}</div><div class="qt">{e(t["q1"])}</div>',
        f'<div class="qr">「{q(w1["answer"], "这是一篇略短的文章，但是信息量巨大。")}」{q(w1["answer"], "（大意，")}</div>'
        + gl(lang, "这是一篇略短的文章，但是信息量巨大。") + gl(lang, "（大意，")
        + f'<div class="big" style="margin-top:6px">{q(w1["answer"], "大约在开头第1~8分钟", False)}</div>'
        + gl(lang, "大约在开头第1~8分钟")
        + f'<div class="lab">{e(t["again"])}</div><div class="qr"><b>{q(w3["answer"], "约在1分50秒-2分30秒之间", False)}</b></div>'
        + gl(lang, "约在1分50秒-2分30秒之间")
        + f'<div class="lab">{e(t["exact"])}</div><div class="qr mono">{q(w2["answer"], "No relevant web results found.", False)}</div>',
        f'<div class="hit"><span class="at mono">{hms(T_QUOTE)}</span> '
        f'<span class="big">{e(QUOTE["text"])}</span>{gl(lang, QUOTE["text"])}</div>'
        f'<div class="ids mono">bilibili.com/video/BV1pu411o7BE?t={T_QUOTE}</div>'
        f'<div class="note">omniseek_transcribe · {e(t["opens"].format(t=hms(T_QUOTE)))}</div>',
    )
    a = R04["web"][0]["answer"]
    cms = sorted((c for c in R04["omniseek"]["comments"] if c["content"] in GLOSS), key=lambda c: -c["likes"])
    r2 = (
        f'<div class="ask">{e(t["ask_en"])}</div><div class="qt">{e(t["q2"])}</div>',
        f'<div class="qr">「{q(a, "顶级评论和弹幕主要分为以下几个类型")}」</div>' + gl(lang, "顶级评论和弹幕主要分为以下几个类型")
        + f'<div class="qr">1. {q(a, "对内容的褒奖和认可", False)}</div>' + gl(lang, "对内容的褒奖和认可")
        + f'<div class="qr">2. {q(a, "学习资料分享与交流", False)} …</div>' + gl(lang, "学习资料分享与交流")
        + f'<div class="note">{e(t["nolikes"])}</div>',
        "".join(f'<div class="cm"><span class="n">{e(t["likes"].format(n=c["likes"]))}</span>'
                f'<div><div class="t">{e(c["content"])}</div>{gl(lang, c["content"])}</div></div>' for c in cms)
        + f'<div class="note">omniseek_search · {e(t["shown"].format(k=len(cms), n=len(R04["omniseek"]["comments"])))}</div>',
    )
    o8 = R08["omniseek"]
    ids = [c["arxiv"] for c in o8["cite_both"] if c["arxiv"]][:3]
    wa = R08["web"][0]["answer"]
    r3 = (
        f'<div class="ask">{e(t["ask_en"])}</div><div class="qt">{e(t["q3"])}</div>',
        f'<div class="qr">“{e(cut(wa, "I could not find a specific paper that directly cites both"))}”</div>'
        + f'<div class="qr">“{e(cut(wa, "no single publication from the web search is unambiguously confirmed to cite both papers together."))}”</div>',
        f'<div class="qr"><span class="num">{o8["cite_both_count"]}</span> <b>{e(t["both"])}</b></div>'
        f'<div class="lab">{e(t["eg"])}</div><div class="ids mono">' + "<br>".join(f"arXiv:{i}" for i in ids) + "</div>"
        f'<div class="note">omniseek_field_skeleton · {e(t["day"].format(d=o8["run_date"]))}</div>',
    )
    return [r1, r2, r3]


def page(lang, theme, w, narrow):
    t = T[lang]
    body = "".join(
        f'<div class="row"><div class="cell cq"><div class="mini">{e(t["q"])}</div>{c1}</div>'
        f'<div class="cell cw"><div class="mini">{e(t["web"])}</div>{c2}</div>'
        f'<div class="cell co"><div class="mini o">{e(t["omni"])}</div>{c3}</div></div>'
        for c1, c2, c3 in rows(lang))
    small_mark = D.MARK
    return (f'<!doctype html><html lang="{t["lang"]}"><head><meta charset="utf-8"><style>'
            + CSS % {"theme": D.THEMES[theme]["css"], "w": w} + "</style></head><body>"
            f'<div id="stage" class="{"narrow" if narrow else ""}">'
            f'<div class="top">{D.MARK}<h1>{e(t["title"])}</h1></div>'
            f'<div class="hdr"><div>{e(t["q"])}</div><div>{e(t["web"])}</div><div class="o">{small_mark}{e(t["omni"])}</div></div>'
            f'{body}<div class="foot">{e(t["foot"])}</div></div></body></html>')


def main():
    from playwright.sync_api import sync_playwright
    D.OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td, sync_playwright() as p:
        b = D.browser(p)
        for lang in ("en", "zh"):
            for theme in ("light", "dark"):
                for narrow in (False, True):
                    w = NARROW if narrow else WIDE
                    ctx = b.new_context(viewport={"width": w + 20, "height": 800}, device_scale_factor=SCALE)
                    pg = ctx.new_page()
                    f = Path(td) / "t.html"
                    f.write_text(page(lang, theme, w, narrow), encoding="utf-8")
                    pg.goto(f.as_uri())
                    pg.wait_for_timeout(300)
                    out = D.OUT / f"compare-table-{lang}-{theme}{'-narrow' if narrow else ''}.png"
                    pg.locator("#stage").screenshot(path=str(out), omit_background=True)
                    ctx.close()
                    print(f"wrote {out.name}: {out.stat().st_size / 1e3:.0f} kB")
        b.close()


if __name__ == "__main__":
    main()
