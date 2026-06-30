"""Walled-garden source adapters — platforms that need special handling.

This subpackage covers sources that don't fit cleanly into "API" or
"public scrape":

- youtube/: search + transcript extraction (no login but specialized)
- zhihu/: needs authenticated browser session (CDP)
- yipinsanfendi/: same (一亩三分地)
- xiaohongshu/: same + aggressive anti-bot
- wechat/: requires self-hosted wewe-rss
"""
