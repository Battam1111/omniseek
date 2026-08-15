"""Contract for video-note handling in the walled xiaohongshu adapters.

The failure this exists to prevent is quiet in four different ways:
  - a video note has no body text, so a text-only content gate reads it as an empty page and the
    login-wall check discards a perfectly readable note;
  - a blob: URL looks like a video URL and is useless to a transcriber, failing later and further
    from the cause than returning None would;
  - the note flow's return tuple is unpacked in the caller, so a return path that keeps the old
    arity raises where the adapter's own except-clause charges it to the browser: the account
    backoff trips and, on the mainland adapter, a forged row lands in the incident black box, so a
    pure code bug gets recorded as an account incident;
  - and the newest one: the eye reported a video URL without saying WHICH path produced it. The
    DOM reader and the wire sniffer are two different mechanisms that answer under opposite
    conditions, yet unstamped they are indistinguishable in every document the eye has ever
    returned. That is how a live success once got credited to the DOM path with nothing behind the
    claim. metadata.video_src makes the question answerable from real traffic instead of memory.
Only the third needs a browser to hit in production, and none of these tests needs one.

STATE ISOLATION: this suite imports no adapter module at all -- it reads their SOURCE text, and
that is deliberate. The mainland adapter (named here only as a filename) writes forensic rows into
a real state file the moment its guard runs, so a suite that pulled it in would first have to
redirect `_INCIDENT_PATH`; the smoke tripwire checks exactly that. Reading the file instead of
loading it keeps this suite outside the hazard entirely.
"""
import ast
import unittest
from pathlib import Path

from omniseek.core.sources.walled import _cdp

SRC = Path(__file__).resolve().parents[1] / "src" / "omniseek" / "core" / "sources" / "walled"


class _StubResponse:
    def __init__(self, url):
        self.url = url


class _StubPage:
    """Just enough page to register a handler, fire it, and answer one evaluate().

    `dom_src` is what the extraction JS RETURNS, not what the DOM holds: the JS already drops a
    blob: src (pinned by test_extraction_js_refuses_a_blob_src), so a blob-fed player is modelled
    here as an empty string, which is exactly what video_from_page sees in production. Pass an
    Exception to model a page that dies under evaluate (a tab that navigated or closed).
    """

    def __init__(self, dom_src=""):
        self.handlers = []
        self.dom_src = dom_src

    def on(self, event, fn):
        self.handlers.append((event, fn))

    def evaluate(self, _js):
        if isinstance(self.dom_src, Exception):
            raise self.dom_src
        return self.dom_src

    def fire(self, url):
        for event, fn in self.handlers:
            if event == "response":
                fn(_StubResponse(url))


class VideoHelperTests(unittest.TestCase):
    def test_wire_regex_takes_streams_and_rejects_blob_and_images(self):
        take = ["https://sns-video-hw.xhscdn.com/stream/1/x.mp4",
                "https://sns-video-hw.xhscdn.com/stream/1/x.mp4?sign=abc&t=1",
                "https://x.xhscdn.com/live/y.m3u8"]
        drop = ["blob:https://www.xiaohongshu.com/8f0c-4a1b",
                "https://x.xhscdn.com/img/z.webp",
                "https://x.xhscdn.com/api/note.json"]
        for u in take:
            self.assertIsNotNone(_cdp._VIDEO_WIRE_RE.fullmatch(u), u)
        for u in drop:
            self.assertIsNone(_cdp._VIDEO_WIRE_RE.fullmatch(u), u)

    def test_extraction_js_refuses_a_blob_src(self):
        """A blob: handle is valid only inside that Chrome process. Returning one would look like
        success and fail at the transcriber, so the JS must drop it on BOTH paths it can read."""
        self.assertEqual(_cdp._VIDEO_SRC_JS.count("blob:"), 2, _cdp._VIDEO_SRC_JS)

    def test_sniffer_records_streams_dedupes_and_never_raises(self):
        page = _StubPage()
        seen = _cdp.attach_video_sniffer(page)
        page.fire("https://x.xhscdn.com/a.mp4")
        page.fire("https://x.xhscdn.com/a.mp4")          # duplicate
        page.fire("blob:https://www.xiaohongshu.com/z")   # not a fetchable URL
        page.fire("https://x.xhscdn.com/b.png")           # not a stream
        self.assertEqual(seen, ["https://x.xhscdn.com/a.mp4"])

    def test_sniffer_swallows_a_broken_response(self):
        """It runs on the page's event loop: a raise there would kill the navigation it only
        observes."""
        class Exploding:
            @property
            def url(self):
                raise RuntimeError("boom")

        page = _StubPage()
        seen = _cdp.attach_video_sniffer(page)
        for _event, fn in page.handlers:
            fn(Exploding())          # must not propagate
        self.assertEqual(seen, [])

    def test_hint_names_both_readers_not_just_the_transcriber(self):
        """A video note carries content on two tracks. The first note this shipped for had a
        music-only audio track and every fact burned into the frames, so a hint naming only the
        transcriber sends the agent to read music and report an empty note."""
        with_url = _cdp.content_with_video("", "https://x/a.mp4", has_player=True)
        self.assertIn("omniseek_transcribe", with_url)
        self.assertIn("omniseek_view", with_url)

    def test_hint_distinguishes_transcribable_from_blob_only(self):
        with_url = _cdp.content_with_video("", "https://x/a.mp4", has_player=True)
        blob_only = _cdp.content_with_video("", None, has_player=True)
        self.assertIn("omniseek_transcribe", with_url)
        self.assertIn("blob:", blob_only)
        # not a video note -> untouched
        self.assertEqual(_cdp.content_with_video("body", None, has_player=False), "body")
        # a body is kept, never replaced
        self.assertTrue(_cdp.content_with_video("body", "https://x/a.mp4",
                                                has_player=True).startswith("body"))


class VideoOriginTests(unittest.TestCase):
    """WHICH path produced the URL.

    The two paths answer under opposite conditions (the DOM read returns nothing at all for a
    blob:-fed player, which is the only case the sniffer exists for), yet the URL they hand back
    looks identical downstream. Until the origin rides on the document, no amount of production
    traffic can say which one is carrying the load, and a guess about it cannot be caught.
    """

    def test_a_real_dom_src_is_labelled_dom(self):
        page = _StubPage(dom_src="https://sns-video-hw.xhscdn.com/stream/1/x.mp4")
        self.assertEqual(_cdp.video_with_origin(page, []),
                         ("https://sns-video-hw.xhscdn.com/stream/1/x.mp4", "dom"))

    def test_a_blob_fed_player_falls_through_to_the_wire(self):
        """The extraction JS returns '' for a blob: src, so the DOM read comes back empty on
        exactly the notes the sniffer exists for. This path IS the sniffer's reason to exist."""
        page = _StubPage(dom_src="")
        self.assertEqual(_cdp.video_with_origin(page, ["https://x.xhscdn.com/a.mp4"]),
                         ("https://x.xhscdn.com/a.mp4", "wire"))

    def test_the_dom_wins_when_both_have_a_url(self):
        """The wire also carries previews and neighbouring cards' streams; the <video> element is
        the one the page is actually playing. Flipping the precedence silently changes which video
        gets transcribed, with no error anywhere."""
        page = _StubPage(dom_src="https://x.xhscdn.com/dom.mp4")
        self.assertEqual(_cdp.video_with_origin(page, ["https://x.xhscdn.com/wire.mp4"]),
                         ("https://x.xhscdn.com/dom.mp4", "dom"))

    def test_an_evaluate_that_raises_still_reaches_the_wire(self):
        """A tab that navigated or closed under us makes page.evaluate throw. That must degrade to
        the sniffed URL, not drop a note the wire had already captured."""
        page = _StubPage(dom_src=RuntimeError("Execution context was destroyed"))
        self.assertEqual(_cdp.video_with_origin(page, ["https://x.xhscdn.com/a.mp4"]),
                         ("https://x.xhscdn.com/a.mp4", "wire"))

    def test_nothing_anywhere_is_unresolved_not_a_bare_none(self):
        """'unresolved' is a MEASUREMENT, not decoration: a video note that neither path could read
        is the denominator for 'does the wire sniffer ever fire'."""
        self.assertEqual(_cdp.video_with_origin(_StubPage(dom_src=""), []), (None, "unresolved"))
        self.assertEqual(_cdp.video_with_origin(_StubPage(dom_src=""), None), (None, "unresolved"))

    def test_metadata_stamps_the_origin_beside_the_url(self):
        self.assertEqual(_cdp.video_metadata("https://x/a.mp4", "dom", has_player=True),
                         {"video_url": "https://x/a.mp4", "video_src": "dom"})
        self.assertEqual(_cdp.video_metadata("https://x/a.mp4", "wire", has_player=True),
                         {"video_url": "https://x/a.mp4", "video_src": "wire"})

    def test_metadata_records_a_video_note_it_could_not_read(self):
        """Without this row a blob-only note is indistinguishable from an image note in the output,
        and 'the sniffer never fired' can never be told apart from 'no blob note ever arrived'."""
        self.assertEqual(_cdp.video_metadata(None, "unresolved", has_player=True),
                         {"video_src": "unresolved"})

    def test_metadata_leaves_a_note_without_video_alone(self):
        """An image note must not grow a video key: it would inflate the very denominator the
        unresolved row exists to keep honest."""
        self.assertEqual(_cdp.video_metadata(None, "unresolved", has_player=False), {})
        self.assertEqual(_cdp.video_metadata(None, None, has_player=False), {})

    def test_a_url_with_no_origin_is_labelled_unknown_never_assumed(self):
        """A wrong label is worse than a visibly missing one: this field exists to be counted."""
        self.assertEqual(_cdp.video_metadata("https://x/a.mp4", None, has_player=True),
                         {"video_url": "https://x/a.mp4", "video_src": "unknown"})


class AdapterContractTests(unittest.TestCase):
    ADAPTERS = ("xiaohongshu_source.py", "xiaohongshu_cn_source.py")

    def _tree(self, filename):
        return ast.parse((SRC / filename).read_text(encoding="utf-8"))

    def _note_flow_returns(self, filename):
        """Every return in the NOTE flow, as its element list. The note flow is the one whose
        returns carry (status, html, images, cdata, video); the search flow returns 2-tuples and is
        identified by not carrying a dict element."""
        returns = []
        for node in ast.walk(self._tree(filename)):
            if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)):
                continue
            elts = node.value.elts
            if any(isinstance(e, ast.Dict) for e in elts) or len(elts) == 5:
                returns.append(elts)
        return returns

    def _pair_producers(self, filename):
        """Local names bound from video_with_origin(...): the only non-literal a return may put in
        the video slot. Matches the aliased import too (_video_with_origin on the mainland side)."""
        names = set()
        for node in ast.walk(self._tree(filename)):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            fn = node.value.func
            called = getattr(fn, "id", None) or getattr(fn, "attr", None) or ""
            if called.endswith("video_with_origin"):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        return names

    def test_every_note_flow_return_carries_the_video_slot(self):
        """The caller unpacks five names. A return path left at four raises where the adapter's own
        except-clause blames the browser: the account backoff trips and the mainland adapter files
        a browser_fetch_error row, so the bug surfaces as an account incident and never as itself."""
        for f in self.ADAPTERS:
            arities = [len(elts) for elts in self._note_flow_returns(f)]
            self.assertTrue(arities, f"{f}: found no note-flow returns to check")
            self.assertEqual(set(arities), {5}, f"{f}: mixed arities {arities}")

    def test_the_video_slot_carries_the_origin_not_just_the_url(self):
        """Slot five is the PAIR (url, origin). A URL with no origin is unmeasurable: the DOM path
        and the wire path look identical in the output. A return that leaves a bare URL (or a bare
        None) there keeps the arity at five and still breaks the caller's
        `video_url, video_src = video` unpack, so arity alone does not cover it."""
        for f in self.ADAPTERS:
            allowed = self._pair_producers(f)
            self.assertTrue(allowed, f"{f}: no name is bound from video_with_origin()")
            for elts in self._note_flow_returns(f):
                if len(elts) != 5:
                    continue        # the arity test owns that failure; don't double-report it
                slot = elts[4]
                if isinstance(slot, ast.Tuple):
                    self.assertEqual(len(slot.elts), 2,
                                     f"{f}: the literal video slot is not a (url, origin) pair")
                else:
                    self.assertTrue(isinstance(slot, ast.Name) and slot.id in allowed,
                                    f"{f}: the video slot is neither a pair literal nor a name "
                                    f"bound from video_with_origin() ({ast.dump(slot)[:80]})")

    def test_the_flow_unpack_sits_outside_the_cdp_try(self):
        """The load-bearing placement, and the reason two tests in test_xiaohongshu_read_contract.py
        could pass while never running: INSIDE the CDP try, a ValueError from OUR OWN tuple shape is
        caught by the browser-failure handler, which trips the account cooldown and (mainland)
        writes a forged browser_fetch_error row. Checked on the AST, not by string match: the whole
        question is which block the statement lives in."""
        for f in self.ADAPTERS:
            tree = self._tree(f)
            unpacks = [n for n in ast.walk(tree)
                       if isinstance(n, ast.Assign)
                       and isinstance(n.targets[0], ast.Tuple) and len(n.targets[0].elts) == 5
                       and isinstance(n.value, ast.Name) and n.value.id == "flow_result"]
            self.assertTrue(unpacks, f"{f}: no `... = flow_result` unpack -- either it moved back "
                                     f"inside the try, or the intermediate binding is gone")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Assign)
                            and isinstance(sub.targets[0], ast.Tuple)
                            and len(sub.targets[0].elts) == 5
                            and isinstance(sub.value, ast.Call)
                            and getattr(sub.value.func, "id", "") == "cdp_call"):
                        self.fail(f"{f}:{sub.lineno} unpacks the flow's five values inside a try, "
                                  f"so a tuple-shape bug is charged to the browser again")

    # How many FULL NOTE READS each adapter has. The mainland side has two (the 9224 browser flow
    # and the signed-API fallback); the international side has one. Everything else these files
    # build is a search CARD. Declared, so a new note path has to come here and say what it is.
    NOTE_DOCUMENT_SITES = {"xiaohongshu_source.py": 1, "xiaohongshu_cn_source.py": 2}

    def _note_document_calls(self, filename):
        """Document(...) calls that are full note reads, identified by carrying the comment
        thread in metadata. That key separates them cleanly from the card builders, which have a
        note id and engagement counts but never the comments."""
        out = []
        for node in ast.walk(self._tree(filename)):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Document"):
                continue
            md = next((k.value for k in node.keywords if k.arg == "metadata"), None)
            if not isinstance(md, ast.Dict):
                continue
            keys = [k.value for k in md.keys if isinstance(k, ast.Constant)]
            if "comments" in keys:
                out.append((node, md))
        return out

    def test_every_full_note_read_stamps_the_video_origin(self):
        """The gap that actually shipped: the mainland adapter has TWO note paths and only the
        browser one got the stamp, so a video note served by the signed fallback came back looking
        exactly like an image note and silently holed the very denominator the stamp exists to keep
        honest. COUNTING the document builders is what found it, so counting them is the guard: a
        third mainland note path (or a second international one) fails here until its author either
        stamps it or declares it."""
        for f, expected in self.NOTE_DOCUMENT_SITES.items():
            sites = self._note_document_calls(f)
            self.assertEqual(len(sites), expected,
                             f"{f}: {len(sites)} full note reads, declared {expected}. A new one must "
                             f"stamp the video origin, or NOTE_DOCUMENT_SITES must say why not.")
            for node, md in sites:
                starred = [ast.unparse(v) for k, v in zip(md.keys, md.values) if k is None]
                self.assertTrue(any("video_metadata(" in s for s in starred),
                                f"{f}:{node.lineno} builds a full note document without a video "
                                f"stamp, so its video notes are indistinguishable from image notes")

    def test_both_adapters_stamp_the_origin_into_the_document_metadata(self):
        """The stamp has to reach the DOCUMENT. metadata.video_src is the only place a later read
        of real traffic can see which path produced the URL, which is the entire point: neither
        path can be confirmed or retired on stub tests alone."""
        for f in self.ADAPTERS:
            src = (SRC / f).read_text(encoding="utf-8")
            self.assertIn("video_metadata(video_url, video_src, has_player=", src, f)
            self.assertNotIn('{"video_url": video_url}', src,
                             f"{f} still builds the video metadata inline, shipping a URL with no origin")

    def test_content_gate_counts_a_player_as_content(self):
        """Without this, _login_wall discards every video note: it has no body text to find."""
        for f in self.ADAPTERS:
            src = (SRC / f).read_text(encoding="utf-8")
            self.assertIn("VIDEO_PLAYER_SELECTORS", src, f)
            self.assertIn("#detail-desc\") + ", src, f)

    def test_both_adapters_use_the_shared_helpers_not_their_own_copy(self):
        """One definition of 'a video note', so the two cannot drift apart. video_with_origin
        replaced a per-adapter `video_from_page(page) or seen_video[0]`, which is exactly where the
        DOM-beats-wire precedence and the origin label would otherwise diverge between them."""
        for f in self.ADAPTERS:
            src = (SRC / f).read_text(encoding="utf-8")
            for helper in ("attach_video_sniffer", "video_with_origin", "video_metadata"):
                self.assertIn(helper, src, f"{f} does not use the shared {helper}")
            self.assertNotIn("document.querySelector('video')", src,
                             f"{f} re-implements the extraction JS instead of using _cdp")
            self.assertNotIn("seen_video[0]", src,
                             f"{f} re-implements the DOM-beats-wire precedence instead of calling "
                             "video_with_origin, so the two can label origins differently")


if __name__ == "__main__":
    unittest.main(verbosity=2)
