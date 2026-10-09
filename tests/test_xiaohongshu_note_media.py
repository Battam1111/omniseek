"""omniseek_read of an international 小红书 note: every slide in media, comment pictures kept apart.

What happened (2026-10-08): a 17-slide note came back with 4 images in media. The adapter scanned the
DOM <img> tags after one or two screens of scrolling, so it saw only the slides the carousel had
already drawn, capped the list at 12, and also swept in pictures posted in the comments. The note page
ships the whole note in window.__INITIAL_STATE__.note.noteDetailMap[<id>].note, whose imageList holds
every slide in order; it is already in the page, so reading it costs no request and no click.

The contract pinned here:
  - an image note's media is the state's imageList, in order, full size, https (17 of 17);
  - the "N 张图" hint in the content counts exactly the media list;
  - comment pictures never enter media: they ride on each comment (``images``) and, once each, in
    metadata.comment_media; a picture-only comment reads "[图片]";
  - a page without the state (or whose state names another note) falls back to the DOM scan;
  - a video note keeps its old media shape ([video_url] + the body images the DOM showed).

FIXTURES (tests/fixtures): xhs_intl_note_multiimage.html is a trimmed copy of a real note page's
state script with the user, the token and every URL hash replaced by fakes, keeping the field shapes
(camelCase imageList entries, a bare ``undefined`` in the literal, a second note in noteDetailMap).
xhs_intl_comment_page_pictures.json is one /api/sns/web/v2/comment/page answer in the real snake_case
shape with made-up text and names. No browser and no network: cdp_call is a stub.
"""
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from omniseek.core import fetcher
from omniseek.core.sources.walled import _human
from omniseek.core.sources.walled import xiaohongshu_cn_source as cn
from omniseek.core.sources.walled import xiaohongshu_source as xhs

FIX = Path(__file__).resolve().parent / "fixtures"
NOTE_ID = "6a2eade6000000001700b7b8"
OTHER_ID = "0123456789abcdef01234567"
NOTE_URL = f"https://www.rednote.com/explore/{NOTE_ID}?xsec_token=ABfake&xsec_source=pc_search"
HTML = (FIX / "xhs_intl_note_multiimage.html").read_text(encoding="utf-8")
CMT_PAGE = json.loads((FIX / "xhs_intl_comment_page_pictures.json").read_text(encoding="utf-8"))

_ISO_TMP = None
_ISO_REAL = None


def setUpModule():
    # importing the cn module obliges every test file to keep its incident black box off the real path
    global _ISO_TMP, _ISO_REAL
    _ISO_TMP = tempfile.TemporaryDirectory()
    _ISO_REAL = cn._INCIDENT_PATH
    cn._INCIDENT_PATH = Path(_ISO_TMP.name) / "xhs-cn-incidents.jsonl"


def tearDownModule():
    if _ISO_REAL is not None:
        cn._INCIDENT_PATH = _ISO_REAL
    if _ISO_TMP is not None:
        _ISO_TMP.cleanup()


def _expected_slides():
    """The 17 slide URLs straight from the fixture's state, by hand: WB_DFT, https, in order."""
    start = HTML.index("window.__INITIAL_STATE__=") + len("window.__INITIAL_STATE__=")
    literal = HTML[start:HTML.index("</script>", start)].replace("undefined", "null")
    note = json.loads(literal)["note"]["noteDetailMap"][NOTE_ID]["note"]
    return ["https://" + e["urlDefault"][len("http://"):] for e in note["imageList"]]


SLIDES = _expected_slides()
DOM_BODY = SLIDES[:4]  # what the old DOM scan saw on the real note
COMMENT_TOKENS = ("/comment/",)


@contextmanager
def _open_slot(*_a, **_kw):
    yield (True, "")


class _Backoff(unittest.TestCase):
    def setUp(self):
        self._saved = (xhs._backoff_until, xhs._consec_cdp_err)
        xhs._backoff_until, xhs._consec_cdp_err = 0.0, 0
        self.a = fetcher.get_adapter("xiaohongshu")

    def tearDown(self):
        xhs._backoff_until, xhs._consec_cdp_err = self._saved

    def _read(self, flow, url=NOTE_URL):
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs.cache, "set", lambda *a, **k: None), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(xhs, "cdp_call", return_value=flow):
            return self.a.fetch_url(url)


def _captured_rows():
    return xhs._flatten_captured_comments(CMT_PAGE["data"]["comments"])


# ── the pure helpers ────────────────────────────────────────────────────────────────────────────
class StateParseTests(unittest.TestCase):
    def test_html_state_gives_every_slide_in_order(self):
        note = xhs._state_note_from_html(HTML, NOTE_ID)
        self.assertEqual(note["noteId"], NOTE_ID)
        self.assertEqual(xhs._image_urls(note["imageList"]), SLIDES)
        self.assertEqual(len(SLIDES), 17)
        self.assertTrue(all(u.startswith("https://") and "!nd_dft_" in u for u in SLIDES))

    def test_no_id_reads_the_current_note(self):
        self.assertEqual(xhs._state_note_from_html(HTML, None)["noteId"], NOTE_ID)

    def test_the_other_note_in_the_map_is_read_only_by_its_own_id(self):
        self.assertEqual(xhs._state_note_from_html(HTML, OTHER_ID)["noteId"], OTHER_ID)
        self.assertIsNone(xhs._state_note_from_html(HTML, "f" * 24))

    def test_a_page_without_state_gives_none(self):
        self.assertIsNone(xhs._state_note_from_html("<html><body>x</body></html>", NOTE_ID))
        self.assertIsNone(xhs._state_note_from_html(
            "<script>window.__INITIAL_STATE__={broken</script>", NOTE_ID))

    def test_live_state_rejects_another_note(self):
        live = json.dumps({"noteId": OTHER_ID, "type": "normal", "imageList": []})
        self.assertIsNone(xhs._state_note_from_live(live, NOTE_ID))
        self.assertIsNone(xhs._state_note_from_live(None, NOTE_ID))
        self.assertIsNone(xhs._state_note_from_live("not json", NOTE_ID))

    def test_image_url_reads_both_spellings_and_falls_back(self):
        self.assertEqual(xhs._image_url({"urlDefault": "http://h/a/x!d"}), "https://h/a/x!d")
        self.assertEqual(xhs._image_url({"url_default": "", "info_list": [
            {"image_scene": "WB_PRV", "url": "http://h/p"},
            {"image_scene": "WB_DFT", "url": "http://h/d"}]}), "https://h/d")
        self.assertEqual(xhs._image_url({"urlPre": "https://h/pre"}), "https://h/pre")
        self.assertEqual(xhs._image_url("nope"), "")

    def test_same_picture_twice_is_kept_once(self):
        a = {"urlDefault": "http://h/1/tok!dft"}
        b = {"urlPre": "https://h/2/tok!prv"}
        self.assertEqual(xhs._image_urls([a, b]), ["https://h/1/tok!dft"])

    def test_captured_comments_carry_their_pictures(self):
        rows = _captured_rows()
        self.assertEqual([r["text"] for r in rows],
                         ["只有文字的评论", "原图直出是这样", "↳ [图片]", "[图片]"])
        self.assertNotIn("images", rows[0])
        for r in rows[1:]:
            self.assertEqual(len(r["images"]), 1)
            self.assertTrue(r["images"][0].startswith("https://"))
            self.assertIn("/comment/", r["images"][0])
        self.assertIn("/oss-sg/comment/", rows[2]["images"][0])


# ── the document omniseek_read returns ───────────────────────────────────────────────────────────────
class ImageNoteTests(_Backoff):
    def _flow(self, html=HTML, dom=DOM_BODY, rows=None, dom_comment=(), state_note=None,
              captured=None):
        rows = _captured_rows() if rows is None else rows
        cdata = {"list": rows, "declared": len(rows), "dom_comment_images": list(dom_comment),
                 "state_note": state_note,
                 "captured_images": [r for r in (captured if captured is not None else rows)
                                     if r.get("images")]}
        return ("ok", html, list(dom), cdata, (None, "unresolved"))

    def test_every_slide_lands_in_media_in_order(self):
        doc = self._read(self._flow())
        self.assertEqual(doc.media, SLIDES)
        self.assertEqual(doc.metadata["media_source"], "initial_state")
        self.assertEqual(doc.to_tool_dict(full=True)["media"], SLIDES)

    def test_the_count_in_the_text_matches_media(self):
        doc = self._read(self._flow())
        self.assertIn(f"{len(doc.media)} 张图", doc.content)
        self.assertIn("17 张图", doc.content)

    def test_comment_pictures_stay_out_of_media(self):
        doc = self._read(self._flow())
        cm = doc.metadata["comment_media"]
        self.assertEqual(len(cm), 3)
        self.assertTrue(all("/comment/" in u for u in cm))
        self.assertFalse(set(cm) & set(doc.media))
        self.assertFalse(any("/comment/" in u for u in doc.media))
        rows = doc.metadata["comments"]
        self.assertEqual([u for r in rows for u in r.get("images", [])], cm)
        self.assertIn("[图片]", [r["text"] for r in rows])
        self.assertIn("评论里的 3 张图在 metadata.comment_media", doc.content)

    def test_comment_pictures_survive_when_the_dom_comment_list_wins(self):
        # the DOM harvest has more rows than the capture, so it becomes metadata.comments; it carries
        # no pictures, yet the captured ones still reach comment_media
        dom_rows = [{"author": "a", "text": f"t{i}", "likes": ""} for i in range(9)]
        doc = self._read(self._flow(rows=dom_rows, captured=_captured_rows()))
        self.assertEqual(len(doc.metadata["comment_media"]), 3)
        self.assertEqual(doc.media, SLIDES)

    def test_dom_comment_pictures_are_added_once(self):
        rows = _captured_rows()
        extra = "https://sns-web-i10.rednotecdn.com/x/y/comment/1040g2h0extra!nd_whgt34_webp_wm_1"
        doc = self._read(self._flow(dom_comment=[rows[1]["images"][0], extra]))
        cm = doc.metadata["comment_media"]
        self.assertEqual(len(cm), 4)
        self.assertEqual(cm[-1], extra)

    def test_live_state_wins_over_the_html_copy(self):
        live = json.dumps({"noteId": NOTE_ID, "type": "normal",
                           "imageList": [{"urlDefault": "http://h/a/live1!d"},
                                         {"urlDefault": "http://h/a/live2!d"}]})
        doc = self._read(self._flow(state_note=live))
        self.assertEqual(doc.media, ["https://h/a/live1!d", "https://h/a/live2!d"])
        self.assertIn("2 张图", doc.content)

    def test_live_state_for_another_note_is_ignored(self):
        live = json.dumps({"noteId": OTHER_ID, "type": "normal",
                           "imageList": [{"urlDefault": "http://h/a/wrong!d"}]})
        doc = self._read(self._flow(state_note=live))
        self.assertEqual(doc.media, SLIDES)

    def test_no_state_falls_back_to_the_dom_scan(self):
        bare = ("<html><body><div id='detail-title'>标题</div>"
                "<div id='detail-desc'>看图</div></body></html>")
        doc = self._read(self._flow(html=bare))
        self.assertEqual(doc.media, DOM_BODY)
        self.assertEqual(doc.metadata["media_source"], "dom")
        self.assertIn("4 张图", doc.content)
        self.assertEqual(len(doc.metadata["comment_media"]), 3)

    def test_state_of_another_note_falls_back_to_the_dom_scan(self):
        # the page moved to another note: its state must not be read as this one
        url = f"https://www.rednote.com/explore/{'a' * 24}?xsec_token=ABfake"
        doc = self._read(self._flow(), url=url)
        self.assertEqual(doc.media, DOM_BODY)
        self.assertEqual(doc.metadata["media_source"], "dom")


class VideoNoteTests(_Backoff):
    VIDEO = "https://sns-video-qc.example/stream/abc.mp4"

    def test_video_note_keeps_its_media_shape(self):
        html = HTML.replace('"type":"normal","title":"测试笔记', '"type":"video","title":"测试笔记')
        self.assertIn('"type":"video"', html)
        rows = _captured_rows()
        cdata = {"list": rows, "declared": None, "dom_comment_images": [], "state_note": None,
                 "captured_images": rows}
        doc = self._read(("ok", html, DOM_BODY[:1], cdata, (self.VIDEO, "dom")))
        self.assertEqual(doc.media, [self.VIDEO] + DOM_BODY[:1])
        self.assertEqual(doc.metadata["media_source"], "dom")
        self.assertEqual(doc.metadata["video_url"], self.VIDEO)
        self.assertEqual(len(doc.metadata["comment_media"]), 3)
        self.assertNotIn("正文主要在", doc.content)

    def test_state_says_video_even_when_no_player_url_was_found(self):
        html = HTML.replace('"type":"normal","title":"测试笔记', '"type":"video","title":"测试笔记')
        cdata = {"list": [], "declared": None}
        doc = self._read(("ok", html, DOM_BODY[:1], cdata, (None, "unresolved")))
        self.assertEqual(doc.media, DOM_BODY[:1])
        self.assertEqual(doc.metadata["video_src"], "unresolved")


# ── the live flow, run against a fake page ──────────────────────────────────────────────────────
class _Loc:
    def __init__(self, text=""):
        self._text = text

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._text else 0

    def is_visible(self):
        return bool(self._text)

    def inner_text(self, **_kw):
        return self._text

    def get_attribute(self, _name):
        return None


class _Resp:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def json(self):
        return self._body


class _FakeNotePage:
    """The note page as the flow sees it. goto fires the page's own comment XHR at every listener;
    evaluate answers the image scan and the state read and records what it was asked."""

    def __init__(self, state_answer):
        self.url = "about:blank"
        self.frames = [self]
        self.handlers = []
        self.evaluated = []
        self._state_answer = state_answer

    def on(self, _event, handler):
        self.handlers.append(handler)

    def goto(self, url, **_kw):
        self.url = url
        for h in list(self.handlers):
            h(_Resp("https://edith.rednote.com/api/sns/web/v2/comment/page?note_id=x", CMT_PAGE))

    def locator(self, sel):
        return _Loc("测试笔记：十七张图" if sel == "#detail-title" else "")

    def evaluate(self, script, *args):
        self.evaluated.append((script, args))
        if script == xhs._DOM_IMAGES_JS:
            return {"body": DOM_BODY, "comment": []}
        if script == xhs._STATE_NOTE_JS:
            return self._state_answer
        return None

    def content(self):
        return HTML

    def mouse(self):  # pragma: no cover - the human helpers are stubbed
        return None


class LiveFlowTests(_Backoff):
    def setUp(self):
        super().setUp()
        self._patches = [mock.patch.object(_human, n, lambda *a, **k: None)
                         for n in ("read_dwell", "scroll_like_reading", "action_pause", "short_pause")]
        self._patches.append(mock.patch.object(xhs, "_load_comments", lambda *a, **k: None))
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def _run(self, page):
        with mock.patch.object(xhs.cache, "get", return_value=None), \
                mock.patch.object(xhs.cache, "set", lambda *a, **k: None), \
                mock.patch.object(xhs, "_live_slot", _open_slot), \
                mock.patch.object(xhs, "cdp_call", lambda cb, **kw: cb(page)):
            return self.a.fetch_url(NOTE_URL)

    def test_flow_reads_the_state_for_the_navigated_note(self):
        page = _FakeNotePage(state_answer=None)
        doc = self._run(page)
        asked = [args for script, args in page.evaluated if script == xhs._STATE_NOTE_JS]
        self.assertEqual(asked, [(NOTE_ID,)])
        self.assertEqual(doc.media, SLIDES)
        self.assertEqual(len(doc.metadata["comment_media"]), 3)
        self.assertIn("[图片]", [r["text"] for r in doc.metadata["comments"]])

    def test_flow_prefers_the_live_state(self):
        live = json.dumps({"noteId": NOTE_ID, "type": "normal",
                           "imageList": [{"urlDefault": f"http://h/a/s{i}!d"} for i in range(5)]})
        doc = self._run(_FakeNotePage(state_answer=live))
        self.assertEqual(doc.media, [f"https://h/a/s{i}!d" for i in range(5)])
        self.assertIn("5 张图", doc.content)

    def test_flow_never_clicks_the_carousel(self):
        page = _FakeNotePage(state_answer=None)
        self._run(page)
        # the flow only reads: the two image reads plus the comment/declared reads, nothing else
        scripts = [s for s, _ in page.evaluated]
        self.assertEqual(scripts.count(xhs._DOM_IMAGES_JS), 1)
        self.assertEqual(scripts.count(xhs._STATE_NOTE_JS), 1)
        self.assertFalse(any("click" in (s or "") for s in scripts))


if __name__ == "__main__":
    unittest.main()
