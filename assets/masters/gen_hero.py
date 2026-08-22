# -*- coding: utf-8 -*-
"""Generate and render the README hero lockup (mark + wordmark + tagline).

Six variants: en, zh, ja, each light and dark, on a transparent background at 2x; each
README swaps its pair with prefers-color-scheme. The amber terminal full stop is the
point of the sentence: the find. CJK variants use full-width punctuation.
"""
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
    "light": {"fg": "#0F172A", "muted": "#475569", "ring3": "#1D4ED8"},
    "dark": {"fg": "#F1F5F9", "muted": "#94A3B8", "ring3": "#2563EB"},
}

# Taglines match each README's own wording; the terminal stop is split out so it can be amber.
LANGS = {
    "en": {"tagline": "Your agent seeks what search can&rsquo;t find", "stop": ".", "tagsize": 21.5},
    "zh": {"tagline": "让你的 Agent 找到难以触及之物", "stop": "。", "tagsize": 21},
    "ja": {"tagline": "検索では届かないものを、あなたのエージェントが探し当てる", "stop": "。", "tagsize": 18.5},
}

BASE = pathlib.Path(__file__).parent

for lang, t in LANGS.items():
    for mode, c in MODES.items():
        html = TEMPLATE.format(
            mark=MARK.format(px=118, ring3=c["ring3"]),
            fg=c["fg"], muted=c["muted"],
            tagline=t["tagline"], stop=t["stop"], tagsize=t["tagsize"],
        )
        (BASE / f"hero-{lang}-{mode}.html").write_text(html, encoding="utf-8")

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1100, "height": 300}, device_scale_factor=2)
    page = ctx.new_page()
    for lang in LANGS:
        for mode in MODES:
            name = f"hero-{lang}-{mode}"
            page.goto((BASE / f"{name}.html").as_uri())
            page.wait_for_timeout(250)
            page.locator("#shot").screenshot(path=str(BASE / f"{name}.png"), omit_background=True)
            print(f"{name}.png", (BASE / f"{name}.png").stat().st_size, "bytes")
    ctx.close()
    browser.close()
