"""Render the 6 localized demo figures at 2x (940 css width -> 1880 px).

Run gen_demo.py first (it writes the HTML next to this file), then this script; copy the
PNGs up into assets/. Requires playwright with chromium installed."""
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
JOBS = [(f"demo-{lang}-{mode}.html", f"demo-{lang}-{mode}.png")
        for lang in ("en", "zh", "ja") for mode in ("light", "dark")]

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1100, "height": 1700}, device_scale_factor=2)
    page = ctx.new_page()
    for html, out in JOBS:
        page.goto((BASE / html).as_uri())
        page.wait_for_timeout(400)
        page.locator("#shot").screenshot(path=str(BASE / out))
        print(out, (BASE / out).stat().st_size, "bytes")
    ctx.close()
    browser.close()
