"""Every hint that names the transcription engine must name the one omniseek_transcribe actually runs.

OmniSeek P6 run R01 (2026-10-11): omniseek_read on a bilibili video appended "用 omniseek_transcribe 取本地
Whisper 全文转写", while asr loads SenseVoice-Small (Whisper was benchmarked and rejected). The hints
now read the engine name off asr's configured model ids; these tests pin that and scan the shipped
source tree so a hard-coded engine name cannot creep back into a user-facing hint."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from omniseek.core import asr, auth

with mock.patch.object(auth, "write_template"):
    from omniseek.core.sources.scrape import bilibili_source, xiaoyuzhou_source


class TranscribeHintModelTests(unittest.TestCase):
    def test_engine_label_follows_the_configured_model(self):
        self.assertEqual(asr.engine_label(), "SenseVoice-Small")
        self.assertEqual(asr.engine_label(diarize=True), "Paraformer-zh + cam++")
        with mock.patch.object(asr, "_MODEL", "org/SomeOtherModel"):
            self.assertEqual(asr.engine_label(), "SomeOtherModel")

    def test_bilibili_hint_names_the_real_engine(self):
        hint = bilibili_source._transcribe_hint()
        self.assertIn(asr.engine_label(), hint)
        self.assertIn(asr.engine_label(diarize=True), hint)
        self.assertNotIn("Whisper", hint)
        with mock.patch.object(asr, "_MODEL", "org/SomeOtherModel"):
            self.assertIn("SomeOtherModel", bilibili_source._transcribe_hint())

    def test_xiaoyuzhou_hint_names_the_real_engine(self):
        doc = xiaoyuzhou_source.XiaoyuzhouAdapter()._ep_to_doc(
            {"eid": "abc", "title": "t", "shownotes": "<p>notes</p>"}, "pod")
        self.assertIn(f"omniseek_transcribe 取本地 {asr.engine_label()} 全文转写", doc.content)
        self.assertNotIn("Whisper", doc.content)

    def test_no_shipped_hint_hard_codes_whisper_as_our_engine(self):
        # "Whisper" may appear where it is named as the REJECTED alternative (asr's design note, the
        # omniseek_transcribe description); it must never appear as the engine a hint points to.
        bad = re.compile(r"(本地|local)\s*Whisper|Whisper\s*(全文)?转写", re.I)
        hits = [f"{p.relative_to(ROOT)}:{i}"
                for p in (ROOT / "src").rglob("*.py")
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if bad.search(line)]
        self.assertEqual(hits, [])

    def test_transcribe_tool_description_names_the_configured_engines(self):
        text = (ROOT / "src" / "omniseek" / "server.py").read_text(encoding="utf-8")
        start = text.index("def omniseek_transcribe(")
        doc = text[start:start + 4000]
        self.assertIn(asr.engine_label().split("-")[0], doc)  # "SenseVoice"
        self.assertIn(asr._MODEL_LABELS[asr._DIAR_MODEL], doc)  # "Paraformer-zh"
        self.assertIn(asr._SPK_MODEL, doc)  # "cam++"


if __name__ == "__main__":
    unittest.main()
