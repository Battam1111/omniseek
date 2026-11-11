"""Comparison infographic: the three recorded questions, each drawn in its own shape.

    python assets/masters/gen_compare_infographic.py
    -> assets/demo/compare-table-{en,zh}-{light,dark}.png        (wide, 1200 CSS px at 2x)
       assets/demo/compare-table-{en,zh}-{light,dark}-narrow.png (stacked, 440 CSS px at 2x)

compare-infographic.html draws it (the minute on the video's timeline, the comments by their likes, the
papers that cite both as dots between two circles); this script hands it the words. Every quoted line is
cut from compare-demo-run.json by cut(), which stops when the line is not in the record verbatim; English
translations of Chinese lines are typed here. Comment authors are never shown, and the long comment in which
a viewer writes about their own hearing is described, not quoted. Same browser and fonts as gen_seek_demo.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "demo"
PAGE = HERE / "compare-infographic.html"
RUN = json.loads((HERE / "compare-demo-run.json").read_text(encoding="utf-8"))
R01, R04, R08 = RUN["R01"], RUN["R04"], RUN["R08"]
SRC = {k: json.dumps(v, ensure_ascii=False) for k, v in RUN.items()}


def cut(rec: str, part: str) -> str:
    if part not in SRC[rec]:
        raise SystemExit(f"not in {rec} verbatim: {part!r}")
    return part


HIT = next(h for h in R01["transcript_hits_写作"] if cut("R01", "这篇文章的写作是非常简洁的") in h["text"])
WEB1, WEB2 = cut("R01", "大约在开头第1~8分钟"), cut("R01", "约在1分50秒-2分30秒之间")
TYPES = [cut("R04", "对内容的褒奖和认可"), cut("R04", "学习资料分享与交流")]
NOTHING = cut("R08", "I could not find a specific paper that directly cites both")
DESCRIBED = {cut("R04", "你好，我是聋人"): {"en": "(a long comment asking for subtitles)",
                                         "zh": "（一条请求加字幕的长评论）"}}
GLOSS = {cut("R04", "沐神一己之力拉高国内研究生教育水平[doge]"):
         "“Mu Li single-handedly raised the level of graduate education in China [doge]”"}

T = {
    "en": {
        "title": "Three questions, two agents",
        "sub": "Real runs on 2026-10-11: one agent with web search only, one with OmniSeek.",
        "web": "Web search", "omni": "OmniSeek",
        "h1": "The minute he said it",
        "q1": "Asked in Chinese: in Mu Li's 87-minute Bilibili walkthrough of the Transformer paper, what does he "
              "say about the paper's writing, and at what minute?",
        "web1": "“Around minutes 1 to 8”",
        "web2": "Asked again: “between about 1:50 and 2:30”",
        "gloss1": "“The writing of this paper is very concise.”",
        "cap1": "All 87 minutes, transcribed on your machine",
        "h2": "The top comments",
        "q2": "Asked in English: " + R04["question_en"],
        "typesLead": "Sorted them into types, with no comment text and no likes:",
        "types": ["“praise and recognition of the content”", "“sharing and swapping study materials”"],
        "commentsLead": "The comments it returned, with their likes:",
        "h3": "Papers that cite both",
        "q3": "Asked in English: " + R08["question"],
        "citeBoth": "papers cite both",
        "citeWhere": "in the citation graph it returned",
        "seeds": [["Leviathan", "et al. 2022"], ["Chen", "et al. 2023"]],
        "foot": "Quotes are cut from the recorded answers, not reworded; comment authors are left out. "
                "Citing papers change every day.",
    },
    "zh": {
        "title": "三个问题，两个 Agent",
        "sub": "2026-10-11 实跑：一个只有网页搜索的 Agent，一个装了 OmniSeek 的 Agent。",
        "web": "网页搜索", "omni": "OmniSeek",
        "h1": "他在第几分钟说的",
        "q1": "用中文问：" + R01["question"],
        "web1": f"「{WEB1}」",
        "web2": f"再问一次：「{WEB2}」",
        "gloss1": f"「{cut('R01', '这篇文章的写作是非常简洁的')}」",
        "cap1": "整段 87 分钟，在你的电脑上转写",
        "h2": "最热的评论",
        "q2": "用英文问：" + R04["question_en"],
        "typesLead": "只分了类型，没有评论原文，也没有点赞数：",
        "types": [f"「{x}」" for x in TYPES],
        "commentsLead": "它取回的评论原文，带点赞数：",
        "h3": "同时引用两篇的论文",
        "q3": "用英文问：" + R08["question"],
        "citeBoth": "篇同时引用了这两篇",
        "citeWhere": "在它取回的引用关系里",
        "seeds": [["Leviathan", "et al. 2022"], ["Chen", "et al. 2023"]],
        "foot": "引文从记录里原样截取，没有改字；不列评论作者。引用这两篇的论文每天都在增加。",
    },
}


def data(lang: str) -> dict:
    t = T[lang]
    comments = []
    for c in sorted(R04["omniseek"]["comments"], key=lambda c: -c["likes"]):
        desc = next((d[lang] for k, d in DESCRIBED.items() if c["content"].startswith(k)), None)
        gloss = GLOSS.get(c["content"]) if lang == "en" else None
        comments.append({"likes": c["likes"], "text": None if desc else c["content"], "desc": desc, "gloss": gloss})
    doc = json.loads((HERE / "compare-demo-transcript.json").read_text(encoding="utf-8"))
    return {
        "lang": lang, "t": t, "types": t["types"], "comments": comments,
        "dur": R01["video"]["duration_seconds"], "find": HIT["start"], "web": [60, 480],
        "segs": [[s["start"], s["end"], len(s["text"])] for s in doc["segments"]],
        "count": R08["omniseek"]["cite_both_count"], "seeds": t["seeds"],
        "webQuote3": f"“{NOTHING}…”",
    }


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    channel = os.environ.get("OMNISEEK_BROWSER_CHANNEL", "msedge")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=channel)
        for lang in ("en", "zh"):
            for theme in ("light", "dark"):
                for layout in ("wide", "narrow"):
                    page = await browser.new_page(viewport={"width": 1240, "height": 1200}, device_scale_factor=2)
                    await page.goto(PAGE.as_uri() + f"?theme={theme}&layout={layout}")
                    await page.evaluate("() => window.ready")
                    await page.evaluate("d => window.setData(d)", data(lang))
                    out = OUT / f"compare-table-{lang}-{theme}{'-narrow' if layout == 'narrow' else ''}.png"
                    await page.locator("#stage").screenshot(path=str(out))
                    print(f"{out.name}: {out.stat().st_size / 1e3:.0f} kB")
                    await page.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
