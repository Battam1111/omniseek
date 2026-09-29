"""Contract for what a mp.weixin.qq.com/s/ page can actually BE (2026-09-19).

Two failures, both of which presented identically as "omniseek_read returned page furniture":

1. NOT EVERY ARTICLE PAGE IS AN ARTICLE. WeChat short-form posts (微信「短内容」) carry the
   whole post in og:title and have NO #js_content, #activity-name, #js_name or
   #publish_time at all. _parse_article returned None on the missing body container, so
   omniseek_read fell through to the generic web fetcher and produced only the page chrome
   ("知道了 / 微信扫一扫 / 分享 / 收藏"). Measured: a real short post was 2.2 MB of HTML
   whose only post text was a 651-character og:title.

2. A BLOCK LOOKED LIKE A PARSE FAILURE. A rate-limited fetch is 302'd to
   /mp/wappoc_appmsgcaptcha, which answers HTTP 200 with a well-formed page carrying
   ~40 visible characters of furniture. The old guard only matched "环境异常" /
   "verify_msg" in the BODY, neither of which appears there, so the captcha reached
   _parse_article, missed every selector, and returned None. Silent, and indistinguishable
   from a genuinely unparseable page.

The regression these guard against is the tempting over-general fix: concluding from one
short post that WeChat had moved articles to client-side rendering and rewriting the
selectors. Long-form articles were never broken, so the long-form case is asserted here
too, on purpose.
"""
import unittest
from unittest.mock import patch

from omniseek.core.sources.walled.wechat_source import WechatAdapter

SHORT_POST = """<html><head>
<meta name="author" content="尹John" />
<meta property="og:title" content="作者看不懂自己的 AI 论文？\\n机器学习期刊 TMLR 的主编警告说，越来越多的研究者正在提交自己完全不理解的 AI 辅助编写的论文。桌面拒稿率已从约 6% 飙升到约 53%。\\n编辑部邀请了 10 位作者通过 Zoom 回答关于论文的问题。" />
</head><body><div id="page-content"></div></body></html>"""

LONG_ARTICLE = """<html><head>
<meta property="og:title" content="某篇正常长文" />
</head><body>
<h1 id="activity-name">某篇正常长文</h1>
<a id="js_name">专知</a>
<em id="publish_time">2026-09-18</em>
<div id="js_content"><p>这是一段真实的正文内容，长度足以和短帖区分开来。</p>
<p>第二段继续，确认段落结构被保留下来。</p></div>
</body></html>"""

# og:title present but too thin to be a real post: must NOT become a stub document.
THIN_PAGE = """<html><head>
<meta property="og:title" content="微信公众平台" />
</head><body></body></html>"""


class ShortFormPosts(unittest.TestCase):
    def test_short_post_yields_the_post_text_not_None(self):
        doc = WechatAdapter._parse_article("https://mp.weixin.qq.com/s/AAA", SHORT_POST)
        self.assertIsNotNone(doc, "short post must parse; None sends omniseek_read to the web fallback")
        self.assertIn("TMLR", doc.content)
        self.assertGreater(len(doc.content), 100)

    def test_short_post_restores_paragraph_breaks(self):
        # og:title escapes newlines as the two characters backslash-n; a post that arrives
        # as one unbroken run is unreadable downstream.
        doc = WechatAdapter._parse_article("https://mp.weixin.qq.com/s/AAA", SHORT_POST)
        self.assertIn("\n", doc.content)
        self.assertNotIn("\\n", doc.content)

    def test_short_post_headline_is_the_first_line_only(self):
        doc = WechatAdapter._parse_article("https://mp.weixin.qq.com/s/AAA", SHORT_POST)
        self.assertEqual(doc.title, "作者看不懂自己的 AI 论文？")

    def test_short_post_is_labelled_so_downstream_can_tell(self):
        doc = WechatAdapter._parse_article("https://mp.weixin.qq.com/s/AAA", SHORT_POST)
        self.assertEqual(doc.metadata.get("post_kind"), "short")
        self.assertEqual(doc.author, "尹John")

    def test_a_thin_page_still_fails_honestly(self):
        # The short-post path must not turn every unparseable page into a stub document.
        self.assertIsNone(WechatAdapter._parse_article("https://mp.weixin.qq.com/s/BBB", THIN_PAGE))


class LongFormStillWorks(unittest.TestCase):
    """Asserted on purpose: the short-post bug invited a rewrite of selectors that were fine."""

    def test_long_article_parses_from_js_content(self):
        doc = WechatAdapter._parse_article("https://mp.weixin.qq.com/s/CCC", LONG_ARTICLE)
        self.assertIsNotNone(doc)
        self.assertIn("这是一段真实的正文内容", doc.content)

    def test_long_article_is_not_mislabelled_as_short(self):
        doc = WechatAdapter._parse_article("https://mp.weixin.qq.com/s/CCC", LONG_ARTICLE)
        self.assertIsNone(doc.metadata.get("post_kind"))
        self.assertEqual(doc.metadata.get("account_name"), "专知")


class _Resp:
    """Minimal httpx-shaped response: the captcha answers 200 with a clean body."""

    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text

    def raise_for_status(self):
        return None


class CaptchaIsNotAParseFailure(unittest.TestCase):
    def test_captcha_redirect_is_detected_by_landing_url(self):
        captcha = _Resp("https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=X",
                        "<html><body>视频 小程序 赞 在看</body></html>")
        with patch("omniseek.core.sources.walled.wechat_source.http.direct", return_value=captcha), \
             patch("omniseek.core.sources.walled.wechat_source.cache.get", return_value=None):
            self.assertIsNone(WechatAdapter().fetch_url("https://mp.weixin.qq.com/s/DDD"))

    def test_a_normal_landing_url_is_not_treated_as_a_block(self):
        ok = _Resp("https://mp.weixin.qq.com/s/CCC", LONG_ARTICLE)
        with patch("omniseek.core.sources.walled.wechat_source.http.direct", return_value=ok), \
             patch("omniseek.core.sources.walled.wechat_source.cache.get", return_value=None), \
             patch("omniseek.core.sources.walled.wechat_source.cache.set", return_value=None):
            doc = WechatAdapter().fetch_url("https://mp.weixin.qq.com/s/CCC")
        self.assertIsNotNone(doc)


if __name__ == "__main__":
    unittest.main()
