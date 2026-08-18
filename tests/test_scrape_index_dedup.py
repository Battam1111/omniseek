"""Contract for the scraped-index source family (2026-08-16).

The failures it pins: (1) a thread pinned or cross-posted in several configured sections of one
site came back once PER SECTION from a named drill (measured on gter: the same /details/ thread
three times in one raw bucket); (2) a cold first drill whose shared-Chrome render came back empty
reported zero items with no second attempt, while the same query against a warm cache returned
real hits, so the miss looked like a query problem when it was a render flake.
"""
import unittest
from unittest.mock import patch
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from omniseek.core.sources.scrape import news_scraper_source as N


HTML = (
    "<html><body>"
    '<a href="/details/S54PT1njmPnb">工作党第一次签证通过总结帖</a>'
    "</body></html>"
)


def _site(name="testsite"):
    return N._ScrapeSite(
        name=name,
        description="test",
        sites=[
            {"url": "https://f.example.net/section/AAA", "render": True, "path_contains": "/details/"},
            {"url": "https://f.example.net/section/BBB", "render": True, "path_contains": "/details/"},
            {"url": "https://f.example.net/section/CCC", "render": True, "path_contains": "/details/"},
        ],
        cache_ttl=60,
    )


class ScrapeIndexDedupTests(unittest.TestCase):
    def _run(self, render_results):
        calls = {"n": 0}

        def fake_render(url):
            calls["n"] += 1
            return render_results.pop(0) if render_results else None

        with patch.object(N, "_render", fake_render), \
             patch.object(N.cache, "get", lambda k: None), \
             patch.object(N.cache, "set", lambda *a, **kw: None):
            return _site()._items(), calls["n"]

    def test_the_same_thread_across_three_sections_is_one_item(self):
        items, _ = self._run([HTML, HTML, HTML])
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]["url"], "https://f.example.net/details/S54PT1njmPnb")

    def test_an_all_empty_cold_render_gets_exactly_one_retry(self):
        # first pass: all three sections come back empty; retry pass: one section delivers
        items, n = self._run([None, None, None, HTML, None, None])
        self.assertEqual(len(items), 1, "the retry's items must be returned")
        self.assertEqual(n, 6, "one full retry pass, not an unbounded loop")

    def test_a_pre_fix_cached_list_is_healed_on_read(self):
        row = {"title": "工作党第一次签证通过总结帖", "url": "https://f.example.net/details/S54PT1njmPnb"}
        with patch.object(N.cache, "get", lambda k: [row, dict(row), dict(row)]):
            items = _site()._items()
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
