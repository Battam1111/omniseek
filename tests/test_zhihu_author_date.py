"""知乎 adapter reports author and date again (2026-08-30).

Both fields were broken, in different ways, and both were silent:

- SEARCH: the adapter read `.AuthorInfo-name` / `.UserLink-link`. A live DOM dump of 38 cards
  showed those selectors match ZERO nodes on today's search page. The author is actually a bare
  `<b>` inside `.RichContent-inner`, and the date is `.SearchItem-time`, which nothing ever read.
- ARTICLE: the same selector matched an EMPTY shell, so author came back as '' rather than None,
  which reads as "this page has no author" instead of "we failed to read it".

Because `date` was never set on either path, callers were reverse-engineering publication dates
out of 知乎's numeric ids, an estimate with a one-to-two day error.

The structural reads are exact and get short tests. The text fallback is a guess and gets most
of the tests, because that is where a wrong answer would look like a right one.
"""
import unittest
from datetime import datetime

from bs4 import BeautifulSoup

from omniseek.core.sources.walled.zhihu_source import (
    _article_date,
    _card_author,
    _card_date,
    _first_text,
    _split_author_prefix,
)


def card(html: str):
    return BeautifulSoup(html, "lxml")


class CardAuthorTests(unittest.TestCase):
    def test_reads_the_bold_tag_the_live_dump_showed(self):
        """The exact shape dumped from the logged-in browser on 2026-08-30."""
        c = card('<div class="RichContent-inner"><b>Woo Tzins</b>：也可供备春招的同学食用。</div>')
        excerpt = c.select_one(".RichContent-inner").get_text("", strip=True)
        author, rest = _card_author(c, excerpt)
        self.assertEqual(author, "Woo Tzins")
        self.assertEqual(rest, "也可供备春招的同学食用。")

    def test_name_with_spaces_survives(self):
        """'AI Box专栏' is a real author name. An earlier text-only rule banned spaces and lost it."""
        c = card('<div class="RichContent-inner"><b>AI Box专栏</b>：稍微一跑就能跟flash差不多</div>')
        excerpt = c.select_one(".RichContent-inner").get_text("", strip=True)
        author, rest = _card_author(c, excerpt)
        self.assertEqual(author, "AI Box专栏")
        self.assertEqual(rest, "稍微一跑就能跟flash差不多")

    def test_falls_back_to_the_text_prefix_when_no_bold_tag(self):
        c = card('<div class="RichContent-inner">归来仍是少年：RLHF 是借RL的壳做对齐</div>')
        excerpt = c.select_one(".RichContent-inner").get_text("", strip=True)
        author, rest = _card_author(c, excerpt)
        self.assertEqual(author, "归来仍是少年")
        self.assertEqual(rest, "RLHF 是借RL的壳做对齐")

    def test_no_author_anywhere_leaves_the_excerpt_untouched(self):
        c = card('<div class="RichContent-inner">这段话没有任何署名信息</div>')
        excerpt = c.select_one(".RichContent-inner").get_text("", strip=True)
        author, rest = _card_author(c, excerpt)
        self.assertIsNone(author)
        self.assertEqual(rest, excerpt)


class AuthorPrefixFallbackTests(unittest.TestCase):
    """The fallback is a guess, so this is where the false-positive tests live."""

    def test_prose_openers_are_not_authors(self):
        for excerpt in [
            "结论：强化学习在这个设置下没有扩展能力边界",
            "总结：三条主线各自卡在不同地方",
            "背景：这项工作始于去年的一个实验",
            "第一步：先定位当前的能力边界",
            "注意：这个数字是作者自报的",
        ]:
            with self.subTest(excerpt=excerpt[:16]):
                author, rest = _split_author_prefix(excerpt)
                self.assertIsNone(author, "prose promoted to author: %r" % excerpt)
                self.assertEqual(rest, excerpt, "excerpt must be untouched when no author")

    def test_punctuated_run_before_the_colon_is_not_a_name(self):
        excerpt = "我们做了三组实验，结果是：第一组失败了"
        author, rest = _split_author_prefix(excerpt)
        self.assertIsNone(author)
        self.assertEqual(rest, excerpt)

    def test_overlong_run_before_the_colon_is_not_a_name(self):
        excerpt = "这里有个很长的说明性句子它明显超过了二十四个字的上限所以不该被当成人名：正文"
        self.assertIsNone(_split_author_prefix(excerpt)[0])

    def test_halfwidth_colon_also_works(self):
        author, rest = _split_author_prefix("guozhen: scaling 在推理阶段继续用算力换效果")
        self.assertEqual(author, "guozhen")
        self.assertEqual(rest, "scaling 在推理阶段继续用算力换效果")

    def test_empty_and_missing_are_safe(self):
        self.assertEqual(_split_author_prefix(""), (None, ""))
        self.assertEqual(_split_author_prefix(None), (None, None))


class CardDateTests(unittest.TestCase):
    """今天 is injected so these do not rot as the calendar moves."""

    TODAY = datetime(2026, 8, 30)

    def test_reads_the_search_item_time_the_live_dump_showed(self):
        c = card('<span class="ContentItem-action SearchItem-time">07-03</span>')
        self.assertEqual(_card_date(c, today=self.TODAY), "2026-07-03")

    def test_month_day_still_ahead_of_today_belongs_to_last_year(self):
        """知乎 drops the year on recent items, so 12-25 seen in August is LAST December."""
        c = card('<span class="SearchItem-time">12-25</span>')
        self.assertEqual(_card_date(c, today=self.TODAY), "2025-12-25")

    def test_today_itself_is_this_year(self):
        c = card('<span class="SearchItem-time">08-30</span>')
        self.assertEqual(_card_date(c, today=self.TODAY), "2026-08-30")

    def test_full_date_is_exact_and_needs_no_inference(self):
        c = card('<span class="SearchItem-time">2024-11-05</span>')
        self.assertEqual(_card_date(c, today=self.TODAY), "2024-11-05")

    def test_no_time_node_returns_none(self):
        self.assertIsNone(_card_date(card('<div class="ContentItem-actions">赞同 20</div>')))

    def test_unparseable_text_returns_none_rather_than_guessing(self):
        c = card('<span class="SearchItem-time">刚刚</span>')
        self.assertIsNone(_card_date(c, today=self.TODAY))


class FirstTextTests(unittest.TestCase):
    def test_skips_the_empty_shell_that_caused_the_empty_string_bug(self):
        soup = card('<div class="AuthorInfo-name"></div><div class="UserLink-link">某位作者</div>')
        self.assertEqual(_first_text(soup, ".AuthorInfo-name, .UserLink-link"), "某位作者")

    def test_returns_none_rather_than_empty_string(self):
        self.assertIsNone(_first_text(card('<div class="AuthorInfo-name">   </div>'), ".AuthorInfo-name"))

    def test_selector_order_is_honoured(self):
        soup = card('<div class="AuthorInfo-name">正确的</div><div class="UserLink-link">次选的</div>')
        self.assertEqual(_first_text(soup, ".AuthorInfo-name, .UserLink-link"), "正确的")


class ArticleDateTests(unittest.TestCase):
    def test_meta_itemprop_wins(self):
        soup = card('<meta itemprop="datePublished" content="2026-08-12T10:00:00+08:00">')
        self.assertEqual(_article_date(soup), "2026-08-12T10:00:00+08:00")

    def test_falls_back_to_json_ld(self):
        soup = card('<script type="application/ld+json">'
                    '{"@type":"Article","datePublished":"2026-07-01T09:30:00Z"}</script>')
        self.assertEqual(_article_date(soup), "2026-07-01T09:30:00Z")

    def test_falls_back_to_the_visible_timestamp(self):
        soup = card('<div class="ContentItem-time"><span>发布于 2026-06-05 21:14</span></div>')
        self.assertEqual(_article_date(soup), "2026-06-05")

    def test_chinese_date_format(self):
        self.assertEqual(_article_date(card('<div class="Post-Time">2026年6月5日</div>')), "2026-06-05")

    def test_no_date_anywhere_returns_none_and_does_not_raise(self):
        self.assertIsNone(_article_date(card("<div>正文</div>")))

    def test_malformed_json_ld_does_not_raise(self):
        self.assertIsNone(_article_date(card('<script type="application/ld+json">{ not json </script>')))


if __name__ == "__main__":
    unittest.main()
