# -*- coding: utf-8 -*-
"""Generate and render the README logo lockup and the older hero lockups.

logo-lockup.png (what the three READMEs show): mark + wordmark, no text line, one file for
both GitHub themes. GitHub picks a <picture> source by the OS colour scheme, not by the page
theme the reader chose, so a light/dark pair can land on the wrong page. The lockup avoids the
pair: its blues read on #ffffff and on #0d1117 alike, and the tagline lives in the README as
text, in each language.

hero-{en,zh,ja}-{light,dark}.png (mark + wordmark + tagline, transparent, 2x) are the earlier
README heroes, kept for reference; the amber terminal full stop is the point of the sentence.

Set OMNISEEK_CHROMIUM to a chromium binary if Playwright's own build is not installed.
"""
import os
import pathlib

from playwright.sync_api import sync_playwright

MARK = """<svg width="{px}" height="{px}" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
  <circle cx="100" cy="100" r="78" fill="none" stroke="#93C5FD" stroke-width="16" stroke-linecap="round" stroke-dasharray="416 74" transform="rotate(-55 100 100)"/>
  <circle cx="100" cy="100" r="50" fill="none" stroke="#3B82F6" stroke-width="16" stroke-linecap="round" stroke-dasharray="267 47" transform="rotate(75 100 100)"/>
  <circle cx="100" cy="100" r="23" fill="none" stroke="{ring3}" stroke-width="15" stroke-linecap="round" stroke-dasharray="116 28" transform="rotate(195 100 100)"/>
  <circle cx="100" cy="100" r="16" fill="#F59E0B"/>
</svg>"""

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html,body {{ background:transparent; }}
  body {{ font-family:-apple-system,"SF Pro Display","Segoe UI","PingFang SC","Hiragino Sans","Microsoft YaHei","Yu Gothic UI",sans-serif; padding:16px; }}
  #shot {{ display:inline-flex; align-items:center; gap:36px; padding:6px 10px; }}
  .name {{ font-size:64px; font-weight:750; letter-spacing:-.025em; color:{fg}; line-height:1.05; }}
  .tag {{ font-size:{tagsize}px; font-weight:500; color:{muted}; margin-top:9px; letter-spacing:-.005em; }}
  .tag .dot {{ color:#F59E0B; font-weight:800; }}
</style></head><body>
<div id="shot">
  {mark}
  <div>
    <div class="name">OmniSeek</div>
    <div class="tag">{tagline}<span class="dot">{stop}</span></div>
  </div>
</div>
</body></html>"""

MODES = {
    # Brand colours only (docs/BRAND.md): navy text on light pages, white on dark, and the tagline
    # is the same colour at lower opacity, so it blends with whatever page the PNG sits on.
    "light": {"fg": "#0F172A", "muted": "rgba(15, 23, 42, .72)", "ring3": "#1D4ED8"},
    "dark": {"fg": "#FFFFFF", "muted": "rgba(255, 255, 255, .70)", "ring3": "#2563EB"},
}

# Taglines match each README's own wording; the terminal stop is split out so it can be amber.
LANGS = {
    "en": {"tagline": "Give your AI agent the parts of the internet web search doesn&rsquo;t reach", "stop": ".", "tagsize": 21.5},
    "zh": {"tagline": "给你的 AI Agent 接上网页搜索够不到的那部分互联网", "stop": "。", "tagsize": 21},
    "ja": {"tagline": "ウェブ検索が届かないインターネットを、あなたの AI エージェントに", "stop": "。", "tagsize": 20},
}

# One colour set for both page themes: blue-500 wordmark (about 3.7:1 on white, 5.1:1 on
# #0d1117, so large bold text clears 3:1 on both) and a blue-600 inner ring.
LOCKUP = """<!doctype html><html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html,body {{ background:transparent; }}
  body {{ font-family:-apple-system,"SF Pro Display","Segoe UI",sans-serif; padding:16px; }}
  #shot {{ display:inline-flex; align-items:center; gap:30px; padding:6px 10px; }}
  .name {{ font-size:76px; font-weight:750; letter-spacing:-.025em; color:#3B82F6; line-height:1.05; }}
</style></head><body>
<div id="shot">
  {mark}
  <div class="name">OmniSeek</div>
</div>
</body></html>"""

BASE = pathlib.Path(__file__).parent
(BASE / "logo-lockup.html").write_text(LOCKUP.format(mark=MARK.format(px=118, ring3="#2563EB")), encoding="utf-8")

for lang, t in LANGS.items():
    for mode, c in MODES.items():
        html = TEMPLATE.format(
            mark=MARK.format(px=118, ring3=c["ring3"]),
            fg=c["fg"], muted=c["muted"],
            tagline=t["tagline"], stop=t["stop"], tagsize=t["tagsize"],
        )
        (BASE / f"hero-{lang}-{mode}.html").write_text(html, encoding="utf-8")

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("OMNISEEK_CHROMIUM") or None)
    ctx = browser.new_context(viewport={"width": 1100, "height": 300}, device_scale_factor=2)
    page = ctx.new_page()
    for name in ["logo-lockup"] + [f"hero-{lang}-{mode}" for lang in LANGS for mode in MODES]:
        page.goto((BASE / f"{name}.html").as_uri())
        page.wait_for_timeout(250)
        page.locator("#shot").screenshot(path=str(BASE / f"{name}.png"), omit_background=True)
        print(f"{name}.png", (BASE / f"{name}.png").stat().st_size, "bytes")
    ctx.close()
    browser.close()
