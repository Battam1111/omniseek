# -*- coding: utf-8 -*-
"""Generate and render the GitHub social preview card (1280x640 logical, rendered 2x).

Composition: the mark at depth on the navy field, lit by its own amber centre, with two
faint echo rings widening outward; the wordmark and tagline answer on the right. After
re-rendering, the PNG still has to be uploaded in the repository's social preview
setting by hand; committing it here only versions the master.
"""
import pathlib

from playwright.sync_api import sync_playwright

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:#0F172A; }
  #shot { position:relative; width:1280px; height:640px; background:#0F172A; overflow:hidden;
          font-family:-apple-system,"SF Pro Display","Segoe UI",sans-serif; }
  .field { position:absolute; inset:0; }
  .text { position:absolute; left:560px; top:50%; transform:translateY(-50%); }
  .name { font-size:98px; font-weight:750; letter-spacing:-.025em; color:#F1F5F9; line-height:1; }
  .tag { font-size:31px; font-weight:500; color:#94A3B8; margin-top:18px; letter-spacing:-.005em; }
  .tag .dot { color:#F59E0B; font-weight:800; }
  .sub { font-size:22px; font-weight:500; color:#64748B; margin-top:14px; }
</style></head><body>
<div id="shot">
  <svg class="field" viewBox="0 0 1280 640" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <radialGradient id="glow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="#F59E0B" stop-opacity=".38"/>
        <stop offset="45%" stop-color="#F59E0B" stop-opacity=".10"/>
        <stop offset="100%" stop-color="#F59E0B" stop-opacity="0"/>
      </radialGradient>
    </defs>
    <g transform="translate(300 320)">
      <circle r="188" fill="url(#glow)"/>
      <g transform="translate(-160 -160) scale(1.6)">
        <circle cx="100" cy="100" r="97" fill="none" stroke="#1E3A5F" stroke-width="5" stroke-linecap="round" stroke-dasharray="540 70" transform="rotate(140 100 100)" opacity=".55"/>
        <circle cx="100" cy="100" r="78" fill="none" stroke="#93C5FD" stroke-width="16" stroke-linecap="round" stroke-dasharray="416 74" transform="rotate(-55 100 100)"/>
        <circle cx="100" cy="100" r="50" fill="none" stroke="#3B82F6" stroke-width="16" stroke-linecap="round" stroke-dasharray="267 47" transform="rotate(75 100 100)"/>
        <circle cx="100" cy="100" r="23" fill="none" stroke="#2563EB" stroke-width="15" stroke-linecap="round" stroke-dasharray="116 28" transform="rotate(195 100 100)"/>
        <circle cx="100" cy="100" r="16" fill="#F59E0B"/>
      </g>
      <circle r="252" fill="none" stroke="#1E3A5F" stroke-width="3" stroke-linecap="round" stroke-dasharray="1220 180" transform="rotate(20)" opacity=".4"/>
    </g>
  </svg>
  <div class="text">
    <div class="name">OmniSeek</div>
    <div class="tag">Your agent seeks what search can&rsquo;t find<span class="dot">.</span></div>
    <div class="sub">Self-hosted perception MCP server for AI agents</div>
  </div>
</div>
</body></html>"""

BASE = pathlib.Path(__file__).parent
(BASE / "social-preview.html").write_text(HTML, encoding="utf-8")

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 720}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto((BASE / "social-preview.html").as_uri())
    page.wait_for_timeout(250)
    page.locator("#shot").screenshot(path=str(BASE / "social-preview.png"))
    print("social-preview.png", (BASE / "social-preview.png").stat().st_size, "bytes")
    ctx.close()
    browser.close()
