"""Terminal demo for the README: real OmniSeek output, rendered to two GIFs (light and dark).

Two steps, so the GIFs never show anything a real run did not return:

  1. record: start the installed `omniseek` over stdio, make the calls in CALLS, and save the
     fields the demo shows (title, url, date, view count of the first N_SHOWN results, the total
     result count, elapsed seconds) to
     terminal-demo-run.json next to this script. Needs the `mcp` package (an OmniSeek dependency).
         python assets/masters/gen_terminal_demo.py record [path/to/omniseek]
  2. render: lay that JSON out as a terminal window in HTML, screenshot each step of the
     animation with Playwright, and write assets/terminal-demo-light.gif and
     assets/terminal-demo-dark.gif with Pillow. Needs `playwright` (with its Chromium) and `pillow`.
         python assets/masters/gen_terminal_demo.py render

What the animation may and may not do: only the typing of the second call and the order in which
lines appear are animated. Every title, url, count, view count, date and elapsed time is copied
from the recorded run, cut to the first N_SHOWN results; a long title or url is shortened with an
ellipsis to fit the line, never reworded. The first frame is the finished answer, so a reader who
sees a single frame sees a real answer; then the last search is replayed and the end frame holds.

Colours are the brand palette in docs/BRAND.md, mixed with white or navy for text and lines.
The README picks the light or dark GIF with <picture> and prefers-color-scheme.

Never edit the GIFs by hand; change this script or re-record.
"""
import html
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = HERE / "terminal-demo-run.json"
OUT = HERE.parent

QUESTION = "What advice do PhD students in China give for the first year? Link the posts."
CALLS = [
    {"tool": "omniseek_search", "args": {"query": "读博 第一年 建议", "sources": ["zhihu_search"]}},
    {"tool": "omniseek_search", "args": {"query": "读博 第一年 建议", "sources": ["bilibili"]}},
]
N_SHOWN = 4
WIDTH = 720  # CSS px, the same display width as the English hero in the README
SCALE = 2    # rendered at 2x for sharp text on high-density screens

# Timing in milliseconds. The search wait is the real elapsed time from the recording.
FIRST_HOLD = 3000
LINE_STEP = 160
BEFORE_TYPING = 500
TYPE_STEP = 34
TYPE_CHARS = 3
LAST_HOLD = 1200

THEMES = {
    "light": {
        "page": "#FFFFFF",
        "css": """
  --bg: color-mix(in srgb, #93C5FD 9%, #FFFFFF);
  --bar: color-mix(in srgb, #93C5FD 22%, #FFFFFF);
  --line: color-mix(in srgb, #3B82F6 24%, #FFFFFF);
  --fg: #0F172A;
  --muted: color-mix(in srgb, #0F172A 62%, #FFFFFF);
  --args: color-mix(in srgb, #0F172A 74%, #FFFFFF);
  --fn: #1D4ED8;
  --url: #1D4ED8;
  --dot: #3B82F6;
  --ring3: #1D4ED8;""",
    },
    "dark": {
        "page": "#0F172A",
        "css": """
  --bg: #0F172A;
  --bar: color-mix(in srgb, #93C5FD 8%, #0F172A);
  --line: color-mix(in srgb, #93C5FD 18%, #0F172A);
  --fg: color-mix(in srgb, #FFFFFF 94%, #0F172A);
  --muted: color-mix(in srgb, #FFFFFF 60%, #0F172A);
  --args: color-mix(in srgb, #FFFFFF 78%, #0F172A);
  --fn: #93C5FD;
  --url: #93C5FD;
  --dot: #3B82F6;
  --ring3: #2563EB;""",
    },
}


def record(exe: str) -> None:
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        out = []
        params = StdioServerParameters(command=exe, args=[], env=dict(os.environ))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for c in CALLS:
                    t = time.time()
                    res = await s.call_tool(c["tool"], c["args"])
                    secs = round(time.time() - t, 1)
                    body = json.loads("".join(getattr(x, "text", "") for x in res.content))
                    docs = [{
                        "source": d.get("source"),
                        "title": d.get("title"),
                        "url": d.get("url"),
                        "date": (d.get("date") or "")[:10] or None,
                        "views": (d.get("metadata") or {}).get("play_count"),
                    } for d in body.get("documents", [])[:N_SHOWN]]
                    out.append({"call": c, "secs": secs, "count": body.get("count", len(docs)), "documents": docs})
        return out

    calls = asyncio.run(go())
    RUN.write_text(json.dumps({"recorded": time.strftime("%Y-%m-%d"), "question": QUESTION, "calls": calls},
                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {RUN}")


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ background:transparent; }}
body {{ padding:8px; }}
#term {{ {css}
  width:{width}px; background:var(--bg); border:1px solid var(--line); border-radius:12px; overflow:hidden;
  font-family:-apple-system,"SF Pro Text","PingFang SC","Hiragino Sans GB","Hiragino Sans",sans-serif;
  color:var(--fg); -webkit-font-smoothing:antialiased; }}
.mono {{ font-family:ui-monospace,"SF Mono",Menlo,monospace; }}
.bar {{ display:flex; align-items:center; gap:9px; padding:8px 16px; background:var(--bar);
  border-bottom:1px solid var(--line); font-size:12px; color:var(--muted); }}
.bar svg {{ width:17px; height:17px; flex:0 0 auto; }}
.bar b {{ color:var(--fg); font-weight:650; font-size:12.5px; }}
.body {{ padding:13px 18px 15px; }}
.you {{ font-size:12px; color:var(--muted); }}
.q {{ font-size:15px; font-weight:650; margin-top:2px; letter-spacing:-.005em; }}
.call {{ margin-top:13px; font-size:12.5px; color:var(--args); white-space:nowrap; }}
.call .dot {{ color:var(--dot); margin-right:6px; }}
.call .fn {{ color:var(--fn); font-weight:600; }}
.caret {{ display:inline-block; width:7px; height:14px; background:var(--fn); vertical-align:-2px; margin-left:1px; }}
.count {{ font-size:12px; color:var(--muted); margin:2px 0 3px 19px; }}
.r {{ display:flex; align-items:baseline; gap:16px; margin-left:19px; padding:2.5px 0; font-size:13.5px; }}
.r .t {{ flex:1 1 auto; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.r .m {{ flex:0 0 auto; max-width:250px; font-size:11.5px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.r .m.url {{ color:var(--url); }}
.h {{ visibility:hidden; }}
</style></head><body>
<div id="term">
  <div class="bar">{mark}<b>OmniSeek</b><span>real run on {recorded}, no account, first {n} results of each search</span></div>
  <div class="body">
    <div class="you">You ask your agent</div>
    <div class="q">{question}</div>
{calls}
  </div>
</div>
</body></html>"""

MARK = """<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
  <circle cx="100" cy="100" r="78" fill="none" stroke="#93C5FD" stroke-width="16" stroke-linecap="round" stroke-dasharray="416 74" transform="rotate(-55 100 100)"/>
  <circle cx="100" cy="100" r="50" fill="none" stroke="#3B82F6" stroke-width="16" stroke-linecap="round" stroke-dasharray="267 47" transform="rotate(75 100 100)"/>
  <circle cx="100" cy="100" r="23" fill="none" stroke="var(--ring3)" stroke-width="15" stroke-linecap="round" stroke-dasharray="116 28" transform="rotate(195 100 100)"/>
  <circle cx="100" cy="100" r="16" fill="#F59E0B"/>
</svg>"""


def signature(call: dict) -> str:
    a = call["call"]["args"]
    return f'(query="{a["query"]}", sources=["{a["sources"][0]}"])'


def call_html(i: int, call: dict) -> str:
    e = html.escape
    rows = []
    for j, d in enumerate(call["documents"][:N_SHOWN]):
        if d.get("views") is not None:
            meta = f'{d["views"]:,} views' + (f' · {d["date"]}' if d.get("date") else "")
            cls = "m mono"
        else:
            meta = d["url"].split("://", 1)[-1]
            cls = "m mono url"
        rows.append(f'    <div class="r" id="r{i}-{j}"><span class="t">{e(d["title"])}</span>'
                    f'<span class="{cls}">{e(meta)}</span></div>')
    return (f'    <div class="call mono" id="c{i}"><span class="dot">●</span><span class="fn">{e(call["call"]["tool"])}</span>'
            f'<span class="sig" data-full="{e(signature(call))}">{e(signature(call))}</span></div>\n'
            f'    <div class="count" id="n{i}">{call["count"]} results in {call["secs"]}s</div>\n' + "\n".join(rows))


def steps(run: dict):
    """The animation as (state, duration in ms). A state is the ids that are visible, the number of
    signature characters typed in the last call (None when it is complete), and whether that call
    is still searching.

    The first frame is the finished answer (both searches with their results), held; then the last
    search is replayed: its line is typed, the real search time passes, its results come in, and the
    loop returns to the finished answer. So no frame shows a mostly empty window."""
    calls = run["calls"]
    every = []
    for i, c in enumerate(calls):
        every += [f"c{i}", f"n{i}"] + [f"r{i}-{j}" for j in range(len(c["documents"][:N_SHOWN]))]
    i = len(calls) - 1
    last = calls[i]
    before = [x for x in every if not (x == f"c{i}" or x == f"n{i}" or x.startswith(f"r{i}-"))]
    out = [((tuple(every), None, False), FIRST_HOLD)]
    out.append(((tuple(before), None, False), BEFORE_TYPING))
    sig = signature(last)
    shown = before + [f"c{i}"]
    for k in range(0, len(sig) + 1, TYPE_CHARS):
        out.append(((tuple(shown), k, False), TYPE_STEP))
    out.append(((tuple(shown), len(sig), False), 240))
    out.append(((tuple(shown + [f"n{i}"]), len(sig), True), int(last["secs"] * 1000)))
    shown.append(f"n{i}")
    for j in range(len(last["documents"][:N_SHOWN])):
        shown.append(f"r{i}-{j}")
        out.append(((tuple(shown), None, False), LINE_STEP))
    out[-1] = (out[-1][0], LAST_HOLD)
    return out


APPLY = """([shown, typed, searching]) => {
  document.querySelectorAll('.call, .count, .r').forEach(el => el.classList.toggle('h', !shown.includes(el.id)));
  const sig = [...document.querySelectorAll('.call .sig')].pop();
  if (typed === null) { sig.textContent = sig.dataset.full; }
  else { sig.innerHTML = ''; sig.textContent = sig.dataset.full.slice(0, typed);
         const c = document.createElement('span'); c.className = 'caret'; sig.appendChild(c); }
  const n = [...document.querySelectorAll('.count')].pop();
  if (!n.dataset.done) n.dataset.done = n.textContent;
  n.textContent = searching ? 'searching…' : n.dataset.done;
}"""


def to_gif(shots, page_hex: str, path: Path) -> None:
    import io
    from PIL import Image

    page = tuple(int(page_hex[i:i + 2], 16) for i in (1, 3, 5))
    rgba = [Image.open(io.BytesIO(png)).convert("RGBA") for png, _ in shots]
    durations = [ms for _, ms in shots]

    def flat(im):
        bg = Image.new("RGBA", im.size, page + (255,))
        return Image.alpha_composite(bg, im).convert("RGB")

    # One palette for every frame, taken from the last frame (it holds every colour), with the
    # last index kept free for the transparent corners outside the rounded window.
    pal = flat(rgba[-1]).quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    entries = pal.getpalette()[: 255 * 3]
    entries += [0, 0, 0] * (256 - len(entries) // 3)
    pal.putpalette(entries)
    frames = []
    for im in rgba:
        q = flat(im).quantize(palette=pal, dither=Image.Dither.NONE)
        outside = im.getchannel("A").point(lambda a: 255 if a < 128 else 0)
        q.paste(255, mask=outside)
        frames.append(q)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   transparency=255, disposal=1, optimize=False)


def render() -> None:
    from playwright.sync_api import sync_playwright

    run = json.loads(RUN.read_text(encoding="utf-8"))
    plan = steps(run)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": WIDTH + 40, "height": 900}, device_scale_factor=SCALE)
        page = ctx.new_page()
        for theme, t in THEMES.items():
            doc = PAGE.format(css=t["css"], width=WIDTH, mark=MARK, recorded=run["recorded"], n=N_SHOWN,
                              question=html.escape(run["question"]),
                              calls="\n".join(call_html(i, c) for i, c in enumerate(run["calls"])))
            (HERE / f"terminal-demo-{theme}.html").write_text(doc, encoding="utf-8")
            page.goto((HERE / f"terminal-demo-{theme}.html").as_uri())
            page.wait_for_timeout(300)
            shots = []
            for (shown, typed, searching), ms in plan:
                page.evaluate(APPLY, [list(shown), typed, searching])
                shots.append((page.locator("#term").screenshot(omit_background=True), ms))
            out = OUT / f"terminal-demo-{theme}.gif"
            to_gif(shots, t["page"], out)
            print(f"wrote {out} ({len(shots)} frames, {sum(ms for _, ms in shots) / 1000:.1f}s, "
                  f"{out.stat().st_size // 1024} KB)")
        ctx.close()
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "record":
        record(sys.argv[2] if len(sys.argv) > 2 else "omniseek")
    elif len(sys.argv) == 2 and sys.argv[1] == "render":
        render()
    else:
        sys.exit(__doc__)
