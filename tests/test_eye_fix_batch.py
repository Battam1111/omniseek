import asyncio
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MUCHONG_WALL = ROOT / "tests" / "fixtures" / "muchong_login_wall.html"
sys.path.insert(0, str(ROOT / "src"))


def test_reddit_non_latin_query_returns_fast_empty_with_reason():
    from omniseek.core import diag
    from omniseek.core.sources.api import reddit_source

    adapter = reddit_source.RedditAdapter()
    with patch.object(reddit_source, "_discover_subreddits", return_value=[]), \
         patch.object(reddit_source, "_arctic_get", return_value=[]) as arctic, \
         patch.object(reddit_source, "_cdp_search", return_value=[]), \
         patch.object(reddit_source.cache, "get_docs", return_value=None), \
         patch.object(reddit_source.cache, "set_docs"):
        diag.enable()
        docs = adapter.search("中文问题")
        notes = diag.drain()

    assert docs == []
    assert arctic.call_count == 0
    assert any(row["helper"] == "reddit.non_latin_query" for row in notes)
    assert any("almost no Chinese text" in (row.get("body") or "") for row in notes)


def test_reddit_async_non_latin_query_returns_without_arctic():
    from omniseek.core.sources.api import reddit_source

    with patch.object(reddit_source, "_discover_subreddits", return_value=[]), \
         patch.object(reddit_source, "_arctic_get", return_value=[]) as arctic, \
         patch.object(reddit_source, "_cdp_search", return_value=[]), \
         patch.object(reddit_source.cache, "get_docs", return_value=None), \
         patch.object(reddit_source.cache, "set_docs"):
        docs = asyncio.run(reddit_source.RedditAdapter().asearch("中文问题"))

    assert docs == []
    assert arctic.call_count == 0


def test_reddit_off_core_route_uses_discovery_without_appending_the_core():
    from omniseek.core.sources.api import reddit_source

    calls = []
    item = {
        "id": "helium1",
        "title": "Helium drive burn-in",
        "subreddit": "HeliumNetwork",
        "permalink": "/r/HeliumNetwork/comments/helium1/x",
        "created_utc": 1,
    }

    def arctic(path, params, **kwargs):
        if path == "/posts/search":
            calls.append(dict(params))
            return [item]
        return []

    with patch.object(reddit_source.cache, "get_docs", return_value=None), \
         patch.object(reddit_source.cache, "set_docs"), \
         patch.object(reddit_source, "_discover_subreddits",
                      return_value=["HeliumNetwork", "DataHoarder"]), \
         patch.object(reddit_source, "_arctic_get", side_effect=arctic):
        docs = reddit_source.RedditAdapter().search(
            "enterprise helium drives", limit=2)

    searched = [p["subreddit"] for p in calls]
    assert searched
    assert searched[0] == "HeliumNetwork"
    assert not set(searched) & set(reddit_source.DEFAULT_SUBREDDITS)
    cap = getattr(reddit_source, "_search_request_cap", None)
    assert callable(cap)
    assert len(searched) <= cap("enterprise helium drives")
    assert docs[0].title == "Helium drive burn-in"


def test_reddit_storage_intent_uses_measured_storage_subs_before_prefix_noise():
    from omniseek.core.sources.api import reddit_source

    calls = []

    def arctic(path, params, **kwargs):
        if path != "/posts/search":
            return []
        calls.append(dict(params))
        if params["subreddit"] == "DataHoarder":
            return [{
                "id": "storage1",
                "title": "Used enterprise helium drives and SMART burn-in",
                "subreddit": "DataHoarder",
                "permalink": "/r/DataHoarder/comments/storage1/x",
                "created_utc": 2,
            }]
        if params["subreddit"] == "HeliumNetwork":
            return [{
                "id": "network1",
                "title": "Helium mobile hotspot",
                "subreddit": "HeliumNetwork",
                "permalink": "/r/HeliumNetwork/comments/network1/x",
                "created_utc": 3,
            }]
        return []

    query = "used enterprise helium drives SMART badblocks burn-in"
    with patch.object(reddit_source.cache, "get_docs", return_value=None), \
         patch.object(reddit_source.cache, "set_docs"), \
         patch.object(reddit_source, "_discover_subreddits",
                      return_value=["HeliumNetwork"]), \
         patch.object(reddit_source, "_arctic_get", side_effect=arctic):
        docs = reddit_source.RedditAdapter().search(query, limit=5)

    assert calls[0]["subreddit"] == "DataHoarder"
    assert all(call["subreddit"] != "HeliumNetwork" for call in calls)
    assert docs and docs[0].metadata["subreddit"] == "DataHoarder"


def test_reddit_core_route_keeps_research_core_first_and_is_bounded():
    from omniseek.core.sources.api import reddit_source

    calls = []

    def arctic(path, params, **kwargs):
        calls.append(dict(params))
        if params["subreddit"] == "PhD":
            return [{
                "id": "phd1",
                "title": "Advisor conflict",
                "subreddit": "PhD",
                "permalink": "/r/PhD/comments/phd1/x",
                "created_utc": 1,
            }]
        return []

    with patch.object(reddit_source.cache, "get_docs", return_value=None), \
         patch.object(reddit_source.cache, "set_docs"), \
         patch.object(reddit_source, "_discover_subreddits",
                      return_value=["UnrelatedTopic"]), \
         patch.object(reddit_source, "_arctic_get", side_effect=arctic):
        docs = reddit_source.RedditAdapter().search("phd advisor conflict", limit=2)

    assert docs and docs[0].title == "Advisor conflict"
    assert calls[0]["subreddit"] == "PhD"
    cap = getattr(reddit_source, "_search_request_cap", None)
    assert callable(cap)
    assert len(calls) <= cap("phd advisor conflict")


def test_reddit_core_route_does_not_spend_discovery_budget():
    from omniseek.core.sources.api import reddit_source

    def arctic(path, params, **kwargs):
        if params["subreddit"] == "PhD":
            return [{
                "id": "phd2",
                "title": "Advisor conflict",
                "subreddit": "PhD",
                "permalink": "/r/PhD/comments/phd2/x",
                "created_utc": 1,
            }]
        return []

    with patch.object(reddit_source, "_discover_subreddits",
                      side_effect=AssertionError("core intent needs no discovery")), \
         patch.object(reddit_source.cache, "get_docs", return_value=None), \
         patch.object(reddit_source.cache, "set_docs"), \
         patch.object(reddit_source, "_arctic_get", side_effect=arctic):
        docs = reddit_source.RedditAdapter().search("phd advisor conflict", limit=2)

    assert docs and docs[0].title == "Advisor conflict"


def test_reddit_discovery_tries_each_content_term_when_prefix_is_all_that_exists():
    from omniseek.core.sources.api import reddit_source

    prefixes = []

    def arctic(path, params, **kwargs):
        if path == "/subreddits/search":
            prefixes.append(params.get("subreddit_prefix"))
        return []

    with patch.object(reddit_source.cache, "get", return_value=None), \
         patch.object(reddit_source.cache, "set"), \
         patch.object(reddit_source, "_arctic_get", side_effect=arctic):
        reddit_source._discover_subreddits("used helium drives")

    assert {"used", "helium", "drives"} <= set(prefixes)


def test_fetch_url_orders_fulltext_before_search_index():
    from omniseek.core import fetcher
    from omniseek.core.normalize import Document

    calls = []

    class Snapshot:
        name = "snapshot"
        fetch_url_class = "search-index"
        fetch_url_hosts = ("example.com",)

        def fetch_url(self, url):
            calls.append(self.name)
            return Document(
                source=self.name, source_id=url, url=url,
                title="snapshot", content="snippet",
                metadata={"body_needs_read": True},
            )

    class Fulltext:
        name = "fulltext"
        fetch_url_class = "fulltext"
        fetch_url_hosts = ("example.com",)

        def fetch_url(self, url):
            calls.append(self.name)
            return Document(
                source=self.name, source_id=url, url=url,
                title="full", content="body",
            )

    with patch.object(fetcher, "_adapters",
                      {"snapshot": Snapshot(), "fulltext": Fulltext()}):
        doc, reason = fetcher._fetch_url_via_adapters_with_reason(
            "https://example.com/thread")

    assert doc.source == "fulltext"
    assert reason is None
    assert calls == ["fulltext"]


def test_search_index_fallback_is_labeled_when_fulltext_misses():
    from omniseek.core import fetcher
    from omniseek.core.normalize import Document

    class Fulltext:
        name = "fulltext"
        fetch_url_class = "fulltext"
        fetch_url_hosts = ("example.com",)

        @staticmethod
        def fetch_url(url):
            return None

    class Snapshot:
        name = "snapshot"
        fetch_url_class = "search-index"
        fetch_url_hosts = ("example.com",)

        @staticmethod
        def fetch_url(url):
            return Document(
                source="snapshot", source_id=url, url=url,
                title="indexed", content="snippet",
                metadata={
                    "read_depth": "search-index-snippet",
                    "body_needs_read": False,
                },
            )

    with patch.object(fetcher, "_adapters",
                      {"snapshot": Snapshot(), "fulltext": Fulltext()}), \
         patch("omniseek.core.diag.note") as note:
        doc, reason = fetcher._fetch_url_via_adapters_with_reason(
            "https://example.com/thread")

    assert doc.metadata["body_needs_read"] is True
    assert doc.metadata["fulltext_fallback"] is True
    assert reason is None
    assert any(
        "full text needs the walled route" in str(call)
        for call in note.call_args_list
    )


def test_search_index_snapshot_docs_always_need_a_deep_read():
    from omniseek.core import cache
    from omniseek.core.sources.api import search_index_source

    venue = search_index_source._SearchVenue(
        name="test_snapshot",
        description="test",
        site="example.com",
    )
    with patch.object(cache, "get_docs", return_value=None), \
         patch.object(cache, "set_docs"), \
         patch.object(search_index_source, "search_web", return_value=[{
             "url": "https://example.com/thread",
             "title": "indexed",
             "snippet": "snippet",
         }]):
        docs = venue.search("query", limit=1)

    assert docs[0].metadata["body_needs_read"] is True


def test_muchong_wall_is_not_returned_as_content_or_sent_to_generic_web():
    from omniseek.core import fetcher
    from omniseek.core.sources.scrape import xiaomuchong_source
    from omniseek.core import web_fallback

    html = MUCHONG_WALL.read_text(encoding="utf-8")
    adapter = xiaomuchong_source.XiaomuchongAdapter()
    url = "https://muchong.com/t-16776812-1"
    with patch.object(fetcher, "_adapters", {"xiaomuchong": adapter}), \
         patch.object(xiaomuchong_source, "cdp_call",
                      return_value=(html, [])), \
         patch.object(web_fallback, "read_via_fallback",
                      side_effect=AssertionError("generic web fallback was reached")):
        doc, reason = fetcher.fetch_url_with_reason(url)

    assert doc is None
    assert "login wall" in (reason or "")


def test_muchong_real_rendered_content_passes_through_unchanged():
    from omniseek.core.sources.scrape import xiaomuchong_source

    html = """
    <html><body>
      <div id="thread_subject">A real Muchong thread</div>
      <td class="t_f">The logged-in body is available.</td>
    </body></html>
    """
    with patch.object(xiaomuchong_source, "cdp_call", return_value=(html, [])):
        doc = xiaomuchong_source.XiaomuchongAdapter().fetch_url(
            "https://muchong.com/t-12345-1")

    assert doc is not None
    assert doc.title == "A real Muchong thread"
    assert "logged-in body" in doc.content


def test_muchong_thread_is_a_declared_known_walled_deep_link():
    from omniseek.core import web_fallback

    assert web_fallback._is_known_walled_deep_link(
        "https://muchong.com/t-16776812-1")
