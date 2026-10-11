"""Terminal demo for the README: real OmniSeek output, replayed as an asciicast, rendered to GIF.

Two steps, so the GIF never shows anything a real run did not return:

  1. record: start the installed `omniseek` over stdio, make the calls in CALLS, and save the
     fields the demo shows (title, url, date, view count of the first N_SHOWN results, the total
     result count, elapsed seconds) to
     terminal-demo-run.json next to this script. Needs the `mcp` package (an OmniSeek dependency).
         python assets/masters/gen_terminal_demo.py record [path/to/omniseek]
  2. cast: turn that JSON into an asciicast v2 file. Only the typing of the call is animated;
     every result line is copied from the recorded run, cut to the first N_SHOWN results.
         python assets/masters/gen_terminal_demo.py cast assets/masters/terminal-demo.cast
     then render with agg (https://github.com/asciinema/agg):
         agg --font-family "Menlo,Hiragino Sans GB" --font-size 15 --theme <THEME below> \\
             --last-frame-duration 6 assets/masters/terminal-demo.cast assets/terminal-demo.gif

Never edit the GIF by hand; change this script or re-record.
"""
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = HERE / "terminal-demo-run.json"

QUESTION = "What advice do PhD students in China give for the first year? Link the posts."
CALLS = [
    {"tool": "omniseek_search", "args": {"query": "读博 第一年 建议", "sources": ["zhihu_search"]}},
    {"tool": "omniseek_search", "args": {"query": "读博 第一年 建议", "sources": ["bilibili"]}},
]
N_SHOWN = 4
TAGLINE = "Give your AI agent the parts of the internet web search doesn't reach."
INSTALL = ["uv tool install omniseek", "claude mcp add omniseek -- omniseek"]

COLS, ROWS = 92, 26
# Brand palette (docs/BRAND.md): background, foreground, then 16 ANSI colors.
THEME = ("0f172a,e2e8f0,"
         "1e293b,f87171,4ade80,f59e0b,3b82f6,c084fc,93c5fd,cbd5e1,"
         "475569,fca5a5,86efac,fbbf24,60a5fa,d8b4fe,bfdbfe,f8fafc")

DIM, BLUE, AMBER, BOLD, RESET = "\x1b[38;5;8m", "\x1b[34m", "\x1b[33m", "\x1b[1m", "\x1b[0m"
LIGHT = "\x1b[36m"


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


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def clip(s: str, cols: int) -> str:
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > cols - 1:
            return out + "…"
        out += ch
        w += cw
    return out


def cast(out_path: str) -> None:
    run = json.loads(RUN.read_text(encoding="utf-8"))
    events, t = [], 0.0

    def emit(text, dt=0.0):
        nonlocal t
        t += dt
        events.append([round(t, 3), "o", text])

    def line(text="", dt=0.06):
        emit(text + "\r\n", dt)

    def typed(prefix, text, per=0.035):
        emit(prefix, 0.3)
        for ch in text:
            emit(ch, per)
        emit("\r\n", 0.25)

    emit("\x1b[?25l")
    line(f"  {DIM}Real run, {run['recorded']}, no account. Results cut to the first {N_SHOWN}.{RESET}", 0.2)
    line()
    line(f"  {DIM}You ask your agent:{RESET}", 0.4)
    typed(f"  {BOLD}> ", run["question"] + RESET)
    line()
    for call in run["calls"]:
        a = call["call"]["args"]
        src = a["sources"][0]
        sig = f'omniseek_search(query="{a["query"]}", sources=["{src}"])'
        typed(f"  {BLUE}● {RESET}", sig, per=0.02)
        emit(f"    {DIM}searching…{RESET}", 0.1)
        emit(f"\r\x1b[2K    {DIM}{call['count']} results in {call['secs']}s{RESET}\r\n", call["secs"])
        for d in call["documents"][:N_SHOWN]:
            tail = ""
            if d.get("views") is not None:
                tail = f"  {d['views']:,} views"
            if d.get("date"):
                tail += f"  {d['date']}"
            head = f"    {src:<13}"
            room = COLS - width(head) - width(tail) - 1
            line(f"{DIM}{head}{RESET}{clip(d['title'], room)}{DIM}{tail}{RESET}", 0.12)
            if d.get("views") is None:
                line(f"    {' ' * 13}{LIGHT}{d['url'].split('://', 1)[-1]}{RESET}", 0.05)
        line("", 0.2)
    emit("", 2.5)
    emit("\x1b[2J\x1b[H", 0.0)
    for _ in range(ROWS // 2 - 3):
        line("", 0.0)
    pad = " " * ((COLS - width(TAGLINE)) // 2)
    line(f"{pad}{BOLD}{TAGLINE}{RESET}", 0.3)
    line()
    for cmd in INSTALL:
        p = " " * ((COLS - width(cmd) - 2) // 2)
        line(f"{p}{AMBER}${RESET} {cmd}", 0.3)
    header = {"version": 2, "width": COLS, "height": ROWS, "title": "OmniSeek"}
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"wrote {out_path} ({t:.1f}s)")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "record":
        record(sys.argv[2] if len(sys.argv) > 2 else "omniseek")
    elif len(sys.argv) == 3 and sys.argv[1] == "cast":
        cast(sys.argv[2])
    else:
        sys.exit(__doc__)
