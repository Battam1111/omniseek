"""Render the README "See the difference" figure from seek-demo.html, frame by frame.

    python gen_seek_demo.py gif                 # assets/demo/seek-{en,zh,ja}-{light,dark}[-narrow].gif
    python gen_seek_demo.py frames 0 3.6 6 8    # PNG stills at those seconds for review (SEEK_LANGS,
                                                # SEEK_LAYOUTS pick which; default en, wide and narrow)

The page draws the run in compare-demo-run.json (R01) on the video's own timeline: one bar per slot of
compare-demo-transcript.json, web search's two answers as brackets, OmniSeek's find as the amber point.
Everything on screen is a function of time (renderAt(t)); this script only samples it. It drives an
installed Chromium-family browser through Playwright (channel "msedge" by default, OMNISEEK_BROWSER_CHANNEL
to change it) and needs network for the page's web fonts. Re-render instead of editing frames.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "demo"
PAGE = HERE / "seek-demo.html"
FPS = 25
LANGS = ("en", "zh", "ja")
THEMES = ("light", "dark")
LAYOUTS = ("wide", "narrow")


def segments() -> list:
    doc = json.loads((HERE / "compare-demo-transcript.json").read_text(encoding="utf-8"))
    return [[s["start"], s["end"], len(s["text"])] for s in doc["segments"]]


async def open_page(browser, lang: str, theme: str, layout: str):
    page = await browser.new_page(viewport={"width": 1200, "height": 600}, device_scale_factor=1)
    await page.goto(PAGE.as_uri() + f"?lang={lang}&theme={theme}&layout={layout}")
    await page.evaluate("() => window.ready")
    await page.evaluate("segs => window.setData(segs)", segments())
    size = await page.evaluate("() => window.SIZE")
    return page, size["W"], size["H"]


async def shot(page, t: float, w: int, h: int) -> Image.Image:
    await page.evaluate(f"() => window.renderAt({t})")
    png = await page.screenshot(clip={"x": 0, "y": 0, "width": w, "height": h})
    return Image.open(io.BytesIO(png)).convert("RGB")


def to_gif(frames: list, path: Path) -> int:
    # One palette for the whole loop (built from a spread of frames), no dithering: flat brand colours
    # stay flat and nothing shimmers from frame to frame. Pillow stores each frame as the box that changed.
    w, h = frames[0].size
    picks = frames[:: max(1, len(frames) // 12)]
    sheet = Image.new("RGB", (w, h * len(picks)))
    for i, f in enumerate(picks):
        sheet.paste(f, (0, h * i))
    pal = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    path.parent.mkdir(parents=True, exist_ok=True)
    q[0].save(path, save_all=True, append_images=q[1:], duration=int(1000 / FPS), loop=0, optimize=False,
              disposal=1)
    return path.stat().st_size


def name(lang: str, theme: str, layout: str) -> str:
    return f"seek-{lang}-{theme}" + ("-narrow" if layout == "narrow" else "")


async def main(argv: list) -> None:
    mode = argv[0] if argv else "gif"
    channel = os.environ.get("OMNISEEK_BROWSER_CHANNEL", "msedge")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=channel)
        if mode == "frames":
            times = [float(x) for x in argv[1:]] or [0.0]
            langs = os.environ.get("SEEK_LANGS", "en").split(",")
            layouts = os.environ.get("SEEK_LAYOUTS", ",".join(LAYOUTS)).split(",")
            for lang in langs:
                for layout in layouts:
                    for theme in THEMES:
                        page, w, h = await open_page(browser, lang, theme, layout)
                        for t in times:
                            dst = HERE / "seek-frames" / f"{name(lang, theme, layout)}-{t:05.2f}.png"
                            dst.parent.mkdir(exist_ok=True)
                            (await shot(page, t, w, h)).save(dst)
                            print(dst.name)
                        await page.close()
        else:
            for lang in LANGS:
                for layout in LAYOUTS:
                    for theme in THEMES:
                        page, w, h = await open_page(browser, lang, theme, layout)
                        n = round(float(await page.evaluate("() => T.end")) * FPS)
                        frames = [await shot(page, i / FPS, w, h) for i in range(n)]
                        size = to_gif(frames, OUT / f"{name(lang, theme, layout)}.gif")
                        print(f"{name(lang, theme, layout)}.gif  {w}x{h}  {n} frames  {size / 1e6:.2f} MB")
                        await page.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
