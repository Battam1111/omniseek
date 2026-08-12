"""Contract for the wechat2rss liveness probe (2026-08-11).

The failure it exists to prevent: the probe downloaded each feed WHOLE (1 to 2.4 MB) merely to read
the newest <pubDate>, so its own cost scaled with the publisher's backlog. Measured 2026-08-11, one
feed took 16.8s against a 12s timeout, so the probe raised URLError and Barked "unreachable" about a
feed that was serving fine. A liveness check whose own cost can exceed its own timeout manufactures
its own false alarms.
"""
import unittest
from unittest.mock import patch

from penumbra.core import infra_jobs as J


class _Resp:
    """A feed far larger than the prefix, recording how much the probe actually asked for."""

    def __init__(self, body: str, log: list):
        self._body = body.encode("utf-8")
        self._log = log

    def read(self, n=-1):
        self._log.append(n)
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _feed(entries: int) -> str:
    items = "".join(
        f"<item><pubDate>Mon, 11 Aug 2026 0{i % 10}:00:00 +0000</pubDate><description>{'x' * 4000}"
        f"</description></item>" for i in range(entries))
    return f"<rss><channel>{items}</channel></rss>"


class WeChat2RssProbeTests(unittest.TestCase):
    def _run(self, body: str):
        reads: list = []
        with patch.object(J, "_WECHAT2RSS_FEEDS", [("PaperWeekly", "https://example.invalid/f.xml")]):
            with patch("urllib.request.urlopen", lambda *a, **k: _Resp(body, reads)):
                ok, msg = J.check_wechat2rss_feeds()
        return ok, msg, reads

    def test_the_probe_reads_a_bounded_prefix_never_the_whole_feed(self):
        ok, msg, reads = self._run(_feed(400))          # ~1.6 MB of feed
        self.assertEqual(reads, [J._WECHAT2RSS_PREFIX_BYTES],
                         "the probe must ask for a bounded prefix, not read() the whole body")
        self.assertTrue(ok, msg)

    def test_the_prefix_still_carries_the_newest_date(self):
        # RSS puts the newest item first, so a prefix is enough to judge freshness.
        ok, msg, _ = self._run(_feed(400))
        self.assertIn("feeds OK", msg)

    def test_a_timeout_is_generous_enough_to_outlast_a_slow_but_healthy_host(self):
        # 16.8s was measured on a HEALTHY feed; a budget at or under that is a false-alarm generator.
        self.assertGreaterEqual(J._WECHAT2RSS_TIMEOUT_S, 20)

    def test_an_unreachable_feed_still_reports_unreachable(self):
        def _boom(*a, **k):
            raise OSError("no route")
        with patch.object(J, "_WECHAT2RSS_FEEDS", [("PaperWeekly", "https://example.invalid/f.xml")]):
            with patch("urllib.request.urlopen", _boom):
                ok, msg = J.check_wechat2rss_feeds()
        self.assertFalse(ok)
        self.assertIn("unreachable", msg)
        self.assertIn("PaperWeekly", msg)

    def test_a_frozen_service_is_still_flagged(self):
        stale = ("<rss><channel><item><pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>"
                 "</item></channel></rss>")
        ok, msg, _ = self._run(stale)
        self.assertFalse(ok)
        self.assertIn("frozen", msg)


if __name__ == "__main__":
    unittest.main()
